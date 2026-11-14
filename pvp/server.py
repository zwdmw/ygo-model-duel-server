"""Multi-room native YGOPro PvP gateway.

The service is deliberately independent from the model room bridge:

    MDPro3/YGOPro2 --TCP--> this gateway --TCP--> YGOPro-compatible server
                                      \
                                       -> tournament callback

The backend native server remains authoritative for cards, rules, legal
actions, duel results, and replay bytes. This service owns tournament-scoped
admission and routing only.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import binascii
import copy
from datetime import datetime
import gzip
import hashlib
import hmac
import io
import json
import logging
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import time
from typing import Any
import zipfile

from aiohttp import ClientError, ClientSession, ClientTimeout, web

from . import protocol as P
from .deck import FixedDeck, load_ydk
from .replay import evrp_duel_to_yrp3d, yrp2_identity


LOG = logging.getLogger("ygo-pvp")
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789"
MAX_REPLAY_BYTES = 16 * 1024 * 1024
MAX_REPLAY_ARCHIVE_BYTES = 512 * 1024 * 1024
MAX_SERIES_REPLAY_BYTES = 64 * 1024 * 1024
MAX_SERIES_REPLAY_JSON_BYTES = 256 * 1024 * 1024
MAX_SERIES_REPLAY_CHUNKS = 4096
MAX_SERIES_REPLAY_DUELS = 10000
MAX_SERIES_REPLAY_FRAMES = 2_000_000
SESSION_TTL = 8 * 60 * 60
CALLBACK_RETRY_INTERVAL = 5
REPLAY_WAIT_SECONDS = 8
RECONNECT_GRACE_SECONDS = 3 * 60
PLAYER_STALL_WARNING_SECONDS = 2 * 60
SERIES_LEAVE_GUARD_SECONDS = 10
TOURNAMENT_TIME_LIMIT_SECONDS = 10 * 60
ACTIVE_SESSION_STATUSES = {"waiting", "connecting", "ready", "dueling"}


def now() -> float:
    return time.time()


def continuous_series_open(session: dict[str, Any]) -> bool:
    return bool(
        session.get("continuous_series")
        and not session.get("series_finished")
        and session.get("status") in ACTIVE_SESSION_STATUSES
    )


def series_replay_available(session: dict[str, Any]) -> bool:
    if (
        session.get("status") != "finished"
        or session.get("series_finished") is not True
    ):
        return False
    return (
        series_replay_stored(session)
        or partial_series_replay_stored(session)
    )


def _series_replay_file_stored(replay: Any) -> bool:
    if not isinstance(replay, dict):
        return False
    byte_count = replay.get("bytes")
    digest = replay.get("sha256")
    path = replay.get("path")
    duel_count = replay.get("duels")
    if (
        type(byte_count) is not int
        or not 0 < byte_count <= MAX_SERIES_REPLAY_BYTES
        or not isinstance(digest, str)
        or len(digest) != 64
        or any(char not in "0123456789abcdef" for char in digest)
        or not isinstance(path, str)
        or not path
        or type(duel_count) is not int
        or duel_count <= 0
    ):
        return False
    try:
        archive = Path(path)
        if not archive.is_file() or archive.stat().st_size != byte_count:
            return False
        return hashlib.sha256(archive.read_bytes()).hexdigest() == digest
    except OSError:
        return False


def series_replay_stored(session: dict[str, Any]) -> bool:
    replay = session.get("series_replay")
    return bool(
        _series_replay_file_stored(replay)
        and replay.get("partial") is not True
        and replay.get("duels") == expected_series_duels(session)
    )


def partial_series_replay_stored(session: dict[str, Any]) -> bool:
    replay = session.get("series_replay")
    if (
        not _series_replay_file_stored(replay)
        or replay.get("partial") is not True
    ):
        return False
    expected = expected_series_duels(session)
    duel_count = replay.get("duels")
    ordinals = replay.get("ordinals")
    missing = replay.get("missing_ordinals")
    if (
        type(duel_count) is not int
        or not 0 < duel_count < expected
        or not isinstance(ordinals, list)
        or not isinstance(missing, list)
        or ordinals != sorted(set(ordinals))
        or missing != sorted(set(missing))
        or any(
            type(number) is not int or not 1 <= number <= expected
            for number in ordinals + missing
        )
        or len(ordinals) != duel_count
    ):
        return False
    return (
        set(ordinals).isdisjoint(missing)
        and sorted(ordinals + missing) == list(range(1, expected + 1))
    )


def series_replay_status(session: dict[str, Any]) -> dict[str, Any]:
    expected = expected_series_duels(session)
    complete = series_replay_stored(session)
    partial = partial_series_replay_stored(session)
    replay = session.get("series_replay")
    if complete or partial:
        replay_count = int(replay["duels"])
        ordinals = (
            list(replay.get("ordinals") or [])
            if partial else list(range(1, expected + 1))
        )
    else:
        ordinals = sorted({
            item.get("ordinal")
            for item in session.get("series_replays", [])
            if (
                isinstance(item, dict)
                and type(item.get("ordinal")) is int
                and 1 <= item["ordinal"] <= expected
            )
        })
        replay_count = len(ordinals)
    missing = sorted(set(range(1, expected + 1)) - set(ordinals))
    available = bool(
        session.get("status") == "finished"
        and session.get("series_finished") is True
        and (complete or partial)
    )
    if complete and available:
        state = "complete"
    elif partial and available:
        state = "partial"
    elif session.get("status") == "finished":
        state = "incomplete"
    else:
        state = "recording"
    return {
        "state": state,
        "available": available,
        "complete": bool(complete and available),
        "partial": bool(partial and available),
        "replay_count": replay_count,
        "expected_replay_count": expected,
        "missing_game_numbers": missing,
    }


def expected_series_duels(session: dict[str, Any]) -> int:
    """Return the number of games an EVRP must contain at this point."""
    sealed_expected = session.get("sealed_expected_duels")
    if (
        session.get("status") == "finished"
        and session.get("series_finished") is True
        and type(sealed_expected) is int
        and sealed_expected >= 0
    ):
        return sealed_expected
    if not session.get("continuous_series"):
        return 1
    prefix = session.get("series_replay_prefix")
    prefix_duels = (
        prefix.get("duels")
        if isinstance(prefix, dict) and type(prefix.get("duels")) is int
        else 0
    )
    if prefix_duels:
        initial_generation = session.get(
            "initial_generation", session.get("game_number")
        )
        game_number = session.get("game_number")
        if (
            type(initial_generation) is int
            and type(game_number) is int
            and game_number >= initial_generation
        ):
            return prefix_duels + game_number - initial_generation + 1
    game_number = session.get("game_number")
    if type(game_number) is int and game_number > 0:
        return game_number
    return max(1, len(session.get("results") or []))


def current_generation(session: dict[str, Any]) -> int:
    generation = session.get("game_generation")
    if type(generation) is int and generation > 0:
        return generation
    game_number = session.get("game_number")
    return game_number if type(game_number) is int and game_number > 0 else 1


def reconnect_scope(
    session: dict[str, Any],
    connection: dict[str, Any],
    timestamp: float | None = None,
) -> str | None:
    """Return the active reconnect policy for a disconnected player seat."""
    if (
        connection.get("connected")
        or not connection.get("last_name")
    ):
        return None
    if continuous_series_open(session):
        return "series"
    if session.get("status") != "dueling":
        return None
    reconnect_until = connection.get("reconnect_until")
    if (
        isinstance(reconnect_until, (int, float))
        and reconnect_until > (now() if timestamp is None else timestamp)
    ):
        return "grace"
    return None


def connection_state(
    session: dict[str, Any],
    connection: dict[str, Any],
    timestamp: float | None = None,
) -> str:
    """Return a public, non-authoritative health state for one player seat."""
    checked_at = now() if timestamp is None else timestamp
    if connection.get("connected"):
        last_activity_at = connection.get("last_activity_at")
        if not isinstance(last_activity_at, (int, float)):
            last_activity_at = connection.get("connected_at")
        if (
            session.get("status") == "dueling"
            and isinstance(last_activity_at, (int, float))
            and checked_at - last_activity_at >= PLAYER_STALL_WARNING_SECONDS
        ):
            return "stalled"
        return "online"
    if reconnect_scope(session, connection, checked_at) is not None:
        return "reconnecting"
    if (
        connection.get("last_name")
        or isinstance(connection.get("disconnected_at"), (int, float))
    ):
        return "offline"
    return "waiting"


def new_id(prefix: str) -> str:
    return f"{prefix}-{secrets.token_hex(6)}"


def random_code(length: int = 18) -> str:
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(length))


def clean_text(value: Any, name: str, maximum: int = 120) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be text")
    value = " ".join(value.strip().split())
    if not value or len(value) > maximum:
        raise ValueError(f"{name} is invalid")
    return value


def native_player_names(session: dict[str, Any]) -> dict[str, str]:
    """Return the exact player identities carried by native PLAYER_INFO."""
    players = session.get("players") or {}
    names = {
        side: P.decode_name(P.encode_name(str(players.get(side, ""))))
        for side in ("a", "b")
    }
    if not all(names.values()) or names["a"] == names["b"]:
        raise ValueError("player names must be unique within 19 native characters")
    return names


def clean_credential(value: Any, name: str) -> str:
    if not isinstance(value, str) or not 8 <= len(value) <= 19:
        raise ValueError(f"{name} must contain 8-19 characters")
    if not value.isascii() or any(
        char.isspace() or char in "#," for char in value
    ):
        raise ValueError(f"{name} contains unsupported characters")
    return value


def clean_seed(value: Any) -> list[int]:
    if not isinstance(value, list) or len(value) != 8:
        raise ValueError("seed must contain exactly 8 uint32 values")
    if any(type(word) is not int or not 0 <= word <= 0xFFFFFFFF for word in value):
        raise ValueError("seed must contain exactly 8 uint32 values")
    return [int(word) for word in value]


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, prefix=".pvp-", delete=False
        ) as stream:
            temporary = Path(stream.name)
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        if os.name == "posix":
            os.chmod(temporary, 0o600)
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def atomic_bytes(path: Path, value: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=path.parent, prefix=".pvp-", delete=False
        ) as stream:
            temporary = Path(stream.name)
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        if os.name == "posix":
            os.chmod(temporary, mode)
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def validate_series_replay(payload: bytes) -> dict[str, Any]:
    if not 0 < len(payload) <= MAX_SERIES_REPLAY_BYTES:
        raise ValueError("EVRP archive size is invalid")
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(payload)) as archive:
            raw = archive.read(MAX_SERIES_REPLAY_JSON_BYTES + 1)
    except (OSError, EOFError) as error:
        raise ValueError("EVRP archive is not valid gzip") from error
    if len(raw) > MAX_SERIES_REPLAY_JSON_BYTES:
        raise ValueError("EVRP JSON exceeds the decompression limit")
    try:
        envelope = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("EVRP archive does not contain valid JSON") from error
    if not isinstance(envelope, dict) or envelope.get("version") != 1:
        raise ValueError("EVRP archive version is unsupported")
    if not isinstance(envelope.get("meta"), dict):
        raise ValueError("EVRP metadata is invalid")
    duels = envelope.get("duels")
    if (
        not isinstance(duels, list)
        or not 1 <= len(duels) <= MAX_SERIES_REPLAY_DUELS
    ):
        raise ValueError("EVRP duel list is invalid")
    total_frames = 0
    game_numbers = set()
    for duel in duels:
        if not isinstance(duel, dict):
            raise ValueError("EVRP duel entry is invalid")
        frames = duel.get("spectatorFrames")
        if not isinstance(frames, list) or not frames:
            raise ValueError("EVRP spectator frame list is missing")
        total_frames += len(frames)
        if total_frames > MAX_SERIES_REPLAY_FRAMES:
            raise ValueError("EVRP contains too many frames")
        for encoded in frames:
            if not isinstance(encoded, str) or len(encoded) > 90_000:
                raise ValueError("EVRP contains an invalid frame")
            try:
                frame = base64.b64decode(encoded, validate=True)
            except (ValueError, binascii.Error) as error:
                raise ValueError("EVRP contains invalid base64") from error
            if (
                not 3 <= len(frame) <= P.MAX_FRAME + 2
                or int.from_bytes(frame[:2], "little") != len(frame) - 2
                or frame[2] != P.GAME_MSG
            ):
                raise ValueError("EVRP contains an invalid native frame")
        try:
            clean_seed(duel.get("seed"))
        except ValueError as error:
            raise ValueError("EVRP duel seed is invalid") from error
        game_number = duel.get("gameNumber")
        if game_number is not None:
            if (
                type(game_number) is not int
                or game_number <= 0
                or game_number in game_numbers
            ):
                raise ValueError("EVRP duel game number is invalid")
            game_numbers.add(game_number)
        players = duel.get("players")
        if players is not None and (
            not isinstance(players, list)
            or len(players) != 2
            or any(
                not isinstance(player, dict)
                or not isinstance(player.get("name"), str)
                or not player["name"]
                for player in players
            )
        ):
            raise ValueError("EVRP duel players are invalid")
    return envelope


def public_session(session: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(session)
    result["replay_status"] = series_replay_status(session)
    result.pop("room_password", None)
    result.pop("credentials", None)
    result.pop("callback", None)
    result.pop("seed", None)
    result.pop("replay_source_ordinals", None)
    result.pop("replay_conflicts", None)
    result.pop("replay_supplements", None)
    if isinstance(result.get("provisioning"), dict):
        result["provisioning"].pop("registry_path", None)
    if isinstance(result.get("replay"), dict):
        result["replay"].pop("path", None)
    if isinstance(result.get("series_replay"), dict):
        result["series_replay"].pop("path", None)
    if isinstance(result.get("series_replay_prefix"), dict):
        result["series_replay_prefix"].pop("path", None)
    for replay in result.get("series_replays", []):
        if isinstance(replay, dict):
            replay.pop("path", None)
    current_result = result.get("result")
    if isinstance(current_result, dict):
        current_replay = current_result.get("replay")
        if isinstance(current_replay, dict):
            current_replay.pop("path", None)
    for game in result.get("results", []):
        if not isinstance(game, dict):
            continue
        game.pop("seed", None)
        completed_result = game.get("result")
        if isinstance(completed_result, dict):
            completed_replay = completed_result.get("replay")
            if isinstance(completed_replay, dict):
                completed_replay.pop("path", None)
    public_connections = {}
    timestamp = now()
    for side, item in session.get("connections", {}).items():
        scope = reconnect_scope(session, item, timestamp)
        public_connections[side] = {
            "connected": bool(item.get("connected")),
            "name": item.get("name", ""),
            "connected_at": item.get("connected_at"),
            "last_activity_at": item.get("last_activity_at"),
            "last_client_activity_at": item.get("last_client_activity_at"),
            "last_server_activity_at": item.get("last_server_activity_at"),
            "disconnected_at": item.get("disconnected_at"),
            "reconnecting": scope is not None,
            "reconnect_scope": scope,
            "reconnect_until": item.get("reconnect_until"),
            "state": connection_state(session, item, timestamp),
        }
    result["connections"] = public_connections
    result["stall_warning_seconds"] = PLAYER_STALL_WARNING_SECONDS
    result["spectators"] = int(session.get("spectators", 0))
    return result


class PvpStore:
    """Persistent session metadata and private access-code lookup."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.replay_root = self.root / "replays"
        self.replay_root.mkdir(parents=True, exist_ok=True)
        if os.name == "posix":
            os.chmod(self.replay_root, 0o755)
        self.sessions: dict[str, dict[str, Any]] = {}
        self._load()
        self.restore_connections_after_restart()
        self.expire_stale()

    def _path(self, session_id: str) -> Path:
        return self.root / f"{session_id}.json"

    def _load(self) -> None:
        for path in sorted(self.root.glob("s-*.json")):
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(value, dict) and isinstance(value.get("id"), str):
                    self.sessions[value["id"]] = value
            except (OSError, ValueError, UnicodeError):
                LOG.exception("ignored invalid session file %s", path)

    def restore_connections_after_restart(self) -> int:
        """Turn persisted sockets into reconnectable seats after a clean restart."""
        restored = 0
        timestamp = now()
        for session in self.sessions.values():
            if session.get("status") not in ACTIVE_SESSION_STATUSES:
                continue
            changed = False
            game_number = session.get("game_number")
            if (
                continuous_series_open(session)
                and type(game_number) is int
                and game_number > 0
                and session.get("game_generation") != game_number
            ):
                session["game_generation"] = game_number
                session["started_generation"] = None
                changed = True
            connections = session.setdefault("connections", {})
            for side in ("a", "b"):
                current = connections.setdefault(side, {
                    "connected": False,
                    "name": "",
                    "connected_at": None,
                    "last_activity_at": None,
                    "last_client_activity_at": None,
                    "last_server_activity_at": None,
                    "disconnected_at": None,
                    "last_name": None,
                    "reconnect_until": None,
                    "connection_id": None,
                })
                for field in (
                    "last_activity_at",
                    "last_client_activity_at",
                    "last_server_activity_at",
                    "disconnected_at",
                ):
                    if field not in current:
                        current[field] = None
                        changed = True
                if current.get("last_name") is None and current.get("name"):
                    current["last_name"] = current["name"]
                    changed = True
                series_reconnect = (
                    continuous_series_open(session)
                    and bool(current.get("last_name"))
                )
                if current.get("connected"):
                    reconnect_until = None
                    if (
                        session.get("status") == "dueling"
                        and current.get("last_name")
                        and not series_reconnect
                    ):
                        reconnect_until = timestamp + RECONNECT_GRACE_SECONDS
                    current.update({
                        "connected": False,
                        "name": "",
                        "connected_at": None,
                        "disconnected_at": timestamp,
                        "connection_id": None,
                        "reconnect_until": reconnect_until,
                    })
                    restored += 1
                    changed = True
                elif current.get("connection_id") is not None:
                    current["connection_id"] = None
                    changed = True
                if series_reconnect and current.get("reconnect_until") is not None:
                    current["reconnect_until"] = None
                    changed = True
                if (
                    session.get("status") != "dueling"
                    and not continuous_series_open(session)
                ) and (
                    current.get("last_name") is not None
                    or current.get("reconnect_until") is not None
                ):
                    current["last_name"] = None
                    current["reconnect_until"] = None
                    changed = True
            if session.get("status") in {"connecting", "ready"} and any(
                not connections[side].get("connected") for side in ("a", "b")
            ):
                session["status"] = "waiting"
                changed = True
            if changed:
                self._save(session)
        return restored

    def _save(self, session: dict[str, Any]) -> None:
        session["updated_at"] = now()
        atomic_json(self._path(session["id"]), session)

    def _get(self, session_id: str) -> dict[str, Any]:
        session = self.sessions.get(session_id)
        if session is None:
            raise web.HTTPNotFound(text="session not found")
        return session

    def expire_stale(self, timestamp: float | None = None) -> int:
        """Close abandoned live sessions while preserving their audit files."""
        cutoff = (now() if timestamp is None else timestamp) - SESSION_TTL
        expired = 0
        for session in self.sessions.values():
            if session.get("status") not in ACTIVE_SESSION_STATUSES:
                continue
            if continuous_series_open(session):
                continue
            updated_at = session.get("updated_at")
            if not isinstance(updated_at, (int, float)) or updated_at >= cutoff:
                continue
            session["status"] = "error"
            session["error"] = "session_expired"
            self._save(session)
            expired += 1
        return expired

    def create(self, body: dict[str, Any], default_host: str, default_port: int,
               callback_url: str, callback_key: str) -> dict[str, Any]:
        room_id = clean_text(body.get("room_id"), "room_id", 80)
        match_id = clean_text(body.get("match_id"), "match_id", 80)
        game_number = body.get("game_number")
        if type(game_number) is not int or not 1 <= game_number <= 10000:
            raise ValueError("game_number must be between 1 and 10000")
        seat_plan = body.get("seat_plan")
        if seat_plan not in ({"a": 0, "b": 1}, {"a": 1, "b": 0}):
            raise ValueError("seat_plan must assign a and b to seats 0 and 1")
        players = body.get("players")
        if not isinstance(players, dict) or not isinstance(players.get("a"), dict) \
                or not isinstance(players.get("b"), dict):
            raise ValueError("players.a and players.b are required")
        names = {
            side: clean_text(players[side].get("name"), f"players.{side}.name", 80)
            for side in ("a", "b")
        }
        native_player_names({"players": names})
        host = clean_text(body.get("engine_host", default_host), "engine_host", 253)
        port = body.get("engine_port", default_port)
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError("engine_port is invalid")
        format_name = clean_text(body.get("format", "tcg"), "format", 20).lower()
        if any(char in format_name for char in "#, \t\r\n"):
            raise ValueError("format contains unsupported characters")
        session_id = new_id("s")
        while session_id in self.sessions:
            session_id = new_id("s")
        room_password = random_code()
        supplied_credentials = body.get("credentials")
        if supplied_credentials is None:
            while True:
                credentials = {
                    "a": random_code(),
                    "b": random_code(),
                    "spectator": random_code(),
                }
                if len(set(credentials.values())) == 3 and not self._code_conflicts(
                    room_id, match_id, credentials
                ):
                    break
        else:
            if not isinstance(supplied_credentials, dict):
                raise ValueError("credentials must be an object")
            credentials = {
                role: clean_credential(
                    supplied_credentials.get(role), f"credentials.{role}"
                )
                for role in ("a", "b", "spectator")
            }
            if len(set(credentials.values())) != 3:
                raise ValueError("native credentials must be unique")
            if self._code_conflicts(room_id, match_id, credentials):
                raise ValueError("native credentials are already in use by another active room")
        seed = clean_seed(body.get("seed")) if body.get("seed") is not None else None
        required_first_player = body.get("required_first_player")
        if required_first_player is None:
            required_first_player = next(
                side for side, seat in seat_plan.items() if seat == 0
            )
        if required_first_player not in ("a", "b"):
            raise ValueError("required_first_player must be a or b")
        if seat_plan[required_first_player] != 0:
            raise ValueError("required_first_player must be assigned engine seat 0")
        seed_mode = body.get("seed_mode", "independent")
        if seed_mode not in ("mirrored", "independent"):
            raise ValueError("seed_mode must be mirrored or independent")
        continuous = body.get("continuous_series", False)
        if type(continuous) is not bool:
            raise ValueError("continuous_series must be boolean")
        if continuous and seed is None:
            raise ValueError("continuous series requires a seed")
        timestamp = now()
        room_command = f"p{session_id[2:]},{'m,' if continuous else ''}{format_name}"
        if len(room_command) > 19:
            raise ValueError("format produces an oversized native room command")
        session = {
            "schema": "ygo-pvp-session/v2",
            "id": session_id,
            "room_id": room_id,
            "match_id": match_id,
            "game_number": game_number,
            "engine": "native-pvp",
            "status": "waiting",
            "format": format_name,
            "engine_host": host,
            "engine_port": port,
            # The Evolution/YGOPro server accepts a rule command followed by
            # a password. The password is unique per tournament game.
            "room_command": room_command,
            "room_password": room_password,
            "seat_plan": {"a": int(seat_plan["a"]), "b": int(seat_plan["b"])},
            "required_first_player": required_first_player,
            "seed_mode": seed_mode,
            "seed": seed,
            "continuous_series": continuous,
            "initial_generation": game_number,
            "game_generation": game_number,
            "started_generation": None,
            "relay_generations": {},
            "game_started": False,
            "results": [],
            # The native engine can swap the actual player positions after
            # rock-paper-scissors.  This is filled from each player's MSG_START
            # and is the authoritative audit mapping for the game result.
            "engine_seats": {},
            "players": names,
            "credentials": credentials,
            "connections": {
                "a": {
                    "connected": False, "name": "", "connected_at": None,
                    "last_activity_at": None,
                    "last_client_activity_at": None,
                    "last_server_activity_at": None,
                    "disconnected_at": None,
                    "last_name": None, "reconnect_until": None,
                    "connection_id": None,
                },
                "b": {
                    "connected": False, "name": "", "connected_at": None,
                    "last_activity_at": None,
                    "last_client_activity_at": None,
                    "last_server_activity_at": None,
                    "disconnected_at": None,
                    "last_name": None, "reconnect_until": None,
                    "connection_id": None,
                },
            },
            "spectators": 0,
            "result": None,
            "replay": None,
            "series_replays": [],
            "series_replay_prefix": None,
            "callback": {
                "url": callback_url.rstrip("/"),
                "key": callback_key,
            } if callback_url and callback_key else None,
            "created_at": timestamp,
            "updated_at": timestamp,
        }
        if body.get("series_replay_prefix") is not None:
            session["series_replay_prefix"] = (
                self._validated_series_replay_prefix(
                    session, body["series_replay_prefix"]
                )
            )
        self.sessions[session_id] = session
        self._save(session)
        return session

    def _code_conflicts(
        self,
        room_id: str,
        match_id: str,
        credentials: dict[str, str],
    ) -> bool:
        for session in self.sessions.values():
            if session.get("status") in {"closed", "error"}:
                continue
            if not (
                set(credentials.values())
                & set(session.get("credentials", {}).values())
            ):
                continue
            same_lineage = (
                session.get("room_id") == room_id
                and session.get("match_id") == match_id
            )
            if same_lineage and session.get("status") == "finished":
                continue
            return True
        return False

    def observe_start(self, session: dict[str, Any], side: str | None,
                      payload: bytes) -> None:
        """Record the engine seat observed by one player connection."""
        if side not in ("a", "b"):
            return
        if len(payload) < 2 or payload[0] != P.MSG_START:
            return
        engine_seat = payload[1] & 0x0F
        if engine_seat not in (0, 1):
            raise ValueError("native MSG_START returned an invalid seat")
        engine_seats = session.setdefault("engine_seats", {})
        previous = engine_seats.get(side)
        if previous is not None and previous != engine_seat:
            raise ValueError("native engine seat changed during the duel")
        other = "b" if side == "a" else "a"
        if engine_seats.get(other) == engine_seat:
            raise ValueError("native MSG_START assigned duplicate engine seats")
        if previous == engine_seat:
            return
        engine_seats[side] = engine_seat
        self._save(session)

    def find_code(self, code: str) -> tuple[dict[str, Any], str, str | None] | None:
        matches = []
        for session in self.sessions.values():
            if session.get("status") in {"closed", "error"}:
                continue
            credentials = session.get("credentials", {})
            for side in ("a", "b"):
                if hmac.compare_digest(str(credentials.get(side, "")), code):
                    matches.append((session, "player", side))
            if hmac.compare_digest(str(credentials.get("spectator", "")), code):
                matches.append((session, "spectator", None))
        if not matches:
            return None
        lineages = {
            (match[0].get("room_id"), match[0].get("match_id"))
            for match in matches
        }
        if len(lineages) != 1:
            LOG.error(
                "ambiguous native credential matches %s lineages",
                len(lineages),
            )
            return None
        active = [
            match for match in matches
            if match[0].get("status") in ACTIVE_SESSION_STATUSES
        ]
        if len(active) == 1:
            return active[0]
        if len(active) > 1:
            LOG.error(
                "ambiguous native credential matches %s active sessions",
                len(active),
            )
            return None
        replayable = [
            match for match in matches
            if series_replay_available(match[0])
        ]
        if not replayable:
            return None
        replayable.sort(
            key=lambda match: (
                expected_series_duels(match[0]),
                match[0].get("created_at", 0),
                match[0].get("updated_at", 0),
                match[0].get("id", ""),
            ),
            reverse=True,
        )
        return replayable[0]

    def reserve(self, session: dict[str, Any], role: str, side: str | None,
                name: str) -> dict[str, str | None]:
        if role == "spectator" and series_replay_available(session):
            if session.get("spectators", 0) >= 16:
                raise ValueError("spectator capacity reached")
            session["spectators"] += 1
            self._save(session)
            return {"connection_id": None, "engine_name": name}
        if session["status"] in {"finished", "error", "closed"}:
            raise ValueError("session is no longer accepting connections")
        if role == "spectator":
            if session["status"] not in {"ready", "connecting", "dueling"}:
                raise ValueError("native spectators may join after both players connect")
            if session.get("spectators", 0) >= 16:
                raise ValueError("spectator capacity reached")
            session["spectators"] += 1
            self._save(session)
            return {"connection_id": None, "engine_name": name}
        assert side in ("a", "b")
        current = session["connections"][side]
        reconnecting = (
            reconnect_scope(session, current) is not None
        )
        if current.get("connected"):
            raise ValueError("player seat is already connected")
        reconnect_until = current.get("reconnect_until")
        if (
            isinstance(reconnect_until, (int, float))
            and reconnect_until <= now()
            and current.get("last_name")
            and session.get("status") == "dueling"
            and not continuous_series_open(session)
        ):
            raise ValueError("player reconnect window has expired")
        # Preserve the engine identity only for a live-duel reconnect. A lobby
        # rejection (for example, a duplicate nickname) must not pin the failed
        # name and override a corrected nickname on the next attempt.
        engine_name = (
            current.get("last_name")
            if reconnecting else native_player_names(session)[side]
        )
        connection_id = new_id("c")
        connected_at = now()
        current.update({
            "connected": True,
            "name": name,
            "connected_at": connected_at,
            "last_activity_at": connected_at,
            "last_client_activity_at": connected_at,
            "last_server_activity_at": connected_at,
            "disconnected_at": None,
        })
        current.update({
            "last_name": engine_name,
            "reconnect_until": None,
            "connection_id": connection_id,
        })
        session["status"] = "ready" if all(
            session["connections"][item].get("connected") for item in ("a", "b")
        ) else "connecting"
        self._save(session)
        return {"connection_id": connection_id, "engine_name": engine_name}

    def release(
        self,
        session: dict[str, Any],
        role: str,
        side: str | None,
        connection_id: str | None = None,
    ) -> None:
        if role == "spectator":
            session["spectators"] = max(0, int(session.get("spectators", 0)) - 1)
        elif side in ("a", "b"):
            current = session["connections"][side]
            if (
                connection_id is not None
                and current.get("connection_id") != connection_id
            ):
                return
            was_dueling = session.get("status") == "dueling"
            series_reconnect = continuous_series_open(session)
            reconnect_until = None
            if was_dueling and not series_reconnect:
                reconnect_until = now() + RECONNECT_GRACE_SECONDS
            disconnected_at = now()
            current.update({
                "connected": False,
                "name": "",
                "connected_at": None,
                "disconnected_at": disconnected_at,
                "connection_id": None,
                "last_name": (
                    current.get("last_name")
                    if was_dueling or series_reconnect else None
                ),
                "reconnect_until": reconnect_until,
            })
            if session["status"] in {"connecting", "ready"} and session.get("result") is None:
                session["status"] = "waiting"
        self._save(session)

    def touch_connection(
        self,
        session: dict[str, Any],
        side: str | None,
        direction: str,
        connection_id: str | None = None,
    ) -> None:
        if side not in ("a", "b") or direction not in {"client", "server"}:
            return
        current = session.get("connections", {}).get(side)
        if not isinstance(current, dict) or not current.get("connected"):
            return
        if (
            connection_id is not None
            and current.get("connection_id") != connection_id
        ):
            return
        timestamp = now()
        current["last_activity_at"] = timestamp
        current[f"last_{direction}_activity_at"] = timestamp

    def mark_dueling(self, session: dict[str, Any]) -> None:
        if session.get("result") is None and session["status"] not in {"finished", "closed"}:
            session["status"] = "dueling"
            self._save(session)

    def bind_relay_start(
        self,
        session: dict[str, Any],
        side: str,
    ) -> tuple[int, int]:
        """Bind one relay's DUEL_START to its next unseen series generation."""
        active_generation = current_generation(session)
        relay_generations = session.setdefault("relay_generations", {})
        last_generation = relay_generations.get(side)
        initial_generation = session.get("initial_generation")
        if type(initial_generation) is not int or initial_generation <= 0:
            known = [
                item.get("generation")
                for item in session.get("results", [])
                if type(item.get("generation")) is int
            ]
            initial_generation = min(known, default=active_generation)
            session["initial_generation"] = initial_generation
        if type(last_generation) is not int:
            generation = initial_generation
        elif last_generation < active_generation:
            generation = last_generation + 1
        else:
            generation = active_generation
        game_number = session.get("game_number")
        if generation != active_generation:
            completed = next(
                (
                    item for item in session.get("results", [])
                    if item.get("generation") == generation
                ),
                None,
            )
            if completed is None:
                raise ValueError("relay generation has no completed game")
            game_number = completed.get("game_number")
        if type(game_number) is not int or game_number <= 0:
            raise ValueError("relay generation has no game number")
        relay_generations[side] = generation
        if generation == active_generation and session.get("result") is None:
            session["started_generation"] = generation
            session["game_started"] = True
        self._save(session)
        return generation, game_number

    def _replay_game_identity(
        self,
        session: dict[str, Any],
        payload: bytes,
        hint: tuple[int | None, int | None],
    ) -> tuple[int | None, int | None] | None:
        identity = yrp2_identity(payload)
        if identity is None:
            return hint
        completed = {
            item["game_number"]: item
            for item in session.get("results", [])
            if isinstance(item, dict)
            and type(item.get("game_number")) is int
            and isinstance(item.get("result"), dict)
        }
        if isinstance(session.get("result"), dict):
            completed.setdefault(session["game_number"], {
                "game_number": session["game_number"],
                "generation": current_generation(session),
                "seed": session.get("seed"),
                "result": session["result"],
            })
        candidates = []
        previous_end = session.get("created_at")
        for number, item in sorted(completed.items()):
            ended_at = item["result"].get("recorded_at")
            generation = item.get("generation", number)
            if (
                item.get("seed") == identity["seed"]
                and type(generation) is int
                and generation > 0
                and (
                    not isinstance(previous_end, (int, float))
                    or identity["started_at"] + 1 >= previous_end
                )
                and (
                    not isinstance(ended_at, (int, float))
                    or identity["started_at"] <= ended_at
                )
            ):
                candidates.append((number, generation))
            if isinstance(ended_at, (int, float)):
                previous_end = ended_at
        if hint in candidates:
            return hint
        if len(candidates) == 1:
            actual = candidates[0]
            if actual != hint:
                LOG.info(
                    "replay identity remapped session=%s hint=%s actual=%s",
                    session["id"], hint, actual,
                )
            return actual
        digest = hashlib.sha256(payload).hexdigest()
        matches = {
            (entry.get("ordinal"), entry.get("generation", entry.get("ordinal")))
            for entry in session.get("series_replays", [])
            if entry.get("sha256") == digest
        }.intersection(candidates)
        if len(matches) == 1:
            return matches.pop()
        LOG.warning(
            "replay identity unmatched or ambiguous session=%s hint=%s candidates=%s",
            session["id"], hint, candidates,
        )
        return None

    def _record_replay_conflict(
        self,
        session: dict[str, Any],
        payload: bytes,
        source: str,
        game_number: int | None,
        generation: int | None,
    ) -> None:
        digest = hashlib.sha256(payload).hexdigest()
        conflicts = session.setdefault("replay_conflicts", [])
        if not any(
            conflict.get("ordinal") == game_number
            and conflict.get("generation") == generation
            and conflict.get("sha256") == digest
            for conflict in conflicts
        ):
            target = self.replay_root / (
                f"{session['id']}.g{game_number or 0:04d}.{source}.{digest[:12]}.yrp"
            )
            atomic_bytes(target, payload, 0o600)
            conflicts.append({
                "ordinal": game_number,
                "generation": generation,
                "source": source,
                "path": str(target),
                "bytes": len(payload),
                "sha256": digest,
            })
        LOG.warning(
            "replay conflict preserved session=%s game=%s generation=%s sha256=%s",
            session["id"], game_number, generation, digest,
        )
        self._save(session)

    def record_replay(
        self,
        session: dict[str, Any],
        payload: bytes,
        source: str | None = None,
        game_number: int | None = None,
        generation: int | None = None,
    ) -> dict[str, Any] | None:
        if len(payload) > MAX_REPLAY_BYTES or payload[:4].lower() not in {b"yrp1", b"yrp2"}:
            return None
        if session.get("continuous_series"):
            if source not in ("a", "b"):
                return None
            actual = self._replay_game_identity(
                session, payload, (game_number, generation)
            )
            if actual is None:
                self._record_replay_conflict(
                    session, payload, source, game_number, generation
                )
                return None
            game_number, generation = actual
            result = session.get("result")
            digest = hashlib.sha256(payload).hexdigest()
            replays = session.setdefault("series_replays", [])
            if game_number is None or generation is None:
                digest_match = next(
                    (
                        replay for replay in replays
                        if replay.get("sha256") == digest
                    ),
                    None,
                )
                if digest_match is not None:
                    sources = digest_match.setdefault("sources", [])
                    if source not in sources:
                        sources.append(source)
                    self._attach_replay_to_result(session, digest_match)
                    self._save(session)
                    return copy.deepcopy(digest_match)
                completed_games = [
                    (
                        completed.get("game_number"),
                        completed.get(
                            "generation", completed.get("game_number")
                        ),
                    )
                    for completed in session.get("results", [])
                ]
                if isinstance(result, dict):
                    completed_games.append((
                        result.get("game_number"),
                        result.get("generation"),
                    ))
                for candidate_number, candidate_generation in completed_games:
                    if (
                        type(candidate_number) is int
                        and candidate_number > 0
                        and type(candidate_generation) is int
                        and candidate_generation > 0
                        and not any(
                            replay.get("ordinal") == candidate_number
                            and replay.get(
                                "generation", candidate_generation
                            ) == candidate_generation
                            for replay in replays
                        )
                    ):
                        game_number = candidate_number
                        generation = candidate_generation
                        break
            if (
                type(game_number) is not int
                or game_number <= 0
                or type(generation) is not int
                or generation <= 0
            ):
                return None
            existing = next(
                (
                    replay
                    for replay in replays
                    if replay.get("ordinal") == game_number
                    and replay.get("generation", generation) == generation
                ),
                None,
            )
            if (
                existing is not None
                and existing.get("sha256") != digest
            ):
                other_match = next(
                    (
                        replay for replay in replays
                        if replay.get("sha256") == digest
                    ),
                    None,
                )
                if other_match is not None:
                    sources = other_match.setdefault("sources", [])
                    if source not in sources:
                        sources.append(source)
                    self._attach_replay_to_result(session, other_match)
                    self._save(session)
                    return copy.deepcopy(other_match)
            if existing is None:
                target = self.replay_root / (
                    f"{session['id']}.g{game_number:04d}.yrp"
                )
                atomic_bytes(target, payload, 0o600)
                existing = {
                    "ordinal": game_number,
                    "generation": generation,
                    "path": str(target),
                    "bytes": len(payload),
                    "sha256": digest,
                    "sources": [source],
                }
                replays.append(existing)
                replays.sort(key=lambda replay: int(replay["ordinal"]))
            elif existing.get("sha256") != digest:
                self._record_replay_conflict(
                    session, payload, source, game_number, generation
                )
                return None
            else:
                sources = existing.setdefault("sources", [])
                if source not in sources:
                    sources.append(source)
            self._attach_replay_to_result(session, existing)
            self._save(session)
            return copy.deepcopy(existing)
        target = self.replay_root / f"{session['id']}.yrp"
        if target.exists():
            replay = session.get("replay")
            return copy.deepcopy(replay) if isinstance(replay, dict) else None
        atomic_bytes(target, payload, 0o600)
        session["replay"] = {
            "path": str(target),
            "bytes": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
        }
        if isinstance(session.get("result"), dict):
            session["result"]["replay"] = copy.deepcopy(session["replay"])
        self._save(session)
        return copy.deepcopy(session["replay"])

    @staticmethod
    def _attach_replay_to_result(
        session: dict[str, Any],
        replay: dict[str, Any],
    ) -> None:
        game_number = replay.get("ordinal")
        generation = replay.get("generation", game_number)
        result = session.get("result")
        if (
            isinstance(result, dict)
            and result.get("generation") == generation
            and result.get("game_number") == game_number
        ):
            result["replay"] = copy.deepcopy(replay)
            session["replay"] = copy.deepcopy(replay)
        for completed in session.get("results", []):
            completed_result = completed.get("result")
            if (
                completed.get("game_number") == game_number
                and isinstance(completed_result, dict)
                and completed_result.get("generation", generation) == generation
            ):
                completed_result["replay"] = copy.deepcopy(replay)
        for supplement in session.get("replay_supplements", []):
            if (
                isinstance(supplement, dict)
                and supplement.get("generation") == generation
                and supplement.get("game_number") == game_number
                and supplement.get("delivered_at") is None
            ):
                body = supplement.get("body")
                if isinstance(body, dict):
                    body["replay"] = copy.deepcopy(replay)
                    body["replay_missing_after_timeout"] = False
                    supplement.setdefault("ready_at", now())

    def replay_entries(
        self,
        session: dict[str, Any],
    ) -> list[tuple[int, Path, dict[str, Any]]]:
        raw_entries = session.get("series_replays")
        continuous_entries = isinstance(raw_entries, list) and bool(raw_entries)
        if not continuous_entries:
            replay = session.get("replay")
            raw_entries = [replay] if isinstance(replay, dict) else []
        root = self.replay_root.resolve()
        entries = []
        seen_ordinals = set()
        for metadata in raw_entries:
            if not isinstance(metadata, dict):
                raise web.HTTPConflict(text="stored replay metadata is invalid")
            ordinal = (
                metadata.get("ordinal")
                if continuous_entries
                else session.get("game_number")
            )
            path_value = metadata.get("path")
            byte_count = metadata.get("bytes")
            digest = metadata.get("sha256")
            if (
                type(ordinal) is not int
                or ordinal <= 0
                or not isinstance(path_value, str)
                or not path_value
                or type(byte_count) is not int
                or not 0 < byte_count <= MAX_REPLAY_BYTES
                or not isinstance(digest, str)
                or len(digest) != 64
                or any(
                    char not in "0123456789abcdef"
                    for char in digest.lower()
                )
            ):
                raise web.HTTPConflict(text="stored replay metadata is invalid")
            if ordinal in seen_ordinals:
                raise web.HTTPConflict(text="stored replay ordinals are duplicated")
            seen_ordinals.add(ordinal)
            try:
                path = Path(path_value).resolve()
                if path.parent != root or not path.is_file():
                    raise web.HTTPConflict(text="stored replay file is unavailable")
                if path.stat().st_size != byte_count:
                    raise web.HTTPConflict(text="stored replay size does not match")
            except web.HTTPConflict:
                raise
            except OSError as error:
                raise web.HTTPConflict(
                    text="stored replay file is unavailable"
                ) from error
            entries.append((ordinal, path, metadata))
        entries.sort(key=lambda item: item[0])
        return entries

    def _playable_series_replay(
        self,
        session: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        metadata = session.get("series_replay")
        if not isinstance(metadata, dict):
            raise web.HTTPConflict(text="playable series replay is not available yet")
        path_value = metadata.get("path")
        byte_count = metadata.get("bytes")
        digest = metadata.get("sha256")
        duel_count = metadata.get("duels")
        if (
            not isinstance(path_value, str)
            or not path_value
            or type(byte_count) is not int
            or not 0 < byte_count <= MAX_SERIES_REPLAY_BYTES
            or not isinstance(digest, str)
            or len(digest) != 64
            or any(char not in "0123456789abcdef" for char in digest.lower())
            or type(duel_count) is not int
            or duel_count <= 0
        ):
            raise web.HTTPConflict(text="stored series replay metadata is invalid")
        try:
            path = Path(path_value).resolve()
            if path.parent != self.replay_root.resolve() or not path.is_file():
                raise web.HTTPConflict(text="stored series replay is unavailable")
            payload = path.read_bytes()
        except web.HTTPConflict:
            raise
        except OSError as error:
            raise web.HTTPConflict(
                text="stored series replay is unavailable"
            ) from error
        if len(payload) != byte_count:
            raise web.HTTPConflict(text="stored series replay size does not match")
        if hashlib.sha256(payload).hexdigest() != digest.lower():
            raise web.HTTPConflict(
                text="stored series replay checksum does not match"
            )
        try:
            envelope = validate_series_replay(payload)
        except ValueError as error:
            raise web.HTTPConflict(text=str(error)) from error
        if len(envelope["duels"]) != duel_count:
            raise web.HTTPConflict(text="stored series replay duel count does not match")
        return metadata, envelope

    @staticmethod
    def _replay_time(value: Any) -> float | None:
        if not isinstance(value, str) or not value:
            return None
        try:
            return datetime.fromisoformat(
                value.replace("Z", "+00:00")
            ).timestamp()
        except ValueError:
            return None

    @staticmethod
    def _game_plans(session: dict[str, Any]) -> list[dict[str, Any]]:
        plans: dict[int, dict[str, Any]] = {}
        for item in session.get("results", []):
            if not isinstance(item, dict):
                continue
            number = item.get("game_number")
            seed = item.get("seed")
            seats = item.get("seat_plan")
            result = item.get("result")
            if (
                type(number) is not int
                or number <= 0
                or not isinstance(seed, list)
                or len(seed) != 8
                or seats not in ({"a": 0, "b": 1}, {"a": 1, "b": 0})
                or not isinstance(result, dict)
            ):
                continue
            plans[number] = {
                "number": number,
                "seed": [int(word) for word in seed],
                "seat_plan": copy.deepcopy(seats),
                "recorded_at": result.get("recorded_at"),
            }
        current = session.get("result")
        current_number = session.get("game_number")
        current_seed = session.get("seed")
        current_seats = session.get("seat_plan")
        if (
            isinstance(current, dict)
            and type(current_number) is int
            and current_number > 0
            and isinstance(current_seed, list)
            and len(current_seed) == 8
            and current_seats in ({"a": 0, "b": 1}, {"a": 1, "b": 0})
        ):
            plans.setdefault(current_number, {
                "number": current_number,
                "seed": [int(word) for word in current_seed],
                "seat_plan": copy.deepcopy(current_seats),
                "recorded_at": current.get("recorded_at"),
            })
        return [plans[number] for number in sorted(plans)]

    @staticmethod
    def _plan_player_names(
        session: dict[str, Any],
        plan: dict[str, Any] | None,
    ) -> list[str] | None:
        if plan is None:
            return None
        names = native_player_names(session)
        by_position = {
            position: names[side]
            for side, position in plan["seat_plan"].items()
        }
        if set(by_position) != {0, 1}:
            return None
        return [by_position[0], by_position[1]]

    def _indexed_evrp_duels(
        self,
        session: dict[str, Any],
        duels: list[dict[str, Any]],
        entries: list[tuple[int, Path, dict[str, Any]]],
    ) -> list[
        tuple[
            int,
            dict[str, Any],
            dict[str, Any] | None,
            tuple[int, Path, dict[str, Any]],
        ]
    ]:
        if len(entries) != len(duels):
            raise web.HTTPConflict(
                text="native YRP and EVRP duel counts do not match"
            )
        plans = self._game_plans(session)
        plans_by_number = {plan["number"]: plan for plan in plans}
        raw_ordinals = [entry[0] for entry in entries]
        game_number = session.get("game_number")
        historical_offset = (
            game_number - len(entries)
            if (
                not plans
                and session.get("continuous_series")
                and session.get("series_finished") is True
                and type(game_number) is int
                and game_number > len(entries)
                and raw_ordinals == list(range(1, len(entries) + 1))
            )
            else 0
        )
        used = set()
        indexed = []
        previous = 0
        for index, (duel, entry) in enumerate(zip(duels, entries), 1):
            seed = clean_seed(duel.get("seed"))
            ordinal = duel.get("gameNumber")
            plan = None
            if ordinal is not None:
                plan = plans_by_number.get(ordinal)
                if plan is None or plan["seed"] != seed:
                    raise web.HTTPConflict(
                        text=f"EVRP duel {index} does not match game {ordinal}"
                    )
            elif plans:
                candidates = [
                    candidate for candidate in plans
                    if candidate["number"] not in used
                    and candidate["seed"] == seed
                ]
                if not candidates:
                    raise web.HTTPConflict(
                        text=f"EVRP duel {index} has no matching completed game"
                    )
                ended_at = self._replay_time(duel.get("endTime"))

                def candidate_order(
                    candidate: dict[str, Any],
                ) -> tuple[bool, float, int]:
                    recorded_at = candidate.get("recorded_at")
                    distance = (
                        abs(float(recorded_at) - ended_at)
                        if ended_at is not None
                        and isinstance(recorded_at, (int, float))
                        else float("inf")
                    )
                    return (
                        candidate["number"] <= previous,
                        distance,
                        candidate["number"],
                    )

                plan = min(candidates, key=candidate_order)
                ordinal = plan["number"]
            else:
                ordinal = entry[0] + historical_offset
            if (
                type(ordinal) is not int
                or ordinal <= 0
                or ordinal in used
                or ordinal <= previous
            ):
                raise web.HTTPConflict(
                    text="EVRP duel order is invalid or duplicated"
                )
            used.add(ordinal)
            previous = ordinal
            indexed.append((ordinal, duel, plan, entry))
        return indexed

    def playable_replay_archive(self, session: dict[str, Any]) -> bytes:
        entries = self.replay_entries(session)
        if not entries:
            raise web.HTTPConflict(text="replays are not available yet")
        series_metadata, envelope = self._playable_series_replay(session)
        duels = envelope["duels"]
        indexed_duels = self._indexed_evrp_duels(
            session, duels, entries
        )
        output = io.BytesIO()
        total = 0
        manifest_entries = []
        export_ordinals = [item[0] for item in indexed_duels]
        game_number = session.get("game_number")
        missing_ordinals = (
            sorted(set(range(1, game_number + 1)) - set(export_ordinals))
            if session.get("continuous_series")
            and type(game_number) is int
            and game_number > 0
            else []
        )
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as archive:
            for index, (
                ordinal,
                duel,
                plan,
                (raw_ordinal, raw_path, raw_metadata),
            ) in enumerate(
                indexed_duels,
                1,
            ):
                try:
                    raw_payload = raw_path.read_bytes()
                except OSError as error:
                    raise web.HTTPConflict(
                        text="stored replay file is unavailable"
                    ) from error
                raw_digest = hashlib.sha256(raw_payload).hexdigest()
                if (
                    len(raw_payload) != raw_metadata["bytes"]
                    or raw_digest != raw_metadata["sha256"].lower()
                ):
                    raise web.HTTPConflict(
                        text="stored replay integrity does not match"
                    )
                identity = yrp2_identity(raw_payload)
                duel_started_at = self._replay_time(duel.get("startTime"))
                if (
                    identity is not None
                    and (
                        identity["seed"] != duel["seed"]
                        or (
                            duel_started_at is not None
                            and abs(
                                identity["started_at"] - duel_started_at
                            ) > 2
                        )
                    )
                ):
                    raise web.HTTPConflict(
                        text=f"native YRP does not match EVRP duel {index}"
                    )
                try:
                    payload = evrp_duel_to_yrp3d(
                        envelope["meta"],
                        duel,
                        self._plan_player_names(session, plan),
                    )
                except ValueError as error:
                    raise web.HTTPConflict(
                        text=f"EVRP duel {index} is not playable: {error}"
                    ) from error
                total += len(payload)
                if total > MAX_REPLAY_ARCHIVE_BYTES:
                    raise web.HTTPRequestEntityTooLarge(
                        max_size=MAX_REPLAY_ARCHIVE_BYTES,
                        actual_size=total,
                    )
                digest = hashlib.sha256(payload).hexdigest()
                filename = f"game-{ordinal:04d}.yrp3d"
                archive.writestr(filename, payload)
                manifest_entries.append({
                    "ordinal": ordinal,
                    "filename": filename,
                    "bytes": len(payload),
                    "sha256": digest,
                    "source_yrp_sha256": raw_digest,
                    "source_yrp_ordinal": raw_ordinal,
                    "source_evrp_sha256": series_metadata["sha256"].lower(),
                    "source_evrp_duel": index,
                })
            manifest = {
                "schema": "ygo-pvp-replay-manifest/v2",
                "session_id": session.get("id"),
                "ordinal_scope": "absolute",
                "format": "mdpro3-yrp3d",
                "complete": not missing_ordinals,
                "historical_prefix_missing": bool(missing_ordinals),
                "missing_ordinals": missing_ordinals,
                "replays": manifest_entries,
            }
            archive.writestr(
                "manifest.json",
                json.dumps(
                    manifest,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8"),
            )
        return output.getvalue()

    def replay_archive(
        self,
        session: dict[str, Any],
        replay_format: str = "raw",
    ) -> bytes:
        if replay_format == "playable":
            return self.playable_replay_archive(session)
        if replay_format != "raw":
            raise ValueError("replay format must be playable or raw")
        entries = self.replay_entries(session)
        if not entries:
            raise web.HTTPConflict(text="replays are not available yet")
        output = io.BytesIO()
        total = 0
        manifest_entries = []
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as archive:
            for ordinal, path, metadata in entries:
                try:
                    payload = path.read_bytes()
                except OSError as error:
                    raise web.HTTPConflict(
                        text="stored replay file is unavailable"
                    ) from error
                total += len(payload)
                if total > MAX_REPLAY_ARCHIVE_BYTES:
                    raise web.HTTPRequestEntityTooLarge(
                        max_size=MAX_REPLAY_ARCHIVE_BYTES,
                        actual_size=total,
                    )
                if len(payload) != metadata["bytes"]:
                    raise web.HTTPConflict(
                        text="stored replay size does not match"
                    )
                digest = hashlib.sha256(payload).hexdigest()
                if digest != metadata["sha256"].lower():
                    raise web.HTTPConflict(
                        text="stored replay checksum does not match"
                    )
                filename = f"game-{ordinal:04d}.yrp"
                archive.writestr(filename, payload)
                manifest_entries.append({
                    "ordinal": ordinal,
                    "filename": filename,
                    "bytes": len(payload),
                    "sha256": digest,
                })
            manifest = {
                "schema": "ygo-pvp-replay-manifest/v1",
                "session_id": session.get("id"),
                "ordinal_scope": "absolute",
                "replays": manifest_entries,
            }
            archive.writestr(
                "manifest.json",
                json.dumps(
                    manifest,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8"),
            )
        return output.getvalue()

    def _read_series_archive_reference(
        self,
        reference: Any,
    ) -> tuple[dict[str, Any], bytes, dict[str, Any]]:
        if not isinstance(reference, dict):
            raise ValueError("series_replay_prefix must be an object")
        path_value = reference.get("path")
        if not isinstance(path_value, str) or not path_value:
            raise ValueError("series_replay_prefix.path is required")
        try:
            path = Path(path_value).resolve()
            if path.parent != self.replay_root.resolve() or not path.is_file():
                raise ValueError(
                    "series_replay_prefix.path must be a replay file"
                )
            payload = path.read_bytes()
        except OSError as error:
            raise ValueError("series_replay_prefix.path is unreadable") from error
        envelope = validate_series_replay(payload)
        metadata = {
            "path": str(path),
            "bytes": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
            "duels": len(envelope["duels"]),
        }
        for field in ("session_id", "room_id", "match_id"):
            if field in reference:
                metadata[field] = reference[field]
        for field in ("bytes", "sha256", "duels"):
            supplied = reference.get(field)
            if supplied is not None and supplied != metadata[field]:
                raise ValueError(
                    f"series_replay_prefix.{field} does not match the archive"
                )
        return metadata, payload, envelope

    def _validated_series_replay_prefix(
        self,
        session: dict[str, Any],
        reference: Any,
    ) -> dict[str, Any]:
        if not session.get("continuous_series"):
            raise ValueError(
                "series_replay_prefix requires a continuous series"
            )
        for field, expected in (
            ("session_id", session.get("id")),
            ("room_id", session.get("room_id")),
            ("match_id", session.get("match_id")),
        ):
            if (
                not isinstance(reference, dict)
                or reference.get(field) != expected
            ):
                raise ValueError(
                    f"series_replay_prefix.{field} must match the target session"
                )
        metadata, _payload, _envelope = (
            self._read_series_archive_reference(reference)
        )
        initial_generation = session.get("initial_generation")
        prefix_start = metadata["duels"] + 1
        if type(initial_generation) is not int:
            initial_generation = prefix_start
        game_number = session.get("game_number")
        if (
            initial_generation != prefix_start
            or type(game_number) is not int
            or game_number < initial_generation
        ):
            raise ValueError(
                "series_replay_prefix duel count must precede game_number"
            )
        session["initial_generation"] = initial_generation
        session.setdefault("game_generation", session.get("game_number"))
        return metadata

    def set_series_replay_prefix(
        self,
        session: dict[str, Any],
        reference: Any,
    ) -> dict[str, Any]:
        if session.get("series_replay") is not None:
            raise ValueError(
                "series_replay_prefix cannot replace a final replay"
            )
        metadata = self._validated_series_replay_prefix(session, reference)
        session["series_replay_prefix"] = metadata
        self._save(session)
        return copy.deepcopy(metadata)

    def _merge_series_replay_prefix(
        self,
        session: dict[str, Any],
        payload: bytes,
        envelope: dict[str, Any],
    ) -> tuple[bytes, dict[str, Any]]:
        reference = session.get("series_replay_prefix")
        if not isinstance(reference, dict):
            return payload, envelope
        metadata, _prefix_payload, prefix_envelope = (
            self._read_series_archive_reference(reference)
        )
        if metadata != reference:
            raise ValueError("series_replay_prefix metadata changed")
        current_expected = expected_series_duels(session) - metadata["duels"]
        if len(envelope["duels"]) != current_expected:
            raise ValueError(
                "EVRP current-session duel count does not match the prefix"
            )
        merged = copy.deepcopy(prefix_envelope)
        merged_meta = copy.deepcopy(prefix_envelope.get("meta", {}))
        current_meta = envelope.get("meta", {})
        for key, value in current_meta.items():
            if key != "startTime":
                merged_meta[key] = copy.deepcopy(value)
        merged["meta"] = merged_meta
        merged["duels"] = (
            copy.deepcopy(prefix_envelope["duels"])
            + copy.deepcopy(envelope["duels"])
        )
        merged_payload = gzip.compress(
            json.dumps(
                merged, ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8")
        )
        return merged_payload, validate_series_replay(merged_payload)

    def record_series_replay(
        self,
        session: dict[str, Any],
        payload: bytes,
        envelope: dict[str, Any],
    ) -> bool:
        payload, envelope = self._merge_series_replay_prefix(
            session, payload, envelope
        )
        duel_count = len(envelope["duels"])
        expected_duels = expected_series_duels(session)
        if duel_count != expected_duels:
            raise ValueError(
                f"EVRP contains {duel_count} duels; expected {expected_duels}"
            )
        digest = hashlib.sha256(payload).hexdigest()
        existing = session.get("series_replay")
        if isinstance(existing, dict):
            existing_duels = existing.get("duels")
            if (
                existing_duels == duel_count
                and existing.get("sha256") == digest
                and series_replay_stored(session)
            ):
                return False
            if type(existing_duels) is int and existing_duels > duel_count:
                return False
        target = self.replay_root / f"{session['id']}.evrp"
        atomic_bytes(target, payload, 0o644)
        session["series_replay"] = {
            "path": str(target),
            "bytes": len(payload),
            "sha256": digest,
            "duels": duel_count,
        }
        self._save(session)
        return True

    def finish(
        self,
        session: dict[str, Any],
        engine_winner: int,
        reason: int,
        generation: int | None = None,
    ) -> dict[str, Any]:
        if session.get("result") is not None:
            return session["result"]
        active_generation = current_generation(session)
        if generation is None:
            generation = active_generation
        if generation != active_generation:
            raise ValueError("native result belongs to a stale game generation")
        if engine_winner == 2:
            winner = "draw"
        elif engine_winner not in (0, 1):
            raise ValueError("native engine returned an invalid winner")
        else:
            engine_seats = session.get("engine_seats") or {}
            observed_mapping = (
                engine_seats.get("a") in (0, 1)
                and engine_seats.get("b") in (0, 1)
                and engine_seats.get("a") != engine_seats.get("b")
            )
            seat_mapping = engine_seats if observed_mapping else session["seat_plan"]
            winner = next(
                side for side, seat in seat_mapping.items()
                if seat == engine_winner
            )
        event_id = "e-" + hashlib.sha256(
            f"{session['id']}:{session['game_number']}:{winner}:{reason}".encode("ascii")
        ).hexdigest()[:32]
        result = {
            "event_id": event_id,
            "game_number": session["game_number"],
            "generation": generation,
            "winner": winner,
            "engine_winner": engine_winner,
            "engine_seats": copy.deepcopy(session.get("engine_seats", {})),
            "reason": reason,
            "recorded_at": now(),
            "replay_wait_until": (
                now() + REPLAY_WAIT_SECONDS
                if session.get("continuous_series") else None
            ),
            "replay": None,
        }
        session["result"] = result
        session["callback_delivered_at"] = None
        continuous = bool(session.get("continuous_series"))
        session["status"] = "dueling" if continuous else "finished"
        if not continuous:
            # Evolution exports EVRP for ordinary single-duel rooms too. Mark
            # the one-game series complete before that export reaches us so
            # the same durable replay path works for every tournament format.
            session["series_finished"] = True
        self._save(session)
        return result

    def advance_series(self, session: dict[str, Any],
                       next_game: dict[str, Any] | None) -> None:
        active_generation = current_generation(session)
        if next_game is not None:
            number = next_game.get("game_number")
            seats = next_game.get("seat_plan")
            seed = next_game.get("seed")
            first = next_game.get("required_first_player")
            if (type(number) is not int or number != session["game_number"] + 1
                or seats not in ({"a": 0, "b": 1}, {"a": 1, "b": 0})
                or first not in ("a", "b") or seats[first] != 0
                or next_game.get("seed_mode") not in ("mirrored", "independent")):
                raise ValueError("invalid next series game plan")
            clean_seed(seed)
        session["results"].append({
            "game_number": session["game_number"],
            "generation": active_generation,
            "seed": copy.deepcopy(session["seed"]),
            "seat_plan": copy.deepcopy(session["seat_plan"]),
            "result": copy.deepcopy(session["result"]),
        })
        if next_game is None:
            session["status"] = "finished"
            session["series_finished"] = True
        else:
            session["seed"] = clean_seed(seed)
            session["game_number"] = number
            session["game_generation"] = active_generation + 1
            session["started_generation"] = None
            session["seat_plan"] = copy.deepcopy(seats)
            session["required_first_player"] = first
            session["seed_mode"] = next_game["seed_mode"]
            session["engine_seats"] = {}
            session["result"] = None
            session["replay"] = None
            session["game_started"] = False

    def seal(self, session_id: str, reason: str = "") -> dict[str, Any]:
        """Finish admission while retaining completed games for replay."""
        session = self._get(session_id)
        if session.get("status") in {"closed", "error"}:
            raise ValueError("session cannot be sealed")
        if (
            session.get("status") == "finished"
            and session.get("series_finished") is True
        ):
            return public_session(session)

        completed_numbers = {
            item.get("game_number")
            for item in session.get("results", [])
            if (
                isinstance(item, dict)
                and type(item.get("game_number")) is int
                and item["game_number"] > 0
                and isinstance(item.get("result"), dict)
            )
        }
        current = session.get("result")
        current_number = session.get("game_number")
        if (
            isinstance(current, dict)
            and type(current_number) is int
            and current_number > 0
            and current_number not in completed_numbers
        ):
            session.setdefault("results", []).append({
                "game_number": current_number,
                "generation": current_generation(session),
                "seed": copy.deepcopy(session.get("seed")),
                "seat_plan": copy.deepcopy(session.get("seat_plan")),
                "result": copy.deepcopy(current),
            })
            completed_numbers.add(current_number)

        prefix = session.get("series_replay_prefix")
        prefix_duels = (
            prefix.get("duels")
            if isinstance(prefix, dict) and type(prefix.get("duels")) is int
            else 0
        )
        completed = max(completed_numbers, default=prefix_duels)
        session["sealed_expected_duels"] = completed
        session["status"] = "finished"
        session["series_finished"] = True
        session["callback_abandoned_at"] = now()
        session["callback_abandoned_reason"] = "administratively_sealed"
        session["administrative_seal"] = {
            "reason": reason.strip()[:200] if isinstance(reason, str) else "",
            "completed_duels": completed,
            "sealed_at": now(),
        }
        session["replay_seal"] = {
            "state": (
                "available" if series_replay_available(session)
                else "pending" if completed > 0 else "empty"
            ),
            "updated_at": now(),
        }
        if (
            completed > 0
            and completed == prefix_duels
            and isinstance(prefix, dict)
            and _series_replay_file_stored(prefix)
        ):
            session["series_replay"] = copy.deepcopy(prefix)
            session["replay_seal"]["state"] = "available"
        self._save(session)
        return public_session(session)

    def install_recovered_series_replay(
        self,
        session: dict[str, Any],
        payload: bytes,
        identity: dict[str, Any],
    ) -> bool:
        envelope = validate_series_replay(payload)
        expected = expected_series_duels(session)
        ordinals = identity.get("ordinals")
        missing = identity.get("missing_ordinals")
        if (
            session.get("status") != "finished"
            or session.get("series_finished") is not True
            or type(expected) is not int
            or expected <= 0
            or identity.get("session_id") != session.get("id")
            or identity.get("room_id") != session.get("room_id")
            or identity.get("match_id") != session.get("match_id")
            or identity.get("expected_duels") != expected
            or identity.get("duels") != len(envelope["duels"])
            or identity.get("sha256") != hashlib.sha256(payload).hexdigest()
            or not isinstance(ordinals, list)
            or not isinstance(missing, list)
            or ordinals != sorted(set(ordinals))
            or missing != sorted(set(missing))
            or len(ordinals) != len(envelope["duels"])
            or not ordinals
            or sorted(ordinals + missing) != list(range(1, expected + 1))
            or set(ordinals).intersection(missing)
        ):
            raise ValueError("recovered series replay identity is invalid")

        plans = {
            item.get("game_number"): item
            for item in session.get("results", [])
            if isinstance(item, dict)
        }
        for ordinal, duel in zip(ordinals, envelope["duels"]):
            plan = plans.get(ordinal)
            if (
                not isinstance(plan, dict)
                or duel.get("gameNumber") != ordinal
                or duel.get("seed") != plan.get("seed")
            ):
                raise ValueError(
                    f"recovered series replay game {ordinal} is invalid"
                )

        if series_replay_available(session):
            return False
        target = self.replay_root / f"{session['id']}.evrp"
        atomic_bytes(target, payload, 0o644)
        metadata = {
            "path": str(target),
            "bytes": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
            "duels": len(envelope["duels"]),
            "recovered_from_yrp": True,
        }
        if missing:
            metadata.update({
                "partial": True,
                "ordinals": list(ordinals),
                "missing_ordinals": list(missing),
            })
        session["series_replay"] = metadata
        session["replay_seal"] = {
            "state": "partial" if missing else "available",
            "updated_at": now(),
        }
        self._save(session)
        return True

    def close(self, session_id: str) -> dict[str, Any]:
        session = self._get(session_id)
        session["status"] = "closed"
        self._save(session)
        return public_session(session)


class RoomProvisioningRegistry:
    """Publish deterministic room options for the local Evolution process."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def publish(
        self,
        session: dict[str, Any],
        fixed_deck: FixedDeck | None,
    ) -> Path:
        room_key = session["room_command"].split(",", 1)[0]
        value = {
            "schema": "ygo-evolution-room-provision/v1",
            "session_id": session["id"],
            "room_key": room_key,
            "room_command": session["room_command"],
            "seed": copy.deepcopy(session.get("seed")),
            "game_number": session.get("game_number"),
            "continuous_series": bool(session.get("continuous_series")),
            "series_finished": bool(session.get("series_finished")),
            "series_replay": (
                copy.deepcopy(session.get("series_replay"))
                if series_replay_available(session) else None
            ),
            "seed_mode": session.get("seed_mode"),
            "seat_plan": copy.deepcopy(session["seat_plan"]),
            "players": copy.deepcopy(native_player_names(session)),
            # A non-seeded native duel keeps the normal RPS flow. Seeded
            # tournament games use this field to bypass RPS deterministically.
            "required_first_player": (
                session["required_first_player"]
                if session.get("seed") is not None else None
            ),
            # Tournament spectators receive the full public duel stream:
            # hands and revealed zones are visible, while the native protocol
            # still keeps unknown deck contents as counts/unknown cards.
            "full_spectator": True,
            "time_limit": TOURNAMENT_TIME_LIMIT_SECONDS,
            "fixed_deck": (
                fixed_deck.public_metadata() if fixed_deck else None
            ),
            "created_at": session["created_at"],
        }
        target = self.root / f"{room_key}.json"
        atomic_json(target, value)
        if os.name == "posix":
            os.chmod(target, 0o644)
        session["provisioning"] = {
            "room_key": room_key,
            "registry_path": str(target),
            "published_at": now(),
        }
        return target


class PvpApplication:
    def __init__(
        self,
        data_dir: Path,
        internal_key: str | None,
        engine_host: str,
        engine_port: int,
        tournament_url: str = "",
        tournament_key: str = "",
        fixed_deck: Path | None = None,
        room_registry_dir: Path | None = None,
    ):
        self.fixed_deck: FixedDeck | None = (
            load_ydk(fixed_deck) if fixed_deck else None
        )
        self.store = PvpStore(data_dir)
        self.internal_key = internal_key or os.environ.get("PVP_INTERNAL_KEY", "")
        self.engine_host = engine_host
        self.engine_port = engine_port
        self.tournament_url = tournament_url.rstrip("/")
        self.tournament_key = tournament_key
        self.room_registry = (
            RoomProvisioningRegistry(room_registry_dir)
            if room_registry_dir else None
        )
        self.lock = asyncio.Lock()
        self.series_replay_chunks: dict[str, dict[str, Any]] = {}
        self.series_replay_inflight: set[str] = set()
        self.tasks: set[asyncio.Task[Any]] = set()
        self.gateway: NativeGateway | None = None
        self.http: ClientSession | None = None
        self.callback_task: asyncio.Task[Any] | None = None
        self.callback_inflight: set[str] = set()
        default_builder = (
            Path(__file__).resolve().parents[1]
            / "tools" / "build_partial_series_replay.py"
        )
        self.replay_builder = Path(os.environ.get(
            "PVP_REPLAY_BUILDER",
            str(
                default_builder
                if default_builder.is_file()
                else Path(
                    "/opt/ygo-evolution/tools/"
                    "build_partial_series_replay.py"
                )
            ),
        ))
        for session in self.store.sessions.values():
            changed = self.ensure_fixed_deck_session(session)
            if self.room_registry is not None and (
                session.get("status") in ACTIVE_SESSION_STATUSES
                or series_replay_available(session)
            ):
                self.room_registry.publish(session, self.fixed_deck)
                changed = True
            if changed:
                self.store._save(session)

    def ensure_fixed_deck_session(self, session: dict[str, Any]) -> bool:
        if self.fixed_deck is None:
            return False
        changed = False
        metadata = self.fixed_deck.public_metadata()
        if session.get("fixed_deck") != metadata:
            session["fixed_deck"] = metadata
            changed = True
        replacements = session.setdefault("deck_replacements", {})
        for side in ("a", "b"):
            if side not in replacements:
                replacements[side] = {
                    "replacement_count": 0,
                    "first_submitted_payload_sha256": None,
                    "submitted_payload_sha256": None,
                    "target_payload_sha256": self.fixed_deck.payload_sha256,
                    "target_deck_sha256": self.fixed_deck.canonical_sha256,
                    "last_replaced_at": None,
                }
                changed = True
        return changed

    def fixed_deck_ready(self, session: dict[str, Any]) -> bool:
        if self.fixed_deck is None:
            return True
        replacements = session.get("deck_replacements") or {}
        return all(
            isinstance(replacements.get(side), dict)
            and replacements[side].get("replacement_count", 0) >= 1
            and replacements[side].get("target_payload_sha256")
            == self.fixed_deck.payload_sha256
            and replacements[side].get("target_deck_sha256")
            == self.fixed_deck.canonical_sha256
            for side in ("a", "b")
        )

    async def record_deck_replacement(
        self,
        session: dict[str, Any],
        side: str,
        submitted_payload: bytes,
    ) -> None:
        if self.fixed_deck is None:
            return
        if side not in ("a", "b"):
            raise ValueError("fixed deck replacement requires a player side")
        submitted_hash = hashlib.sha256(submitted_payload).hexdigest()
        async with self.lock:
            self.ensure_fixed_deck_session(session)
            audit = session["deck_replacements"][side]
            if audit["first_submitted_payload_sha256"] is None:
                audit["first_submitted_payload_sha256"] = submitted_hash
            audit["submitted_payload_sha256"] = submitted_hash
            audit["replacement_count"] += 1
            audit["target_payload_sha256"] = self.fixed_deck.payload_sha256
            audit["target_deck_sha256"] = self.fixed_deck.canonical_sha256
            audit["last_replaced_at"] = now()
            self.store._save(session)

    async def record_series_replay_chunk(
        self,
        session: dict[str, Any],
        payload: bytes,
    ) -> bool:
        if len(payload) < 6:
            LOG.warning("ignored short EVRP chunk session=%s", session["id"])
            return False
        version = payload[0]
        index = int.from_bytes(payload[1:3], "little")
        count = int.from_bytes(payload[3:5], "little")
        chunk = payload[5:]
        if (
            version != 1
            or not 1 <= count <= MAX_SERIES_REPLAY_CHUNKS
            or index >= count
            or not chunk
        ):
            LOG.warning("ignored invalid EVRP chunk session=%s", session["id"])
            return False
        session_id = session["id"]
        archive: bytes | None = None
        async with self.lock:
            if (
                series_replay_stored(session)
                or session_id in self.series_replay_inflight
            ):
                return False
            accumulator = self.series_replay_chunks.setdefault(session_id, {
                "version": version,
                "count": count,
                "chunks": {},
                "bytes": 0,
            })
            if (
                accumulator["version"] != version
                or accumulator["count"] != count
            ):
                self.series_replay_chunks.pop(session_id, None)
                LOG.warning(
                    "discarded inconsistent EVRP chunks session=%s", session_id
                )
                return False
            previous = accumulator["chunks"].get(index)
            if previous is not None:
                if previous != chunk:
                    self.series_replay_chunks.pop(session_id, None)
                    LOG.warning(
                        "discarded conflicting EVRP chunk session=%s", session_id
                    )
                return False
            accumulator["chunks"][index] = bytes(chunk)
            accumulator["bytes"] += len(chunk)
            if accumulator["bytes"] > MAX_SERIES_REPLAY_BYTES:
                self.series_replay_chunks.pop(session_id, None)
                LOG.warning(
                    "discarded oversized EVRP archive session=%s", session_id
                )
                return False
            if len(accumulator["chunks"]) == count:
                archive = b"".join(
                    accumulator["chunks"][part] for part in range(count)
                )
                self.series_replay_chunks.pop(session_id, None)
                self.series_replay_inflight.add(session_id)
        if archive is None:
            return False
        try:
            envelope = await asyncio.to_thread(validate_series_replay, archive)
            async with self.lock:
                stored = self.store.record_series_replay(
                    session, archive, envelope
                )
                if stored:
                    if self.room_registry is not None:
                        self.room_registry.publish(session, self.fixed_deck)
                        self.store._save(session)
            LOG.info(
                "series replay archived session=%s duels=%s bytes=%s",
                session_id,
                session.get("series_replay", {}).get(
                    "duels", len(envelope["duels"])
                ),
                session.get("series_replay", {}).get("bytes", len(archive)),
            )
            return True
        except (OSError, ValueError):
            LOG.exception("ignored invalid EVRP archive session=%s", session_id)
            return False
        finally:
            async with self.lock:
                self.series_replay_inflight.discard(session_id)

    def require_internal(self, request: web.Request) -> None:
        if not self.internal_key or not hmac.compare_digest(
            request.headers.get("X-PVP-Internal-Key", ""), self.internal_key
        ):
            raise web.HTTPUnauthorized(text="pvp internal key required")

    async def create_session(self, request: web.Request) -> web.Response:
        self.require_internal(request)
        body = await request.json()
        async with self.lock:
            session = self.store.create(
                body,
                self.engine_host,
                self.engine_port,
                self.tournament_url + "/api/internal/duel-events"
                if self.tournament_url else "",
                self.tournament_key,
            )
            if self.ensure_fixed_deck_session(session):
                self.store._save(session)
            if self.room_registry is not None:
                self.room_registry.publish(session, self.fixed_deck)
                self.store._save(session)
        player_passwords = {
            "a": session["credentials"]["a"],
            "b": session["credentials"]["b"],
        }
        return web.json_response({
            "session": public_session(session),
            "native": {
                "host": request.headers.get("X-PVP-Public-Host", request.host.split(":")[0]),
                "port": request.app["tcp_port"],
                "player_passwords": player_passwords,
                "player_codes": player_passwords,
                "spectator_password": session["credentials"]["spectator"],
                "spectator_code": session["credentials"]["spectator"],
            },
        })

    async def session(self, request: web.Request) -> web.Response:
        session = self.store._get(request.match_info["session_id"])
        code = request.query.get("code", "")
        found = self.store.find_code(code) if code else None
        if not found or found[0]["id"] != session["id"]:
            self.require_internal(request)
        return web.json_response(public_session(session))

    async def close_session(self, request: web.Request) -> web.Response:
        self.require_internal(request)
        session_id = request.match_info["session_id"]
        async with self.lock:
            result = self.store.close(session_id)
        if self.gateway is not None:
            self.gateway.disconnect_session(session_id)
        return web.json_response(result)

    async def recover_sealed_replay(self, session_id: str) -> None:
        await asyncio.sleep(0.25)
        async with self.lock:
            session = self.store._get(session_id)
            if series_replay_available(session):
                return
            expected = expected_series_duels(session)
            if expected <= 0:
                return
            session["replay_seal"] = {
                "state": "recovering",
                "updated_at": now(),
            }
            self.store._save(session)

        try:
            if not self.replay_builder.is_file():
                raise FileNotFoundError(
                    f"replay builder not found: {self.replay_builder}"
                )
            with tempfile.TemporaryDirectory(
                prefix=f"{session_id}-seal-",
                dir=self.store.replay_root,
            ) as temporary:
                root = Path(temporary)
                candidate = root / "series.evrp"
                identity_path = root / "series.identity.json"
                completed = await asyncio.to_thread(
                    subprocess.run,
                    [
                        sys.executable,
                        str(self.replay_builder),
                        str(self.store._path(session_id)),
                        str(candidate),
                        "--identity",
                        str(identity_path),
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=600,
                )
                payload = candidate.read_bytes()
                identity = json.loads(
                    identity_path.read_text(encoding="utf-8")
                )
                async with self.lock:
                    session = self.store._get(session_id)
                    stored = self.store.install_recovered_series_replay(
                        session, payload, identity
                    )
                    if stored and self.room_registry is not None:
                        self.room_registry.publish(
                            session, self.fixed_deck
                        )
                        self.store._save(session)
                LOG.info(
                    "sealed replay recovered session=%s output=%s",
                    session_id,
                    completed.stdout.strip(),
                )
        except (
            OSError,
            ValueError,
            subprocess.CalledProcessError,
            subprocess.TimeoutExpired,
        ) as error:
            LOG.warning(
                "sealed replay recovery unavailable session=%s error=%s",
                session_id,
                error,
            )
            async with self.lock:
                session = self.store._get(session_id)
                if not series_replay_available(session):
                    session["replay_seal"] = {
                        "state": "unavailable",
                        "error": str(error)[:500],
                        "updated_at": now(),
                    }
                    self.store._save(session)

    async def seal_session(self, request: web.Request) -> web.Response:
        self.require_internal(request)
        session_id = request.match_info["session_id"]
        body = await request.json() if request.can_read_body else {}
        reason = body.get("reason", "") if isinstance(body, dict) else ""
        async with self.lock:
            result = self.store.seal(session_id, reason)
            session = self.store._get(session_id)
            if self.room_registry is not None:
                self.room_registry.publish(session, self.fixed_deck)
                self.store._save(session)
        if self.gateway is not None:
            self.gateway.disconnect_session(session_id)
        if not result["replay_status"]["available"]:
            task = asyncio.create_task(
                self.recover_sealed_replay(session_id)
            )
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)
        return web.json_response(result)

    async def set_replay_prefix(self, request: web.Request) -> web.Response:
        self.require_internal(request)
        session = self.store._get(request.match_info["session_id"])
        body = await request.json()
        async with self.lock:
            prefix = self.store.set_series_replay_prefix(session, body)
        return web.json_response({
            "session": public_session(session),
            "series_replay_prefix": {
                key: value for key, value in prefix.items() if key != "path"
            },
        })

    async def download_replays(self, request: web.Request) -> web.Response:
        self.require_internal(request)
        session = self.store._get(request.match_info["session_id"])
        replay_format = request.query.get("format", "playable").lower()
        if replay_format not in {"playable", "raw"}:
            raise web.HTTPBadRequest(text="format must be playable or raw")
        archive = await asyncio.to_thread(
            self.store.replay_archive,
            session,
            replay_format,
        )
        return web.Response(
            body=archive,
            content_type="application/zip",
            headers={
                "Content-Disposition": (
                    f'attachment; filename="{session["id"]}-replays.zip"'
                ),
            },
        )

    async def health(self, _request: web.Request) -> web.Response:
        active = sum(
            session["status"] in ACTIVE_SESSION_STATUSES
            for session in self.store.sessions.values()
        )
        return web.json_response({
            "status": "ok",
            "service": "ygo-pvp",
            "sessions": len(self.store.sessions),
            "active_sessions": active,
            "native_tcp_port": self.gateway.port if self.gateway else None,
            "fixed_deck": (
                self.fixed_deck.public_metadata() if self.fixed_deck else None
            ),
            "room_registry": (
                str(self.room_registry.root) if self.room_registry else None
            ),
        })

    def app(self, tcp_port: int) -> web.Application:
        application = web.Application(client_max_size=64 * 1024)
        application["tcp_port"] = tcp_port
        application.router.add_get("/health", self.health)
        application.router.add_post("/api/sessions", self.create_session)
        application.router.add_get("/api/sessions/{session_id}", self.session)
        application.router.add_get(
            "/api/sessions/{session_id}/replays",
            self.download_replays,
        )
        application.router.add_post(
            "/api/sessions/{session_id}/replay-prefix",
            self.set_replay_prefix,
        )
        application.router.add_post(
            "/api/sessions/{session_id}/seal",
            self.seal_session,
        )
        application.router.add_post("/api/sessions/{session_id}/close", self.close_session)
        return application

    async def finish_result(
        self,
        session: dict[str, Any],
        engine_winner: int,
        reason: int,
        generation: int | None = None,
    ) -> bool:
        async with self.lock:
            if session["status"] == "closed":
                return False
            if session.get("continuous_series"):
                active_generation = current_generation(session)
                if (
                    generation != active_generation
                    or session.get("started_generation") != active_generation
                    or session.get("result") is not None
                ):
                    return False
            if not self.fixed_deck_ready(session):
                if session.get("result") is None:
                    session["status"] = "error"
                    session["error"] = "fixed_deck_not_applied_to_both_players"
                    session["rejected_result"] = {
                        "engine_winner": engine_winner,
                        "reason": reason,
                        "recorded_at": now(),
                    }
                    self.store._save(session)
                LOG.error(
                    "native result rejected before both fixed decks were applied session=%s",
                    session["id"],
                )
                return False
            self.store.finish(session, engine_winner, reason, generation)
            return True

    async def publish_result(self, session: dict[str, Any]) -> None:
        """Publish only after the result and replay (when available) are durable."""
        if (session["status"] in {"closed", "error"}
                or session.get("result") is None
                or session.get("callback_delivered_at")
                or session.get("callback_abandoned_at")):
            return
        session_id = session["id"]
        if session_id in self.callback_inflight:
            return
        callback = session.get("callback") or {}
        callback_url = callback.get("url")
        callback_key = callback.get("key")
        if not callback_url or not callback_key or self.http is None:
            return
        result = session["result"]
        replay = result.get("replay")
        replay_fallback = False
        if session.get("continuous_series") and not isinstance(replay, dict):
            replay_wait_until = result.get("replay_wait_until")
            if (
                isinstance(replay_wait_until, (int, float))
                and now() < replay_wait_until
            ):
                return
            replay_fallback = True
        if session.get("seed") is not None:
            observed = session.get("engine_seats") or {}
            expected = session.get("seat_plan") or {}
            if not all(observed.get(side) in (0, 1) for side in ("a", "b")):
                return
            if observed != expected:
                async with self.lock:
                    session["status"] = "error"
                    session["error"] = "required_first_player_not_enforced"
                    session["callback_abandoned_at"] = now()
                    self.store._save(session)
                LOG.error(
                    "native result rejected because engine seats differ session=%s",
                    session["id"],
                )
                return
        self.callback_inflight.add(session_id)
        body = {
            "event_id": result["event_id"],
            "session_id": session["id"],
            "room_id": session["room_id"],
            "match_id": session["match_id"],
            "game_number": session["game_number"],
            "winner": result["winner"],
            "engine_winner": result.get("engine_winner"),
            "engine_seats": copy.deepcopy(session.get("engine_seats", {})),
            "reason": result["reason"],
            "seed": copy.deepcopy(session.get("seed")),
            "seed_mode": session.get("seed_mode"),
            "seat_plan": copy.deepcopy(session.get("seat_plan")),
            "required_first_player": session.get(
                "required_first_player"
            ),
            "replay": copy.deepcopy(replay),
            "replay_missing_after_timeout": replay_fallback,
            "fixed_deck": copy.deepcopy(session.get("fixed_deck")),
            "deck_replacements": copy.deepcopy(
                session.get("deck_replacements", {})
            ),
        }
        raw = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()
        signature = hmac.new(callback_key.encode(), raw, hashlib.sha256).hexdigest()
        try:
            async with self.http.post(
                callback_url,
                data=raw,
                headers={
                    "Content-Type": "application/json",
                    "X-PVP-Signature": signature,
                },
            ) as response:
                if response.status == 404:
                    async with self.lock:
                        if (
                            session.get("result", {}).get("event_id")
                            == result.get("event_id")
                            and not session.get("callback_delivered_at")
                        ):
                            session["callback_abandoned_at"] = now()
                            session["callback_abandoned_status"] = 404
                            session[
                                "callback_abandoned_reason"
                            ] = "tournament_target_not_found"
                            self.store._save(session)
                    LOG.warning(
                        "tournament callback abandoned status=404 session=%s",
                        session["id"],
                    )
                elif response.status >= 300:
                    LOG.error("tournament callback failed status=%s session=%s",
                              response.status, session["id"])
                else:
                    next_game = None
                    if session.get("continuous_series"):
                        answer = await response.json()
                        if not answer.get("ok") or (
                            not answer.get("series_finished")
                            and not isinstance(answer.get("next_game"), dict)
                        ):
                            raise ValueError("invalid continuous series callback")
                        next_game = answer.get("next_game")
                    async with self.lock:
                        if session["status"] == "closed":
                            return
                        if (
                            session.get("result", {}).get("event_id")
                            != result.get("event_id")
                        ):
                            return
                        candidate = copy.deepcopy(session)
                        registry_target = None
                        registry_before = None
                        if replay_fallback and isinstance(
                            candidate.get("result"), dict
                        ):
                            candidate["result"][
                                "replay_missing_after_timeout"
                            ] = True
                            supplement_body = copy.deepcopy(body)
                            late_replay = candidate["result"].get("replay")
                            if isinstance(late_replay, dict):
                                supplement_body["replay"] = copy.deepcopy(
                                    late_replay
                                )
                                supplement_body[
                                    "replay_missing_after_timeout"
                                ] = False
                            supplements = candidate.setdefault(
                                "replay_supplements", []
                            )
                            if not any(
                                isinstance(item, dict)
                                and item.get("event_id")
                                == result.get("event_id")
                                for item in supplements
                            ):
                                supplements.append({
                                    "event_id": result["event_id"],
                                    "generation": result.get("generation"),
                                    "game_number": result.get("game_number"),
                                    "body": supplement_body,
                                    "queued_at": now(),
                                    "ready_at": (
                                        now()
                                        if isinstance(late_replay, dict)
                                        else None
                                    ),
                                    "attempts": 0,
                                    "last_attempt_at": None,
                                    "delivered_at": None,
                                })
                        if candidate.get("continuous_series"):
                            if self.room_registry is None:
                                raise ValueError(
                                    "continuous series requires room registry"
                                )
                            self.store.advance_series(candidate, next_game)
                            room_key = candidate["room_command"].split(",", 1)[0]
                            registry_target = (
                                self.room_registry.root / f"{room_key}.json"
                            )
                            if registry_target.exists():
                                registry_before = registry_target.read_bytes()
                            self.room_registry.publish(
                                candidate, self.fixed_deck
                            )
                        candidate["callback_delivered_at"] = now()
                        try:
                            self.store._save(candidate)
                        except Exception:
                            if registry_target is not None:
                                if registry_before is None:
                                    registry_target.unlink(missing_ok=True)
                                else:
                                    atomic_bytes(
                                        registry_target,
                                        registry_before,
                                        0o644,
                                    )
                            raise
                        session.clear()
                        session.update(candidate)
        except (ClientError, OSError, asyncio.TimeoutError, ValueError):
            LOG.exception("tournament callback failed session=%s", session["id"])
        finally:
            self.callback_inflight.discard(session_id)

    async def publish_replay_supplement(
        self,
        session: dict[str, Any],
    ) -> None:
        session_id = session["id"]
        if session_id in self.callback_inflight:
            return
        callback = session.get("callback") or {}
        callback_url = callback.get("url")
        callback_key = callback.get("key")
        if not callback_url or not callback_key or self.http is None:
            return
        pending = next(
            (
                item for item in session.get("replay_supplements", [])
                if isinstance(item, dict)
                and item.get("delivered_at") is None
                and isinstance(item.get("body"), dict)
                and isinstance(item["body"].get("replay"), dict)
            ),
            None,
        )
        if pending is None:
            return
        event_id = pending.get("event_id")
        if not isinstance(event_id, str):
            return
        self.callback_inflight.add(session_id)
        try:
            async with self.lock:
                candidate = copy.deepcopy(session)
                candidate_pending = next(
                    (
                        item for item in candidate.get(
                            "replay_supplements", []
                        )
                        if isinstance(item, dict)
                        and item.get("event_id") == event_id
                        and item.get("delivered_at") is None
                        and isinstance(item.get("body"), dict)
                        and isinstance(item["body"].get("replay"), dict)
                    ),
                    None,
                )
                if candidate_pending is None:
                    return
                candidate_pending["attempts"] = (
                    int(candidate_pending.get("attempts", 0)) + 1
                )
                candidate_pending["last_attempt_at"] = now()
                body = copy.deepcopy(candidate_pending["body"])
                self.store._save(candidate)
                session.clear()
                session.update(candidate)
            raw = json.dumps(
                body, ensure_ascii=False, separators=(",", ":")
            ).encode()
            signature = hmac.new(
                callback_key.encode(), raw, hashlib.sha256
            ).hexdigest()
            async with self.http.post(
                callback_url,
                data=raw,
                headers={
                    "Content-Type": "application/json",
                    "X-PVP-Signature": signature,
                },
            ) as response:
                if response.status >= 300:
                    LOG.error(
                        "tournament replay supplement failed status=%s "
                        "session=%s event=%s",
                        response.status,
                        session_id,
                        event_id,
                    )
                    return
            async with self.lock:
                candidate = copy.deepcopy(session)
                candidate_pending = next(
                    (
                        item for item in candidate.get(
                            "replay_supplements", []
                        )
                        if isinstance(item, dict)
                        and item.get("event_id") == event_id
                        and item.get("delivered_at") is None
                    ),
                    None,
                )
                if candidate_pending is None:
                    return
                candidate_pending["delivered_at"] = now()
                self.store._save(candidate)
                session.clear()
                session.update(candidate)
        except (ClientError, OSError, asyncio.TimeoutError, ValueError):
            LOG.exception(
                "tournament replay supplement failed session=%s event=%s",
                session_id,
                event_id,
            )
        finally:
            self.callback_inflight.discard(session_id)

    async def _callback_retry_loop(self) -> None:
        while True:
            await asyncio.sleep(CALLBACK_RETRY_INTERVAL)
            for session in list(self.store.sessions.values()):
                if session.get("result") is not None and not session.get(
                    "callback_delivered_at"
                ):
                    await self.publish_result(session)
                await self.publish_replay_supplement(session)

    async def start_http(self, host: str, port: int, tcp_port: int) -> web.AppRunner:
        self.http = ClientSession(timeout=ClientTimeout(total=8), trust_env=False)
        runner = web.AppRunner(self.app(tcp_port), access_log=None)
        await runner.setup()
        await web.TCPSite(runner, host, port).start()
        self.callback_task = asyncio.create_task(self._callback_retry_loop())
        return runner

    async def close(self) -> None:
        if self.callback_task is not None:
            self.callback_task.cancel()
            await asyncio.gather(self.callback_task, return_exceptions=True)
            self.callback_task = None
        for task in list(self.tasks):
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        if self.http is not None:
            await self.http.close()


class NativeGateway:
    def __init__(self, application: PvpApplication, host: str, port: int):
        self.application = application
        self.host = host
        self.port = port
        self.server: asyncio.AbstractServer | None = None
        self.tasks: set[asyncio.Task[Any]] = set()
        self.session_tasks: dict[str, set[asyncio.Task[Any]]] = {}

    def disconnect_session(self, session_id: str) -> None:
        for task in list(self.session_tasks.get(session_id, ())):
            task.cancel()

    async def start(self) -> None:
        self.server = await asyncio.start_server(
            self.accept, self.host, self.port, limit=65536, backlog=64
        )

    def accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.create_task(self.handle(reader, writer))
        self.tasks.add(task)
        self.application.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        task.add_done_callback(self.application.tasks.discard)

    async def reject(self, writer: asyncio.StreamWriter, reason: str) -> None:
        LOG.info("native connection rejected: %s", reason)
        try:
            writer.write(P.packet(0x02, b"\x01\0\0\0\0\0\0\0"))
            await writer.drain()
        except (ConnectionError, OSError):
            pass
        await P.close_writer(writer)

    async def handle(self, reader: asyncio.StreamReader, client_writer: asyncio.StreamWriter) -> None:
        backend_writer: asyncio.StreamWriter | None = None
        session: dict[str, Any] | None = None
        role = ""
        side: str | None = None
        joined = False
        reserved = False
        connection_id: str | None = None
        try:
            prefix: list[tuple[int, bytes]] = []
            player_name = ""
            join_payload: bytes | None = None
            for _ in range(8):
                opcode, payload, raw = await asyncio.wait_for(P.read_frame(reader), 10)
                if opcode == P.PLAYER_INFO:
                    if len(payload) < 40:
                        raise P.ProtocolError("PLAYER_INFO is too short")
                    player_name = P.decode_name(payload)
                    prefix.append((opcode, raw))
                elif opcode == P.JOIN_GAME:
                    join_payload = payload
                    break
                elif opcode == 0x17:
                    prefix.append((opcode, raw))
                else:
                    raise P.ProtocolError("expected PLAYER_INFO before JOIN_GAME")
            if join_payload is None:
                raise P.ProtocolError("JOIN_GAME was not received")
            version = P.client_version(join_payload)
            code = P.join_password(join_payload)
            found = self.application.store.find_code(code)
            if found is None:
                raise ValueError("unknown session code")
            session, role, side = found
            task = asyncio.current_task()
            if task is not None:
                self.session_tasks.setdefault(session["id"], set()).add(task)
            if role == "spectator" and (
                session["status"] not in {"ready", "connecting", "dueling"}
                and not series_replay_available(session)
            ):
                raise ValueError("session is not watchable")
            async with self.application.lock:
                reservation = self.application.store.reserve(
                    session, role, side, player_name
                )
                connection_id = reservation["connection_id"]
                engine_name = reservation["engine_name"] or player_name
                reserved = True
            backend_reader, backend_writer = await asyncio.wait_for(
                asyncio.open_connection(session["engine_host"], session["engine_port"]),
                15,
            )
            for opcode, raw in prefix:
                if (
                    opcode == P.PLAYER_INFO
                    and role == "player"
                ):
                    # The access code selects the tournament side. Evolution
                    # receives the canonical session identity so it can assign
                    # the planned physical seat regardless of arrival order or
                    # the nickname configured in the client.
                    raw = P.packet(P.PLAYER_INFO, P.encode_name(engine_name))
                backend_writer.write(raw)
            backend_writer.write(
                P.packet(P.JOIN_GAME, P.engine_join(
                    join_payload, session["room_command"]
                ))
            )
            await backend_writer.drain()
            joined = True
            LOG.info("native joined session=%s role=%s client_version=0x%04x",
                     session["id"], role, version)
            await self.relay(
                reader, client_writer, backend_reader, backend_writer, session, role, side,
                version in P.LEGACY_GAME_MESSAGE_VERSIONS,
                connection_id,
            )
        except asyncio.CancelledError:
            raise
        except (P.ProtocolError, ValueError, asyncio.TimeoutError,
                asyncio.IncompleteReadError, ConnectionError, OSError) as error:
            if not joined:
                await self.reject(client_writer, str(error))
            elif not isinstance(error, asyncio.IncompleteReadError):
                LOG.info("native relay ended session=%s reason=%s",
                         session["id"], type(error).__name__)
        finally:
            if session is not None:
                self.session_tasks.get(session["id"], set()).discard(
                    asyncio.current_task()
                )
            if backend_writer is not None:
                await P.close_writer(backend_writer)
            if session is not None and reserved:
                async with self.application.lock:
                    self.application.store.release(
                        session, role, side, connection_id
                    )
            await P.close_writer(client_writer)

    async def relay(
        self,
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
        backend_reader: asyncio.StreamReader,
        backend_writer: asyncio.StreamWriter,
        session: dict[str, Any],
        role: str,
        side: str | None,
        legacy: bool = False,
        connection_id: str | None = None,
    ) -> None:
        relay_state: dict[str, Any] = {
            "series_leave_guard_until": 0.0,
            "generation": None,
            "game_number": None,
            "game_complete": False,
            "completed_games": [],
        }
        client_to_backend = asyncio.create_task(
            self._client_to_backend(
                client_reader, backend_writer, session, role, side, relay_state,
                connection_id,
            )
        )
        backend_to_client = asyncio.create_task(
            self._backend_to_client(
                backend_reader, client_writer, session, role, side, legacy,
                relay_state, connection_id,
            )
        )
        done, pending = await asyncio.wait(
            (client_to_backend, backend_to_client),
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        results = await asyncio.gather(*done, *pending, return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException) and not isinstance(
                result, (asyncio.CancelledError, asyncio.IncompleteReadError)
            ):
                raise result

    async def _client_to_backend(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        session: dict[str, Any],
        role: str,
        side: str | None,
        relay_state: dict[str, Any] | None = None,
        connection_id: str | None = None,
    ) -> None:
        if relay_state is None:
            relay_state = {"series_leave_guard_until": 0.0}
        while True:
            opcode, payload, raw = await P.read_frame(reader)
            self.application.store.touch_connection(
                session, side if role == "player" else None,
                "client", connection_id,
            )
            if (
                opcode == P.LEAVE_GAME
                and role == "player"
                and session.get("continuous_series")
                and time.monotonic()
                < relay_state.get("series_leave_guard_until", 0.0)
            ):
                LOG.info(
                    "ignored stale between-game LEAVE_GAME session=%s side=%s",
                    session["id"],
                    side,
                )
                continue
            if (
                opcode == P.UPDATE_DECK
                and role == "player"
                and side in ("a", "b")
                and self.application.fixed_deck is not None
            ):
                await self.application.record_deck_replacement(
                    session, side, payload
                )
                raw = P.packet(
                    P.UPDATE_DECK, self.application.fixed_deck.payload
                )
            writer.write(raw)
            await writer.drain()

    async def _backend_to_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        session: dict[str, Any],
        role: str,
        side: str | None,
        legacy: bool = False,
        relay_state: dict[str, Any] | None = None,
        connection_id: str | None = None,
    ) -> None:
        if relay_state is None:
            relay_state = {
                "series_leave_guard_until": 0.0,
                "generation": None,
                "game_number": None,
                "game_complete": False,
                "completed_games": [],
            }
        while True:
            opcode, payload, raw = await P.read_frame(reader)
            self.application.store.touch_connection(
                session, side if role == "player" else None,
                "server", connection_id,
            )
            if legacy and opcode == P.GAME_MSG:
                raw = P.packet(opcode, P.legacy_game_message(payload))
            if opcode == P.DUEL_START:
                async with self.application.lock:
                    self.application.store.mark_dueling(session)
                    if (
                        role == "player"
                        and side in ("a", "b")
                        and session.get("continuous_series")
                    ):
                        if (
                            relay_state.get("generation") is None
                            or relay_state.get("game_complete")
                        ):
                            generation, game_number = (
                                self.application.store.bind_relay_start(
                                    session, side
                                )
                            )
                            relay_state["generation"] = generation
                            relay_state["game_number"] = game_number
                            relay_state["game_complete"] = False
            elif opcode == P.GAME_MSG:
                if role == "player":
                    async with self.application.lock:
                        self.application.store.observe_start(session, side, payload)
                result = P.win_from_game_message(payload)
                if result is not None and role == "player":
                    if session.get("continuous_series"):
                        # Each player has an independent backend relay, while
                        # only the first relay wins the durable-result race.
                        # Arm the guard on every relay that observes MSG_WIN so
                        # either client can emit its delayed single-duel leave.
                        relay_state["series_leave_guard_until"] = float("inf")
                    winner, reason = result
                    generation = (
                        relay_state.get("generation")
                        if session.get("continuous_series") else None
                    )
                    accepted = await self.application.finish_result(
                        session, winner, reason, generation
                    )
                    current_result = session.get("result")
                    completed_result = next(
                        (
                            item for item in session.get("results", [])
                            if item.get("generation") == generation
                        ),
                        None,
                    )
                    if (
                        session.get("continuous_series")
                        and (
                            (
                                isinstance(current_result, dict)
                                and current_result.get("generation")
                                == generation
                            )
                            or completed_result is not None
                        )
                    ):
                        completed_game_number = relay_state.get(
                            "game_number"
                        )
                        if (
                            completed_result is not None
                            and type(completed_result.get("game_number"))
                            is int
                        ):
                            completed_game_number = completed_result[
                                "game_number"
                            ]
                        completed = (
                            generation,
                            completed_game_number,
                        )
                        pending = relay_state.setdefault(
                            "completed_games", []
                        )
                        if completed not in pending:
                            pending.append(completed)
                        relay_state["game_complete"] = True
                    elif (
                        session.get("continuous_series")
                        and type(generation) is int
                        and generation < current_generation(session)
                    ):
                        # The other player relay already advanced the series.
                        # Re-arm this relay so its next DUEL_START binds to the
                        # new generation instead of remaining stuck on the
                        # stale game.
                        relay_state["game_complete"] = True
                    if accepted and session.get("continuous_series"):
                        await self.application.publish_result(session)
                    if session.get("continuous_series"):
                        relay_state["series_leave_guard_until"] = (
                            time.monotonic() + SERIES_LEAVE_GUARD_SECONDS
                        )
            elif opcode == P.REPLAY:
                replay_generation = None
                replay_game_number = None
                pending = []
                if session.get("continuous_series") and role == "player":
                    pending = relay_state.setdefault("completed_games", [])
                    if pending:
                        replay_generation, replay_game_number = pending[0]
                async with self.application.lock:
                    archived = self.application.store.record_replay(
                        session,
                        payload,
                        side if role == "player" else None,
                        replay_game_number,
                        replay_generation,
                    )
                    if archived is not None and pending:
                        archived_identity = (
                            archived.get("generation"),
                            archived.get("ordinal"),
                        )
                        pending[:] = [
                            identity for identity in pending
                            if identity != archived_identity
                        ]
                await self.application.publish_result(session)
                await self.application.publish_replay_supplement(session)
            elif opcode == P.EVRP_EXPORT:
                await self.application.record_series_replay_chunk(session, payload)
                continue
            elif opcode == P.DUEL_END:
                await self.application.publish_result(session)
            writer.write(raw)
            await writer.drain()


async def run(args: argparse.Namespace) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    application = PvpApplication(
        Path(args.data_dir),
        args.internal_key,
        args.engine_host,
        args.engine_port,
        args.tournament_url,
        args.tournament_key,
        args.fixed_deck,
        args.room_registry_dir,
    )
    gateway = NativeGateway(application, args.tcp_host, args.tcp_port)
    application.gateway = gateway
    runner = await application.start_http(args.http_host, args.http_port, args.tcp_port)
    await gateway.start()
    LOG.info(
        "ygo-pvp listening http=%s:%s native=%s:%s engine=%s:%s",
        args.http_host, args.http_port, args.tcp_host, args.tcp_port,
        args.engine_host, args.engine_port,
    )
    try:
        await asyncio.Event().wait()
    finally:
        if gateway.server is not None:
            gateway.server.close()
            await gateway.server.wait_closed()
        await runner.cleanup()
        await application.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="YGOPro native PvP gateway")
    parser.add_argument("--data-dir", type=Path, default=Path("data/pvp"))
    parser.add_argument("--internal-key", default=None)
    parser.add_argument("--engine-host", default="127.0.0.1")
    parser.add_argument("--engine-port", type=int, default=7711)
    parser.add_argument(
        "--tournament-url", default=os.environ.get("PVP_TOURNAMENT_URL", "")
    )
    parser.add_argument(
        "--tournament-key", default=os.environ.get("TOURNAMENT_INTERNAL_KEY", "")
    )
    parser.add_argument(
        "--fixed-deck",
        type=Path,
        default=(
            Path(os.environ["PVP_FIXED_DECK"])
            if os.environ.get("PVP_FIXED_DECK") else None
        ),
    )
    parser.add_argument(
        "--room-registry-dir",
        type=Path,
        default=(
            Path(os.environ["PVP_ROOM_REGISTRY_DIR"])
            if os.environ.get("PVP_ROOM_REGISTRY_DIR") else None
        ),
    )
    parser.add_argument("--http-host", default="127.0.0.1")
    parser.add_argument("--http-port", type=int, default=8790)
    parser.add_argument("--tcp-host", default="0.0.0.0")
    parser.add_argument("--tcp-port", type=int, default=6009)
    return parser


def main() -> None:
    asyncio.run(run(build_parser().parse_args()))


if __name__ == "__main__":
    main()
