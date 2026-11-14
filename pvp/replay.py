"""Replay export helpers for MDPro3's frame-recording format."""

from __future__ import annotations

import base64
import binascii
import struct
from typing import Any

from . import protocol as P


MDPRO3_NAME_MESSAGE = 235
MDPRO3_NAME_CHARS = 50
MDPRO3_NAME_PADDING = 0xCC
YRP2_MAGIC = b"yrp2"
YRP2_IDENTITY_BYTES = 64


def _unicode_slot(value: Any) -> bytes:
    text = value if isinstance(value, str) and value else "Player"
    encoded = text.encode("utf-16-le", errors="replace")
    maximum = MDPRO3_NAME_CHARS * 2 - 2
    encoded = encoded[:maximum]
    if len(encoded) >= 2:
        last_unit = int.from_bytes(encoded[-2:], "little")
        if 0xD800 <= last_unit <= 0xDBFF:
            encoded = encoded[:-2]
    padding = MDPRO3_NAME_CHARS * 2 - len(encoded) - 2
    return encoded + b"\x00\x00" + bytes((MDPRO3_NAME_PADDING,)) * padding


def _record(opcode: int, payload: bytes) -> bytes:
    return struct.pack("<BI", opcode, len(payload)) + payload


def _name_record(
    meta: dict[str, Any],
    duel: dict[str, Any],
    player_names: list[str] | None = None,
) -> bytes:
    players: Any = None
    if player_names is not None:
        players = [{"name": name} for name in player_names]
    elif isinstance(duel.get("players"), list):
        players = duel["players"]
    if players is None:
        players = meta.get("players")
    if not isinstance(players, list):
        players = []
    names = []
    for index in range(2):
        player = players[index] if index < len(players) else None
        name = player.get("name") if isinstance(player, dict) else None
        names.append(name if isinstance(name, str) and name else f"Player {index + 1}")
    host_info = meta.get("hostInfo")
    duel_rule = host_info.get("duel_rule") if isinstance(host_info, dict) else None
    if type(duel_rule) is not int or not 0 <= duel_rule <= 0x7FFFFFFF:
        duel_rule = 5
    payload = b"".join(
        _unicode_slot(name)
        for name in (names[0], names[0], names[0], names[1], names[1], names[1])
    )
    payload += struct.pack("<i", duel_rule)
    return _record(MDPRO3_NAME_MESSAGE, payload)


def evrp_duel_to_yrp3d(
    meta: dict[str, Any],
    duel: dict[str, Any],
    player_names: list[str] | None = None,
) -> bytes:
    """Convert one authoritative EVRP spectator stream to MDPro3 YRP3D."""
    if not isinstance(meta, dict) or not isinstance(duel, dict):
        raise ValueError("EVRP duel metadata is invalid")
    encoded_frames = duel.get("spectatorFrames")
    if not isinstance(encoded_frames, list) or not encoded_frames:
        raise ValueError("EVRP spectator frame list is missing")

    if (
        player_names is not None
        and (
            len(player_names) != 2
            or any(not isinstance(name, str) or not name for name in player_names)
        )
    ):
        raise ValueError("EVRP duel player names are invalid")

    records = [_name_record(meta, duel, player_names)]
    opcodes = []
    for encoded in encoded_frames:
        if not isinstance(encoded, str) or len(encoded) > 90_000:
            raise ValueError("EVRP contains an invalid spectator frame")
        try:
            frame = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as error:
            raise ValueError("EVRP contains invalid spectator base64") from error
        if (
            not 4 <= len(frame) <= P.MAX_FRAME + 2
            or int.from_bytes(frame[:2], "little") != len(frame) - 2
            or frame[2] != P.GAME_MSG
        ):
            raise ValueError("EVRP contains an invalid native GAME_MSG frame")
        opcode = frame[3]
        opcodes.append(opcode)
        records.append(_record(opcode, frame[4:]))

    if opcodes[0] != P.MSG_START or opcodes.count(P.MSG_START) != 1:
        raise ValueError("EVRP duel does not start with exactly one MSG_START")
    if opcodes[-1] != P.MSG_WIN or opcodes.count(P.MSG_WIN) != 1:
        raise ValueError("EVRP duel does not end with exactly one MSG_WIN")
    return b"".join(records)


def yrp2_identity(payload: bytes) -> dict[str, Any] | None:
    """Read stable YRP2 identity fields without replaying the response stream."""
    if len(payload) < YRP2_IDENTITY_BYTES or payload[:4].lower() != YRP2_MAGIC:
        return None
    return {
        "seed": list(struct.unpack_from("<8I", payload, 32)),
        "started_at": struct.unpack_from("<I", payload, 20)[0],
    }
