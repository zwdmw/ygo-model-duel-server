"""Verify that two read-only web spectators receive the same live result."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import os
import sys
from typing import Any

from aiohttp import ClientSession, WSMsgType

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.tournament_native_e2e import request_json, room_view, run_smoke


async def collect(
    websocket,
    messages: list[dict[str, Any]],
    result_event: asyncio.Event,
) -> None:
    try:
        async for message in websocket:
            if message.type == WSMsgType.TEXT:
                value = json.loads(message.data)
                messages.append(value)
                active = value.get("active_match") or {}
                if active.get("regular_game_count", 0) >= 1:
                    result_event.set()
            elif message.type in (WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.ERROR):
                return
    except asyncio.CancelledError:
        raise


async def main_async(args: argparse.Namespace) -> int:
    admin_key = os.environ.get("TOURNAMENT_ADMIN_KEY", "")
    if not admin_key:
        raise SystemExit("TOURNAMENT_ADMIN_KEY is required")
    base = args.tournament_url.rstrip("/")
    repo = Path(__file__).resolve().parents[1]
    admin_headers = {"X-Tournament-Admin-Key": admin_key}

    created = await asyncio.to_thread(
        request_json,
        "POST",
        f"{base}/api/rooms",
        body={
            "name": "Spectator E2E",
            "players": ["Alice", "Bob", "Carol", "Dave"],
            "engine": "native-pvp",
        },
        headers=admin_headers,
    )
    room_id = created["id"]
    access = {
        entry["player_id"]: entry
        for entry in created["player_access"]
    }
    await asyncio.to_thread(
        request_json, "POST", f"{base}/api/rooms/{room_id}/start",
        headers=admin_headers,
    )
    p1, p2 = access["p-1"], access["p-2"]

    async with ClientSession() as client:
        websockets = [
            await client.ws_connect(f"{base}/ws/{room_id}"),
            await client.ws_connect(f"{base}/ws/{room_id}"),
        ]
        messages = [[], []]
        result_events = [asyncio.Event(), asyncio.Event()]
        collectors = [
            asyncio.create_task(collect(websockets[index], messages[index], result_events[index]))
            for index in range(2)
        ]
        await asyncio.to_thread(
            request_json,
            "POST",
            f"{base}/api/rooms/{room_id}/players/p-1/ready",
            body={"ready": True},
            headers={"X-Tournament-Player-Token": p1["token"]},
        )
        await asyncio.to_thread(
            request_json,
            "POST",
            f"{base}/api/rooms/{room_id}/players/p-2/ready",
            body={"ready": True},
            headers={"X-Tournament-Player-Token": p2["token"]},
        )
        p1_room = await asyncio.to_thread(room_view, base, room_id, p1["token"])
        p2_room = await asyncio.to_thread(room_view, base, room_id, p2["token"])
        p1_connection = p1_room["active_match"]["session"]["connection"]
        p2_connection = p2_room["active_match"]["session"]["connection"]
        smoke_code, smoke_output = await asyncio.to_thread(
            run_smoke,
            repo,
            args.database,
            p1_connection["port"],
            {"a": p1_connection["code"], "b": p2_connection["code"]},
            "a",
        )
        if smoke_code != 0:
            tail = smoke_output.strip().splitlines()[-1] if smoke_output.strip() else ""
            raise RuntimeError(f"native smoke failed: {tail}")
        await asyncio.wait_for(asyncio.gather(*(event.wait() for event in result_events)), 30)

        final_messages = [
            next(
                message for message in reversed(items)
                if (message.get("active_match") or {}).get("regular_game_count", 0) >= 1
            )
            for items in messages
        ]
        final_games = [
            (message["active_match"]["games"][-1])
            for message in final_messages
        ]
        if final_games[0].get("event_id") != final_games[1].get("event_id"):
            raise RuntimeError("spectators received different event ids")
        output = {
            "ok": True,
            "room_id": room_id,
            "spectator_message_counts": [len(items) for items in messages],
            "event_id": final_games[0]["event_id"],
            "winner": final_games[0]["winner"],
            "regular_game_count": final_messages[0]["active_match"]["regular_game_count"],
        }
        print(json.dumps(output, ensure_ascii=False), flush=True)
        for websocket in websockets:
            await websocket.close()
        for collector in collectors:
            collector.cancel()
        await asyncio.gather(*collectors, return_exceptions=True)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tournament-url", default="http://127.0.0.1:8787")
    parser.add_argument("--pvp-port", type=int, default=6010)
    parser.add_argument("--database", type=Path, required=True)
    return asyncio.run(main_async(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
