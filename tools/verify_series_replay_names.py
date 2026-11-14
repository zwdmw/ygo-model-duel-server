"""Read-only TCP acceptance check for a finished two-player spectator series."""

from __future__ import annotations

import argparse
import base64
import gzip
import hashlib
import json
from pathlib import Path
import socket
import struct
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pvp import protocol as P


def read_full(stream: socket.socket, length: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < length:
        value = stream.recv(length - len(chunks))
        if not value:
            raise RuntimeError("spectator stream ended before DUEL_END")
        chunks.extend(value)
    return bytes(chunks)


def verify(session_path: Path, host: str, port: int, version: int) -> dict:
    session = json.loads(session_path.read_text(encoding="utf-8"))
    if session.get("series_finished") is not True:
        raise RuntimeError("acceptance check requires a finished series")
    archive_path = Path(session["series_replay"]["path"])
    archive = archive_path.read_bytes()
    digest = hashlib.sha256(archive).hexdigest()
    if digest != session["series_replay"]["sha256"]:
        raise RuntimeError("stored series archive checksum mismatch")
    envelope = json.loads(gzip.decompress(archive))
    duels = envelope["duels"]
    first = duels[0]
    first_names = [player["name"] for player in first.get("players", envelope["meta"]["players"])]
    first_payloads = [base64.b64decode(frame)[3:] for frame in first["spectatorFrames"]]
    first_type = next(payload[1] for payload in first_payloads if payload[0] == P.MSG_START)
    if ("players" in first or envelope["meta"].get("recoveredFromYrp")) and first_type == 0x11:
        first_names.reverse()

    expected = []
    core_names = []
    for duel in duels:
        names = [player["name"] for player in duel.get("players", envelope["meta"]["players"])]
        core_names.append(names)
        for encoded in duel["spectatorFrames"]:
            payload = base64.b64decode(encoded)[3:]
            if payload[0] == P.MSG_START and "players" in duel:
                if names == first_names:
                    target_type = 0x10
                elif names == first_names[::-1]:
                    target_type = 0x11
                else:
                    raise RuntimeError("archive changes player identities")
                payload = payload[:1] + bytes((target_type,)) + payload[2:]
            if version in P.LEGACY_GAME_MESSAGE_VERSIONS:
                payload = P.legacy_game_message(payload)
            expected.append(payload)

    labels = {}
    received = []
    game_number = 0
    joined = False
    observer = False
    with socket.create_connection((host, port), timeout=20) as stream:
        stream.settimeout(20)
        stream.sendall(P.packet(P.PLAYER_INFO, P.encode_name("Name-Acceptance")))
        join = struct.pack("<H2xI", version, 0) + P.encode_name(session["credentials"]["spectator"])
        stream.sendall(P.packet(P.JOIN_GAME, join))
        while True:
            length = struct.unpack("<H", read_full(stream, 2))[0]
            if not 1 <= length <= P.MAX_FRAME:
                raise RuntimeError("invalid spectator frame length")
            body = read_full(stream, length)
            opcode, payload = body[0], body[1:]
            if opcode == 0x12:
                joined = True
            elif opcode == 0x13:
                observer = bool(payload and (payload[0] & 0x0F) == 7)
            elif opcode == 0x20:
                labels[payload[40] & 3] = P.decode_name(payload)
            elif opcode == P.GAME_MSG:
                received.append(payload)
                if payload[0] == P.MSG_START:
                    if game_number >= len(duels):
                        raise RuntimeError("spectator stream has an extra game")
                    if [labels.get(0), labels.get(1)] != first_names:
                        raise RuntimeError(f"wrong spectator names in game {game_number + 1}")
                    if "players" in duels[game_number]:
                        actual = core_names[game_number]
                        if payload[1] == 0x11:
                            actual = actual[::-1]
                        if actual != first_names:
                            raise RuntimeError(f"wrong spectator orientation in game {game_number + 1}")
                    game_number += 1
            elif opcode == P.DUEL_END:
                break
    if not joined or not observer:
        raise RuntimeError("gateway did not admit an observer")
    if game_number != len(duels) or received != expected:
        raise RuntimeError("spectator duel stream does not match the recorded archive")
    if hashlib.sha256(archive_path.read_bytes()).hexdigest() != digest:
        raise RuntimeError("acceptance check changed the archive")
    return {
        "ok": True,
        "version": f"0x{version:04x}",
        "session_id": session["id"],
        "bottom": first_names[0],
        "top": first_names[1],
        "games": game_number,
        "game_frames": len(received),
        "archive_unchanged": True,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("session", type=Path)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=6009)
    parser.add_argument("--version", type=lambda value: int(value, 0), action="append")
    args = parser.parse_args()
    for version in args.version or (P.MDPRO3_VERSION, P.LEGACY_VERSION, P.ENGINE_VERSION):
        print(json.dumps(verify(args.session, args.host, args.port, version), ensure_ascii=False))


if __name__ == "__main__":
    main()
