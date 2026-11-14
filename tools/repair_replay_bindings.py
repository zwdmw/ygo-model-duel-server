"""Audit and repair finished-session YRP associations using authoritative EVRP."""

from __future__ import annotations

import argparse
import copy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pvp.replay import yrp2_identity
from pvp.server import PvpStore, atomic_bytes, atomic_json, now


def repair_candidate(
    store: PvpStore, session: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if session.get("status") != "finished" or not session.get("series_finished"):
        raise ValueError("only finished sessions may be repaired")
    candidate = copy.deepcopy(session)
    _metadata, envelope = store._playable_series_replay(session)
    indexed = store._indexed_evrp_duels(
        session, envelope["duels"], store.replay_entries(session)
    )
    pool = []
    for entry in session.get("series_replays", []) + session.get("replay_conflicts", []):
        path = Path(entry["path"]).resolve()
        if path.parent != store.replay_root.resolve():
            raise ValueError("replay candidate is outside the replay directory")
        payload = path.read_bytes()
        if (
            len(payload) != entry["bytes"]
            or hashlib.sha256(payload).hexdigest() != entry["sha256"].lower()
        ):
            raise ValueError("replay candidate integrity failed")
        pool.append((entry, yrp2_identity(payload)))
    changes = []
    by_number = {entry["ordinal"]: entry for entry in candidate["series_replays"]}
    for ordinal, duel, _plan, (_raw_ordinal, _raw_path, original) in indexed:
        started_at = store._replay_time(duel.get("startTime"))
        if started_at is None:
            raise ValueError("authoritative EVRP start time is missing")
        matching = {
            entry["sha256"]: entry
            for entry, identity in pool
            if identity is not None
            and identity["seed"] == duel["seed"]
            and abs(identity["started_at"] - started_at) <= 2
        }
        if original["sha256"] in matching:
            continue
        if len(matching) != 1:
            raise ValueError(f"game {ordinal} has no unique verified repair candidate")
        replacement = next(iter(matching.values()))
        entry = by_number[ordinal]
        changes.append({
            "ordinal": ordinal,
            "previous_sha256": entry["sha256"],
            "sha256": replacement["sha256"],
        })
        for key in ("path", "bytes", "sha256"):
            entry[key] = replacement[key]
        entry["sources"] = replacement.get("sources") or [replacement["source"]]
        store._attach_replay_to_result(candidate, entry)
        for supplement in candidate.get("replay_supplements", []):
            if supplement.get("game_number") == ordinal:
                body = supplement.get("body")
                if isinstance(body, dict):
                    body["replay"] = copy.deepcopy(entry)
    store.playable_replay_archive(candidate)
    if changes:
        candidate.setdefault("replay_repairs", []).append({
            "repaired_at": now(),
            "reason": "verified_against_authoritative_evrp",
            "games": changes,
        })
    return candidate, changes


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("session", type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--backup-dir", type=Path)
    args = parser.parse_args()
    original = args.session.read_bytes()
    session = json.loads(original)
    store = PvpStore.__new__(PvpStore)
    store.root = args.session.parent
    store.replay_root = store.root / "replays"
    candidate, changes = repair_candidate(store, session)
    report = {
        "session_id": session["id"],
        "verified_replays": len(candidate["series_replays"]),
        "corrected_games": [change["ordinal"] for change in changes],
        "applied": False,
    }
    if args.apply and changes:
        if any(connection.get("connected") for connection in session.get("connections", {}).values()):
            raise ValueError("refusing repair while a session has connected players")
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup = args.backup_dir or store.root / "backups" / f"replay-bindings-{stamp}"
        backup.mkdir(parents=True, exist_ok=True)
        atomic_bytes(backup / args.session.name, original, 0o600)
        if args.session.read_bytes() != original:
            raise ValueError("session changed during audit; retry without overwriting it")
        candidate["updated_at"] = now()
        atomic_json(args.session, candidate)
        report.update({"applied": True, "backup": str(backup)})
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
