"""Run native PvP games through the tournament HTTP workflow.

The script creates a fresh two-player series room, repeatedly obtains the
per-game native connection codes, runs the protocol smoke duel, and waits for
the signed callback to advance the tournament session. By default it drives
one mirrored BO2 followed by one two-game tiebreak block. Each BO2 swaps the
first player between its two games.

Required environment:

    TOURNAMENT_ADMIN_KEY

The PvP gateway receives the per-game player codes through child-process
environment variables, never through command-line arguments.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def request_json(
    method: str,
    url: str,
    *,
    body: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    raw = None
    request_headers = dict(headers or {})
    if body is not None:
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        request_headers["Content-Type"] = "application/json"
    request = Request(url, data=raw, headers=request_headers, method=method)
    try:
        with urlopen(request, timeout=15) as response:
            return json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError) as error:
        detail = error.read().decode("utf-8", errors="replace") if isinstance(error, HTTPError) else str(error)
        raise RuntimeError(f"{method} {url} failed: {detail}") from error


def room_view(base: str, room_id: str, token: str) -> dict[str, Any]:
    return request_json(
        "GET",
        f"{base}/api/rooms/{room_id}",
        headers={"X-Tournament-Player-Token": token},
    )


def ready(base: str, room_id: str, player_id: str, token: str) -> dict[str, Any]:
    return request_json(
        "POST",
        f"{base}/api/rooms/{room_id}/players/{player_id}/ready",
        body={"ready": True},
        headers={"X-Tournament-Player-Token": token},
    )


def match_view(room: dict[str, Any], match_id: str) -> dict[str, Any]:
    for match in room["matches"]:
        if match["id"] == match_id:
            return match
    raise RuntimeError(f"match {match_id} is missing from the room snapshot")


def wait_for_game(
    base: str,
    room_id: str,
    token: str,
    match_id: str,
    expected_count: int,
    expected_phase: str | None = None,
) -> dict[str, Any]:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        room = room_view(base, room_id, token)
        match = match_view(room, match_id)
        if (
            match["total_game_count"] >= expected_count
            and (expected_phase is None or match["phase"] == expected_phase)
        ):
            return room
        time.sleep(0.25)
    raise RuntimeError(f"timed out waiting for tournament game {expected_count}")


def run_smoke(
    repo: Path,
    database: Path,
    host: str,
    port: int,
    codes: dict[str, str],
    seat_zero: str,
    surrender: str,
) -> tuple[int, str]:
    environment = os.environ.copy()
    environment["NATIVE_PVP_A_CODE"] = codes["a"]
    environment["NATIVE_PVP_B_CODE"] = codes["b"]
    process = subprocess.run(
        [
            sys.executable,
            str(repo / "tools" / "native_pvp_smoke.py"),
            "--host",
            host,
            "--port",
            str(port),
            "--database",
            str(database),
            "--timeout",
            "45",
            "--seat-zero",
            seat_zero,
            "--surrender",
            surrender,
        ],
        cwd=repo,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=70,
    )
    return process.returncode, process.stdout


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tournament-url", default="http://127.0.0.1:8787")
    parser.add_argument("--pvp-host", default="127.0.0.1")
    parser.add_argument("--pvp-port", type=int, default=6009)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument(
        "--bo2-count",
        type=int,
        default=1,
        help="number of regular BO2s to run before the tiebreak",
    )
    parser.add_argument(
        "--games",
        type=int,
        default=None,
        help="optional maximum game count; useful for a short smoke run",
    )
    args = parser.parse_args()
    if not 1 <= args.bo2_count <= 100:
        raise SystemExit("--bo2-count must be between 1 and 100")
    regular_games = args.bo2_count * 2
    total_games = regular_games + 2
    if args.games is not None and not 1 <= args.games <= total_games:
        raise SystemExit(f"--games must be between 1 and {total_games}")
    games_to_run = args.games or total_games

    admin_key = os.environ.get("TOURNAMENT_ADMIN_KEY", "")
    if not admin_key:
        raise SystemExit("TOURNAMENT_ADMIN_KEY is required")
    base = args.tournament_url.rstrip("/")
    repo = Path(__file__).resolve().parents[1]
    admin_headers = {"X-Tournament-Admin-Key": admin_key}

    created = request_json(
        "POST",
        f"{base}/api/rooms",
        body={
            "name": "Native E2E",
            "players": ["Alice", "Bob"],
            "room_type": "series",
            "base_bo2_count": args.bo2_count,
            "seed_mode": "mirrored",
            "overtime_enabled": True,
            "auto_next_game": True,
            "engine": "native-pvp",
        },
        headers=admin_headers,
    )
    room_id = created["id"]
    access = {
        entry["player_id"]: entry
        for entry in created["player_access"]
    }
    request_json("POST", f"{base}/api/rooms/{room_id}/start", headers=admin_headers)
    p1, p2 = access["p-1"], access["p-2"]
    initial = room_view(base, room_id, p1["token"])
    match_id = initial["active_match_id"]
    if not match_id:
        raise RuntimeError("room did not activate its first match")
    results: list[dict[str, Any]] = []

    for game_number in range(1, games_to_run + 1):
        ready(base, room_id, "p-1", p1["token"])
        ready(base, room_id, "p-2", p2["token"])
        p1_room = room_view(base, room_id, p1["token"])
        p2_room = room_view(base, room_id, p2["token"])
        p1_connection = p1_room["active_match"]["session"]["connection"]
        p2_connection = p2_room["active_match"]["session"]["connection"]
        if game_number <= regular_games:
            expected = (
                {"a": 0, "b": 1}
                if game_number % 2 == 1
                else {"a": 1, "b": 0}
            )
        else:
            expected = (
                {"a": 0, "b": 1}
                if (game_number - regular_games) % 2 == 1
                else {"a": 1, "b": 0}
            )
        actual = {
            "a": p1_connection["seat"],
            "b": p2_connection["seat"],
        }
        if actual != expected:
            raise RuntimeError(
                f"game {game_number} seat plan mismatch: expected {expected}, got {actual}"
            )

        if game_number <= regular_games:
            # Split every regular BO2 so the first block reaches overtime.
            winner_side = "a" if game_number % 2 == 1 else "b"
        else:
            winner_side = "a"
        surrender = "b" if winner_side == "a" else "a"
        code, output = run_smoke(
            repo,
            args.database,
            args.pvp_host,
            args.pvp_port,
            {"a": p1_connection["code"], "b": p2_connection["code"]},
            "a" if expected["a"] == 0 else "b",
            surrender,
        )
        lines = output.strip().splitlines()
        tail = lines[-1] if lines else ""
        if code != 0:
            raise RuntimeError(f"game {game_number} smoke failed: {tail}")
        smoke = json.loads(tail)
        completed = wait_for_game(
            base,
            room_id,
            p1["token"],
            match_id,
            game_number,
            (
                "tiebreak" if game_number == 21
                else "finished" if game_number == 22
                else None
            ),
        )
        recorded_match = match_view(completed, match_id)
        recorded = recorded_match["games"][-1]
        if recorded["seat_map"] != expected:
            raise RuntimeError(
                f"game {game_number} recorded seat mismatch: {recorded['seat_map']}"
            )
        engine_seats = recorded.get("engine_seats")
        client_seats = {
            side: smoke["results"][side]["our_player"] for side in ("a", "b")
        }
        if engine_seats != client_seats:
            raise RuntimeError(
                f"game {game_number} engine seat audit mismatch: "
                f"{engine_seats} != {client_seats}"
            )
        engine_winner = smoke["results"]["a"]["winner"]
        if recorded.get("engine_winner") != engine_winner:
            raise RuntimeError(
                f"game {game_number} engine winner audit mismatch: "
                f"{recorded.get('engine_winner')} != {engine_winner}"
            )
        if recorded["winner"] != winner_side:
            raise RuntimeError(
                f"game {game_number} tournament winner mismatch: "
                f"{recorded['winner']} != {winner_side}"
            )
        seed = recorded.get("seed")
        if not isinstance(seed, list):
            raise RuntimeError(f"game {game_number} is missing its audit seed")
        if game_number > 1:
            previous = results[-1]
            if game_number % 2 == 0 and seed != previous["seed"]:
                raise RuntimeError(
                    f"game {game_number} did not mirror its pair seed"
                )
            if game_number % 2 == 1 and seed == previous["seed"]:
                raise RuntimeError(
                    f"game {game_number} unexpectedly reused the prior pair seed"
                )
        fixed_deck = recorded.get("fixed_deck") or {}
        if fixed_deck.get("name") != "SkyStriker":
            raise RuntimeError(
                f"game {game_number} did not record the SkyStriker fixed deck"
            )
        replacements = recorded.get("deck_replacements") or {}
        if any(
            (replacements.get(side) or {}).get("replacement_count", 0) < 1
            for side in ("a", "b")
        ):
            raise RuntimeError(
                f"game {game_number} is missing fixed-deck replacement audit"
            )
        results.append({
            "game": game_number,
            "seat_map": recorded["seat_map"],
            "engine_seats": engine_seats,
            "engine_winner": engine_winner,
            "winner": recorded["winner"],
            "seed": seed,
            "event_id": recorded["event_id"],
            "replay": recorded.get("replay"),
        })
        print(json.dumps(results[-1], ensure_ascii=False), flush=True)

    final_room = room_view(base, room_id, p1["token"])
    final_match = match_view(final_room, match_id)
    if games_to_run == total_games:
        if final_match["status"] != "finished" or final_match["winner"] != "p-1":
            raise RuntimeError(
                f"tiebreak did not finish the match: "
                f"status={final_match['status']} winner={final_match['winner']}"
            )
        if final_match["phase"] != "finished":
            raise RuntimeError("finished native match retained a non-finished phase")

    print(json.dumps({
        "ok": True,
        "room_id": room_id,
        "match_id": match_id,
        "games": results,
        "next_game": games_to_run + 1,
        "match_status": final_match["status"],
        "match_winner": final_match["winner"],
    }, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
