#!/usr/bin/env python3
"""Rebuild an audited EVRP archive from stored per-game YRP files."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
from typing import Any


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser()
    result.add_argument("session", type=Path)
    result.add_argument("output", type=Path)
    result.add_argument(
        "--identity",
        type=Path,
        help="defaults to OUTPUT.identity.json",
    )
    result.add_argument(
        "--node",
        default="/opt/node-v24.14.0/bin/node",
    )
    result.add_argument(
        "--recover-tool",
        type=Path,
        default=Path("/opt/ygo-evolution/tools/recover_evrp_from_yrp.js"),
    )
    result.add_argument(
        "--evolution-root",
        default="/opt/ygo-evolution",
    )
    return result


def sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def result_plans(session: dict[str, Any]) -> dict[int, dict[str, Any]]:
    plans = {}
    for item in session.get("results") or []:
        if not isinstance(item, dict):
            continue
        number = item.get("game_number")
        result = item.get("result")
        if (
            type(number) is int
            and number > 0
            and isinstance(item.get("seed"), list)
            and len(item["seed"]) == 8
            and isinstance(result, dict)
        ):
            plans[number] = {
                "seed": [int(word) for word in item["seed"]],
                "engine_winner": result.get("engine_winner"),
            }
    return plans


def replay_entries(session: dict[str, Any]) -> list[dict[str, Any]]:
    entries = []
    seen = set()
    for item in session.get("series_replays") or []:
        if not isinstance(item, dict):
            continue
        ordinal = item.get("ordinal")
        path_value = item.get("path")
        byte_count = item.get("bytes")
        digest = item.get("sha256")
        if (
            type(ordinal) is not int
            or ordinal <= 0
            or ordinal in seen
            or not isinstance(path_value, str)
            or type(byte_count) is not int
            or byte_count <= 0
            or not isinstance(digest, str)
            or len(digest) != 64
        ):
            raise SystemExit("stored replay metadata is invalid")
        path = Path(path_value)
        payload = path.read_bytes()
        if len(payload) != byte_count or sha256(payload) != digest.lower():
            raise SystemExit(f"stored replay integrity failed for game {ordinal}")
        entries.append({
            "ordinal": ordinal,
            "path": path,
            "sha256": digest.lower(),
        })
        seen.add(ordinal)
    entries.sort(key=lambda item: item["ordinal"])
    return entries


def recover_one(
    args: argparse.Namespace,
    source: Path,
    output: Path,
) -> dict[str, Any]:
    environment = os.environ.copy()
    environment["YGO_EVOLUTION_ROOT"] = args.evolution_root
    try:
        subprocess.run(
            [
                args.node,
                str(args.recover_tool),
                str(source),
                str(output),
                "true",
            ],
            check=True,
            capture_output=True,
            text=True,
            env=environment,
        )
    except subprocess.CalledProcessError as error:
        raise SystemExit(
            f"YRP recovery failed for {source}: {error.stderr.strip()}"
        ) from error
    envelope = json.loads(gzip.decompress(output.read_bytes()))
    duels = envelope.get("duels")
    if not isinstance(duels, list) or len(duels) != 1:
        raise SystemExit(f"recovered EVRP is invalid for {source}")
    return envelope


def main() -> None:
    args = parser().parse_args()
    session = json.loads(args.session.read_text(encoding="utf-8"))
    session_id = session.get("id")
    if (
        not isinstance(session_id, str)
        or not session_id.startswith("s-")
        or session.get("status") != "finished"
        or session.get("series_finished") is not True
    ):
        raise SystemExit("partial recovery requires a finished PvP session")
    expected = session.get("sealed_expected_duels")
    if type(expected) is not int:
        expected = session.get("game_number")
    if type(expected) is not int or expected <= 0:
        raise SystemExit("finished session has an invalid expected game count")
    plans = result_plans(session)
    entries = replay_entries(session)
    ordinals = [item["ordinal"] for item in entries]
    missing = sorted(set(range(1, expected + 1)) - set(ordinals))
    if (
        not entries
        or any(number not in plans for number in ordinals)
        or any(not 1 <= number <= expected for number in ordinals)
    ):
        raise SystemExit("session is not eligible for replay recovery")

    merged_duels = []
    merged_meta = None
    with tempfile.TemporaryDirectory(prefix="partial-evrp-") as temporary:
        temporary_root = Path(temporary)
        for entry in entries:
            ordinal = entry["ordinal"]
            recovered = recover_one(
                args,
                entry["path"],
                temporary_root / f"game-{ordinal:04d}.evrp",
            )
            duel = recovered["duels"][0]
            plan = plans[ordinal]
            if duel.get("seed") != plan["seed"]:
                raise SystemExit(
                    f"recovered EVRP seed does not match game {ordinal}"
                )
            engine_winner = plan.get("engine_winner")
            if (
                engine_winner in (0, 1)
                and duel.get("winPosition") != engine_winner
            ):
                raise SystemExit(
                    f"recovered EVRP winner does not match game {ordinal}"
                )
            duel["gameNumber"] = ordinal
            players = (recovered.get("meta") or {}).get("players")
            if isinstance(players, list):
                duel["players"] = players
            merged_duels.append(duel)
            if merged_meta is None:
                merged_meta = recovered.get("meta") or {}

    assert merged_meta is not None
    merged_meta.update({
        "session_id": session_id,
        "room_id": session.get("room_id"),
        "match_id": session.get("match_id"),
        "partial": bool(missing),
        "ordinals": ordinals,
        "missingOrdinals": missing,
        "recoveredFromYrp": True,
    })
    envelope = {
        "version": 1,
        "meta": merged_meta,
        "duels": merged_duels,
    }
    payload = gzip.compress(
        json.dumps(
            envelope,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8"),
        mtime=0,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(payload)
    os.chmod(args.output, 0o600)
    identity_path = args.identity or Path(f"{args.output}.identity.json")
    identity = {
        "session_id": session_id,
        "room_id": session.get("room_id"),
        "match_id": session.get("match_id"),
        "duels": len(merged_duels),
        "expected_duels": expected,
        "ordinals": ordinals,
        "missing_ordinals": missing,
        "sha256": sha256(payload),
        "recovered_from_yrp": True,
    }
    identity_path.write_text(
        json.dumps(identity, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.chmod(identity_path, 0o600)
    print(json.dumps({
        **identity,
        "output": str(args.output),
        "identity": str(identity_path),
        "bytes": len(payload),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
