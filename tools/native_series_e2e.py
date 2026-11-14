"""Verify a two-game native series without reconnecting either client."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time

from tools.tournament_native_e2e import request_json, room_view


def natural_win_reason(value: object) -> bool:
    return value in (1, 2) or (
        isinstance(value, int)
        and not isinstance(value, bool)
        and value >= 0x10
    )


def main() -> int:
    key = os.environ.get("TOURNAMENT_ADMIN_KEY", "")
    admin_headers = {"X-Tournament-Admin-Key": key} if key else {}
    base = os.environ.get("TOURNAMENT_URL", "http://127.0.0.1:8787").rstrip("/")
    repo = Path(__file__).resolve().parents[1]
    overtime = os.environ.get("TEST_OVERTIME") == "1"
    natural_mirror = os.environ.get("TEST_NATURAL_MIRROR") == "1"
    natural_result = (
        os.environ.get("TEST_NATURAL_RESULT") == "1"
        or natural_mirror
    )
    created = request_json("POST", base + "/api/rooms", body={
        "name": "Continuous series acceptance",
        "players": ["Smoke-A", "Smoke-B"],
        "room_type": "series",
        "base_bo2_count": 1,
        "seed_mode": "mirrored",
        "overtime_enabled": overtime,
        "auto_next_game": True,
        "engine": "native-pvp",
    }, headers=admin_headers)
    room_id = created["id"]
    access = {item["player_id"]: item["token"] for item in created["player_access"]}
    request_json("POST", base + f"/api/rooms/{room_id}/start",
                 headers=admin_headers)
    deadline = time.monotonic() + 30
    while True:
        first = room_view(base, room_id, access["p-1"])
        second = room_view(base, room_id, access["p-2"])
        a = first["active_match"]["session"].get("connection")
        b = second["active_match"]["session"].get("connection")
        if a and b:
            break
        if time.monotonic() >= deadline:
            raise RuntimeError("native session was not provisioned")
        time.sleep(0.2)

    environment = os.environ.copy()
    environment["NATIVE_PVP_A_CODE"] = a["code"]
    environment["NATIVE_PVP_B_CODE"] = b["code"]
    smoke_timeout = 300 if natural_result else 60
    command = [
        sys.executable, str(repo / "tools" / "native_pvp_smoke.py"),
        "--host", os.environ.get("PVP_HOST", "127.0.0.1"),
        "--port", os.environ.get("PVP_PORT", str(a["port"])),
        "--database", os.environ["CARDS_CDB"],
        "--seat-zero", "a", "--surrender", "b",
        "--series-games", "4" if overtime else "2",
        "--timeout", str(smoke_timeout),
        "--client-version", os.environ.get("NATIVE_CLIENT_VERSION", "modern"),
    ]
    connect_first = os.environ.get("CONNECT_FIRST")
    if connect_first:
        command.extend(("--connect-first", connect_first))
    stale_leave_side = os.environ.get("TEST_STALE_LEAVE_SIDE")
    if stale_leave_side:
        command.extend((
            "--stale-leave-side", stale_leave_side,
            "--stale-leave-game", "1",
            "--stale-leave-delay", "0.9",
        ))
    if overtime:
        command.extend(("--surrender-plan", "b,a,b,b"))
    elif natural_mirror:
        command.extend(("--surrender-plan", "natural,natural"))
    elif natural_result:
        command.extend(("--surrender-plan", "natural,b"))
    run = subprocess.run(
        command, env=environment, cwd=repo, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
        timeout=700 if natural_result else 160,
    )
    lines = run.stdout.strip().splitlines()
    smoke = json.loads(lines[-1]) if lines else {}
    if run.returncode or not smoke.get("ok"):
        raise RuntimeError(f"two-game smoke failed: {smoke or run.stderr[-500:]}")
    if connect_first == "b":
        positions = smoke["results"]["b"].get("lobby_positions", [])
        if positions[:2] != [0, 1]:
            raise RuntimeError(
                "seat-1-first compatibility failed: expected temporary seat 0 "
                f"followed by planned seat 1, got {positions}"
            )
    deadline = time.monotonic() + 15
    while True:
        room = room_view(base, room_id, access["p-1"])
        if room["status"] == "finished":
            break
        if time.monotonic() >= deadline:
            raise RuntimeError("tournament did not finish")
        time.sleep(0.2)
    match = room["matches"][0]
    games = match["games"]
    expected_winners = ["a", "b", "a", "a"] if overtime else ["a", "a"]
    expected_points = {"a": 1, "b": 1} if overtime else {"a": 3, "b": 0}
    if natural_mirror:
        first_winner = games[0].get("winner") if games else None
        if first_winner not in ("a", "b"):
            raise RuntimeError("natural mirror game did not produce a decisive engine result")
        expected_winners = [
            first_winner,
            "b" if first_winner == "a" else "a",
        ]
        expected_points = {"a": 1, "b": 1}
    elif natural_result:
        first_winner = games[0].get("winner") if games else None
        if first_winner not in ("a", "b"):
            raise RuntimeError("natural game did not produce a decisive engine result")
        expected_winners = [first_winner, "a"]
        expected_points = (
            {"a": 3, "b": 0}
            if first_winner == "a"
            else {"a": 1, "b": 1}
        )
    if (len(games) != (4 if overtime else 2)
        or games[0]["seed"] != games[1]["seed"]
        or games[0]["seat_map"] != {"a": 0, "b": 1}
        or games[1]["seat_map"] != {"a": 1, "b": 0}
        or (overtime and (games[2]["seat_map"] != {"a": 0, "b": 1}
                          or games[3]["seat_map"] != {"a": 1, "b": 0}
                          or games[2]["seed"] != games[3]["seed"]))
        or any(game.get("engine_seats") != game["seat_map"] for game in games)
        or [game.get("winner") for game in games] != expected_winners
        or match["points"] != expected_points
        or (natural_result and not natural_win_reason(
            games[0].get("result_reason")
        ))
        or (natural_mirror and not natural_win_reason(
            games[1].get("result_reason")
        ))):
        raise RuntimeError("series audit or score mismatch")
    print(json.dumps({
        "ok": True, "room_id": room_id, "games": len(games),
        "points": match["points"],
        "natural_result": natural_result,
        "natural_mirror": natural_mirror,
        "first_result_reason": games[0].get("result_reason"),
        "second_result_reason": games[1].get("result_reason"),
        "client_games": {side: value["games"]
                         for side, value in smoke["results"].items()},
        "stale_leaves_sent": {
            side: value.get("stale_leaves_sent", 0)
            for side, value in smoke["results"].items()
        },
        "lobby_positions": {side: value.get("lobby_positions", [])
                            for side, value in smoke["results"].items()},
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
