"""Verify that an existing finished native room plays its archived replay."""

from __future__ import annotations

import argparse
import json
import threading

from tools.native_spectator_e2e import NativeObserver


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--password", required=True)
    parser.add_argument("--timeout", type=float, default=45)
    args = parser.parse_args()

    observer = NativeObserver(
        args.host,
        args.port,
        args.password,
        args.timeout,
    )
    thread = threading.Thread(target=observer.run, name="native-replay-observer")
    thread.start()
    thread.join(args.timeout + 10)

    required = {
        "joined",
        "observer",
        "duel_start",
        "game_start",
        "field_update",
        "win",
        "duel_end",
    }
    passed = (
        not thread.is_alive()
        and not observer.error
        and required.issubset(observer.events)
        and observer.hand_players == {0, 1}
        and bool(observer.drawn_cards)
        and observer.field_updates > 0
    )
    print(json.dumps({
        "passed": passed,
        "events": observer.events,
        "visible_hand_players": sorted(observer.hand_players),
        "visible_hand_card_count": len(observer.drawn_cards),
        "field_update_count": observer.field_updates,
        "error": observer.error,
        "thread_alive": thread.is_alive(),
    }, ensure_ascii=False, indent=2))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
