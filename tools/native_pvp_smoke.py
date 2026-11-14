"""Drive two native YGOPro clients through one PvP gateway session.

This is a protocol smoke test, not a replacement for MDPro3/YGOPro2.  It
uses the same client-side wire implementation already used by the outbound
room and deliberately surrenders after the engine starts the duel.  That
keeps the acceptance test focused on admission, deck submission, authoritative
MSG_WIN, replay delivery, and the tournament callback.

Credentials are read from the environment so they do not appear in the
command line or in the test output:

    NATIVE_PVP_A_CODE=...
    NATIVE_PVP_B_CODE=...
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
from pathlib import Path
import sys
import threading
import time
from typing import Any

# Make `python tools/native_pvp_smoke.py` behave like `python -m ...`.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from room.outbound.client import ExternalClient
from room.outbound.vendor.netduel.cards import CardPool
from room.outbound.vendor.netduel import protocol as NativeProtocol


TYPE_MONSTER = 0x1
TYPE_NORMAL = 0x10
TYPE_EXTRA = 0x40 | 0x2000 | 0x8000 | 0x4000000
TYPE_TOKEN = 0x4000


class FirstLegalPolicy:
    """Choose the first legal option; enough for a surrender smoke test."""

    def bind(self, _client: ExternalClient) -> None:
        return

    def observe_game_message(self, _msg: int, _body: bytes) -> None:
        return

    def choose(self, _state: Any) -> int:
        return 0

    def record(self, *_args: Any) -> None:
        return


class SeriesSmokeClient(ExternalClient):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.lobby_admitted = threading.Event()
        self.lobby_positions: list[int] = []
        self.stale_leave_after_game = 0
        self.stale_leave_delay = 0.9
        self.stale_leaves_sent = 0

    def _handle(self, op, payload):
        games_before = len(self.games)
        if op == NativeProtocol.STOC.TYPE_CHANGE and payload:
            position = payload[0] & 0xF
            if position in (0, 1):
                self.lobby_positions.append(position)
                self.lobby_admitted.set()
        # Opt into the existing Match packet handling without account login.
        if op == 0x12:
            self.auth_mode = "duelchronicle"
            try:
                result = super()._handle(op, payload)
            finally:
                self.auth_mode = "none"
        else:
            result = super()._handle(op, payload)
        if (
            len(self.games) > games_before
            and len(self.games) == self.stale_leave_after_game
        ):
            threading.Thread(
                target=self._send_stale_leave,
                name=f"stale-leave-{self.name}",
                daemon=True,
            ).start()
        return result

    def _send_stale_leave(self) -> None:
        time.sleep(self.stale_leave_delay)
        stream = self.stream
        if stream is None:
            return
        try:
            stream.send(NativeProtocol.CTOS.LEAVE_GAME)
            self.stale_leaves_sent += 1
        except OSError:
            return


class Mdpro3SmokeClient(SeriesSmokeClient):
    """Send a 0x1361 JOIN while parsing its older duel-message layouts."""

    def connect(self) -> None:
        super().connect()
        # The gateway translates the two 0x1362 core layouts back to the
        # 0x1361 form. ExternalClient's existing 0x133E parser consumes that
        # same wire shape after the JOIN has already been sent.
        self.version = 0x133E


def smoke_deck(database: Path) -> list[int]:
    """Return 40 distinct normal monsters, avoiding banlist-sensitive cards."""
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        rows = connection.execute(
            "SELECT id FROM datas "
            "WHERE (type & ?) != 0 AND (type & ?) != 0 "
            "AND (type & ?) = 0 AND (type & ?) = 0 "
            "ORDER BY id",
            (TYPE_MONSTER, TYPE_NORMAL, TYPE_EXTRA, TYPE_TOKEN),
        ).fetchall()
    finally:
        connection.close()
    cards = [int(row[0]) for row in rows]
    if len(cards) < 40:
        raise RuntimeError(f"database contains only {len(cards)} usable normal monsters")
    return cards[:40]


def run_client(
    client: ExternalClient,
    started: threading.Event,
    label: str,
    results: dict[str, Any],
) -> None:
    def state_change(state: str, **fields: Any) -> None:
        if state == "dueling":
            started.set()
        print(json.dumps({"client": label, "state": state, **fields}, ensure_ascii=False),
              flush=True)

    client.state_change = state_change
    try:
        result = client.run()
        results[label] = {
            "outcome": client.outcome(),
            "winner": result.winner,
            "our_player": result.our_player,
            "win_reason": result.win_reason,
            "error": result.error,
            "replay_received": client.replay_received,
            "replay_errors": list(client.replay_errors),
            "games": len(client.games),
            "lobby_positions": list(getattr(client, "lobby_positions", [])),
            "stale_leaves_sent": getattr(client, "stale_leaves_sent", 0),
        }
    except BaseException as error:
        results[label] = {
            "outcome": "incomplete",
            "error": f"{type(error).__name__}: {error}",
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=45)
    parser.add_argument("--seat-zero", choices=("a", "b"), default="a")
    parser.add_argument("--connect-first", choices=("a", "b"))
    parser.add_argument("--first-alone-seconds", type=float, default=1.0)
    parser.add_argument("--surrender", choices=("a", "b"), default="b")
    parser.add_argument("--series-games", type=int, default=1)
    parser.add_argument("--surrender-plan", default="")
    parser.add_argument(
        "--client-version",
        choices=("modern", "mdpro3"),
        default="modern",
    )
    parser.add_argument("--stale-leave-side", choices=("a", "b"))
    parser.add_argument("--stale-leave-game", type=int, default=1)
    parser.add_argument("--stale-leave-delay", type=float, default=0.9)
    args = parser.parse_args()
    if not 1 <= args.series_games <= 5:
        raise SystemExit("--series-games must be between 1 and 5")
    if not 0 <= args.first_alone_seconds <= 30:
        raise SystemExit("--first-alone-seconds must be between 0 and 30")
    if not 1 <= args.stale_leave_game <= args.series_games:
        raise SystemExit("--stale-leave-game must be within the requested series")
    if not 0 <= args.stale_leave_delay <= 30:
        raise SystemExit("--stale-leave-delay must be between 0 and 30")
    surrender_plan = (
        args.surrender_plan.split(",")
        if args.surrender_plan else [args.surrender] * args.series_games
    )
    if len(surrender_plan) != args.series_games or any(
        side not in ("a", "b", "natural") for side in surrender_plan
    ):
        raise SystemExit(
            "--surrender-plan must specify a, b, or natural for each game"
        )

    codes = {
        "a": os.environ.get("NATIVE_PVP_A_CODE", ""),
        "b": os.environ.get("NATIVE_PVP_B_CODE", ""),
    }
    if not all(codes.values()):
        raise SystemExit("NATIVE_PVP_A_CODE and NATIVE_PVP_B_CODE are required")

    deck = smoke_deck(args.database)
    card_pool = CardPool(args.database)
    client_type = (
        Mdpro3SmokeClient
        if args.client_version == "mdpro3"
        else SeriesSmokeClient
    )
    client_version = (
        0x1361
        if args.client_version == "mdpro3"
        else NativeProtocol.PRO_VERSION
    )
    clients = {
        "a": client_type(
            args.host, args.port, "Smoke-A", deck, [], FirstLegalPolicy(),
            password=codes["a"], preference="random", card_pool=card_pool,
            timeout=args.timeout, max_options=128, seed=11,
            version=client_version,
        ),
        "b": client_type(
            args.host, args.port, "Smoke-B", deck, [], FirstLegalPolicy(),
            password=codes["b"], preference="random", card_pool=card_pool,
            timeout=args.timeout, max_options=128, seed=29,
            version=client_version,
        ),
    }
    if args.stale_leave_side:
        stale_client = clients[args.stale_leave_side]
        stale_client.stale_leave_after_game = args.stale_leave_game
        stale_client.stale_leave_delay = args.stale_leave_delay
    started = {"a": threading.Event(), "b": threading.Event()}
    results: dict[str, Any] = {}
    threads: list[threading.Thread] = []

    def start_client(label: str) -> None:
        thread = threading.Thread(
            target=run_client,
            args=(clients[label], started[label], label, results),
            name=f"native-pvp-{label}",
        )
        threads.append(thread)
        thread.start()

    # Connection order is independent from the planned physical seats. Keeping
    # this selectable lets the smoke test prove that seat 1 can create the
    # native room before seat 0 connects.
    first_label = args.connect_first or args.seat_zero
    second_label = "b" if first_label == "a" else "a"
    start_client(first_label)
    if not clients[first_label].lobby_admitted.wait(args.timeout):
        clients[first_label].surrender()
        for thread in threads:
            thread.join(5)
        print(json.dumps({"ok": False, "error": "first client did not enter lobby",
                          "results": results}), flush=True)
        return 2
    time.sleep(args.first_alone_seconds)
    start_client(second_label)

    deadline = time.monotonic() + args.timeout
    while time.monotonic() < deadline and not all(
        event.is_set() for event in started.values()
    ):
        time.sleep(0.1)
    if not all(event.is_set() for event in started.values()):
        clients["a"].surrender()
        clients["b"].surrender()
        for thread in threads:
            thread.join(5)
        print(json.dumps({"ok": False, "error": "duel did not start", "results": results}),
              flush=True)
        return 2

    # A plan entry can surrender one side or leave both clients playing until
    # ocgcore reports a natural result.
    for number in range(1, args.series_games + 1):
        deadline = time.monotonic() + args.timeout
        while time.monotonic() < deadline and not all(
            len(client.games) >= number - 1 and not client.terminal_seen
            for client in clients.values()
        ):
            time.sleep(0.1)
        if time.monotonic() >= deadline:
            break
        surrender_side = surrender_plan[number - 1]
        if surrender_side != "natural":
            time.sleep(0.5)
            clients[surrender_side].surrender()
        deadline = time.monotonic() + args.timeout
        while time.monotonic() < deadline and not all(
            len(client.games) >= number for client in clients.values()
        ):
            time.sleep(0.1)
    for thread in threads:
        thread.join(args.timeout + 5)

    settled = all(
        item.get("outcome") in {"win", "loss", "draw"}
        and item.get("games") == args.series_games
        for item in results.values()
    )
    output = {
        "ok": settled,
        "client_version": args.client_version,
        "results": results,
    }
    print(json.dumps(output, ensure_ascii=False), flush=True)
    return 0 if settled else 1


if __name__ == "__main__":
    raise SystemExit(main())
