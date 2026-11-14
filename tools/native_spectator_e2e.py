"""Verify a native YGOPro observer can watch a real PvP duel.

The player credentials are created through the PvP HTTP API and kept in
process memory.  Both player seats are occupied before the observer joins,
matching how an MDPro3/YGOPro2 spectator enters an already running duel.
"""

from __future__ import annotations

import argparse
import atexit
import json
import os
from pathlib import Path
import socket
import struct
import sys
import threading
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from room.outbound.client import ExternalClient
from room.outbound.vendor.netduel import constants as C
from room.outbound.vendor.netduel import protocol as N
from room.outbound.vendor.netduel.cards import CardPool
from pvp.deck import load_ydk
from tools.native_pvp_smoke import FirstLegalPolicy


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
        raw = json.dumps(body).encode("utf-8")
        request_headers["Content-Type"] = "application/json"
    request = Request(url, data=raw, headers=request_headers, method=method)
    try:
        with urlopen(request, timeout=15) as response:
            return json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError) as error:
        detail = (
            error.read().decode("utf-8", errors="replace")
            if isinstance(error, HTTPError)
            else str(error)
        )
        raise RuntimeError(f"{method} {url} failed: {detail}") from error


class NativeObserver:
    def __init__(self, host: str, port: int, password: str,
                 timeout: float = 45) -> None:
        self.host = host
        self.port = port
        self.password = password
        self.timeout = timeout
        self.events: list[str] = []
        self.drawn_cards: list[int] = []
        self.hand_players: set[int] = set()
        self.field_updates = 0
        self.error = ""

    def run(self) -> None:
        stream: N.PacketStream | None = None
        try:
            sock = socket.create_connection((self.host, self.port), timeout=15)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            stream = N.PacketStream(sock)
            stream.send(N.CTOS.PLAYER_INFO, N.player_info("Native-Observer"))
            stream.send(
                N.CTOS.JOIN_GAME,
                N.join_game(password=self.password),
            )
            deadline = time.monotonic() + self.timeout
            while time.monotonic() < deadline:
                opcode, payload = stream.recv(
                    max(0.1, min(2.0, deadline - time.monotonic()))
                )
                if opcode == N.STOC.JOIN_GAME:
                    self.events.append("joined")
                elif opcode == N.STOC.TYPE_CHANGE and payload:
                    position = payload[0] & 0x0F
                    if position == N.NETPLAYER_TYPE_OBSERVER:
                        self.events.append("observer")
                elif opcode == N.STOC.DUEL_START:
                    self.events.append("duel_start")
                elif opcode == N.STOC.GAME_MSG and payload:
                    if payload[0] == C.MSG_START:
                        self.events.append("game_start")
                    elif payload[0] in (C.MSG_UPDATE_DATA, C.MSG_UPDATE_CARD):
                        self.field_updates += 1
                        if "field_update" not in self.events:
                            self.events.append("field_update")
                    elif payload[0] == C.MSG_DRAW and len(payload) >= 3:
                        count = payload[2]
                        if len(payload) >= 3 + count * 4:
                            cards = [
                                struct.unpack_from(
                                    "<I", payload, 3 + index * 4
                                )[0]
                                for index in range(count)
                            ]
                            self.drawn_cards.extend(
                                card & 0x7fffffff for card in cards
                            )
                            if any(cards):
                                self.events.append("visible_hand")
                                self.hand_players.add(payload[1])
                    elif payload[0] == C.MSG_WIN:
                        self.events.append("win")
                elif opcode == N.STOC.REPLAY:
                    self.events.append("replay")
                elif opcode == N.STOC.DUEL_END:
                    self.events.append("duel_end")
                    if "win" in self.events:
                        return
        except (ConnectionError, OSError, N.ProtocolError, socket.timeout) as error:
            self.error = f"{type(error).__name__}: {error}"
        finally:
            if stream is not None:
                try:
                    stream.send(N.CTOS.LEAVE_GAME)
                except OSError:
                    pass
                stream.close()


def run_player(
    client: ExternalClient,
    label: str,
    joined: dict[str, threading.Event],
    dueling: dict[str, threading.Event],
    states: dict[str, list[str]],
    results: dict[str, Any],
) -> None:
    def state_change(state: str, **_fields: Any) -> None:
        states[label].append(state)
        if state == "joining":
            joined[label].set()
        elif state == "dueling":
            dueling[label].set()

    client.state_change = state_change
    try:
        result = client.run()
        results[label] = {
            "outcome": client.outcome(),
            "winner": result.winner,
            "our_player": result.our_player,
            "error": result.error,
            "replay_received": client.replay_received,
            "states": states[label],
        }
    except BaseException as error:
        results[label] = {
            "outcome": "incomplete",
            "error": f"{type(error).__name__}: {error}",
            "states": states[label],
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pvp-url", default="http://127.0.0.1:8790")
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=45)
    parser.add_argument("--seat-zero", choices=("a", "b"), default="a")
    parser.add_argument("--surrender", choices=("a", "b"), default="b")
    args = parser.parse_args()

    internal_key = os.environ.get("PVP_INTERNAL_KEY", "")
    if not internal_key:
        raise SystemExit("PVP_INTERNAL_KEY is required")
    fixed_deck = load_ydk(
        Path(__file__).resolve().parents[1] / "decks" / "SkyStriker.ydk"
    )
    card_pool = CardPool(args.database)
    pvp_url = args.pvp_url.rstrip("/")
    created = request_json(
        "POST",
        f"{pvp_url}/api/sessions",
        body={
            "room_id": "native-spectator-e2e",
            "match_id": "native-spectator-e2e",
            "game_number": 1,
            "format": "nc",
            "seat_plan": {"a": 0, "b": 1}
            if args.seat_zero == "a" else {"a": 1, "b": 0},
            "players": {
                "a": {"name": "Spectator-A"},
                "b": {"name": "Spectator-B"},
            },
        },
        headers={
            "X-PVP-Internal-Key": internal_key,
            "X-PVP-Public-Host": "127.0.0.1",
        },
    )
    native = created["native"]
    session_id = created["session"]["id"]
    codes = native["player_codes"]

    def close_test_session() -> None:
        try:
            request_json(
                "POST",
                f"{pvp_url}/api/sessions/{session_id}/close",
                headers={"X-PVP-Internal-Key": internal_key},
            )
        except Exception:
            print("warning: failed to close native spectator test session",
                  file=sys.stderr)

    atexit.register(close_test_session)
    clients = {
        "a": ExternalClient(
            "127.0.0.1", native["port"], "Spectator-A",
            list(fixed_deck.main), list(fixed_deck.extra),
            FirstLegalPolicy(), password=codes["a"], preference="random",
            timeout=args.timeout, seed=11, card_pool=card_pool,
        ),
        "b": ExternalClient(
            "127.0.0.1", native["port"], "Spectator-B",
            list(fixed_deck.main), list(fixed_deck.extra),
            FirstLegalPolicy(), password=codes["b"], preference="random",
            timeout=args.timeout, seed=29, card_pool=card_pool,
        ),
    }
    joined = {"a": threading.Event(), "b": threading.Event()}
    dueling = {"a": threading.Event(), "b": threading.Event()}
    states = {"a": [], "b": []}
    results: dict[str, Any] = {}
    observer = NativeObserver(
        "127.0.0.1", native["port"], native["spectator_code"], args.timeout
    )
    observer_thread = threading.Thread(target=observer.run, name="native-observer")
    client_threads: list[threading.Thread] = []

    first = args.seat_zero
    second = "b" if first == "a" else "a"

    def start_player(label: str) -> None:
        thread = threading.Thread(
            target=run_player,
            args=(clients[label], label, joined, dueling, states, results),
            name=f"native-player-{label}",
        )
        client_threads.append(thread)
        thread.start()

    start_player(first)
    if not joined[first].wait(args.timeout):
        raise RuntimeError("seat-zero player did not join")
    start_player(second)

    deadline = time.monotonic() + args.timeout
    while time.monotonic() < deadline and not all(
        event.is_set() for event in dueling.values()
    ):
        time.sleep(0.1)
    if not all(event.is_set() for event in dueling.values()):
        clients["a"].surrender()
        clients["b"].surrender()
        for thread in client_threads:
            thread.join(5)
        observer_thread.join(5)
        print(json.dumps({
            "ok": False,
            "error": "native duel did not start",
            "player_results": results,
            "player_states": states,
            "observer_events": observer.events,
            "observer_error": observer.error,
        }, ensure_ascii=False), flush=True)
        return 1
    observer_thread.start()
    observer_deadline = time.monotonic() + args.timeout
    while time.monotonic() < observer_deadline and not (
        {"joined", "observer", "duel_start", "game_start", "visible_hand"}
        .issubset(set(observer.events))
        and len(observer.hand_players) == 2
    ):
        time.sleep(0.1)
    clients[args.surrender].surrender()

    for thread in client_threads:
        thread.join(args.timeout + 10)
    observer_thread.join(args.timeout + 10)

    settled = all(
        item.get("outcome") in {"win", "loss", "draw"}
        for item in results.values()
    )
    required = {"joined", "observer", "duel_start", "game_start", "win", "duel_end"}
    required.add("visible_hand")
    observed = required.issubset(set(observer.events))
    hand_matches_fixed_deck = (
        bool(observer.drawn_cards)
        and set(observer.drawn_cards).issubset(set(fixed_deck.main))
    )
    output = {
        "ok": (
            settled and observed and len(observer.hand_players) == 2
            and hand_matches_fixed_deck
            and not observer.error
        ),
        "player_results": results,
        "observer_events": observer.events,
        "observer_hand_card_count": len(observer.drawn_cards),
        "observer_hand_players": sorted(observer.hand_players),
        "observer_hand_matches_fixed_deck": hand_matches_fixed_deck,
        "observer_error": observer.error,
    }
    print(json.dumps(output, ensure_ascii=False), flush=True)
    return 0 if output["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
