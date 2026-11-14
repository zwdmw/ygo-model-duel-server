"""Small, transparent subset of the YGOPro TCP protocol.

The gateway never interprets duel decisions. It authenticates JOIN_GAME,
adapts the known legacy wire layouts, and observes public results and replays.
"""

from __future__ import annotations

import asyncio
import hashlib
import struct


MAX_FRAME = 0xFFFE
PLAYER_INFO = 0x10
JOIN_GAME = 0x12
LEAVE_GAME = 0x13
UPDATE_DECK = 0x02
GAME_MSG = 0x01
MSG_START = 0x04
DUEL_START = 0x15
DUEL_END = 0x16
REPLAY = 0x17
EVRP_EXPORT = 0xF0
MSG_WIN = 0x05
LEGACY_VERSION = 0x133E
MDPRO3_VERSION = 0x1361
ENGINE_VERSION = 0x1362
ENGINE_COMPATIBLE_VERSIONS = frozenset((LEGACY_VERSION, MDPRO3_VERSION))
LEGACY_GAME_MESSAGE_VERSIONS = frozenset((LEGACY_VERSION, MDPRO3_VERSION))
MSG_SELECT_CHAIN = 16
MSG_CONFIRM_CARDS = 31


class ProtocolError(ValueError):
    """The peer sent a malformed native frame."""


def packet(opcode: int, payload: bytes = b"") -> bytes:
    body = bytes((opcode,)) + bytes(payload)
    if len(body) > MAX_FRAME:
        raise ProtocolError("native frame is too large")
    return struct.pack("<H", len(body)) + body


async def read_frame(reader: asyncio.StreamReader) -> tuple[int, bytes, bytes]:
    header = await reader.readexactly(2)
    (length,) = struct.unpack("<H", header)
    if not 1 <= length <= MAX_FRAME:
        raise ProtocolError("invalid native frame length")
    body = await reader.readexactly(length)
    return body[0], body[1:], header + body


def encode_name(value: str, units: int = 20) -> bytes:
    chars = [ord(char) for char in value][: units - 1]
    chars += [0] * (units - len(chars))
    return struct.pack(f"<{units}H", *chars)


def decode_name(value: bytes) -> str:
    text = value[:40].decode("utf-16-le", errors="replace")
    return text.split("\0", 1)[0]


def join_password(payload: bytes) -> str:
    if len(payload) < 48:
        raise ProtocolError("JOIN_GAME payload is too short")
    text = payload[8:48].decode("utf-16-le", errors="strict")
    return text.split("\0", 1)[0]


def rewrite_join(payload: bytes, password: str) -> bytes:
    if len(payload) < 48:
        raise ProtocolError("JOIN_GAME payload is too short")
    if len(password) > 19 or "#" in password:
        raise ProtocolError("backend room password is not native-safe")
    return payload[:8] + encode_name(password) + payload[48:]


def client_version(payload: bytes) -> int:
    if len(payload) < 48:
        raise ProtocolError("JOIN_GAME payload is too short")
    return struct.unpack_from("<H", payload)[0]


def engine_join(payload: bytes, password: str) -> bytes:
    rewritten = rewrite_join(payload, password)
    if client_version(payload) in ENGINE_COMPATIBLE_VERSIONS:
        return struct.pack("<H", ENGINE_VERSION) + rewritten[2:]
    return rewritten


def legacy_game_message(payload: bytes) -> bytes:
    """Translate the two newer core message layouts used by the 1.036.2 host."""
    if not payload:
        return payload
    if payload[0] == MSG_CONFIRM_CARDS:
        if len(payload) < 4 or len(payload) != 4 + payload[3] * 7:
            raise ProtocolError("invalid modern MSG_CONFIRM_CARDS")
        return payload[:2] + payload[3:]
    if payload[0] == MSG_SELECT_CHAIN:
        if len(payload) < 12 or len(payload) != 12 + payload[2] * 14:
            raise ProtocolError("invalid modern MSG_SELECT_CHAIN")
        flags = {payload[offset + 1] for offset in range(12, len(payload), 14)}
        if len(flags) > 1:
            raise ProtocolError("MSG_SELECT_CHAIN flags cannot be represented by legacy client")
        converted = bytearray(payload[:4] + bytes((next(iter(flags), 0),)) + payload[4:12])
        for offset in range(12, len(payload), 14):
            converted.extend(payload[offset:offset + 1] + payload[offset + 2:offset + 14])
        return bytes(converted)
    return payload


def deck_payload(main: list[int], extra: list[int],
                 side: list[int]) -> bytes:
    """Build the native UPDATE_DECK payload.

    The protocol combines main and extra into ``mainc``; the engine splits
    extra-deck cards by their card types.
    """
    combined = list(main) + list(extra)
    cards = combined + list(side)
    if not 40 <= len(main) <= 60 or len(extra) > 15 or len(side) > 15:
        raise ProtocolError("fixed deck has invalid section counts")
    if any(type(code) is not int or not 0 < code <= 0xFFFFFFFF for code in cards):
        raise ProtocolError("fixed deck contains an invalid card code")
    return (
        struct.pack("<II", len(combined), len(side))
        + struct.pack(f"<{len(cards)}I", *cards)
    )


def parse_deck_payload(payload: bytes) -> tuple[list[int], list[int]]:
    if len(payload) < 8:
        raise ProtocolError("UPDATE_DECK payload is too short")
    main_count, side_count = struct.unpack_from("<II", payload)
    total = main_count + side_count
    if main_count > 75 or side_count > 15 or len(payload) != 8 + total * 4:
        raise ProtocolError("UPDATE_DECK payload has invalid counts")
    cards = list(struct.unpack_from(f"<{total}I", payload, 8))
    return cards[:main_count], cards[main_count:]


def deck_payload_sha256(payload: bytes) -> str:
    main, side = parse_deck_payload(payload)
    canonical = (
        f"main_extra:{','.join(map(str, main))}\n"
        f"side:{','.join(map(str, side))}"
    ).encode("ascii")
    return hashlib.sha256(canonical).hexdigest()


def win_from_game_message(payload: bytes) -> tuple[int, int] | None:
    """Return ``(engine_winner, reason)`` for a public MSG_WIN buffer."""
    if len(payload) >= 3 and payload[0] == MSG_WIN:
        return payload[1], payload[2]
    return None


async def close_writer(writer: asyncio.StreamWriter) -> None:
    writer.close()
    try:
        await writer.wait_closed()
    except (ConnectionError, OSError):
        pass
