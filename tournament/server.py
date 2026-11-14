"""Tournament rooms with persistent scoring and read-only live spectators.

This service intentionally sits beside the model duel runtime. It records the
human tournament and exposes a small HTTP/WebSocket surface; it does not alter
the existing YGO engine or its single-duel admission lock.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import hashlib
import hmac
import io
import json
import os
from pathlib import Path
import secrets
import time
from typing import Any
import zipfile

from aiohttp import ClientError, ClientSession, ClientTimeout, WSMsgType, web


BO2_GAMES = 2
REGULAR_BO2 = 10
FINAL_BO2 = 10
REGULAR_GAMES = REGULAR_BO2 * BO2_GAMES
SWAP_AFTER = BO2_GAMES
TIEBREAK_BLOCK_GAMES = 2
MIN_PLAYERS = 3
MAX_PLAYERS = 32
FINAL_TARGET_WINS = 4
FINAL_MAX_GAMES = 7
FINAL_COMPLETION_POLICIES = {"first_to_four", "play_all_seven"}
ENGINES = {"manual-admin", "native-pvp"}
ROOM_TYPES = {"league", "series"}
SEED_MODES = {"mirrored", "independent"}
SERIES_FORMATS = {"bo2", "games"}
MATCH_FORMATS = {"bo2", "games", "first_to_four"}
ADJUDICATION_SCOPES = {"remaining", "all"}
MIN_SERIES_BO2 = 1
MAX_SERIES_BO2 = 100
MAX_SERIES_GAMES = 200
MAX_ADJUDICATION_REASON = 200
SEED_WORDS = 8
MAX_REPLAY_DOWNLOAD_BYTES = 512 * 1024 * 1024


class TournamentError(ValueError):
    """A client-correctable tournament error."""


def _now() -> float:
    return time.time()


def _new_id(prefix: str) -> str:
    return f"{prefix}-{secrets.token_hex(4)}"


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _clean_name(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise TournamentError(f"{label} must be text")
    value = " ".join(value.strip().split())
    if not value or len(value) > 40:
        raise TournamentError(f"{label} must contain 1-40 characters")
    return value


def _requested_player_count(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise TournamentError("player_count must be an integer")
    if isinstance(value, str):
        value = value.strip()
        if not value.isdigit():
            raise TournamentError("player_count must be an integer")
    try:
        count = int(value)
    except (TypeError, ValueError):
        raise TournamentError("player_count must be an integer") from None
    if not MIN_PLAYERS <= count <= MAX_PLAYERS:
        raise TournamentError(
            f"tournament rooms support {MIN_PLAYERS}-{MAX_PLAYERS} players"
        )
    return count


def _players_from_body(
    value: Any,
    player_count: Any = None,
    room_type: str = "league",
) -> list[str]:
    if isinstance(value, str):
        values = value.splitlines()
    elif isinstance(value, list):
        values = value
    else:
        raise TournamentError("players must be a list or one name per line")
    names = [_clean_name(name, "player name") for name in values]
    if room_type == "series":
        if len(names) != 2:
            raise TournamentError("series rooms require exactly 2 players")
        requested_count = None
        if player_count is not None:
            if isinstance(player_count, bool):
                raise TournamentError("player_count must be an integer")
            try:
                requested_count = int(player_count)
            except (TypeError, ValueError):
                raise TournamentError("player_count must be an integer") from None
            if requested_count != 2:
                raise TournamentError("series player_count must be 2")
    else:
        if not MIN_PLAYERS <= len(names) <= MAX_PLAYERS:
            raise TournamentError(
                f"tournament rooms require {MIN_PLAYERS}-{MAX_PLAYERS} players"
            )
        requested_count = _requested_player_count(player_count)
    if requested_count is not None and requested_count != len(names):
        raise TournamentError(
            f"player_count must match the {len(names)} supplied player names"
        )
    if len(set(name.casefold() for name in names)) != len(names):
        raise TournamentError("player names must be unique")
    return names


def _positive_int(
    value: Any,
    label: str,
    minimum: int,
    maximum: int,
    default: int,
) -> int:
    if value is None:
        return default
    if isinstance(value, bool):
        raise TournamentError(f"{label} must be an integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise TournamentError(f"{label} must be an integer") from None
    if not minimum <= parsed <= maximum:
        raise TournamentError(
            f"{label} must be between {minimum} and {maximum}"
        )
    return parsed


def _boolean(value: Any, label: str, default: bool) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise TournamentError(f"{label} must be boolean")
    return value


def _new_seed() -> list[int]:
    return [secrets.randbits(32) for _ in range(SEED_WORDS)]


def _new_native_credentials() -> dict[str, str]:
    while True:
        credentials = {
            role: secrets.token_urlsafe(13)[:18]
            for role in ("a", "b", "spectator")
        }
        if len(set(credentials.values())) == 3:
            return credentials


def _player(room: dict[str, Any], player_id: str) -> dict[str, Any]:
    for player in room["players"]:
        if player["id"] == player_id:
            return player
    raise TournamentError("unknown player")


def _match_players(match: dict[str, Any], side: str) -> str:
    if side == "a":
        return match["player_a"]
    if side == "b":
        return match["player_b"]
    raise TournamentError("winner must be a, b, or draw")


def _pairings(player_ids: list[str]) -> list[tuple[str, str]]:
    return [
        (player_ids[left], player_ids[right])
        for left in range(len(player_ids))
        for right in range(left + 1, len(player_ids))
    ]


def _new_duel_session(
    engine: str = "manual-admin",
    auto_ready: bool = False,
) -> dict[str, Any]:
    return {
        "id": _new_id("s"),
        "status": "ready" if auto_ready else "waiting",
        "engine": engine,
        "player_ready": {"a": auto_ready, "b": auto_ready},
        "opened_at": _now() if auto_ready else None,
    }


def _new_match(index: int, player_a: str, player_b: str,
               engine: str = "manual-admin",
               stage: str = "round_robin",
               bo2_limit: int | None = None,
               match_format: str | None = None,
               target_wins: int | None = None,
               max_games: int | None = None,
               game_limit: int | None = None,
               completion_policy: str | None = None) -> dict[str, Any]:
    if stage not in ("round_robin", "final", "series"):
        raise TournamentError("unsupported match stage")
    if match_format is None:
        match_format = "bo2"
    if match_format not in MATCH_FORMATS:
        raise TournamentError("unsupported match format")
    if stage != "final" and match_format == "first_to_four":
        raise TournamentError("only final matches support first_to_four")
    if stage == "final" and match_format == "games":
        raise TournamentError("final matches do not support fixed game counts")
    if match_format == "first_to_four":
        completion_policy = completion_policy or "first_to_four"
        if completion_policy not in FINAL_COMPLETION_POLICIES:
            raise TournamentError("unsupported final completion policy")
        target_wins = target_wins or FINAL_TARGET_WINS
        max_games = max_games or FINAL_MAX_GAMES
        bo2_limit = 0
        game_limit = max_games
    elif match_format == "games":
        completion_policy = None
        target_wins = None
        max_games = None
        bo2_limit = 0
        if not isinstance(game_limit, int) or game_limit <= 0:
            raise TournamentError("games matches require a positive game limit")
    else:
        completion_policy = None
        target_wins = None
        max_games = None
        game_limit = None
    if bo2_limit is None:
        bo2_limit = FINAL_BO2 if stage == "final" else REGULAR_BO2
    return {
        "id": f"m-{index:02d}",
        "order": index,
        "stage": stage,
        "player_a": player_a,
        "player_b": player_b,
        "status": "pending",
        "phase": "finals" if stage == "final" else "regular",
        "games": [],
        "bo2s": [],
        "regular_wins": {"a": 0, "b": 0},
        "game_wins": {"a": 0, "b": 0},
        "bo2_wins": {"a": 0, "b": 0},
        "bo2_losses": {"a": 0, "b": 0},
        "bo2_draws": 0,
        "points": {"a": 0, "b": 0},
        "playoff_block": 0,
        "playoff_block_wins": {"a": 0, "b": 0},
        "match_result": None,
        "winner": None,
        "loser": None,
        "finished_at": None,
        "bo2_limit": bo2_limit,
        "match_format": match_format,
        "target_wins": target_wins,
        "max_games": max_games,
        "game_limit": game_limit,
        "completion_policy": completion_policy,
        "draw_replays": 0,
        "game_plans": [],
        "pvp_sessions": [],
        "administrative_rulings": [],
        "administrative_score": None,
        "administratively_closed_sessions": [],
        "session": _new_duel_session(engine),
        "native_credentials": (
            _new_native_credentials() if engine == "native-pvp" else None
        ),
    }


def _new_room(name: str, player_names: list[str],
              engine: str = "manual-admin",
              player_count: Any = None,
              room_type: str = "league",
              base_bo2_count: Any = None,
              seed_mode: Any = "mirrored",
              overtime_enabled: Any = True,
              auto_next_game: Any = True,
              series_format: Any = "bo2",
              game_count: Any = None,
              final_opponent_name: Any = None,
              final_completion_policy: Any = "first_to_four",
              ) -> tuple[
                  dict[str, Any],
                  list[dict[str, str]],
                  dict[str, str] | None,
              ]:
    if engine not in ENGINES:
        raise TournamentError("unsupported duel engine")
    if room_type not in ROOM_TYPES:
        raise TournamentError("unsupported room type")
    if room_type == "series":
        player_count = 2
    else:
        player_count = _requested_player_count(player_count) or len(player_names)
    if series_format not in SERIES_FORMATS:
        raise TournamentError("series_format must be bo2 or games")
    if final_completion_policy not in FINAL_COMPLETION_POLICIES:
        raise TournamentError(
            "final_completion_policy must be first_to_four or play_all_seven"
        )
    base_bo2_count = _positive_int(
        base_bo2_count,
        "base_bo2_count",
        MIN_SERIES_BO2,
        MAX_SERIES_BO2,
        REGULAR_BO2,
    )
    if seed_mode not in SEED_MODES:
        raise TournamentError("seed_mode must be mirrored or independent")
    if series_format == "games":
        game_count = _positive_int(
            game_count, "game_count", 1, MAX_SERIES_GAMES, 10
        )
        seed_mode = "independent"
        overtime_enabled = False
    else:
        game_count = base_bo2_count * BO2_GAMES
        overtime_enabled = _boolean(
            overtime_enabled, "overtime_enabled", room_type == "series"
        )
        if room_type == "league":
            overtime_enabled = False
    auto_next_game = _boolean(auto_next_game, "auto_next_game", True)
    if player_count != len(player_names):
        raise TournamentError(
            f"player_count must match the {len(player_names)} supplied player names"
        )
    players = []
    access = []
    for index, player_name in enumerate(player_names):
        player_id = f"p-{index + 1}"
        token = secrets.token_urlsafe(24)
        players.append({
            "id": player_id,
            "name": player_name,
            "join_token_hash": _token_hash(token),
        })
        access.append({
            "player_id": player_id,
            "name": player_name,
            "token": token,
        })
    final_opponent = None
    final_opponent_access = None
    if room_type == "league" and final_opponent_name is not None:
        challenger_name = _clean_name(final_opponent_name, "final opponent name")
        challenger_token = secrets.token_urlsafe(24)
        final_opponent = {
            "id": "p-final",
            "name": challenger_name,
            "join_token_hash": _token_hash(challenger_token),
        }
        final_opponent_access = {
            "player_id": final_opponent["id"],
            "name": final_opponent["name"],
            "token": challenger_token,
        }
    if room_type == "series":
        matches = [
            _new_match(
                1,
                players[0]["id"],
                players[1]["id"],
                engine,
                stage="series",
                bo2_limit=base_bo2_count,
                match_format=series_format,
                game_limit=game_count,
            )
        ]
    else:
        matches = [
            _new_match(
                index,
                player_a,
                player_b,
                engine,
                stage="round_robin",
                bo2_limit=base_bo2_count,
                match_format=series_format,
                game_limit=game_count,
            )
            for index, (player_a, player_b) in enumerate(
                _pairings([player["id"] for player in players]), 1)
        ]
    for match in matches:
        match["session"]["engine"] = engine
    timestamp = _now()
    room = {
        "schema": "ygo-tournament-room/v1",
        "id": _new_id("r"),
        "name": name,
        "room_type": room_type,
        "engine": engine,
        "status": "lobby",
        "stage": "series" if room_type == "series" else "round_robin",
        "final_match_id": None,
            "champion": None,
        "qualifying_champion": None,
        "final_opponent": final_opponent,
        "players": players,
        "rules": {
            "player_count": player_count,
            "bo2_games": BO2_GAMES,
            "regular_bo2": REGULAR_BO2,
            "final_bo2": FINAL_BO2,
            "regular_games": REGULAR_GAMES,
            "swap_after": SWAP_AFTER,
            "tiebreak_block_games": TIEBREAK_BLOCK_GAMES,
            "win_points": 3,
            "draw_points": 1,
            "loss_points": 0,
            "base_bo2_count": base_bo2_count,
            "series_format": series_format,
            "game_count": game_count,
            "seed_mode": seed_mode,
            "overtime_enabled": overtime_enabled,
            "auto_next_game": auto_next_game,
            "parallel_matches": room_type == "league",
            "public_observer_access": engine == "native-pvp",
            "final_format": (
                "first_to_four" if final_opponent is not None else "bo2"
            ),
            "final_target_wins": FINAL_TARGET_WINS,
            "final_max_games": FINAL_MAX_GAMES,
            "final_completion_policy": final_completion_policy,
        },
        "matches": matches,
        "administrative_ruling_log": [],
        "_adjudication_undo_stack": [],
        "active_match_id": None,
        "created_at": timestamp,
        "updated_at": timestamp,
    }
    if engine == "native-pvp" and room_type == "series":
        room["native_credentials"] = copy.deepcopy(
            matches[0]["native_credentials"]
        )
    for entry in access:
        entry["join_url"] = (
            f"/?room={room['id']}&mode=player&player={entry['player_id']}"
            f"&token={entry['token']}"
        )
    if final_opponent_access is not None:
        final_opponent_access["join_url"] = (
            f"/?room={room['id']}&mode=player&player={final_opponent['id']}"
            f"&token={final_opponent_access['token']}"
        )
    return room, access, final_opponent_access


class TournamentStore:
    """Room-level persistence and the tournament state machine."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = asyncio.Lock()
        self.rooms: dict[str, dict[str, Any]] = {}
        self.presence: dict[str, dict[str, float]] = {}
        self._load()

    def _path(self, room_id: str) -> Path:
        return self.root / f"{room_id}.json"

    def _load(self) -> None:
        for path in sorted(self.root.glob("r-*.json")):
            try:
                room = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(room, dict) and isinstance(room.get("id"), str):
                    room.setdefault("room_type", "league")
                    room.setdefault("engine", "manual-admin")
                    room.setdefault("stage", "round_robin")
                    room.setdefault("final_match_id", None)
                    room.setdefault("champion", None)
                    room.setdefault("qualifying_champion", None)
                    room.setdefault("final_opponent", None)
                    room.setdefault("administrative_ruling_log", [])
                    room.setdefault("_adjudication_undo_stack", [])
                    room.setdefault("rules", {})
                    room["rules"].setdefault(
                        "player_count", len(room.get("players", []))
                    )
                    room["rules"].setdefault("bo2_games", BO2_GAMES)
                    room["rules"].setdefault("regular_bo2", REGULAR_BO2)
                    room["rules"].setdefault("final_bo2", FINAL_BO2)
                    room["rules"].setdefault(
                        "base_bo2_count",
                        room["rules"].get("regular_bo2", REGULAR_BO2),
                    )
                    room["rules"].setdefault("seed_mode", "independent")
                    room["rules"].setdefault("series_format", "bo2")
                    room["rules"].setdefault(
                        "game_count", room["rules"]["base_bo2_count"] * BO2_GAMES
                    )
                    room["rules"].setdefault("overtime_enabled", False)
                    room["rules"].setdefault("auto_next_game", False)
                    room["rules"].setdefault("parallel_matches", False)
                    room["rules"].setdefault("public_observer_access", False)
                    room["rules"].setdefault(
                        "final_format",
                        "first_to_four"
                        if room.get("final_opponent") is not None
                        else "bo2",
                    )
                    room["rules"].setdefault(
                        "final_target_wins", FINAL_TARGET_WINS
                    )
                    room["rules"].setdefault(
                        "final_max_games", FINAL_MAX_GAMES
                    )
                    room["rules"].setdefault(
                        "final_completion_policy", "first_to_four"
                    )
                    for player in room.get("players", []):
                        player.setdefault("join_token_hash", "")
                    for match in room.get("matches", []):
                        if (
                            match.get("stage") in ("round_robin", "series")
                            and room["rules"].get("series_format") == "games"
                        ):
                            match["match_format"] = "games"
                            match["bo2_limit"] = 0
                            match.setdefault(
                                "game_limit", room["rules"]["game_count"]
                            )
                        if match.get("stage") == "series":
                            for game in match.get("games", []):
                                if game.get("phase") == "finals":
                                    game["phase"] = "regular"
                            for plan in match.get("game_plans", []):
                                if plan.get("phase") == "finals":
                                    plan["phase"] = "regular"
                                    plan["pair_key"] = str(
                                        plan.get("pair_key", "")
                                    ).replace("finals:", "regular:", 1)
                            for bo2 in match.get("bo2s", []):
                                if bo2.get("phase") == "finals":
                                    bo2["phase"] = "regular"
                        self._migrate_match(match)
                        match.setdefault(
                            "session",
                            _new_duel_session(room.get("engine", "manual-admin")),
                        )
                        match["session"].setdefault(
                            "engine", room.get("engine", "manual-admin")
                        )
                    for match in room.get("matches", []):
                        session = match.get("session") or {}
                        if (
                            room.get("status") == "running"
                            and self._match_is_active(room, match)
                            and self._auto_next(room, match)
                            and not session.get("pvp_session_id")
                        ):
                            session["status"] = "ready"
                            session["player_ready"] = {"a": True, "b": True}
                            session["opened_at"] = (
                                session.get("opened_at") or _now()
                            )
                    self.rooms[room["id"]] = room
            except (OSError, ValueError):
                continue

    def _migrate_match(self, match: dict[str, Any]) -> None:
        """Add BO2 fields while keeping old room files readable."""
        stage = match.setdefault("stage", "round_robin")
        if stage not in ("round_robin", "final", "series"):
            match["stage"] = "round_robin"
            stage = "round_robin"
        match.setdefault("bo2s", [])
        match.setdefault("game_wins", {"a": 0, "b": 0})
        match.setdefault("bo2_wins", {"a": 0, "b": 0})
        if "bo2_losses" not in match:
            match["bo2_losses"] = {
                "a": sum(
                    bo2.get("winner") == "b"
                    for bo2 in match["bo2s"]
                ),
                "b": sum(
                    bo2.get("winner") == "a"
                    for bo2 in match["bo2s"]
                ),
            }
        match.setdefault("bo2_draws", 0)
        match.setdefault("points", {"a": 0, "b": 0})
        match.setdefault("match_result", None)
        match.setdefault("regular_wins", {"a": 0, "b": 0})
        match.setdefault("playoff_block", 0)
        match.setdefault("playoff_block_wins", {"a": 0, "b": 0})
        match.setdefault("winner", None)
        match.setdefault("loser", None)
        match.setdefault("finished_at", None)
        match.setdefault(
            "bo2_limit",
            FINAL_BO2 if stage == "final" else REGULAR_BO2,
        )
        match.setdefault(
            "match_format",
            "first_to_four"
            if stage == "final"
            and match.get("bo2_limit") == 0
            else "bo2",
        )
        match.setdefault(
            "target_wins",
            FINAL_TARGET_WINS if match["match_format"] == "first_to_four" else None,
        )
        match.setdefault(
            "max_games",
            FINAL_MAX_GAMES if match["match_format"] == "first_to_four" else None,
        )
        match.setdefault(
            "game_limit",
            (
                match.get("max_games")
                if match["match_format"] == "first_to_four"
                else None
            ),
        )
        match.setdefault(
            "completion_policy",
            "first_to_four"
            if match["match_format"] == "first_to_four"
            else None,
        )
        if (
            match["match_format"] == "first_to_four"
            and match["completion_policy"] not in FINAL_COMPLETION_POLICIES
        ):
            match["completion_policy"] = "first_to_four"
        match.setdefault("native_credentials", None)
        match.setdefault("draw_replays", 0)
        match.setdefault("game_plans", [])
        match.setdefault("administrative_rulings", [])
        match.setdefault("administrative_score", None)
        match.setdefault("administratively_closed_sessions", [])
        session = match.get("session") or {}
        pvp_sessions = []
        seen_pvp_sessions = set()
        for item in match.get("pvp_sessions", []):
            if not isinstance(item, dict):
                continue
            session_id = item.get("id")
            start_game = item.get("start_game")
            if (
                isinstance(session_id, str)
                and session_id
                and type(start_game) is int
                and start_game > 0
                and session_id not in seen_pvp_sessions
            ):
                pvp_sessions.append({
                    "id": session_id,
                    "start_game": start_game,
                })
                seen_pvp_sessions.add(session_id)
        for game in match.get("games", []):
            session_id = game.get("session_id")
            if (
                isinstance(session_id, str)
                and session_id
                and session_id not in seen_pvp_sessions
            ):
                pvp_sessions.append({
                    "id": session_id,
                    "start_game": int(game.get("number") or 1),
                })
                seen_pvp_sessions.add(session_id)
        current_session_id = session.get("pvp_session_id")
        if (
            isinstance(current_session_id, str)
            and current_session_id
            and current_session_id not in seen_pvp_sessions
        ):
            current_start_game = (
                1
                if session.get("continuous_series")
                and session.get("status") == "finished"
                else len(match.get("games", [])) + 1
            )
            pvp_sessions.append({
                "id": current_session_id,
                "start_game": current_start_game,
            })
        match["pvp_sessions"] = pvp_sessions
        # Older rooms already stored twenty game rows. Rebuild their BO2
        # summaries in pairs so the new standings code can read them.
        if not match["bo2s"] and match.get("match_format") == "bo2":
            match["bo2_wins"] = {"a": 0, "b": 0}
            match["bo2_losses"] = {"a": 0, "b": 0}
            match["bo2_draws"] = 0
            match["points"] = {"a": 0, "b": 0}
            phase = self._bo2_phase(match)
            rows = [game for game in match.get("games", [])
                    if game.get("phase") == phase]
            for offset in range(0, len(rows) - 1, BO2_GAMES):
                self._append_bo2_summary(match, rows[offset:offset + BO2_GAMES])
        if match.get("status") == "closed":
            match["phase"] = "closed"
        elif stage in ("round_robin", "series"):
            if match.get("phase") not in ("tiebreak", "finished"):
                match["phase"] = "finished" if match.get("status") == "finished" else "regular"
        elif match.get("phase") not in ("tiebreak", "finished"):
            match["phase"] = "finals"

    def _save(self, room: dict[str, Any]) -> None:
        room["updated_at"] = _now()
        target = self._path(room["id"])
        temporary = target.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(room, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(target)

    def _room(self, room_id: str) -> dict[str, Any]:
        room = self.rooms.get(room_id)
        if room is None:
            raise web.HTTPNotFound(text="room not found")
        return room

    def _find_match(self, room: dict[str, Any], match_id: str) -> dict[str, Any]:
        for match in room["matches"]:
            if match["id"] == match_id:
                return match
        raise web.HTTPNotFound(text="match not found")

    def _phase_games(self, match: dict[str, Any], phase: str) -> list[dict[str, Any]]:
        return [game for game in match.get("games", [])
                if game.get("phase") == phase]

    def _bo2_phase(self, match: dict[str, Any]) -> str:
        return (
            "finals"
            if match.get("stage", "round_robin") == "final"
            else "regular"
        )

    def _bo2_limit(self, match: dict[str, Any]) -> int:
        if match.get("match_format") == "games":
            return 0
        stored = match.get("bo2_limit")
        if isinstance(stored, int) and stored > 0:
            return stored
        if match.get("match_format") == "first_to_four":
            return 0
        return (
            FINAL_BO2
            if match.get("stage", "round_robin") == "final"
            else REGULAR_BO2
        )

    def _first_to_four(self, match: dict[str, Any]) -> bool:
        return (
            match.get("stage") == "final"
            and match.get("match_format") == "first_to_four"
        )

    def _single_games(self, room: dict[str, Any], match: dict[str, Any]) -> bool:
        return (
            match.get("stage") in ("round_robin", "series")
            and (
                match.get("match_format") == "games"
                or room.get("rules", {}).get("series_format", "bo2") == "games"
            )
        )

    def _parallel_league(self, room: dict[str, Any]) -> bool:
        return (
            room.get("room_type") == "league"
            and bool(room.get("rules", {}).get("parallel_matches", False))
        )

    def _active_matches(self, room: dict[str, Any]) -> list[dict[str, Any]]:
        return [
            match for match in room.get("matches", [])
            if match.get("status") in ("running", "tiebreak")
        ]

    def _match_is_active(
        self,
        room: dict[str, Any],
        match: dict[str, Any],
    ) -> bool:
        if match.get("status") not in ("running", "tiebreak"):
            return False
        if (
            self._parallel_league(room)
            and room.get("stage") == "round_robin"
            and match.get("stage") == "round_robin"
        ):
            return True
        return room.get("active_match_id") == match.get("id")

    def _native_credentials(
        self,
        room: dict[str, Any],
        match: dict[str, Any],
    ) -> dict[str, str]:
        credentials = match.get("native_credentials")
        if isinstance(credentials, dict) and all(
            isinstance(credentials.get(role), str)
            and credentials.get(role)
            for role in ("a", "b", "spectator")
        ):
            return credentials
        legacy = room.get("native_credentials")
        if (
            room.get("room_type") == "series"
            and isinstance(legacy, dict)
            and all(
                isinstance(legacy.get(role), str) and legacy.get(role)
                for role in ("a", "b", "spectator")
            )
        ):
            credentials = copy.deepcopy(legacy)
        else:
            credentials = _new_native_credentials()
        match["native_credentials"] = credentials
        return credentials

    def _bo2_result(self, games: list[dict[str, Any]]) -> str:
        wins = {"a": 0, "b": 0}
        for game in games:
            if game.get("winner") in wins:
                wins[game["winner"]] += 1
        if wins["a"] > wins["b"]:
            return "a"
        if wins["b"] > wins["a"]:
            return "b"
        return "draw"

    def _append_bo2_summary(self, match: dict[str, Any],
                            games: list[dict[str, Any]]) -> dict[str, Any] | None:
        if len(games) != BO2_GAMES:
            return None
        result = self._bo2_result(games)
        points = {"a": 0, "b": 0}
        if result == "a":
            points["a"] = 3
        elif result == "b":
            points["b"] = 3
        else:
            points = {"a": 1, "b": 1}
        summary = {
            "number": len(match["bo2s"]) + 1,
            "game_numbers": [game["number"] for game in games],
            "phase": games[0].get("phase"),
            "winner": result,
            "game_wins": {
                "a": sum(game.get("winner") == "a" for game in games),
                "b": sum(game.get("winner") == "b" for game in games),
            },
            "points": points,
            "recorded_at": games[-1].get("recorded_at", _now()),
        }
        match["bo2s"].append(summary)
        if result in ("a", "b"):
            match["bo2_wins"][result] += 1
            other = "b" if result == "a" else "a"
            match.setdefault("bo2_losses", {"a": 0, "b": 0})[other] += 1
        else:
            match["bo2_draws"] += 1
        match["points"]["a"] += points["a"]
        match["points"]["b"] += points["b"]
        return summary

    def _auto_next(
        self,
        room: dict[str, Any],
        match: dict[str, Any] | None = None,
    ) -> bool:
        return (
            room.get("engine") == "native-pvp"
            and (
                (
                    match is not None
                    and match.get("stage") in (
                        "round_robin",
                        "series",
                        "final",
                    )
                    and bool(room.get("rules", {}).get("auto_next_game", True))
                    and (
                        room.get("room_type") == "series"
                        or self._parallel_league(room)
                    )
                )
                or self._first_to_four(match or {})
            )
        )

    def _reset_duel_session(
        self,
        room: dict[str, Any],
        match: dict[str, Any],
    ) -> None:
        engine = match.get("session", {}).get("engine", "manual-admin")
        match["session"] = _new_duel_session(
            engine, self._auto_next(room, match)
        )

    def _ensure_game_plan(
        self,
        room: dict[str, Any],
        match: dict[str, Any],
    ) -> dict[str, Any]:
        game_number = len(match.get("games", [])) + 1
        for plan in match.setdefault("game_plans", []):
            if plan.get("number") == game_number:
                return plan

        phase = match.get("phase", "regular")
        seat_map = self._seat_plan(match)
        if phase in ("regular", "finals"):
            phase_count = len(self._phase_games(match, phase))
            if self._single_games(room, match):
                pair_number = None
                pair_key = f"{phase}:game:{phase_count + 1}"
                bo2_game = None
            else:
                pair_number = phase_count // BO2_GAMES + 1
                pair_key = f"{phase}:{pair_number}"
                bo2_game = phase_count % BO2_GAMES + 1
            block = 0
        else:
            block = match.get("playoff_block") or 1
            pair_key = f"tiebreak:{block}"
            bo2_game = self._tiebreak_game_count(match, block) + 1
            pair_number = len(match.get("bo2s", [])) + block

        seed_mode = room.get("rules", {}).get("seed_mode", "independent")
        if self._single_games(room, match) or self._first_to_four(match):
            seed_mode = "independent"
        seed = None
        if seed_mode == "mirrored":
            previous = next(
                (
                    candidate
                    for candidate in match["game_plans"]
                    if candidate.get("pair_key") == pair_key
                ),
                None,
            )
            if previous is not None:
                seed = copy.deepcopy(previous.get("seed"))
        if seed is None:
            seed = _new_seed()

        first_side = next(
            side for side, seat in seat_map.items() if seat == 0
        )
        plan = {
            "number": game_number,
            "phase": phase,
            "block": block,
            "bo2": pair_number,
            "bo2_game": bo2_game,
            "pair_key": pair_key,
            "seed_mode": seed_mode,
            "seed": seed,
            "seat_map": seat_map,
            "required_first_player": first_side,
            "created_at": _now(),
        }
        match["game_plans"].append(plan)
        return plan

    def _activate_next(self, room: dict[str, Any]) -> None:
        if (
            self._parallel_league(room)
            and room.get("stage", "round_robin") == "round_robin"
        ):
            for match in room["matches"]:
                if (
                    match.get("stage") == "round_robin"
                    and match.get("status") == "pending"
                ):
                    match["status"] = "running"
                    self._reset_duel_session(room, match)
                    if room.get("engine") == "native-pvp":
                        self._native_credentials(room, match)
            running = [
                match for match in room["matches"]
                if (
                    match.get("stage") == "round_robin"
                    and match.get("status") in ("running", "tiebreak")
                )
            ]
            if running:
                room["active_match_id"] = running[0]["id"]
                return
        pending = next((match for match in room["matches"]
                        if match["status"] == "pending"), None)
        if pending is None:
            if room.get("room_type") == "series":
                room["active_match_id"] = None
                room["status"] = "finished"
                room["stage"] = "finished"
                only_match = room["matches"][0] if room.get("matches") else None
                room["champion"] = (
                    only_match.get("winner") if only_match else None
                )
                return
            if room.get("stage", "round_robin") == "round_robin":
                regular = [
                    match for match in room["matches"]
                    if match.get("stage", "round_robin") == "round_robin"
                ]
                if not regular or not all(
                    match["status"] == "finished" for match in regular
                ):
                    room["active_match_id"] = None
                    return
                ranked = self._standings(room)
                if len(ranked) < 2:
                    raise TournamentError("at least two qualified players are required")
                room["qualifying_champion"] = {
                    "player_id": ranked[0]["player_id"],
                    "name": ranked[0]["name"],
                    "rank": ranked[0]["rank"],
                    "match_points": ranked[0]["match_points"],
                }
                final_opponent = room.get("final_opponent")
                if final_opponent is not None:
                    final = _new_match(
                        len(room["matches"]) + 1,
                        ranked[0]["player_id"],
                        final_opponent["id"],
                        room.get("engine", "manual-admin"),
                        stage="final",
                        match_format="first_to_four",
                        target_wins=room["rules"].get(
                            "final_target_wins", FINAL_TARGET_WINS
                        ),
                        max_games=room["rules"].get(
                            "final_max_games", FINAL_MAX_GAMES
                        ),
                        completion_policy=room["rules"].get(
                            "final_completion_policy", "first_to_four"
                        ),
                    )
                else:
                    # Keep the original model-vs-model final for legacy rooms.
                    final = _new_match(
                        len(room["matches"]) + 1,
                        ranked[0]["player_id"],
                        ranked[1]["player_id"],
                        room.get("engine", "manual-admin"),
                        stage="final",
                    )
                final["id"] = "m-final"
                room["matches"].append(final)
                room["final_match_id"] = final["id"]
                room["stage"] = "final"
                pending = final
            else:
                room["active_match_id"] = None
                room["status"] = "finished"
                room["stage"] = "finished"
                final = next(
                    (match for match in room["matches"]
                     if match.get("stage") == "final"),
                    None,
                )
                room["champion"] = final.get("winner") if final else None
                return
        pending["status"] = "running"
        room["active_match_id"] = pending["id"]
        self._reset_duel_session(room, pending)
        if room.get("engine") == "native-pvp":
            self._native_credentials(room, pending)

    def _finish_match(self, room: dict[str, Any],
                      match: dict[str, Any], winner_side: str) -> None:
        match["winner"] = _match_players(match, winner_side)
        match["loser"] = _match_players(match, "b" if winner_side == "a" else "a")
        match["status"] = "finished"
        match["phase"] = "finished"
        match["match_result"] = winner_side
        match["finished_at"] = _now()
        room["active_match_id"] = None
        self._activate_next(room)

    def _finish_draw(self, room: dict[str, Any],
                     match: dict[str, Any]) -> None:
        match["winner"] = None
        match["loser"] = None
        match["status"] = "finished"
        match["phase"] = "finished"
        match["match_result"] = "draw"
        match["finished_at"] = _now()
        room["active_match_id"] = None
        self._activate_next(room)

    def _finish_final_from_points(self, match: dict[str, Any]) -> None:
        winner_side = (
            "a" if match["points"]["a"] > match["points"]["b"] else "b"
        )
        match["winner"] = _match_players(match, winner_side)
        match["loser"] = _match_players(
            match, "b" if winner_side == "a" else "a"
        )
        match["match_result"] = winner_side
        match["status"] = "finished"
        match["phase"] = "finished"
        match["finished_at"] = _now()

    def _seat_plan(self, match: dict[str, Any]) -> dict[str, int]:
        if match["phase"] in ("regular", "finals"):
            phase_games = self._phase_games(match, match["phase"])
            bo2_game = len(phase_games) % BO2_GAMES
            return {"a": 0, "b": 1} if bo2_game == 0 else {"a": 1, "b": 0}
        block = match["playoff_block"] or 1
        block_game = self._tiebreak_game_count(match, block) + 1
        if match.get("stage") == "series":
            return {"a": 0, "b": 1} if block_game == 1 else {"a": 1, "b": 0}
        return {"a": 1, "b": 0} if block_game == 1 else {"a": 0, "b": 1}

    def _recompute_match(
        self,
        room: dict[str, Any],
        match: dict[str, Any],
    ) -> None:
        stage = match.get("stage", "round_robin")
        regular_wins = {"a": 0, "b": 0}
        for game in self._phase_games(match, "regular"):
            if game.get("winner") in regular_wins:
                regular_wins[game["winner"]] += 1
        match["regular_wins"] = regular_wins
        match["game_wins"] = {
            "a": sum(game.get("winner") == "a" for game in self._phase_games(match, "finals"))
            if self._first_to_four(match)
            else regular_wins["a"],
            "b": sum(game.get("winner") == "b" for game in self._phase_games(match, "finals"))
            if self._first_to_four(match)
            else regular_wins["b"],
        }
        match["bo2s"] = []
        match["bo2_wins"] = {"a": 0, "b": 0}
        match["bo2_losses"] = {"a": 0, "b": 0}
        match["bo2_draws"] = 0
        match["points"] = {"a": 0, "b": 0}
        phase = "finals" if stage == "final" else "regular"
        phase_games = self._phase_games(match, phase)
        if self._first_to_four(match):
            match["points"] = copy.deepcopy(match["game_wins"])
            target = int(match.get("target_wins") or FINAL_TARGET_WINS)
            max_games = int(match.get("max_games") or FINAL_MAX_GAMES)
            completion_policy = match.get(
                "completion_policy", "first_to_four"
            )
            match["phase"] = "finals"
            match["status"] = "running"
            match["match_result"] = None
            match["winner"] = None
            match["loser"] = None
            match["finished_at"] = None
            match["draw_replays"] = max(
                0, len(phase_games) - max_games
            )
            if completion_policy == "first_to_four":
                for side in ("a", "b"):
                    if match["game_wins"][side] >= target:
                        match["winner"] = match[f"player_{side}"]
                        match["loser"] = match[
                            "player_b" if side == "a" else "player_a"
                        ]
                        match["match_result"] = side
                        match["phase"] = "finished"
                        match["status"] = "finished"
                        return
            if len(phase_games) < max_games:
                return
            if (
                completion_policy == "play_all_seven"
                and match["game_wins"]["a"] != match["game_wins"]["b"]
            ):
                self._finish_final_from_points(match)
                return
            # A tied fixed-seven final, or a first-to-four final without four
            # decisive wins, keeps extending one game at a time.
            match["draw_replays"] = max(
                0, len(phase_games) - max_games + 1
            )
            return
        if self._single_games(room, match):
            for game in phase_games:
                winner = game["winner"]
                for side in ("a", "b"):
                    match["points"][side] += (
                        3 if winner == side else 1 if winner == "draw" else 0
                    )
        else:
            for offset in range(0, len(phase_games) - 1, BO2_GAMES):
                self._append_bo2_summary(match, phase_games[offset:offset + BO2_GAMES])
        match["playoff_block"] = 0
        match["playoff_block_wins"] = {"a": 0, "b": 0}
        match["winner"] = None
        match["loser"] = None
        match["match_result"] = None
        match["finished_at"] = None
        tiebreak_blocks: dict[int, list[dict[str, Any]]] = {}
        for game in match["games"]:
            if game.get("phase") != "tiebreak":
                continue
            block = int(game.get("block", 1))
            tiebreak_blocks.setdefault(block, []).append(game)
        if self._single_games(room, match):
            game_limit = int(
                match.get("game_limit") or room["rules"]["game_count"]
            )
            if len(phase_games) < game_limit:
                match["phase"] = "regular"
                match["status"] = "running"
            else:
                match["phase"] = "finished"
                match["status"] = "finished"
                match["match_result"] = (
                    "a" if match["points"]["a"] > match["points"]["b"] else
                    "b" if match["points"]["b"] > match["points"]["a"] else "draw"
                )
                if match["match_result"] != "draw":
                    side = match["match_result"]
                    match["winner"] = match[f"player_{side}"]
                    match["loser"] = match["player_b" if side == "a" else "player_a"]
            return
        if stage in ("round_robin", "series") and (
            len(match["bo2s"]) < self._bo2_limit(match)
        ):
            match["phase"] = "regular"
            match["status"] = "running"
            return
        if stage == "round_robin":
            result = "a" if match["points"]["a"] > match["points"]["b"] else (
                "b" if match["points"]["b"] > match["points"]["a"] else "draw"
            )
            match["phase"] = "finished"
            match["status"] = "finished"
            match["match_result"] = result
            if result in ("a", "b"):
                match["winner"] = match[f"player_{result}"]
                match["loser"] = match["player_b" if result == "a" else "player_a"]
            return
        if stage == "series":
            if match["points"]["a"] != match["points"]["b"]:
                winner_side = (
                    "a"
                    if match["points"]["a"] > match["points"]["b"]
                    else "b"
                )
                match["winner"] = match[f"player_{winner_side}"]
                match["loser"] = match[
                    "player_b" if winner_side == "a" else "player_a"
                ]
                match["match_result"] = winner_side
                match["phase"] = "finished"
                match["status"] = "finished"
                return
            if not room.get("rules", {}).get("overtime_enabled", True):
                match["phase"] = "finished"
                match["status"] = "finished"
                match["match_result"] = "draw"
                return
        if stage == "final" and len(match["bo2s"]) < self._bo2_limit(match):
            match["phase"] = "finals"
            match["status"] = "running"
            return
        if match["points"]["a"] != match["points"]["b"]:
            self._finish_final_from_points(match)
            return
        match["phase"] = "tiebreak"
        match["status"] = "tiebreak"
        match["playoff_block"] = 1
        match["playoff_block_wins"] = {"a": 0, "b": 0}
        for block in sorted(tiebreak_blocks):
            games = tiebreak_blocks[block]
            wins = {
                "a": sum(game.get("winner") == "a" for game in games),
                "b": sum(game.get("winner") == "b" for game in games),
            }
            if len(games) < TIEBREAK_BLOCK_GAMES:
                match["playoff_block"] = block
                match["playoff_block_wins"] = wins
                break
            if wins["a"] == TIEBREAK_BLOCK_GAMES:
                match["winner"] = match["player_a"]
                match["loser"] = match["player_b"]
                match["match_result"] = "a"
                match["status"] = "finished"
                match["phase"] = "finished"
                break
            if wins["b"] == TIEBREAK_BLOCK_GAMES:
                match["winner"] = match["player_b"]
                match["loser"] = match["player_a"]
                match["match_result"] = "b"
                match["status"] = "finished"
                match["phase"] = "finished"
                break
            match["playoff_block"] = block + 1
            match["playoff_block_wins"] = {"a": 0, "b": 0}
        if (
            match["status"] in ("running", "tiebreak")
            and not self._auto_next(room, match)
        ):
            self._reset_duel_session(room, match)

    def _regular_game_count(self, match: dict[str, Any]) -> int:
        return len(self._phase_games(match, "regular"))

    def _tiebreak_game_count(self, match: dict[str, Any], block: int) -> int:
        return sum(game.get("phase") == "tiebreak" and game.get("block") == block
                   for game in match["games"])

    def _public_player(self, room_id: str, player: dict[str, Any]) -> dict[str, Any]:
        last_seen = self.presence.get(room_id, {}).get(player["id"], 0)
        return {
            "id": player["id"],
            "name": player["name"],
            "online": last_seen > _now() - 45,
        }

    def _participant_name(self, room: dict[str, Any], player_id: str) -> str:
        for player in room.get("players", []):
            if player.get("id") == player_id:
                return player["name"]
        final_opponent = room.get("final_opponent") or {}
        if final_opponent.get("id") == player_id:
            return final_opponent["name"]
        raise TournamentError("unknown participant")

    def _public_session(self, session: dict[str, Any],
                        side: str | None = None) -> dict[str, Any]:
        result = copy.deepcopy(session)
        result.pop("room_password", None)
        result.pop("callback", None)
        player_codes = result.pop("native_player_codes", {})
        result.pop("spectator_code", None)
        result.pop("seed", None)
        if isinstance(result.get("native"), dict):
            result["native"].pop("player_codes", None)
            result["native"].pop("spectator_code", None)
        if side in ("a", "b") and result.get("engine") == "native-pvp":
            code = player_codes.get(side)
            native = result.get("native") or {}
            if code:
                result["connection"] = {
                    "host": native.get("host"),
                    "port": native.get("port"),
                    "code": code,
                    "seat": native.get("seat_plan", {}).get(side),
                    "status": result.get("status"),
                }
        return result

    def _native_replay_status(
        self,
        match: dict[str, Any],
    ) -> dict[str, Any]:
        session = match.get("session") or {}
        replay_games = [
            game for game in match.get("games", [])
            if not (
                isinstance(game.get("administrative_forfeit"), dict)
                and game["administrative_forfeit"].get("synthetic") is True
            )
        ]
        expected = len(replay_games)
        recorded = sorted({
            game.get("number")
            for game in replay_games
            if (
                type(game.get("number")) is int
                and isinstance(game.get("replay"), dict)
            )
        })
        fallback = {
            "state": (
                "incomplete"
                if match.get("status") == "finished"
                else "recording"
            ),
            "available": False,
            "complete": False,
            "partial": False,
            "replay_count": len(recorded),
            "expected_replay_count": expected,
            "missing_game_numbers": sorted(
                set(range(1, expected + 1)) - set(recorded)
            ),
        }
        status = session.get("replay_status")
        known_sessions = {
            entry.get("id")
            for entry in match.get("pvp_sessions", [])
            if isinstance(entry, dict)
        }
        known_sessions.add(session.get("pvp_session_id"))
        if (
            not isinstance(status, dict)
            or status.get("session_id") not in known_sessions
        ):
            return fallback
        replay_count = status.get("replay_count")
        expected_count = status.get("expected_replay_count")
        missing = status.get("missing_game_numbers")
        if (
            type(replay_count) is not int
            or replay_count < 0
            or type(expected_count) is not int
            or expected_count < replay_count
            or not isinstance(missing, list)
            or any(
                type(number) is not int or number <= 0
                for number in missing
            )
        ):
            return fallback
        return {
            "state": status.get("state", fallback["state"]),
            "available": status.get("available") is True,
            "complete": status.get("complete") is True,
            "partial": status.get("partial") is True,
            "replay_count": replay_count,
            "expected_replay_count": expected_count,
            "missing_game_numbers": sorted(set(missing)),
        }

    def _public_match(self, room: dict[str, Any],
                      match: dict[str, Any], side: str | None = None) -> dict[str, Any]:
        result = copy.deepcopy(match)
        result.pop("game_plans", None)
        result.pop("native_credentials", None)
        result.pop("pvp_sessions", None)
        result.pop("administratively_closed_sessions", None)
        result["session"] = self._public_session(match["session"], side)
        result["duel_room_id"] = f"{room['id']}:{match['id']}"
        result["player_a_name"] = self._participant_name(
            room, match["player_a"]
        )
        result["player_b_name"] = self._participant_name(
            room, match["player_b"]
        )
        result["regular_game_count"] = self._regular_game_count(match)
        result["total_game_count"] = len(match["games"])
        result["bo2_count"] = len(match.get("bo2s", []))
        result["bo2_limit"] = self._bo2_limit(match)
        result["forfeit_game_count"] = sum(
            isinstance(game.get("administrative_forfeit"), dict)
            for game in match.get("games", [])
        )
        result["can_undo_adjudication"] = self._can_undo_adjudication(
            room, match
        )
        if self._single_games(room, match):
            result["game_limit"] = int(
                match.get("game_limit")
                or room["rules"]["game_count"]
            )
        elif self._first_to_four(match):
            result["game_limit"] = (
                int(match.get("max_games") or FINAL_MAX_GAMES)
                + int(match.get("draw_replays") or 0)
            )
        else:
            result["game_limit"] = self._bo2_limit(match) * BO2_GAMES
        phase_games = self._phase_games(match, match["phase"])
        result["bo2_game"] = (
            len(phase_games) % BO2_GAMES + 1
            if match["phase"] in ("regular", "finals")
            and not self._single_games(room, match)
            and not self._first_to_four(match) else None
        )
        if match["phase"] in ("regular", "finals"):
            if self._first_to_four(match):
                result["next_seat_plan"] = (
                    "a-first" if len(phase_games) % 2 == 0 else "b-first"
                )
                result["score"] = copy.deepcopy(
                    match.get("game_wins", {"a": 0, "b": 0})
                )
                result["target_wins"] = match.get(
                    "target_wins", FINAL_TARGET_WINS
                )
                result["max_games"] = match.get(
                    "max_games", FINAL_MAX_GAMES
                )
            else:
                result["next_seat_plan"] = (
                    "a-first" if len(phase_games) % BO2_GAMES == 0 else "b-first"
                )
            result["next_game"] = len(match["games"]) + 1
            result["next_bo2"] = (
                None if self._single_games(room, match)
                or self._first_to_four(match)
                else len(match.get("bo2s", [])) + 1
            )
            if not self._first_to_four(match):
                result["score"] = copy.deepcopy(
                    match.get("points", {"a": 0, "b": 0})
                )
        elif match["phase"] == "tiebreak":
            block = match["playoff_block"] or 1
            result["next_seat_plan"] = "swap-block"
            result["next_game"] = len(match["games"]) + 1
            result["playoff_block_progress"] = (
                f"block {block}: "
                f"{self._tiebreak_game_count(match, block)} / {TIEBREAK_BLOCK_GAMES} games"
            )
        else:
            result["next_game"] = None
            result["next_seat_plan"] = None
        replay_status = self._native_replay_status(match)
        if (match.get("session") or {}).get("engine") == "native-pvp":
            result["replay_status"] = replay_status
        if (
            room.get("status") != "closed"
            and room.get("rules", {}).get("public_observer_access", False)
        ):
            session = match.get("session") or {}
            native = session.get("native") or {}
            code = session.get("spectator_code")
            if (
                session.get("engine") == "native-pvp"
                and session.get("pvp_session_id")
                and isinstance(code, str)
                and code
                and (
                    match.get("status") != "finished"
                    or replay_status["available"]
                )
            ):
                result["observer_connection"] = {
                    "host": native.get("host"),
                    "port": native.get("port"),
                    "code": code,
                    "status": (
                        "finished"
                        if match.get("status") == "finished"
                        else session.get("status")
                    ),
                }
        return result

    def _standings(self, room: dict[str, Any]) -> list[dict[str, Any]]:
        rows = {
            player["id"]: {
                "player_id": player["id"],
                "name": player["name"],
                "match_points": 0,
                "bo2_wins": 0,
                "bo2_losses": 0,
                "bo2_draws": 0,
                "match_wins": 0,
                "match_losses": 0,
                "matches_played": 0,
                "game_wins": 0,
                "game_losses": 0,
                "game_draws": 0,
                "game_diff": 0,
            }
            for player in room["players"]
        }
        for match in room["matches"]:
            if match.get("stage") == "series":
                a, b = rows[match["player_a"]], rows[match["player_b"]]
                for game in self._phase_games(match, "regular"):
                    if game["winner"] == "a":
                        a["game_wins"] += 1
                        b["game_losses"] += 1
                    elif game["winner"] == "b":
                        b["game_wins"] += 1
                        a["game_losses"] += 1
                    elif game["winner"] == "draw":
                        a["game_draws"] += 1
                        b["game_draws"] += 1
                for side, row in (("a", a), ("b", b)):
                    row["match_points"] = match["points"][side]
                    row["bo2_wins"] = match["bo2_wins"][side]
                    row["bo2_losses"] = match["bo2_losses"][side]
                    row["bo2_draws"] = match["bo2_draws"]
                    row["matches_played"] = int(match["status"] == "finished")
                continue
            if match.get("stage", "round_robin") != "round_robin":
                continue
            a, b = rows[match["player_a"]], rows[match["player_b"]]
            for game in match["games"]:
                if game.get("phase") != "regular":
                    continue
                if game.get("winner") == "a":
                    a["game_wins"] += 1
                    b["game_losses"] += 1
                elif game.get("winner") == "b":
                    b["game_wins"] += 1
                    a["game_losses"] += 1
                elif game.get("winner") == "draw":
                    a["game_draws"] += 1
                    b["game_draws"] += 1
            if self._single_games(room, match):
                a["match_points"] += match["points"]["a"]
                b["match_points"] += match["points"]["b"]
                if match["status"] == "finished":
                    a["matches_played"] += 1
                    b["matches_played"] += 1
                continue
            for bo2 in match.get("bo2s", []):
                winner = bo2.get("winner")
                if winner == "a":
                    a["bo2_wins"] += 1
                    b["bo2_losses"] += 1
                elif winner == "b":
                    b["bo2_wins"] += 1
                    a["bo2_losses"] += 1
                else:
                    a["bo2_draws"] += 1
                    b["bo2_draws"] += 1
            a["match_points"] += match["points"]["a"]
            b["match_points"] += match["points"]["b"]
            if match["status"] == "finished":
                a["matches_played"] += 1
                b["matches_played"] += 1
        for row in rows.values():
            row["match_wins"] = row["bo2_wins"]
            row["match_losses"] = row["bo2_losses"]
            row["game_diff"] = row["game_wins"] - row["game_losses"]
        values = sorted(
            rows.values(),
            key=lambda row: (
                -row["match_points"],
                -row["bo2_wins"],
                -row["game_diff"],
                -row["game_wins"],
                row["name"].casefold(),
            ),
        )
        for index, row in enumerate(values, 1):
            row["rank"] = index
        return values

    def public(self, room_id: str, player_id: str | None = None) -> dict[str, Any]:
        room = self._room(room_id)
        public_matches = []
        for match in room["matches"]:
            side = None
            if player_id:
                if match["player_a"] == player_id:
                    side = "a"
                elif match["player_b"] == player_id:
                    side = "b"
            public_matches.append(self._public_match(room, match, side))
        public_room = {
            "id": room["id"],
            "name": room["name"],
            "room_type": room.get("room_type", "league"),
            "engine": room.get("engine", "manual-admin"),
            "status": room["status"],
            "stage": room.get("stage", "round_robin"),
            "final_match_id": room.get("final_match_id"),
            "champion": room.get("champion"),
            "qualifying_champion": copy.deepcopy(
                room.get("qualifying_champion")
            ),
            "final_opponent": (
                {
                    "id": room["final_opponent"]["id"],
                    "name": room["final_opponent"]["name"],
                }
                if isinstance(room.get("final_opponent"), dict)
                else None
            ),
            "player_count": room.get("rules", {}).get(
                "player_count", len(room["players"])
            ),
            "players": [
                self._public_player(room["id"], player)
                for player in room["players"]
            ],
            "rules": copy.deepcopy(room["rules"]),
            "active_match_id": room["active_match_id"],
            "created_at": room["created_at"],
            "updated_at": room["updated_at"],
            "administrative_ruling_log": copy.deepcopy(
                room.get("administrative_ruling_log", [])
            ),
            "standings": self._standings(room),
            "matches": public_matches,
        }
        active_matches = [
            match for match in public_matches
            if match.get("status") in ("running", "tiebreak")
        ]
        public_room["active_matches"] = active_matches
        active = next(
            (match for match in public_room["matches"]
             if match["id"] == room["active_match_id"]),
            None,
        )
        if player_id:
            player_active = next(
                (
                    match for match in active_matches
                    if player_id in (match["player_a"], match["player_b"])
                ),
                None,
            )
            if player_active is not None:
                active = player_active
        public_room["active_match"] = active
        public_room["spectator_url"] = f"/?room={room['id']}&mode=spectator"
        return public_room

    def native_spectator(
        self,
        room_id: str,
        match_id: str | None = None,
    ) -> dict[str, Any]:
        """Return protected MDPro3/YGOPro2 observer connection details."""
        room = self._room(room_id)
        if room["status"] == "closed":
            raise TournamentError("room is closed")
        if match_id:
            match = self._find_match(room, match_id)
        else:
            active_match_id = room.get("active_match_id")
            if active_match_id:
                match = self._find_match(room, active_match_id)
            else:
                replayable = [
                    candidate
                    for candidate in room.get("matches", [])
                    if (candidate.get("session") or {}).get("engine")
                    == "native-pvp"
                    and (candidate.get("session") or {}).get("pvp_session_id")
                    and (candidate.get("session") or {}).get("spectator_code")
                ]
                if not replayable:
                    raise TournamentError(
                        "there is no native match with spectator access"
                    )
                match = replayable[-1]
            match_id = match["id"]
        session = match.get("session") or {}
        if session.get("engine") != "native-pvp":
            raise TournamentError("the selected match is not a native PvP match")
        native = session.get("native") or {}
        code = session.get("spectator_code")
        if not session.get("pvp_session_id") or not code:
            raise TournamentError("native spectator is not ready yet")
        replay = (
            match.get("status") == "finished"
            or session.get("status") == "finished"
        )
        if replay:
            replay_status = self._native_replay_status(match)
            if not replay_status["available"]:
                missing = replay_status["missing_game_numbers"]
                detail = (
                    "; missing games " + ", ".join(map(str, missing))
                    if missing else ""
                )
                raise TournamentError(
                    "native replay is incomplete "
                    f"({replay_status['replay_count']}/"
                    f"{replay_status['expected_replay_count']}{detail})"
                )
        return {
            "room_id": room_id,
            "match_id": match_id,
            "game_number": (
                len(match.get("games", []))
                if replay
                else len(match.get("games", [])) + 1
            ),
            "mode": "replay" if replay else "live",
            "status": (
                "finished" if replay else session.get("status", "waiting")
            ),
            "connection": {
                "host": native.get("host"),
                "port": native.get("port"),
                "code": code,
            },
        }

    def summaries(self) -> list[dict[str, Any]]:
        values = []
        for room in self.rooms.values():
            values.append({
                "id": room["id"],
                "name": room["name"],
                "room_type": room.get("room_type", "league"),
                "status": room["status"],
                "player_count": len(room["players"]),
                "active_match_id": room["active_match_id"],
                "updated_at": room["updated_at"],
            })
        return sorted(values, key=lambda value: value["updated_at"], reverse=True)

    def create(self, name: Any, players: Any,
               engine: Any = "manual-admin",
               player_count: Any = None,
               room_type: Any = "league",
               base_bo2_count: Any = None,
               seed_mode: Any = "mirrored",
               overtime_enabled: Any = True,
               auto_next_game: Any = True,
               series_format: Any = "bo2",
               game_count: Any = None,
               final_opponent_name: Any = None,
               final_completion_policy: Any = "first_to_four") -> dict[str, Any]:
        clean_name = _clean_name(name, "room name")
        if engine not in ENGINES:
            raise TournamentError("unsupported duel engine")
        if room_type not in ROOM_TYPES:
            raise TournamentError("unsupported room type")
        player_names = _players_from_body(players, player_count, room_type)
        room, access, final_opponent_access = _new_room(
            clean_name,
            player_names,
            engine,
            player_count if player_count is not None else len(player_names),
            room_type,
            base_bo2_count,
            seed_mode,
            overtime_enabled,
            auto_next_game,
            series_format,
            game_count,
            final_opponent_name,
            final_completion_policy,
        )
        self.rooms[room["id"]] = room
        self._save(room)
        result = self.public(room["id"])
        result["player_access"] = access
        if final_opponent_access is not None:
            result["final_opponent_access"] = final_opponent_access
        return result

    def native_access(
        self,
        room_id: str,
        match_id: str | None = None,
    ) -> dict[str, Any]:
        """Return protected stable native credentials for room administration."""
        room = self._room(room_id)
        if room["status"] == "closed":
            raise TournamentError("room is closed")
        if room.get("engine") != "native-pvp":
            raise TournamentError("room is not configured for native PvP")
        matches = room.get("matches", [])
        if match_id is not None:
            matches = [self._find_match(room, match_id)]
        elif not self._parallel_league(room):
            selected_id = room.get("active_match_id")
            selected = (
                self._find_match(room, selected_id)
                if selected_id
                else matches[0] if matches else None
            )
            matches = [selected] if selected is not None else []
        entries = []
        for match in matches:
            credentials = self._native_credentials(room, match)
            native = match.get("session", {}).get("native") or {}
            entries.append({
                "room_id": room_id,
                "duel_room_id": f"{room_id}:{match['id']}",
                "match_id": match["id"],
                "status": match.get("status"),
                "player_a": {
                    "id": match["player_a"],
                    "name": self._participant_name(room, match["player_a"]),
                },
                "player_b": {
                    "id": match["player_b"],
                    "name": self._participant_name(room, match["player_b"]),
                },
                "host": native.get("host"),
                "port": native.get("port"),
                "player_passwords": {
                    "a": credentials.get("a"),
                    "b": credentials.get("b"),
                },
                "spectator_password": credentials.get("spectator"),
            })
        if self._parallel_league(room) and match_id is None:
            return {
                "room_id": room_id,
                "room_type": "league",
                "parallel_matches": True,
                "matches": entries,
            }
        if not entries:
            raise TournamentError("native room credentials are unavailable")
        result = entries[0]
        result["room_type"] = room.get("room_type", "league")
        return result

    def rotate_player_access(self, room_id: str) -> dict[str, Any]:
        room = self._room(room_id)
        access = []
        for player in room["players"]:
            token = secrets.token_urlsafe(24)
            player["join_token_hash"] = _token_hash(token)
            access.append({
                "player_id": player["id"],
                "name": player["name"],
                "token": token,
                "join_url": (
                    f"/?room={room['id']}&mode=player&player={player['id']}"
                    f"&token={token}"
                ),
            })
        final_opponent = room.get("final_opponent")
        if isinstance(final_opponent, dict):
            token = secrets.token_urlsafe(24)
            final_opponent["join_token_hash"] = _token_hash(token)
            access.append({
                "player_id": final_opponent["id"],
                "name": final_opponent["name"],
                "token": token,
                "join_url": (
                    f"/?room={room['id']}&mode=player&player={final_opponent['id']}"
                    f"&token={token}"
                ),
                "role": "final_opponent",
            })
        self._save(room)
        result = self.public(room_id)
        result["player_access"] = [
            entry for entry in access if entry["player_id"] != "p-final"
        ]
        if final_opponent is not None:
            result["final_opponent_access"] = next(
                entry for entry in access if entry["player_id"] == "p-final"
            )
        return result

    def _authenticate_player(self, room: dict[str, Any], token: Any) -> dict[str, Any]:
        if not isinstance(token, str) or not 16 <= len(token) <= 256:
            raise web.HTTPUnauthorized(text="player token required")
        digest = _token_hash(token)
        for player in room["players"]:
            stored = player.get("join_token_hash", "")
            if stored and hmac.compare_digest(stored, digest):
                return player
        final_opponent = room.get("final_opponent")
        if isinstance(final_opponent, dict):
            stored = final_opponent.get("join_token_hash", "")
            if stored and hmac.compare_digest(stored, digest):
                return final_opponent
        raise web.HTTPUnauthorized(text="invalid player token")

    def identify_player(self, room_id: str, token: Any) -> dict[str, Any]:
        room = self._room(room_id)
        return self._authenticate_player(room, token)

    def join_player(self, room_id: str, token: Any) -> dict[str, Any]:
        room = self._room(room_id)
        if room["status"] == "closed":
            raise TournamentError("room is closed")
        player = self._authenticate_player(room, token)
        self.presence.setdefault(room_id, {})[player["id"]] = _now()
        return {
            "room": self.public(room_id, player["id"]),
            "player": {
                "id": player["id"],
                "name": player["name"],
            },
        }

    def set_presence(self, room_id: str, player_id: str, online: bool) -> None:
        if online:
            self.presence.setdefault(room_id, {})[player_id] = _now()
        else:
            self.presence.get(room_id, {}).pop(player_id, None)

    def set_player_ready(self, room_id: str, player_id: str,
                         token: Any, ready: bool = True,
                         match_id: str | None = None) -> dict[str, Any]:
        room = self._room(room_id)
        player = self._authenticate_player(room, token)
        if not isinstance(ready, bool):
            raise TournamentError("ready must be boolean")
        if player["id"] != player_id:
            raise web.HTTPForbidden(text="player token does not match seat")
        if room["status"] != "running":
            raise TournamentError("room is not running")
        if match_id is not None:
            match = self._find_match(room, match_id)
        else:
            candidates = [
                candidate for candidate in self._active_matches(room)
                if player_id in (
                    candidate.get("player_a"),
                    candidate.get("player_b"),
                )
            ]
            preferred = next(
                (
                    candidate for candidate in candidates
                    if candidate.get("id") == room.get("active_match_id")
                ),
                None,
            )
            if preferred is not None:
                match = preferred
            elif len(candidates) == 1:
                match = candidates[0]
            else:
                raise TournamentError(
                    "match_id is required when a player has multiple active rooms"
                )
            match_id = match["id"]
        if not self._match_is_active(room, match):
            raise TournamentError("match is not active")
        side = "a" if match["player_a"] == player_id else "b" if match["player_b"] == player_id else None
        if side is None:
            raise TournamentError("player is not in the active match")
        if not ready and match["session"].get("pvp_session_id"):
            raise TournamentError("native duel session already exists")
        match["session"]["player_ready"][side] = bool(ready)
        match["session"]["status"] = (
            "ready"
            if all(match["session"]["player_ready"].values())
            else "waiting"
        )
        if match["session"]["status"] == "ready" and match["session"]["opened_at"] is None:
            match["session"]["opened_at"] = _now()
        self.presence.setdefault(room_id, {})[player_id] = _now()
        self._save(room)
        return self.public(room_id, player_id)

    def start(self, room_id: str) -> dict[str, Any]:
        room = self._room(room_id)
        if room["status"] != "lobby":
            raise TournamentError("only a lobby can be started")
        room["status"] = "running"
        self._activate_next(room)
        self._save(room)
        return self.public(room_id)

    def close(self, room_id: str) -> dict[str, Any]:
        room = self._room(room_id)
        if room["status"] == "closed":
            return self.public(room_id)
        for match in room.get("matches", []):
            if match.get("status") not in ("running", "tiebreak", "pending"):
                continue
            match["status"] = "closed"
            match["phase"] = "closed"
            match["session"]["status"] = "closed"
        room["active_match_id"] = None
        room["status"] = "closed"
        room["stage"] = "closed"
        room["closed_at"] = _now()
        self._save(room)
        return self.public(room_id)

    def pause(self, room_id: str) -> dict[str, Any]:
        room = self._room(room_id)
        if room["status"] != "running":
            raise TournamentError("only a running room can be paused")
        for active_match in self._active_matches(room):
            if active_match["session"].get("engine") != "native-pvp":
                continue
            session = active_match["session"]
            if (
                session.get("pvp_session_id")
                or session.get("status") in {"ready", "connecting", "dueling"}
            ):
                raise TournamentError(
                    "native duel must finish before the tournament can be paused"
                )
        room["status"] = "paused"
        self._save(room)
        return self.public(room_id)

    def resume(self, room_id: str) -> dict[str, Any]:
        room = self._room(room_id)
        if room["status"] != "paused":
            raise TournamentError("only a paused room can be resumed")
        room["status"] = "running"
        self._save(room)
        return self.public(room_id)

    def _finish_round_robin_match(self, room: dict[str, Any],
                                  match: dict[str, Any]) -> None:
        result = (
            "a" if match["points"]["a"] > match["points"]["b"] else
            "b" if match["points"]["b"] > match["points"]["a"] else
            "draw"
        )
        match["match_result"] = result
        match["status"] = "finished"
        match["phase"] = "finished"
        match["finished_at"] = _now()
        if result in ("a", "b"):
            match["winner"] = _match_players(match, result)
            match["loser"] = _match_players(
                match, "b" if result == "a" else "a"
            )
        room["active_match_id"] = None
        self._activate_next(room)

    def _validate_native_winner(self, event: dict[str, Any]) -> str:
        winner = event.get("winner")
        if winner not in ("a", "b", "draw"):
            raise TournamentError("native winner is invalid")
        engine_winner = event.get("engine_winner")
        if type(engine_winner) is not int or engine_winner not in (0, 1, 2):
            raise TournamentError("native engine winner is invalid")
        engine_seats = event.get("engine_seats")
        if (
            not isinstance(engine_seats, dict)
            or {engine_seats.get("a"), engine_seats.get("b")} != {0, 1}
        ):
            raise TournamentError("native engine seats are invalid")
        expected = (
            "draw"
            if engine_winner == 2
            else next(
                side for side in ("a", "b")
                if engine_seats[side] == engine_winner
            )
        )
        if winner != expected:
            raise TournamentError(
                "native winner does not match engine winner and seats"
            )
        return winner

    def _attach_native_event(
        self,
        game: dict[str, Any],
        event: dict[str, Any],
    ) -> None:
        game["event_id"] = event["event_id"]
        game["session_id"] = event["session_id"]
        game["native_event_fingerprint"] = self._native_event_fingerprint(event)
        game["result_reason"] = event.get("reason")
        game["engine_winner"] = event["engine_winner"]
        if isinstance(event.get("seed"), list):
            game["engine_seed"] = copy.deepcopy(event["seed"])
        if event.get("required_first_player") in ("a", "b"):
            game["required_first_player"] = event[
                "required_first_player"
            ]
        if isinstance(event.get("seat_plan"), dict):
            game["planned_engine_seats"] = {
                side: event["seat_plan"][side]
                for side in ("a", "b")
                if event["seat_plan"].get(side) in (0, 1)
            }
        engine_seats = event["engine_seats"]
        game["engine_seats"] = {
            side: engine_seats[side]
            for side in ("a", "b")
        }
        replay = event.get("replay")
        if isinstance(replay, dict):
            if game.get("replay_missing_after_timeout") is True:
                game["replay_recovered_after_timeout"] = True
            game["replay"] = {
                key: replay[key] for key in ("bytes", "sha256")
                if key in replay
            }
        if event.get("replay_missing_after_timeout") is True:
            game["replay_missing_after_timeout"] = True
        fixed_deck = event.get("fixed_deck")
        if isinstance(fixed_deck, dict):
            game["fixed_deck"] = {
                key: copy.deepcopy(fixed_deck[key])
                for key in (
                    "name",
                    "source_sha256",
                    "canonical_sha256",
                    "payload_sha256",
                    "counts",
                )
                if key in fixed_deck
            }
        replacements = event.get("deck_replacements")
        if isinstance(replacements, dict):
            game["deck_replacements"] = {
                side: {
                    key: replacements[side][key]
                    for key in (
                        "replacement_count",
                        "first_submitted_payload_sha256",
                        "submitted_payload_sha256",
                        "target_payload_sha256",
                        "target_deck_sha256",
                        "last_replaced_at",
                    )
                    if key in replacements.get(side, {})
                }
                for side in ("a", "b")
                if isinstance(replacements.get(side), dict)
            }

    def _native_event_fingerprint(self, event: dict[str, Any]) -> str:
        fixed_deck = event.get("fixed_deck") or {}
        replacements = event.get("deck_replacements") or {}
        value = {
            "session_id": event.get("session_id"),
            "game_number": event.get("game_number"),
            "winner": event.get("winner"),
            "engine_winner": event.get("engine_winner"),
            "engine_seats": event.get("engine_seats"),
            "reason": event.get("reason"),
            "seed": event.get("seed"),
            "seat_plan": event.get("seat_plan"),
            "required_first_player": event.get("required_first_player"),
            "fixed_deck": fixed_deck.get("canonical_sha256"),
            "deck_replacements": {
                side: {
                    "replacement_count": (replacements.get(side) or {}).get(
                        "replacement_count"
                    ),
                    "target_deck_sha256": (replacements.get(side) or {}).get(
                        "target_deck_sha256"
                    ),
                    "target_payload_sha256": (replacements.get(side) or {}).get(
                        "target_payload_sha256"
                    ),
                }
                for side in ("a", "b")
            },
        }
        payload = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def _native_event_matches_game(
        self,
        game: dict[str, Any],
        event: dict[str, Any],
        winner: str,
    ) -> bool:
        fingerprint = game.get("native_event_fingerprint")
        if isinstance(fingerprint, str):
            return hmac.compare_digest(
                fingerprint,
                self._native_event_fingerprint(event),
            )
        checks = (
            game.get("number") == event.get("game_number"),
            game.get("winner") == winner,
            game.get("engine_winner") == event.get("engine_winner"),
            game.get("engine_seats") == event.get("engine_seats"),
            game.get("result_reason") == event.get("reason"),
        )
        if not all(checks):
            return False
        optional_checks = (
            ("session_id", "session_id"),
            ("engine_seed", "seed"),
            ("planned_engine_seats", "seat_plan"),
            ("required_first_player", "required_first_player"),
        )
        return all(
            stored not in game or game.get(stored) == event.get(incoming)
            for stored, incoming in optional_checks
        )

    def _remember_pvp_session(
        self,
        match: dict[str, Any],
        session_id: str,
        start_game: int,
    ) -> None:
        sessions = match.setdefault("pvp_sessions", [])
        for item in sessions:
            if isinstance(item, dict) and item.get("id") == session_id:
                if (
                    type(item.get("start_game")) is not int
                    or start_game < item["start_game"]
                ):
                    item["start_game"] = start_game
                return
        sessions.append({
            "id": session_id,
            "start_game": start_game,
        })
        sessions.sort(key=lambda item: int(item.get("start_game") or 1))

    def _can_undo_adjudication(
        self,
        room: dict[str, Any],
        match: dict[str, Any],
    ) -> bool:
        stack = room.get("_adjudication_undo_stack") or []
        if not stack or stack[-1].get("match_id") != match.get("id"):
            return False
        entry = stack[-1]
        created_at = float(entry.get("created_at") or 0)
        before_ids = set(entry.get("match_ids_before") or [])
        for candidate in room.get("matches", []):
            if candidate.get("id") == match.get("id"):
                continue
            if (
                candidate.get("id") not in before_ids
                and candidate.get("games")
            ):
                return False
            if any(
                float(game.get("recorded_at") or 0) > created_at
                for game in candidate.get("games", [])
            ):
                return False
        return True

    def adjudication_undo_session_ids(
        self,
        room_id: str,
        match_id: str,
    ) -> list[str]:
        room = self._room(room_id)
        match = self._find_match(room, match_id)
        if not self._can_undo_adjudication(room, match):
            raise TournamentError(
                "the adjudication cannot be undone after later results"
            )
        entry = room["_adjudication_undo_stack"][-1]
        before_ids = set(entry.get("match_ids_before") or [])
        return sorted({
            session_id
            for candidate in room.get("matches", [])
            if candidate.get("id") not in before_ids
            for session_id in [
                (candidate.get("session") or {}).get("pvp_session_id")
            ]
            if isinstance(session_id, str) and session_id
        })

    def adjudicate_forfeit(
        self,
        room_id: str,
        match_id: str,
        penalized_side: Any,
        scope: Any,
        reason: Any,
    ) -> dict[str, Any]:
        room = self._room(room_id)
        if room.get("status") != "running":
            raise TournamentError("room is not running")
        match = self._find_match(room, match_id)
        if not self._match_is_active(room, match):
            raise TournamentError("only an active match can be adjudicated")
        if penalized_side not in ("a", "b"):
            raise TournamentError("penalized_side must be a or b")
        if scope not in ADJUDICATION_SCOPES:
            raise TournamentError("scope must be remaining or all")
        if not isinstance(reason, str) or not reason.strip():
            raise TournamentError("an adjudication reason is required")
        reason = reason.strip()
        if len(reason) > MAX_ADJUDICATION_REASON:
            raise TournamentError(
                f"adjudication reason exceeds {MAX_ADJUDICATION_REASON} characters"
            )

        winner_side = "b" if penalized_side == "a" else "a"
        created_at = _now()
        ruling_id = _new_id("ruling")
        room_state = {
            key: copy.deepcopy(room.get(key))
            for key in (
                "status",
                "stage",
                "active_match_id",
                "final_match_id",
                "champion",
                "qualifying_champion",
            )
        }
        match_ids_before = [
            candidate["id"] for candidate in room.get("matches", [])
        ]
        affected: list[int] = []
        overridden: list[int] = []
        session_id = (match.get("session") or {}).get("pvp_session_id")
        if isinstance(session_id, str) and session_id:
            self._remember_pvp_session(
                match, session_id, len(match.get("games", [])) + 1
            )
            closed = match.setdefault(
                "administratively_closed_sessions", []
            )
            if session_id not in closed:
                closed.append(session_id)
        match_before = copy.deepcopy(match)

        forfeit_base = {
            "ruling_id": ruling_id,
            "penalized_side": penalized_side,
            "winner_side": winner_side,
            "scope": scope,
            "reason": reason,
            "created_at": created_at,
        }
        try:
            if scope == "all":
                scored_phase = (
                    "finals" if self._first_to_four(match) else "regular"
                )
                retained_games = []
                for game in match.get("games", []):
                    if game.get("phase") == "tiebreak":
                        continue
                    if game.get("phase") == scored_phase:
                        game.setdefault("original_winner", game.get("winner"))
                        game["winner"] = winner_side
                        game["administrative_forfeit"] = {
                            **copy.deepcopy(forfeit_base),
                            "synthetic": False,
                        }
                        affected.append(int(game["number"]))
                        overridden.append(int(game["number"]))
                    retained_games.append(game)
                match["games"] = retained_games
                match["game_plans"] = [
                    plan for plan in match.get("game_plans", [])
                    if int(plan.get("number") or 0) <= len(retained_games)
                ]
                self._recompute_match(room, match)
                if match.get("status") == "finished":
                    match["finished_at"] = created_at
                    if room.get("active_match_id") == match_id:
                        room["active_match_id"] = None
                    self._activate_next(room)

            safety_limit = MAX_SERIES_GAMES + FINAL_MAX_GAMES + 20
            synthetic_count = 0
            while match.get("status") in ("running", "tiebreak"):
                if synthetic_count >= safety_limit:
                    raise TournamentError(
                        "adjudication exceeded the scheduled game limit"
                    )
                before_count = len(match.get("games", []))
                self.record_game(
                    room_id,
                    match_id,
                    winner_side,
                    source="adjudication",
                    administrative_forfeit={
                        **copy.deepcopy(forfeit_base),
                        "synthetic": True,
                    },
                    persist=False,
                )
                if len(match.get("games", [])) <= before_count:
                    raise TournamentError(
                        "adjudication did not advance the match"
                    )
                affected.append(int(match["games"][-1]["number"]))
                synthetic_count += 1
        except Exception:
            room["matches"] = [
                copy.deepcopy(match_before)
                if candidate.get("id") == match_id else candidate
                for candidate in room.get("matches", [])
                if candidate.get("id") in match_ids_before
            ]
            for key, value in room_state.items():
                room[key] = copy.deepcopy(value)
            raise

        ruling = {
            "id": ruling_id,
            "type": "forfeit",
            "match_id": match_id,
            "penalized_side": penalized_side,
            "penalized_player_id": match[f"player_{penalized_side}"],
            "penalized_player_name": self._participant_name(
                room, match[f"player_{penalized_side}"]
            ),
            "winner_side": winner_side,
            "winner_player_id": match[f"player_{winner_side}"],
            "winner_player_name": self._participant_name(
                room, match[f"player_{winner_side}"]
            ),
            "scope": scope,
            "reason": reason,
            "affected_game_numbers": sorted(set(affected)),
            "overridden_game_numbers": sorted(set(overridden)),
            "synthetic_game_count": len(set(affected) - set(overridden)),
            "created_at": created_at,
            "reverted_at": None,
        }
        match.setdefault("administrative_rulings", []).append(
            copy.deepcopy(ruling)
        )
        match["session"]["status"] = "adjudicated"
        room.setdefault("administrative_ruling_log", []).append(
            copy.deepcopy(ruling)
        )
        stack = room.setdefault("_adjudication_undo_stack", [])
        stack.append({
            "ruling_id": ruling_id,
            "match_id": match_id,
            "created_at": created_at,
            "match_ids_before": match_ids_before,
            "room_state_before": room_state,
            "match_before": match_before,
        })
        del stack[:-20]
        self._save(room)
        return self.public(room_id)

    def _validate_force_finish(
        self,
        room: dict[str, Any],
        match: dict[str, Any],
        points_a: Any,
        points_b: Any,
        reason: Any,
    ) -> tuple[int, str]:
        if room.get("status") != "running":
            raise TournamentError("room is not running")
        if room.get("stage") != "round_robin":
            raise TournamentError(
                "only round-robin matches can be force-finished"
            )
        if (
            match.get("stage") != "round_robin"
            or not self._match_is_active(room, match)
        ):
            raise TournamentError(
                "only an active round-robin match can be force-finished"
            )
        score_limit = (
            int(match.get("game_limit") or room["rules"]["game_count"]) * 3
            if self._single_games(room, match)
            else self._bo2_limit(match) * 3
        )
        for label, value in (("points_a", points_a), ("points_b", points_b)):
            if type(value) is not int or value < 0 or value > score_limit:
                raise TournamentError(
                    f"{label} must be an integer between 0 and {score_limit}"
                )
        if not isinstance(reason, str) or not reason.strip():
            raise TournamentError("a force-finish reason is required")
        reason = reason.strip()
        if len(reason) > MAX_ADJUDICATION_REASON:
            raise TournamentError(
                f"force-finish reason exceeds {MAX_ADJUDICATION_REASON} characters"
            )
        return score_limit, reason

    def force_finish_round_robin(
        self,
        room_id: str,
        match_id: str,
        points_a: Any,
        points_b: Any,
        reason: Any,
    ) -> dict[str, Any]:
        room = self._room(room_id)
        match = self._find_match(room, match_id)
        score_limit, reason = self._validate_force_finish(
            room, match, points_a, points_b, reason
        )

        created_at = _now()
        ruling_id = _new_id("ruling")
        room_state = {
            key: copy.deepcopy(room.get(key))
            for key in (
                "status",
                "stage",
                "active_match_id",
                "final_match_id",
                "champion",
                "qualifying_champion",
            )
        }
        match_ids_before = [
            candidate["id"] for candidate in room.get("matches", [])
        ]
        session_id = (match.get("session") or {}).get("pvp_session_id")
        if isinstance(session_id, str) and session_id:
            self._remember_pvp_session(
                match, session_id, len(match.get("games", [])) + 1
            )
            closed = match.setdefault(
                "administratively_closed_sessions", []
            )
            if session_id not in closed:
                closed.append(session_id)
        match_before = copy.deepcopy(match)

        assigned_points = {"a": points_a, "b": points_b}
        ruling = {
            "id": ruling_id,
            "type": "forced_score",
            "match_id": match_id,
            "points": copy.deepcopy(assigned_points),
            "reason": reason,
            "score_limit": score_limit,
            "created_at": created_at,
            "reverted_at": None,
        }
        try:
            match["points"] = copy.deepcopy(assigned_points)
            match["administrative_score"] = {
                "ruling_id": ruling_id,
                "points": copy.deepcopy(assigned_points),
                "reason": reason,
                "created_at": created_at,
            }
            match["session"]["status"] = "adjudicated"
            self._finish_round_robin_match(room, match)
        except Exception:
            room["matches"] = [
                copy.deepcopy(match_before)
                if candidate.get("id") == match_id else candidate
                for candidate in room.get("matches", [])
                if candidate.get("id") in match_ids_before
            ]
            for key, value in room_state.items():
                room[key] = copy.deepcopy(value)
            raise

        match.setdefault("administrative_rulings", []).append(
            copy.deepcopy(ruling)
        )
        room.setdefault("administrative_ruling_log", []).append(
            copy.deepcopy(ruling)
        )
        stack = room.setdefault("_adjudication_undo_stack", [])
        stack.append({
            "ruling_id": ruling_id,
            "match_id": match_id,
            "created_at": created_at,
            "match_ids_before": match_ids_before,
            "room_state_before": room_state,
            "match_before": match_before,
        })
        del stack[:-20]
        self._save(room)
        return self.public(room_id)

    def undo_adjudication(
        self,
        room_id: str,
        match_id: str,
    ) -> dict[str, Any]:
        room = self._room(room_id)
        match = self._find_match(room, match_id)
        if not self._can_undo_adjudication(room, match):
            raise TournamentError(
                "the adjudication cannot be undone after later results"
            )
        entry = room["_adjudication_undo_stack"].pop()
        before_ids = set(entry["match_ids_before"])
        room["matches"] = [
            candidate for candidate in room.get("matches", [])
            if candidate.get("id") in before_ids
        ]
        restored = copy.deepcopy(entry["match_before"])
        for index, candidate in enumerate(room["matches"]):
            if candidate.get("id") == match_id:
                room["matches"][index] = restored
                break
        else:
            room["matches"].append(restored)
        for key, value in entry["room_state_before"].items():
            room[key] = copy.deepcopy(value)
        if (
            restored.get("session", {}).get("engine") == "native-pvp"
            and restored.get("status") in ("running", "tiebreak")
        ):
            restored["session"] = _new_duel_session(
                "native-pvp", self._auto_next(room, restored)
            )
        reverted_at = _now()
        room.setdefault("administrative_ruling_log", []).append({
            "id": _new_id("ruling"),
            "type": "revert",
            "ruling_id": entry["ruling_id"],
            "match_id": match_id,
            "created_at": reverted_at,
            "reverted_at": reverted_at,
        })
        self._save(room)
        return self.public(room_id)

    def record_game(self, room_id: str, match_id: str, winner: Any,
                    source: str = "admin",
                    native_event: dict[str, Any] | None = None,
                    administrative_forfeit: dict[str, Any] | None = None,
                    persist: bool = True) -> dict[str, Any]:
        room = self._room(room_id)
        if room["status"] != "running":
            raise TournamentError("room is not running")
        match = self._find_match(room, match_id)
        if not self._match_is_active(room, match):
            raise TournamentError("this match is not active")
        if match["status"] not in ("running", "tiebreak"):
            raise TournamentError("match is not accepting results")
        if (
            match["session"].get("engine") == "native-pvp"
            and source not in ("native", "adjudication")
        ):
            raise TournamentError("native matches accept results from the duel engine only")
        if winner not in ("a", "b", "draw"):
            raise TournamentError("winner must be a, b, or draw")

        plan = self._ensure_game_plan(room, match)
        if match["phase"] in ("regular", "finals"):
            phase = match["phase"]
            phase_count = len(self._phase_games(match, phase))
            single_games = self._single_games(room, match)
            first_to_four = self._first_to_four(match)
            limit = (
                int(match.get("game_limit") or room["rules"]["game_count"])
                if single_games
                else (
                    int(match.get("max_games") or FINAL_MAX_GAMES)
                    + int(match.get("draw_replays") or 0)
                ) if first_to_four
                else self._bo2_limit(match) * BO2_GAMES
            )
            if phase_count >= limit:
                raise TournamentError("scheduled games are complete")
            bo2_game = phase_count % BO2_GAMES + 1
            game = {
                "number": len(match["games"]) + 1,
                "phase": phase,
                "block": 0,
                "bo2": (
                    None
                    if single_games or first_to_four
                    else len(match.get("bo2s", [])) + 1
                ),
                "bo2_game": None if single_games or first_to_four else bo2_game,
                "seat_map": copy.deepcopy(plan["seat_map"]),
                "seat_plan": "a-first" if plan["required_first_player"] == "a" else "b-first",
                "seed_mode": plan["seed_mode"],
                "seed": copy.deepcopy(plan["seed"]),
                "required_first_player": plan["required_first_player"],
                "winner": winner,
                "recorded_at": _now(),
            }
            if native_event is not None:
                self._attach_native_event(game, native_event)
            if administrative_forfeit is not None:
                game["administrative_forfeit"] = copy.deepcopy(
                    administrative_forfeit
                )
            match["games"].append(game)
            if phase == "regular" and winner in ("a", "b"):
                match["regular_wins"][winner] += 1
            if first_to_four:
                if winner in ("a", "b"):
                    match["game_wins"][winner] += 1
                match["points"] = copy.deepcopy(match["game_wins"])
                target = int(match.get("target_wins") or FINAL_TARGET_WINS)
                completion_policy = match.get(
                    "completion_policy", "first_to_four"
                )
                if (
                    completion_policy == "first_to_four"
                    and winner in ("a", "b")
                    and match["game_wins"][winner] >= target
                ):
                    self._finish_match(room, match, winner)
                elif phase_count + 1 >= int(
                    match.get("max_games") or FINAL_MAX_GAMES
                ) + int(match.get("draw_replays") or 0):
                    if (
                        completion_policy == "play_all_seven"
                        and match["game_wins"]["a"] != match["game_wins"]["b"]
                    ):
                        self._finish_match(
                            room,
                            match,
                            "a"
                            if match["game_wins"]["a"] > match["game_wins"]["b"]
                            else "b",
                        )
                    else:
                        # Drawn games can leave either policy undecided after
                        # its nominal seven slots, so add one game at a time.
                        match["draw_replays"] = int(
                            match.get("draw_replays") or 0
                        ) + 1
            elif single_games:
                match["points"]["a"] += 3 if winner == "a" else 1 if winner == "draw" else 0
                match["points"]["b"] += 3 if winner == "b" else 1 if winner == "draw" else 0
                if phase_count + 1 == limit:
                    if match["points"]["a"] == match["points"]["b"]:
                        self._finish_draw(room, match)
                    else:
                        self._finish_match(
                            room, match,
                            "a" if match["points"]["a"] > match["points"]["b"] else "b",
                        )
            elif bo2_game == BO2_GAMES:
                self._append_bo2_summary(
                    match, self._phase_games(match, phase)[-BO2_GAMES:]
                )
                if len(match["bo2s"]) == self._bo2_limit(match):
                    if match.get("stage", "round_robin") == "round_robin":
                        self._finish_round_robin_match(room, match)
                    elif match.get("stage") == "series":
                        if match["points"]["a"] != match["points"]["b"]:
                            winner_side = (
                                "a"
                                if match["points"]["a"] > match["points"]["b"]
                                else "b"
                            )
                            self._finish_match(room, match, winner_side)
                        elif not room.get("rules", {}).get(
                            "overtime_enabled", True
                        ):
                            self._finish_draw(room, match)
                        else:
                            match["phase"] = "tiebreak"
                            match["status"] = "tiebreak"
                            match["playoff_block"] = 1
                            match["playoff_block_wins"] = {"a": 0, "b": 0}
                    elif match["points"]["a"] != match["points"]["b"]:
                        self._finish_final_from_points(match)
                        room["active_match_id"] = None
                        self._activate_next(room)
                    else:
                        match["phase"] = "tiebreak"
                        match["status"] = "tiebreak"
                        match["playoff_block"] = 1
                        match["playoff_block_wins"] = {"a": 0, "b": 0}
        else:
            block = match["playoff_block"] or 1
            block_count = self._tiebreak_game_count(match, block)
            if block_count >= TIEBREAK_BLOCK_GAMES:
                raise TournamentError("current tiebreak block is complete")
            block_game = block_count + 1
            game = {
                "number": len(match["games"]) + 1,
                "phase": "tiebreak",
                "block": block,
                "bo2": len(match.get("bo2s", [])) + 1,
                "bo2_game": block_game,
                "seat_map": copy.deepcopy(plan["seat_map"]),
                "seat_plan": "swap-block",
                "seed_mode": plan["seed_mode"],
                "seed": copy.deepcopy(plan["seed"]),
                "required_first_player": plan["required_first_player"],
                "winner": winner,
                "recorded_at": _now(),
            }
            if native_event is not None:
                self._attach_native_event(game, native_event)
            if administrative_forfeit is not None:
                game["administrative_forfeit"] = copy.deepcopy(
                    administrative_forfeit
                )
            match["games"].append(game)
            if winner in ("a", "b"):
                match["playoff_block_wins"][winner] += 1
            if block_game == TIEBREAK_BLOCK_GAMES:
                if match["playoff_block_wins"]["a"] == 2:
                    self._finish_match(room, match, "a")
                elif match["playoff_block_wins"]["b"] == 2:
                    self._finish_match(room, match, "b")
                else:
                    match["playoff_block"] = block + 1
                    match["playoff_block_wins"] = {"a": 0, "b": 0}
        if (
            match["status"] in ("running", "tiebreak")
            and not self._auto_next(room, match)
        ):
            self._reset_duel_session(room, match)
        if persist:
            self._save(room)
        return self.public(room_id)

    def record_native_game(self, room_id: str, match_id: str,
                           event: dict[str, Any]) -> dict[str, Any]:
        room = self._room(room_id)
        match = self._find_match(room, match_id)
        event_id = event.get("event_id")
        if not isinstance(event_id, str) or not event_id:
            raise TournamentError("native result event_id is required")
        if event.get("session_id") in match.get(
            "administratively_closed_sessions", []
        ):
            return self.public(room_id)
        winner = self._validate_native_winner(event)
        for candidate in room["matches"]:
            for game in candidate["games"]:
                if game.get("event_id") == event_id:
                    if (
                        candidate["id"] == match_id
                        and self._native_event_matches_game(game, event, winner)
                    ):
                        self._attach_native_event(game, event)
                        if isinstance(event.get("session_id"), str):
                            self._remember_pvp_session(
                                candidate,
                                event["session_id"],
                                int(event.get("game_number") or game["number"]),
                            )
                        self._save(room)
                        return self.public(room_id)
                    if candidate["id"] == match_id:
                        raise TournamentError(
                            "native result event_id conflicts with the recorded event"
                        )
                    raise TournamentError("native result event_id was already used")
        session = match["session"]
        if session.get("engine") != "native-pvp":
            raise TournamentError("match is not a native PvP match")
        if session.get("pvp_session_id") != event.get("session_id"):
            raise TournamentError("native session does not match the active game")
        if event.get("game_number") != len(match["games"]) + 1:
            raise TournamentError("native game number does not match the active game")
        if self._auto_next(room, match):
            plan = self._ensure_game_plan(room, match)
            expected_seed = plan["seed"]
            if (
                not isinstance(expected_seed, list)
                or event.get("seed") != expected_seed
            ):
                raise TournamentError("native engine seed audit does not match")
            required_first = plan["required_first_player"]
            if (
                required_first not in ("a", "b")
                or event.get("required_first_player") != required_first
            ):
                raise TournamentError(
                    "native required first-player audit does not match"
                )
            expected_seats = plan["seat_map"]
            if event.get("seat_plan") != expected_seats:
                raise TournamentError("native seat plan audit does not match")
            if event.get("engine_seats") != expected_seats:
                raise TournamentError(
                    "native engine did not enforce the required first player"
                )
        expected_deck = session.get("fixed_deck")
        if isinstance(expected_deck, dict):
            fixed_deck = event.get("fixed_deck")
            if not isinstance(fixed_deck, dict) or fixed_deck.get(
                "canonical_sha256"
            ) != expected_deck.get("canonical_sha256"):
                raise TournamentError("native fixed deck does not match the session")
            replacements = event.get("deck_replacements")
            if not isinstance(replacements, dict) or not all(
                isinstance(replacements.get(side), dict)
                and replacements[side].get("replacement_count", 0) >= 1
                and replacements[side].get("target_deck_sha256")
                == expected_deck.get("canonical_sha256")
                for side in ("a", "b")
            ):
                raise TournamentError("native fixed deck replacement audit is incomplete")
        self._remember_pvp_session(
            match,
            event["session_id"],
            int(event["game_number"]),
        )
        return self.record_game(
            room_id,
            match_id,
            winner,
            source="native",
            native_event=event,
        )

    def undo_last_game(self, room_id: str, match_id: str) -> dict[str, Any]:
        room = self._room(room_id)
        if room["status"] not in ("running", "paused"):
            raise TournamentError("room is not active")
        match = self._find_match(room, match_id)
        if not self._match_is_active(room, match):
            raise TournamentError("only the active match can be corrected")
        if match["session"].get("engine") == "native-pvp":
            raise TournamentError(
                "native results cannot be undone; use a reviewed correction workflow"
            )
        if not match["games"]:
            raise TournamentError("there is no game to undo")
        match["games"].pop()
        self._recompute_match(room, match)
        self._save(room)
        return self.public(room_id)

    def delete(self, room_id: str) -> None:
        room = self._room(room_id)
        if room["status"] == "running":
            raise TournamentError("pause or finish the room before deleting it")
        if any(
            match.get("session", {}).get("pvp_session_id")
            and match.get("status") in ("running", "tiebreak")
            for match in room.get("matches", [])
        ):
            raise TournamentError(
                "native duel must finish before the room can be deleted"
            )
        self.rooms.pop(room_id)
        self.presence.pop(room_id, None)
        try:
            self._path(room_id).unlink()
        except FileNotFoundError:
            pass


class TournamentApplication:
    def __init__(
        self,
        data_dir: Path,
        admin_key: str | None = None,
        pvp_url: str | None = None,
        pvp_key: str | None = None,
        internal_key: str | None = None,
        pvp_public_host: str | None = None,
    ):
        self.store = TournamentStore(data_dir)
        self.admin_key = (
            admin_key
            if admin_key is not None
            else os.environ.get("TOURNAMENT_ADMIN_KEY", "")
        ).strip()
        self.pvp_url = (pvp_url or os.environ.get("PVP_URL", "")).rstrip("/")
        self.pvp_key = pvp_key or os.environ.get("PVP_INTERNAL_KEY", "")
        self.internal_key = internal_key or os.environ.get(
            "TOURNAMENT_INTERNAL_KEY", ""
        )
        if not self.internal_key:
            self.internal_key = secrets.token_urlsafe(32)
        self.pvp_public_host = pvp_public_host or os.environ.get(
            "PVP_PUBLIC_HOST", ""
        )
        self.clients: dict[
            str, dict[web.WebSocketResponse, str | None]
        ] = {}
        self.mutation_lock = asyncio.Lock()
        self.native_lock = asyncio.Lock()
        self.native_provision_locks: dict[
            tuple[str, str], asyncio.Lock
        ] = {}
        self.native_closing_rooms: set[str] = set()
        self.static = Path(__file__).parent / "static"

    def require_admin(self, request: web.Request) -> None:
        if not self.admin_key:
            return
        if request.headers.get("X-Tournament-Admin-Key") != self.admin_key:
            raise web.HTTPUnauthorized(text="admin key required")

    async def broadcast(self, room_id: str) -> None:
        clients = list(self.clients.get(room_id, {}).items())
        if not clients:
            return
        stale = []
        for client, player_id in clients:
            if client.closed:
                stale.append(client)
                continue
            try:
                await client.send_json(self.store.public(room_id, player_id))
            except (ConnectionError, RuntimeError):
                stale.append(client)
        for client in stale:
            self.clients.get(room_id, {}).pop(client, None)

    async def mutate(self, request: web.Request, operation):
        self.require_admin(request)
        async with self.mutation_lock:
            result = operation()
        await self.broadcast(result["id"])
        return web.json_response(result)

    async def index(self, request: web.Request) -> web.StreamResponse:
        return web.FileResponse(self.static / "index.html")

    async def static_file(self, request: web.Request) -> web.StreamResponse:
        name = request.match_info["name"]
        if name not in {"app.js", "app.css"}:
            raise web.HTTPNotFound()
        return web.FileResponse(self.static / name)

    async def favicon(self, request: web.Request) -> web.Response:
        return web.Response(status=204)

    async def config(self, request: web.Request) -> web.Response:
        return web.json_response({
            "admin_required": bool(self.admin_key),
        })

    async def health(self, request: web.Request) -> web.Response:
        return web.json_response({
            "status": "ok",
            "service": "ygo-tournament",
            "rooms": len(self.store.rooms),
            "native_pvp_configured": bool(self.pvp_url and self.pvp_key),
        })

    async def rooms(self, request: web.Request) -> web.Response:
        return web.json_response({"items": self.store.summaries()})

    async def refresh_native_replay_status(
        self,
        room_id: str,
        match_id: str | None = None,
    ) -> bool:
        if not self.pvp_url or not self.pvp_key:
            return False
        room = self.store._room(room_id)
        replay_targets: list[tuple[str, list[str]]] = []
        connection_targets: list[tuple[str, str]] = []
        for match in room.get("matches", []):
            if match_id is not None and match.get("id") != match_id:
                continue
            session = match.get("session") or {}
            if session.get("engine") != "native-pvp":
                continue
            session_id = session.get("pvp_session_id")
            if (
                match.get("status") in {"running", "tiebreak"}
                and isinstance(session_id, str)
                and session_id
            ):
                connection_targets.append((match["id"], session_id))
            if match.get("status") == "finished":
                session_ids = [
                    entry["id"]
                    for entry in self.replay_session_entries(match)
                ]
                if session_ids:
                    replay_targets.append((match["id"], session_ids))
        requested_session_ids = sorted({
            session_id
            for _target_match_id, session_ids in replay_targets
            for session_id in session_ids
        } | {
            session_id
            for _target_match_id, session_id in connection_targets
        })
        if not requested_session_ids:
            return False

        async def fetch(
            client: ClientSession,
            session_id: str,
        ) -> dict[str, Any] | None:
            try:
                async with client.get(
                    f"{self.pvp_url}/api/sessions/{session_id}",
                    headers={"X-PVP-Internal-Key": self.pvp_key},
                ) as response:
                    if response.status >= 300:
                        return None
                    payload = await response.json()
            except (
                ClientError,
                asyncio.TimeoutError,
                OSError,
                ValueError,
            ):
                return None
            return payload if isinstance(payload, dict) else None

        payloads: dict[str, dict[str, Any]] = {}
        async with ClientSession(
            timeout=ClientTimeout(total=5),
            trust_env=False,
        ) as client:
            responses = await asyncio.gather(*(
                fetch(client, session_id)
                for session_id in requested_session_ids
            ))
        payloads.update({
            session_id: payload
            for session_id, payload in zip(requested_session_ids, responses)
            if payload is not None
        })

        refreshed_replays: dict[str, dict[str, Any]] = {}
        for target_match_id, session_ids in replay_targets:
            statuses = []
            for session_id in session_ids:
                status = (payloads.get(session_id) or {}).get(
                    "replay_status"
                )
                if not isinstance(status, dict):
                    continue
                result = copy.deepcopy(status)
                result["session_id"] = session_id
                statuses.append(result)
            if not statuses:
                continue

            def replay_rank(
                status: dict[str, Any],
            ) -> tuple[bool, bool, int, int, str]:
                replay_count = status.get("replay_count")
                expected_count = status.get("expected_replay_count")
                return (
                    status.get("available") is True,
                    status.get("complete") is True,
                    replay_count if type(replay_count) is int else -1,
                    expected_count if type(expected_count) is int else -1,
                    status.get("session_id", ""),
                )

            refreshed_replays[target_match_id] = max(
                statuses,
                key=replay_rank,
            )

        refreshed_connections: dict[str, dict[str, Any]] = {}
        valid_states = {
            "waiting", "online", "reconnecting",
            "offline", "stalled",
        }
        timestamp_fields = (
            "connected_at",
            "last_activity_at",
            "last_client_activity_at",
            "last_server_activity_at",
            "disconnected_at",
            "reconnect_until",
        )
        for target_match_id, session_id in connection_targets:
            payload = payloads.get(session_id)
            if payload is None:
                refreshed_connections[target_match_id] = {
                    "session_id": session_id,
                    "status": "unavailable",
                    "players": {},
                    "spectators": 0,
                }
                continue
            connections = payload.get("connections")
            players: dict[str, dict[str, Any]] = {}
            if isinstance(connections, dict):
                for side in ("a", "b"):
                    item = connections.get(side)
                    if not isinstance(item, dict):
                        item = {}
                    state = item.get("state")
                    if state not in valid_states:
                        if item.get("connected"):
                            state = "online"
                        elif item.get("reconnecting"):
                            state = "reconnecting"
                        elif item.get("disconnected_at"):
                            state = "offline"
                        else:
                            state = "waiting"
                    public_item = {
                        "state": state,
                        "connected": bool(item.get("connected")),
                        "reconnecting": bool(item.get("reconnecting")),
                        "reconnect_scope": (
                            item.get("reconnect_scope")
                            if item.get("reconnect_scope") in {"series", "grace"}
                            else None
                        ),
                    }
                    for field in timestamp_fields:
                        value = item.get(field)
                        public_item[field] = (
                            value if isinstance(value, (int, float)) else None
                        )
                    players[side] = public_item
            stall_warning_seconds = payload.get("stall_warning_seconds")
            refreshed_connections[target_match_id] = {
                "session_id": session_id,
                "status": payload.get("status", "unknown"),
                "players": players,
                "spectators": (
                    payload.get("spectators")
                    if type(payload.get("spectators")) is int
                    else 0
                ),
                "stall_warning_seconds": (
                    stall_warning_seconds
                    if type(stall_warning_seconds) is int
                    and stall_warning_seconds > 0
                    else 120
                ),
            }

        changed = False
        replay_changed = False
        async with self.mutation_lock:
            room = self.store._room(room_id)
            for target_match_id, replay_status in refreshed_replays.items():
                try:
                    match = self.store._find_match(room, target_match_id)
                except web.HTTPException:
                    continue
                session = match.get("session") or {}
                if session.get("replay_status") != replay_status:
                    session["replay_status"] = replay_status
                    changed = True
                    replay_changed = True
            for target_match_id, connection_status in (
                refreshed_connections.items()
            ):
                try:
                    match = self.store._find_match(room, target_match_id)
                except web.HTTPException:
                    continue
                session = match.get("session") or {}
                if (
                    session.get("pvp_session_id")
                    != connection_status["session_id"]
                ):
                    continue
                if session.get("connection_status") != connection_status:
                    session["connection_status"] = connection_status
                    changed = True
            if replay_changed:
                self.store._save(room)
        return changed

    async def room(self, request: web.Request) -> web.Response:
        room_id = request.match_info["room_id"]
        await self.refresh_native_replay_status(room_id)
        player_id = None
        token = self.player_token(request)
        if token:
            try:
                player_id = self.store.identify_player(room_id, token)["id"]
            except web.HTTPException:
                player_id = None
        return web.json_response(self.store.public(room_id, player_id))

    async def create_room(self, request: web.Request) -> web.Response:
        body = await request.json()
        self.require_admin(request)
        async with self.mutation_lock:
            result = self.store.create(
                body.get("name"),
                body.get("players"),
                body.get("engine", "manual-admin"),
                body.get("player_count"),
                body.get("room_type", "league"),
                body.get("base_bo2_count"),
                body.get("seed_mode", "mirrored"),
                body.get("overtime_enabled", True),
                body.get("auto_next_game", True),
                body.get("series_format", "bo2"),
                body.get("game_count"),
                body.get("final_opponent_name"),
                body.get("final_completion_policy", "first_to_four"),
            )
        await self.broadcast(result["id"])
        return web.json_response(result)

    async def rotate_player_access(self, request: web.Request) -> web.Response:
        self.require_admin(request)
        room_id = request.match_info["room_id"]
        async with self.mutation_lock:
            result = self.store.rotate_player_access(room_id)
        await self.broadcast(room_id)
        return web.json_response(result)

    async def native_spectator(self, request: web.Request) -> web.Response:
        self.require_admin(request)
        await self.refresh_native_replay_status(
            request.match_info["room_id"],
            request.query.get("match_id"),
        )
        return web.json_response(
            self.store.native_spectator(
                request.match_info["room_id"],
                request.query.get("match_id"),
            )
        )

    async def native_access(self, request: web.Request) -> web.Response:
        self.require_admin(request)
        return web.json_response(
            self.store.native_access(
                request.match_info["room_id"],
                request.query.get("match_id"),
            )
        )

    def replay_matches(
        self,
        request: web.Request,
        room_id: str,
        match_id: str | None = None,
    ) -> list[dict[str, Any]]:
        room = self.store._room(room_id)
        player_id = None
        token = self.player_token(request)
        if token:
            player_id = self.store.identify_player(room_id, token)["id"]
        else:
            self.require_admin(request)
        matches = (
            [self.store._find_match(room, match_id)]
            if match_id else list(room.get("matches", []))
        )
        if player_id:
            matches = [
                match for match in matches
                if player_id in (match.get("player_a"), match.get("player_b"))
            ]
            if match_id and not matches:
                raise web.HTTPForbidden(
                    text="player is not assigned to this duel room"
                )
        matches = [
            match for match in matches
            if (match.get("session") or {}).get("engine") == "native-pvp"
            and self.replay_session_entries(match)
        ]
        if not matches:
            raise web.HTTPConflict(text="no native replays are available yet")
        return matches

    def replay_session_entries(
        self,
        match: dict[str, Any],
    ) -> list[dict[str, Any]]:
        entries = []
        seen = set()
        for item in match.get("pvp_sessions", []):
            if not isinstance(item, dict):
                continue
            session_id = item.get("id")
            start_game = item.get("start_game")
            if (
                isinstance(session_id, str)
                and session_id
                and type(start_game) is int
                and start_game > 0
                and session_id not in seen
            ):
                entries.append({
                    "id": session_id,
                    "start_game": start_game,
                })
                seen.add(session_id)
        for game in match.get("games", []):
            session_id = game.get("session_id")
            if (
                isinstance(session_id, str)
                and session_id
                and session_id not in seen
            ):
                entries.append({
                    "id": session_id,
                    "start_game": int(game.get("number") or 1),
                })
                seen.add(session_id)
        session = match.get("session") or {}
        current = session.get("pvp_session_id")
        if isinstance(current, str) and current and current not in seen:
            entries.append({
                "id": current,
                "start_game": len(match.get("games", [])) + 1,
            })
        entries.sort(key=lambda item: int(item["start_game"]))
        return entries

    async def fetch_pvp_replays(
        self,
        client: ClientSession,
        session_id: str,
    ) -> bytes | None:
        async with client.get(
            f"{self.pvp_url}/api/sessions/{session_id}/replays",
            params={"format": "playable"},
            headers={"X-PVP-Internal-Key": self.pvp_key},
        ) as response:
            if response.status == 404:
                return None
            if response.status == 409:
                detail = (await response.content.read(4096)).decode(
                    "utf-8", errors="replace"
                ).strip()
                raise web.HTTPConflict(
                    text=f"native replay export rejected for {session_id}: "
                    + (detail or "replay archive is not ready"),
                )
            if response.status >= 300:
                raise TournamentError("native replay service rejected the request")
            payload = await response.read()
        if len(payload) > MAX_REPLAY_DOWNLOAD_BYTES:
            raise TournamentError("native replay archive is too large")
        return payload

    async def append_match_replays(
        self,
        target: zipfile.ZipFile,
        client: ClientSession,
        match: dict[str, Any],
        prefix: str = "",
        total: int = 0,
        manifest_entries: list[dict[str, Any]] | None = None,
        missing_entries: list[dict[str, Any]] | None = None,
    ) -> tuple[int, int]:
        replay_count = 0
        written: dict[int, str] = {}
        declared_missing: dict[int, str] = {}
        expected_games = {
            game.get("number")
            for game in match.get("games", [])
            if type(game.get("number")) is int and game["number"] > 0
        }
        for source_session in self.replay_session_entries(match):
            start_game = int(source_session["start_game"])
            if (
                expected_games
                and not any(number >= start_game for number in expected_games)
            ):
                continue
            try:
                payload = await self.fetch_pvp_replays(
                    client,
                    source_session["id"],
                )
            except web.HTTPConflict:
                if not expected_games:
                    continue
                raise
            if payload is None:
                if expected_games:
                    raise web.HTTPConflict(
                        text=(
                            "native replay session is missing for completed "
                            f"games: {source_session['id']}"
                        )
                    )
                continue
            try:
                source = zipfile.ZipFile(io.BytesIO(payload))
            except zipfile.BadZipFile as error:
                raise TournamentError(
                    "native replay archive is invalid"
                ) from error
            with source:
                manifests = [
                    entry for entry in source.infolist()
                    if not entry.is_dir()
                    and Path(entry.filename).name == "manifest.json"
                ]
                if len(manifests) != 1:
                    raise TournamentError(
                        "native replay archive must contain one manifest"
                    )
                manifest_info = manifests[0]
                if manifest_info.file_size > 1024 * 1024:
                    raise TournamentError(
                        "native replay archive manifest is too large"
                    )
                try:
                    manifest = json.loads(
                        source.read(manifest_info).decode("utf-8")
                    )
                except (UnicodeError, ValueError) as error:
                    raise TournamentError(
                        "native replay archive manifest is invalid"
                    ) from error
                if not isinstance(manifest, dict):
                    raise TournamentError(
                        "native replay archive is not a playable indexed export"
                    )
                complete = manifest.get("complete")
                historical_prefix_missing = manifest.get(
                    "historical_prefix_missing"
                )
                missing_ordinals = manifest.get("missing_ordinals")
                if (
                    manifest.get("schema")
                    != "ygo-pvp-replay-manifest/v2"
                    or manifest.get("session_id")
                    != source_session["id"]
                    or manifest.get("ordinal_scope") != "absolute"
                    or manifest.get("format") != "mdpro3-yrp3d"
                    or not isinstance(manifest.get("replays"), list)
                    or type(complete) is not bool
                    or type(historical_prefix_missing) is not bool
                    or not isinstance(missing_ordinals, list)
                    or any(
                        type(ordinal) is not int or ordinal <= 0
                        for ordinal in missing_ordinals
                    )
                    or missing_ordinals
                    != sorted(set(missing_ordinals))
                    or complete != (not missing_ordinals)
                    or historical_prefix_missing != bool(missing_ordinals)
                ):
                    raise TournamentError(
                        "native replay archive is not a playable indexed export"
                    )
                indexed: dict[str, dict[str, Any]] = {}
                indexed_ordinals: set[int] = set()
                for item in manifest["replays"]:
                    if not isinstance(item, dict):
                        raise TournamentError(
                            "native replay archive manifest is invalid"
                        )
                    filename = item.get("filename")
                    ordinal = item.get("ordinal")
                    byte_count = item.get("bytes")
                    digest = item.get("sha256")
                    source_yrp = item.get("source_yrp_sha256")
                    source_yrp_ordinal = item.get("source_yrp_ordinal")
                    source_evrp = item.get("source_evrp_sha256")
                    source_duel = item.get("source_evrp_duel")
                    hashes = (digest, source_yrp, source_evrp)
                    if (
                        not isinstance(filename, str)
                        or Path(filename).name != filename
                        or not filename.lower().endswith(".yrp3d")
                        or filename in indexed
                        or type(ordinal) is not int
                        or ordinal <= 0
                        or ordinal in indexed_ordinals
                        or type(byte_count) is not int
                        or not 0 < byte_count <= MAX_REPLAY_DOWNLOAD_BYTES
                        or any(
                            not isinstance(value, str)
                            or len(value) != 64
                            or any(
                                char not in "0123456789abcdef"
                                for char in value.lower()
                            )
                            for value in hashes
                        )
                        or type(source_yrp_ordinal) is not int
                        or source_yrp_ordinal <= 0
                        or type(source_duel) is not int
                        or source_duel <= 0
                    ):
                        raise TournamentError(
                            "native replay archive manifest is invalid"
                        )
                    if filename != f"game-{ordinal:04d}.yrp3d":
                        raise TournamentError(
                            "native replay archive manifest is invalid"
                        )
                    indexed[filename] = item
                    indexed_ordinals.add(ordinal)
                ordered_ordinals = sorted(indexed_ordinals)
                if (
                    not ordered_ordinals
                    or ordered_ordinals
                    != list(range(
                        ordered_ordinals[0],
                        ordered_ordinals[-1] + 1,
                    ))
                    or (
                        missing_ordinals
                        and missing_ordinals
                        != list(range(start_game, ordered_ordinals[0]))
                    )
                    or (
                        not missing_ordinals
                        and ordered_ordinals[0] != start_game
                    )
                ):
                    raise TournamentError(
                        "native replay archive manifest is invalid"
                    )
                for ordinal in missing_ordinals:
                    declared_missing.setdefault(
                        ordinal, source_session["id"]
                    )
                seen_files = set()
                for entry in source.infolist():
                    name = Path(entry.filename).name
                    if (
                        entry.is_dir()
                        or not name.lower().endswith(".yrp3d")
                        or entry.file_size > MAX_REPLAY_DOWNLOAD_BYTES
                    ):
                        continue
                    item = indexed.get(name)
                    if item is None or name in seen_files:
                        raise TournamentError(
                            "native replay archive does not match its manifest"
                        )
                    seen_files.add(name)
                    if entry.file_size != item["bytes"]:
                        raise TournamentError(
                            "native replay archive size does not match its manifest"
                        )
                    game_number = item["ordinal"]
                    if expected_games and game_number not in expected_games:
                        continue
                    replay = source.read(entry)
                    total += len(replay)
                    if total > MAX_REPLAY_DOWNLOAD_BYTES:
                        raise TournamentError(
                            "combined replay archive is too large"
                        )
                    digest = hashlib.sha256(replay).hexdigest()
                    if digest != item["sha256"].lower():
                        raise TournamentError(
                            "native replay archive checksum does not match "
                            "its manifest"
                        )
                    previous = written.get(game_number)
                    if previous is not None:
                        if previous != digest:
                            raise TournamentError(
                                "native replay archives conflict for the same game"
                            )
                        continue
                    written[game_number] = digest
                    filename = f"{prefix}game-{game_number:04d}.yrp3d"
                    target.writestr(
                        filename,
                        replay,
                    )
                    if manifest_entries is not None:
                        manifest_entries.append({
                            "match_id": match.get("id"),
                            "session_id": source_session["id"],
                            "ordinal": game_number,
                            "filename": filename,
                            "bytes": len(replay),
                            "sha256": digest,
                            "source_yrp_sha256": item[
                                "source_yrp_sha256"
                            ].lower(),
                            "source_yrp_ordinal": item[
                                "source_yrp_ordinal"
                            ],
                            "source_evrp_sha256": item[
                                "source_evrp_sha256"
                            ].lower(),
                            "source_evrp_duel": item["source_evrp_duel"],
                        })
                    replay_count += 1
                if seen_files != set(indexed):
                    raise TournamentError(
                        "native replay archive is missing a manifested replay"
                    )
        if expected_games:
            missing = sorted(expected_games - set(written))
            undeclared = [
                number for number in missing
                if number not in declared_missing
            ]
            if undeclared:
                raise web.HTTPConflict(
                    text=(
                        "native replays are incomplete for completed games: "
                        + ", ".join(str(number) for number in undeclared)
                    )
                )
            if missing_entries is not None:
                missing_entries.extend({
                    "match_id": match.get("id"),
                    "session_id": declared_missing[number],
                    "ordinal": number,
                    "reason": "historical_prefix_missing",
                } for number in missing)
        return total, replay_count

    async def download_match_replays(
        self,
        request: web.Request,
    ) -> web.Response:
        if not self.pvp_url or not self.pvp_key:
            raise TournamentError("native PvP service is not configured")
        room_id = request.match_info["room_id"]
        match = self.replay_matches(
            request, room_id, request.match_info["match_id"]
        )[0]
        output = io.BytesIO()
        manifest_entries: list[dict[str, Any]] = []
        missing_entries: list[dict[str, Any]] = []
        try:
            async with ClientSession(
                timeout=ClientTimeout(total=30), trust_env=False
            ) as client:
                with zipfile.ZipFile(
                    output, "w", compression=zipfile.ZIP_STORED
                ) as target:
                    _total, replay_count = await self.append_match_replays(
                        target,
                        client,
                        match,
                        manifest_entries=manifest_entries,
                        missing_entries=missing_entries,
                    )
                    target.writestr(
                        "manifest.json",
                        json.dumps({
                            "schema": "ygo-tournament-replay-manifest/v1",
                            "room_id": room_id,
                            "match_id": match["id"],
                            "format": "mdpro3-yrp3d",
                            "complete": not missing_entries,
                            "missing_games": missing_entries,
                            "replays": manifest_entries,
                        }, ensure_ascii=False, separators=(",", ":")).encode(
                            "utf-8"
                        ),
                    )
        except (ClientError, asyncio.TimeoutError, OSError) as error:
            raise TournamentError(
                "native replay service is unreachable"
            ) from error
        if replay_count == 0:
            raise web.HTTPConflict(text="replays are not available yet")
        return web.Response(
            body=output.getvalue(),
            content_type="application/zip",
            headers={
                "Content-Disposition": (
                    f'attachment; filename="{room_id}-{match["id"]}-replays.zip"'
                ),
            },
        )

    async def download_room_replays(
        self,
        request: web.Request,
    ) -> web.Response:
        if not self.pvp_url or not self.pvp_key:
            raise TournamentError("native PvP service is not configured")
        room_id = request.match_info["room_id"]
        matches = self.replay_matches(request, room_id)
        output = io.BytesIO()
        total = 0
        replay_count = 0
        manifest_entries: list[dict[str, Any]] = []
        missing_entries: list[dict[str, Any]] = []
        try:
            async with ClientSession(
                timeout=ClientTimeout(total=60), trust_env=False
            ) as client:
                with zipfile.ZipFile(
                    output, "w", compression=zipfile.ZIP_STORED
                ) as target:
                    for match in matches:
                        total, added = await self.append_match_replays(
                            target,
                            client,
                            match,
                            prefix=f"{match['id']}/",
                            total=total,
                            manifest_entries=manifest_entries,
                            missing_entries=missing_entries,
                        )
                        replay_count += added
                    target.writestr(
                        "manifest.json",
                        json.dumps({
                            "schema": "ygo-tournament-replay-manifest/v1",
                            "room_id": room_id,
                            "format": "mdpro3-yrp3d",
                            "complete": not missing_entries,
                            "missing_games": missing_entries,
                            "replays": manifest_entries,
                        }, ensure_ascii=False, separators=(",", ":")).encode(
                            "utf-8"
                        ),
                    )
        except (ClientError, asyncio.TimeoutError, OSError) as error:
            raise TournamentError(
                "native replay service is unreachable"
            ) from error
        if replay_count == 0:
            raise web.HTTPConflict(text="replays are not available yet")
        return web.Response(
            body=output.getvalue(),
            content_type="application/zip",
            headers={
                "Content-Disposition": (
                    f'attachment; filename="{room_id}-replays.zip"'
                ),
            },
        )

    def player_token(self, request: web.Request) -> str | None:
        return (
            request.headers.get("X-Tournament-Player-Token")
            or request.query.get("token")
        )

    async def provision_native_session(self, room_id: str, match_id: str) -> None:
        if not self.pvp_url or not self.pvp_key:
            raise TournamentError("native PvP service is not configured")
        key = (room_id, match_id)
        provision_lock = self.native_provision_locks.setdefault(
            key, asyncio.Lock()
        )
        async with provision_lock:
            if room_id in self.native_closing_rooms:
                return
            room = self.store._room(room_id)
            if room["status"] != "running":
                return
            match = self.store._find_match(room, match_id)
            if not self.store._match_is_active(room, match):
                return
            session = match["session"]
            if session.get("engine") != "native-pvp":
                return
            if session.get("pvp_session_id"):
                return
            if not all(session["player_ready"].values()):
                return
            plan = self.store._ensure_game_plan(room, match)
            self.store._save(room)
            payload = {
                "room_id": room_id,
                "match_id": match_id,
                "game_number": len(match["games"]) + 1,
                "seat_plan": copy.deepcopy(plan["seat_map"]),
                "seed": copy.deepcopy(plan["seed"]),
                "required_first_player": plan["required_first_player"],
                "seed_mode": plan["seed_mode"],
                "players": {
                    "a": {
                        "name": self.store._participant_name(
                            room, match["player_a"]
                        )
                    },
                    "b": {
                        "name": self.store._participant_name(
                            room, match["player_b"]
                        )
                    },
                },
                # The gateway replaces both submissions with the tournament's
                # fixed deck. Let that audited deck define legality instead of
                # rejecting it against Evolution's current public ban list.
                "format": room.get("native_format", "nc"),
            }
            payload["credentials"] = copy.deepcopy(
                self.store._native_credentials(room, match)
            )
            payload["continuous_series"] = self.store._auto_next(
                room, match
            )
            headers = {
                "X-PVP-Internal-Key": self.pvp_key,
                "Content-Type": "application/json",
            }
            if self.pvp_public_host:
                headers["X-PVP-Public-Host"] = self.pvp_public_host
            try:
                async with ClientSession(
                    timeout=ClientTimeout(total=10), trust_env=False
                ) as client:
                    async with client.post(
                        self.pvp_url + "/api/sessions",
                        json=payload,
                        headers=headers,
                    ) as response:
                        data = await response.json()
                        if response.status >= 300:
                            raise TournamentError(
                                data.get("error", "native PvP session creation failed")
                            )
            except (ClientError, asyncio.TimeoutError, OSError) as error:
                session["provision_error"] = type(error).__name__
                self.store._save(room)
                raise TournamentError("native PvP service is unreachable") from error
            native = data.get("native") or {}
            created = data.get("session") or {}
            if not isinstance(created.get("id"), str):
                raise TournamentError("native PvP service returned an invalid session")
            session.update({
                "status": created.get("status", "waiting"),
                "pvp_session_id": created["id"],
                "continuous_series": payload["continuous_series"],
                "native": {
                    "host": native.get("host"),
                    "port": native.get("port"),
                    "seat_plan": payload["seat_plan"],
                },
                "seat_plan": copy.deepcopy(payload["seat_plan"]),
                "seed": copy.deepcopy(payload["seed"]),
                "seed_mode": payload["seed_mode"],
                "required_first_player": payload[
                    "required_first_player"
                ],
                "native_player_codes": (
                    native.get("player_passwords")
                    or native.get("player_codes", {})
                ),
                "spectator_code": (
                    native.get("spectator_password")
                    or native.get("spectator_code")
                ),
                "connection_status": None,
                "fixed_deck": created.get("fixed_deck"),
                "provision_error": None,
            })
            self.store._remember_pvp_session(
                match,
                created["id"],
                int(payload["game_number"]),
            )
            self.store._save(room)

    async def try_provision_native_session(
        self,
        room_id: str,
        match_id: str,
    ) -> None:
        try:
            await self.provision_native_session(room_id, match_id)
        except (TournamentError, web.HTTPException) as error:
            try:
                room = self.store._room(room_id)
                match = self.store._find_match(room, match_id)
                match["session"]["provision_error"] = str(error)
                self.store._save(room)
            except (TournamentError, web.HTTPException):
                pass

    async def provision_native_sessions(
        self,
        candidates: list[tuple[str, str]],
        limit: int = 8,
    ) -> None:
        semaphore = asyncio.Semaphore(limit)

        async def provision(room_id: str, match_id: str) -> None:
            async with semaphore:
                await self.try_provision_native_session(room_id, match_id)

        await asyncio.gather(*(
            provision(room_id, match_id)
            for room_id, match_id in candidates
        ))

    async def _provision_retry_loop(self) -> None:
        while True:
            await asyncio.sleep(3)
            candidates = []
            for room in list(self.store.rooms.values()):
                if (
                    room.get("status") == "running"
                    and room.get("engine") == "native-pvp"
                ):
                    for match in self.store._active_matches(room):
                        match_id = match["id"]
                        session = match.get("session") or {}
                        if (
                            all(session.get("player_ready", {}).values())
                            and not session.get("pvp_session_id")
                        ):
                            candidates.append((room["id"], match_id))
            if candidates:
                await self.provision_native_sessions(candidates)

    async def duel_event(self, request: web.Request) -> web.Response:
        raw = await request.read()
        signature = request.headers.get("X-PVP-Signature", "")
        expected = hmac.new(
            self.internal_key.encode("utf-8"), raw, hashlib.sha256
        ).hexdigest()
        if not signature or not hmac.compare_digest(signature, expected):
            raise web.HTTPUnauthorized(text="invalid PvP callback signature")
        try:
            event = json.loads(raw)
        except (ValueError, UnicodeDecodeError) as error:
            raise web.HTTPBadRequest(text="invalid callback JSON") from error
        if not isinstance(event, dict):
            raise web.HTTPBadRequest(text="invalid callback payload")
        async with self.mutation_lock:
            room = self.store._room(event.get("room_id"))
            if room["status"] == "closed":
                return web.json_response({
                    "ok": True, "event_id": event.get("event_id"),
                    "next_game": None, "series_finished": True,
                })
            result = self.store.record_native_game(
                event.get("room_id"), event.get("match_id"), event
            )
        room = self.store._room(event["room_id"])
        current_match = self.store._find_match(room, event["match_id"])
        continuous = self.store._auto_next(room, current_match)
        next_game = None
        if (
            continuous
            and self.store._match_is_active(room, current_match)
            and current_match["status"] in ("running", "tiebreak")
        ):
            plan = self.store._ensure_game_plan(room, current_match)
            next_game = {
                "game_number": plan["number"],
                "seat_plan": copy.deepcopy(plan["seat_map"]),
                "seed": copy.deepcopy(plan["seed"]),
                "seed_mode": plan["seed_mode"],
                "required_first_player": plan["required_first_player"],
            }
            self.store._save(room)
        if room.get("engine") == "native-pvp":
            candidates = []
            for next_match in self.store._active_matches(room):
                next_session = next_match.get("session") or {}
                if (
                    all(next_session.get("player_ready", {}).values())
                    and not next_session.get("pvp_session_id")
                ):
                    candidates.append(
                        (event["room_id"], next_match["id"])
                    )
            if candidates:
                await self.provision_native_sessions(candidates)
            result = self.store.public(event["room_id"])
        await self.broadcast(event["room_id"])
        return web.json_response({
            "ok": True,
            "event_id": event["event_id"],
            "room": result,
            "next_game": next_game,
            "series_finished": continuous and next_game is None,
        })

    async def join_player(self, request: web.Request) -> web.Response:
        room_id = request.match_info["room_id"]
        body = await request.json()
        token = body.get("token") or self.player_token(request)
        async with self.mutation_lock:
            result = self.store.join_player(room_id, token)
        await self.broadcast(room_id)
        return web.json_response(result)

    async def player_ready(self, request: web.Request) -> web.Response:
        room_id = request.match_info["room_id"]
        player_id = request.match_info["player_id"]
        body = await request.json()
        ready = body.get("ready", True)
        token = self.player_token(request) or body.get("token")
        match_id = request.match_info.get("match_id") or body.get("match_id")
        async with self.mutation_lock:
            self.store.set_player_ready(
                room_id, player_id, token, ready, match_id
            )
        if ready:
            room = self.store._room(room_id)
            if room.get("engine") == "native-pvp":
                for match in self.store._active_matches(room):
                    if (
                        player_id in (match["player_a"], match["player_b"])
                        and all(
                            match.get("session", {})
                            .get("player_ready", {})
                            .values()
                        )
                        and not match.get("session", {}).get("pvp_session_id")
                    ):
                        await self.provision_native_session(
                            room_id, match["id"]
                        )
        result = self.store.public(room_id, player_id)
        await self.broadcast(room_id)
        return web.json_response(result)

    async def start_room(self, request: web.Request) -> web.Response:
        room_id = request.match_info["room_id"]
        self.require_admin(request)
        async with self.mutation_lock:
            self.store.start(room_id)
        room = self.store._room(room_id)
        if room.get("engine") == "native-pvp":
            candidates = [
                (room_id, match["id"])
                for match in self.store._active_matches(room)
                if all(
                    match.get("session", {})
                    .get("player_ready", {})
                    .values()
                )
            ]
            if candidates:
                await self.provision_native_sessions(candidates)
        result = self.store.public(room_id)
        await self.broadcast(room_id)
        return web.json_response(result)

    async def pause_room(self, request: web.Request) -> web.Response:
        room_id = request.match_info["room_id"]
        return await self.mutate(request, lambda: self.store.pause(room_id))

    async def resume_room(self, request: web.Request) -> web.Response:
        room_id = request.match_info["room_id"]
        return await self.mutate(request, lambda: self.store.resume(room_id))

    async def close_native_sessions(
        self,
        session_ids: list[str],
    ) -> None:
        session_ids = sorted({
            session_id for session_id in session_ids
            if isinstance(session_id, str) and session_id
        })
        if not session_ids:
            return
        if not self.pvp_url or not self.pvp_key:
            raise TournamentError("native PvP service is not configured")
        try:
            async with ClientSession(
                timeout=ClientTimeout(total=10),
                trust_env=False,
            ) as client:
                for session_id in session_ids:
                    async with client.post(
                        f"{self.pvp_url}/api/sessions/{session_id}/close",
                        headers={"X-PVP-Internal-Key": self.pvp_key},
                    ) as response:
                        if response.status >= 300:
                            raise TournamentError(
                                "native PvP session close failed"
                            )
        except (
            ClientError,
            asyncio.TimeoutError,
            OSError,
        ) as error:
            raise TournamentError(
                "native PvP service is unreachable"
            ) from error

    async def seal_native_sessions(
        self,
        session_ids: list[str],
        reason: str,
    ) -> None:
        session_ids = sorted({
            session_id for session_id in session_ids
            if isinstance(session_id, str) and session_id
        })
        if not session_ids:
            return
        if not self.pvp_url or not self.pvp_key:
            raise TournamentError("native PvP service is not configured")
        try:
            async with ClientSession(
                timeout=ClientTimeout(total=10),
                trust_env=False,
            ) as client:
                for session_id in session_ids:
                    async with client.post(
                        f"{self.pvp_url}/api/sessions/{session_id}/seal",
                        headers={"X-PVP-Internal-Key": self.pvp_key},
                        json={"reason": reason},
                    ) as response:
                        if response.status >= 300:
                            raise TournamentError(
                                "native PvP session seal failed"
                            )
        except (
            ClientError,
            asyncio.TimeoutError,
            OSError,
        ) as error:
            raise TournamentError(
                "native PvP service is unreachable"
            ) from error

    async def adjudicate_match(self, request: web.Request) -> web.Response:
        self.require_admin(request)
        body = await request.json()
        room_id = request.match_info["room_id"]
        match_id = request.match_info["match_id"]
        penalized_side = body.get("penalized_side")
        scope = body.get("scope", "remaining")
        reason = body.get("reason")
        if penalized_side not in ("a", "b"):
            raise TournamentError("penalized_side must be a or b")
        if scope not in ADJUDICATION_SCOPES:
            raise TournamentError("scope must be remaining or all")
        if not isinstance(reason, str) or not reason.strip():
            raise TournamentError("an adjudication reason is required")
        if len(reason.strip()) > MAX_ADJUDICATION_REASON:
            raise TournamentError(
                f"adjudication reason exceeds {MAX_ADJUDICATION_REASON} characters"
            )

        provision_lock = self.native_provision_locks.setdefault(
            (room_id, match_id), asyncio.Lock()
        )
        async with self.native_lock:
            await provision_lock.acquire()
            try:
                async with self.mutation_lock:
                    room = self.store._room(room_id)
                    match = self.store._find_match(room, match_id)
                    if room.get("status") != "running":
                        raise TournamentError("room is not running")
                    if not self.store._match_is_active(room, match):
                        raise TournamentError(
                            "only an active match can be adjudicated"
                        )
                    session_id = (
                        (match.get("session") or {}).get("pvp_session_id")
                        if room.get("engine") == "native-pvp"
                        else None
                    )
                    await self.seal_native_sessions(
                        [session_id] if session_id else [],
                        reason,
                    )
                    result = self.store.adjudicate_forfeit(
                        room_id,
                        match_id,
                        penalized_side,
                        scope,
                        reason,
                    )
            finally:
                provision_lock.release()
        await self.broadcast(room_id)
        return web.json_response(result)

    async def force_finish_match(self, request: web.Request) -> web.Response:
        self.require_admin(request)
        body = await request.json()
        room_id = request.match_info["room_id"]
        match_id = request.match_info["match_id"]
        provision_lock = self.native_provision_locks.setdefault(
            (room_id, match_id), asyncio.Lock()
        )
        async with self.native_lock:
            await provision_lock.acquire()
            try:
                async with self.mutation_lock:
                    room = self.store._room(room_id)
                    match = self.store._find_match(room, match_id)
                    self.store._validate_force_finish(
                        room,
                        match,
                        body.get("points_a"),
                        body.get("points_b"),
                        body.get("reason"),
                    )
                    session_id = (
                        (match.get("session") or {}).get("pvp_session_id")
                        if room.get("engine") == "native-pvp"
                        else None
                    )
                    await self.seal_native_sessions(
                        [session_id] if session_id else [],
                        str(body.get("reason") or ""),
                    )
                    result = self.store.force_finish_round_robin(
                        room_id,
                        match_id,
                        body.get("points_a"),
                        body.get("points_b"),
                        body.get("reason"),
                    )
            finally:
                provision_lock.release()

        room = self.store._room(room_id)
        if room.get("engine") == "native-pvp":
            candidates = [
                (room_id, match["id"])
                for match in self.store._active_matches(room)
                if (
                    all(
                        (match.get("session") or {})
                        .get("player_ready", {})
                        .values()
                    )
                    and not (match.get("session") or {}).get(
                        "pvp_session_id"
                    )
                )
            ]
            if candidates:
                await self.provision_native_sessions(candidates)
            result = self.store.public(room_id)
        await self.broadcast(room_id)
        return web.json_response(result)

    async def undo_match_adjudication(
        self,
        request: web.Request,
    ) -> web.Response:
        self.require_admin(request)
        room_id = request.match_info["room_id"]
        match_id = request.match_info["match_id"]
        async with self.native_lock:
            acquired = []
            try:
                locks = [
                    lock
                    for (candidate_room, _candidate_match), lock
                    in self.native_provision_locks.items()
                    if candidate_room == room_id
                ]
                for lock in locks:
                    await lock.acquire()
                    acquired.append(lock)
                async with self.mutation_lock:
                    session_ids = self.store.adjudication_undo_session_ids(
                        room_id, match_id
                    )
                    await self.close_native_sessions(session_ids)
                    result = self.store.undo_adjudication(
                        room_id, match_id
                    )
            finally:
                for lock in reversed(acquired):
                    lock.release()

        room = self.store._room(room_id)
        if room.get("engine") == "native-pvp":
            candidates = [
                (room_id, match["id"])
                for match in self.store._active_matches(room)
                if (
                    all(
                        (match.get("session") or {})
                        .get("player_ready", {})
                        .values()
                    )
                    and not (match.get("session") or {}).get(
                        "pvp_session_id"
                    )
                )
            ]
            if candidates:
                await self.provision_native_sessions(candidates)
            result = self.store.public(room_id)
        await self.broadcast(room_id)
        return web.json_response(result)

    async def close_room(self, request: web.Request) -> web.Response:
        self.require_admin(request)
        room_id = request.match_info["room_id"]
        async with self.native_lock:
            self.native_closing_rooms.add(room_id)
            acquired = []
            try:
                provision_locks = [
                    lock
                    for (candidate_room, _match_id), lock
                    in self.native_provision_locks.items()
                    if candidate_room == room_id
                ]
                for lock in provision_locks:
                    await lock.acquire()
                    acquired.append(lock)
                async with self.mutation_lock:
                    room = self.store._room(room_id)
                    if room["status"] != "closed":
                        session_ids = {
                            entry["id"]
                            for match in room.get("matches", [])
                            for entry in self.replay_session_entries(match)
                        }
                        for session_id in sorted(session_ids):
                            if not self.pvp_url or not self.pvp_key:
                                raise TournamentError(
                                    "native PvP service is not configured"
                                )
                            try:
                                async with ClientSession(
                                    timeout=ClientTimeout(total=10),
                                    trust_env=False,
                                ) as client:
                                    async with client.post(
                                        (
                                            f"{self.pvp_url}/api/sessions/"
                                            f"{session_id}/close"
                                        ),
                                        headers={
                                            "X-PVP-Internal-Key": self.pvp_key
                                        },
                                    ) as response:
                                        if response.status >= 300:
                                            raise TournamentError(
                                                "native PvP session close failed"
                                            )
                            except (
                                ClientError,
                                asyncio.TimeoutError,
                                OSError,
                            ) as error:
                                raise TournamentError(
                                    "native PvP service is unreachable"
                                ) from error
                    result = self.store.close(room_id)
            finally:
                for lock in reversed(acquired):
                    lock.release()
                try:
                    closed = self.store._room(room_id).get("status") == "closed"
                except web.HTTPException:
                    closed = False
                if not closed:
                    self.native_closing_rooms.discard(room_id)
        await self.broadcast(room_id)
        return web.json_response(result)

    async def record_game(self, request: web.Request) -> web.Response:
        body = await request.json()
        room_id = request.match_info["room_id"]
        match_id = request.match_info["match_id"]
        return await self.mutate(
            request,
            lambda: self.store.record_game(room_id, match_id, body.get("winner")),
        )

    async def undo_game(self, request: web.Request) -> web.Response:
        room_id = request.match_info["room_id"]
        match_id = request.match_info["match_id"]
        return await self.mutate(
            request,
            lambda: self.store.undo_last_game(room_id, match_id),
        )

    async def delete_room(self, request: web.Request) -> web.Response:
        self.require_admin(request)
        room_id = request.match_info["room_id"]
        async with self.mutation_lock:
            self.store.delete(room_id)
        return web.json_response({"ok": True, "id": room_id})

    async def websocket(self, request: web.Request) -> web.StreamResponse:
        room_id = request.match_info["room_id"]
        await self.refresh_native_replay_status(room_id)
        self.store.public(room_id)
        player = None
        token = self.player_token(request)
        if token:
            player = self.store.identify_player(room_id, token)
        ws = web.WebSocketResponse(heartbeat=25)
        await ws.prepare(request)
        if player:
            self.store.set_presence(room_id, player["id"], True)
        self.clients.setdefault(room_id, {})[ws] = (
            player["id"] if player else None
        )
        await ws.send_json(self.store.public(room_id, player["id"] if player else None))
        if player:
            await self.broadcast(room_id)
        try:
            async for message in ws:
                if message.type == WSMsgType.ERROR:
                    break
                if message.type == WSMsgType.TEXT and message.data == "ping":
                    await ws.send_str("pong")
                    if player:
                        self.store.set_presence(room_id, player["id"], True)
                elif message.type == WSMsgType.CLOSE:
                    break
        finally:
            self.clients.get(room_id, {}).pop(ws, None)
            if player:
                self.store.set_presence(room_id, player["id"], False)
                await self.broadcast(room_id)
        return ws

    async def _replay_status_loop(self) -> None:
        while True:
            await asyncio.sleep(5)
            for room_id in list(self.clients):
                if not self.clients.get(room_id):
                    continue
                try:
                    changed = await self.refresh_native_replay_status(room_id)
                except web.HTTPException:
                    continue
                if changed:
                    await self.broadcast(room_id)

    def app(self) -> web.Application:
        @web.middleware
        async def errors(request: web.Request, handler):
            try:
                return await handler(request)
            except web.HTTPException:
                raise
            except TournamentError as error:
                return web.json_response({"error": str(error)}, status=400)
            except (json.JSONDecodeError, TypeError) as error:
                return web.json_response({"error": f"invalid JSON: {error}"}, status=400)

        app = web.Application(middlewares=[errors], client_max_size=64 * 1024)
        app.router.add_get("/", self.index)
        app.router.add_get("/favicon.ico", self.favicon)
        app.router.add_get("/static/{name}", self.static_file)
        app.router.add_get("/health", self.health)
        app.router.add_get("/api/config", self.config)
        app.router.add_get("/api/rooms", self.rooms)
        app.router.add_get("/api/rooms/{room_id}", self.room)
        app.router.add_post("/api/rooms", self.create_room)
        app.router.add_post("/api/rooms/{room_id}/player-access",
                            self.rotate_player_access)
        app.router.add_get("/api/rooms/{room_id}/native-spectator",
                           self.native_spectator)
        app.router.add_get("/api/rooms/{room_id}/native-access",
                           self.native_access)
        app.router.add_get(
            "/api/rooms/{room_id}/matches/{match_id}/replays",
            self.download_match_replays,
        )
        app.router.add_get(
            "/api/rooms/{room_id}/replays",
            self.download_room_replays,
        )
        app.router.add_post("/api/rooms/{room_id}/join", self.join_player)
        app.router.add_post(
            "/api/rooms/{room_id}/matches/{match_id}/players/{player_id}/ready",
            self.player_ready,
        )
        app.router.add_post("/api/rooms/{room_id}/players/{player_id}/ready",
                            self.player_ready)
        app.router.add_post("/api/rooms/{room_id}/start", self.start_room)
        app.router.add_post("/api/rooms/{room_id}/pause", self.pause_room)
        app.router.add_post("/api/rooms/{room_id}/resume", self.resume_room)
        app.router.add_post("/api/rooms/{room_id}/close", self.close_room)
        app.router.add_post("/api/rooms/{room_id}/matches/{match_id}/games",
                            self.record_game)
        app.router.add_post("/api/rooms/{room_id}/matches/{match_id}/undo",
                            self.undo_game)
        app.router.add_post(
            "/api/rooms/{room_id}/matches/{match_id}/adjudicate",
            self.adjudicate_match,
        )
        app.router.add_post(
            "/api/rooms/{room_id}/matches/{match_id}/force-finish",
            self.force_finish_match,
        )
        app.router.add_post(
            "/api/rooms/{room_id}/matches/{match_id}/adjudication/undo",
            self.undo_match_adjudication,
        )
        app.router.add_post("/api/internal/duel-events", self.duel_event)
        app.router.add_delete("/api/rooms/{room_id}", self.delete_room)
        app.router.add_get("/ws/{room_id}", self.websocket)

        async def start_background(_app: web.Application) -> None:
            _app["provision_task"] = asyncio.create_task(
                self._provision_retry_loop()
            )
            _app["replay_status_task"] = asyncio.create_task(
                self._replay_status_loop()
            )

        async def stop_background(_app: web.Application) -> None:
            tasks = [
                task for task in (
                    _app.get("provision_task"),
                    _app.get("replay_status_task"),
                )
                if task is not None
            ]
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

        app.on_startup.append(start_background)
        app.on_cleanup.append(stop_background)
        return app


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="YGO tournament room service")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--data-dir", type=Path, default=Path("data/tournament"))
    parser.add_argument("--admin-key")
    parser.add_argument("--pvp-url", default=os.environ.get("PVP_URL"))
    parser.add_argument("--pvp-key", default=os.environ.get("PVP_INTERNAL_KEY"))
    parser.add_argument("--internal-key",
                        default=os.environ.get("TOURNAMENT_INTERNAL_KEY"))
    parser.add_argument("--pvp-public-host", default=None)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    application = TournamentApplication(
        args.data_dir,
        args.admin_key,
        args.pvp_url,
        args.pvp_key,
        args.internal_key,
        args.pvp_public_host,
    )
    if application.admin_key:
        print("TOURNAMENT_ADMIN_KEY=provided", flush=True)
    else:
        print("TOURNAMENT_ADMIN_KEY=disabled", flush=True)
    print(f"TOURNAMENT_URL=http://{args.host}:{args.port}", flush=True)
    web.run_app(application.app(), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
