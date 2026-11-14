"""Run two independent native BO2 rooms concurrently on one PvP gateway."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time

from tools.tournament_native_e2e import request_json, room_view


def prepare(base: str, key: str, label: str) -> dict:
    room = request_json("POST", base + "/api/rooms", body={
        "name": f"Parallel acceptance {label}",
        "players": [f"Smoke-{label}-A", f"Smoke-{label}-B"],
        "room_type": "series",
        "base_bo2_count": 1,
        "seed_mode": "mirrored",
        "overtime_enabled": False,
        "auto_next_game": True,
        "engine": "native-pvp",
    }, headers={"X-Tournament-Admin-Key": key})
    room_id = room["id"]
    access = {item["player_id"]: item["token"] for item in room["player_access"]}
    request_json("POST", base + f"/api/rooms/{room_id}/start",
                 headers={"X-Tournament-Admin-Key": key})
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        first = room_view(base, room_id, access["p-1"])
        second = room_view(base, room_id, access["p-2"])
        a = first["active_match"]["session"].get("connection")
        b = second["active_match"]["session"].get("connection")
        if a and b:
            return {
                "id": room_id, "token": access["p-1"],
                "session_id": first["active_match"]["session"]["pvp_session_id"],
                "a": a, "b": b,
            }
        time.sleep(0.2)
    raise RuntimeError(f"native session for {room_id} was not provisioned")


def main() -> int:
    key = os.environ["TOURNAMENT_ADMIN_KEY"]
    base = os.environ.get("TOURNAMENT_URL", "http://127.0.0.1:8787").rstrip("/")
    repo = Path(__file__).resolve().parents[1]
    rooms = [prepare(base, key, label) for label in ("One", "Two")]
    first, second = rooms
    if first["session_id"] == second["session_id"]:
        raise RuntimeError("rooms share a native session")
    for room in rooms:
        if room["a"]["port"] != room["b"]["port"]:
            raise RuntimeError("players in a room have different gateway ports")
    if first["a"]["port"] != second["a"]["port"]:
        raise RuntimeError("test rooms are not sharing the gateway port")
    codes = {
        room["a"]["code"] for room in rooms
    } | {room["b"]["code"] for room in rooms}
    if len(codes) != 4:
        raise RuntimeError("test rooms share a player password")

    gateway_port = int(os.environ.get("PVP_PORT", first["a"]["port"]))
    processes = []
    try:
        for room in rooms:
            environment = os.environ.copy()
            environment["NATIVE_PVP_A_CODE"] = room["a"]["code"]
            environment["NATIVE_PVP_B_CODE"] = room["b"]["code"]
            processes.append(subprocess.Popen(
                [
                    sys.executable, str(repo / "tools" / "native_pvp_smoke.py"),
                    "--host", os.environ.get("PVP_HOST", "127.0.0.1"),
                    "--port", str(gateway_port),
                    "--database", os.environ["CARDS_CDB"],
                    "--seat-zero", "a", "--surrender", "b",
                    "--series-games", "2", "--timeout", "60",
                ],
                env=environment, cwd=repo, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
            ))
        overlapping = all(process.poll() is None for process in processes)
        for room, process in zip(rooms, processes):
            stdout, stderr = process.communicate(timeout=160)
            lines = stdout.strip().splitlines()
            smoke = json.loads(lines[-1]) if lines else {}
            if process.returncode or not smoke.get("ok"):
                transitions = [
                    line for line in lines[:-1]
                    if '"state"' in line
                ]
                raise RuntimeError(
                    f"smoke failed for {room['id']}: {smoke or stderr[-500:]}; "
                    f"transitions={transitions[-18:]}"
                )
            room["client_games"] = {
                side: value["games"] for side, value in smoke["results"].items()
            }
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
                try:
                    process.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.communicate()

    result = []
    game_times = []
    for room in rooms:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            view = room_view(base, room["id"], room["token"])
            if view["status"] == "finished":
                break
            time.sleep(0.2)
        else:
            raise RuntimeError(f"room {room['id']} did not finish")
        match = view["matches"][0]
        games = match["games"]
        if (
            len(games) != 2 or games[0]["seed"] != games[1]["seed"]
            or games[0]["seat_map"] != {"a": 0, "b": 1}
            or games[1]["seat_map"] != {"a": 1, "b": 0}
            or any(game.get("engine_seats") != game["seat_map"] for game in games)
            or [game.get("winner") for game in games] != ["a", "a"]
            or match["points"] != {"a": 3, "b": 0}
            or room["client_games"] != {"a": 2, "b": 2}
        ):
            raise RuntimeError(f"room {room['id']} game audit or score mismatch")
        game_times.append([game["recorded_at"] for game in games])
        result.append({
            "room_id": room["id"], "session_id": room["session_id"],
            "games": len(games), "points": match["points"],
            "client_games": room["client_games"],
        })
    if not overlapping:
        raise RuntimeError("the two client processes did not overlap")
    if not (game_times[0][0] < game_times[1][1]
            and game_times[1][0] < game_times[0][1]):
        raise RuntimeError("the two rooms did not overlap during their BO2 games")
    print(json.dumps({
        "ok": True, "overlapping_clients": overlapping,
        "interleaved_games": True,
        "gateway_port": first["a"]["port"], "rooms": result,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
