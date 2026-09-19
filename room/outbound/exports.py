"""Snapshot all historical external sessions into bounded, verified ZIP exports."""
import hashlib
import json
import os
import time
import zipfile

from .continuous import authoritative_outcome
from .manager import SESSION
from .opponent_deck import LIMIT


KINDS = {"replays", "decks", "loss-decks"}
MAX_ARCHIVE_BYTES = 256 * 1024 * 1024
MAX_SESSIONS = 50000


def recorded_replays(manager, folder):
    from common import safe_path
    saved = manager._read(folder / "replay-status.json")
    if not saved.get("finalized_at") or not saved.get("recording_available"):
        return []
    entries = saved.get("replays", [])
    if not 1 <= len(entries) <= 5:
        raise ValueError("Invalid replay count")
    rows = []
    for entry in entries:
        path = safe_path(manager.root, entry["archive"])
        if (not path.is_relative_to((manager.root / "recordings").resolve())
                or path.suffix != ".yrp" or path.stat().st_size > LIMIT):
            raise ValueError("Invalid recording")
        with path.open("rb") as source:
            raw = source.read(LIMIT + 1)
        if (not 32 <= len(raw) <= LIMIT or raw[:4] not in (b"yrp1", b"yrp2")
                or hashlib.sha256(raw).hexdigest() != entry["sha256"]):
            raise ValueError("Replay integrity failure")
        rows.append((entry, raw))
    return rows


def recorded_replay(manager, folder):
    rows = recorded_replays(manager, folder)
    if len(rows) > 1:
        raise ValueError("Multiple game replays")
    return rows[0][1] if rows else None


def build_archive(manager, kind, output, cancelled):
    if kind not in KINDS:
        raise ValueError("Unknown export")
    deadline = time.monotonic() + 120

    def check():
        if cancelled.is_set():
            raise InterruptedError("Export cancelled")
        if time.monotonic() >= deadline:
            raise ValueError("\u6253\u5305\u8d85\u65f6\uff0c\u8bf7\u7a0d\u540e\u91cd\u8bd5")

    folders = []
    with os.scandir(manager.sessions) as entries:
        for entry in entries:
            check()
            if SESSION.fullmatch(entry.name) and entry.is_dir(follow_symlinks=False):
                folders.append(manager.sessions / entry.name)
                if len(folders) > MAX_SESSIONS:
                    raise ValueError("Export exceeds 50000 sessions; no partial archive was returned")
    report = {"kind": kind, "scope": "all_historical_external_sessions",
              "created_at": time.time(), "scanned": len(folders),
              "excluded": {"selftest": 0, "not_loss": 0},
              "files": [], "skipped": [], "side_deck_recorded": False}
    total = 0
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=1) as archive:
        for folder in sorted(folders):
            check()
            try:
                metadata = manager._read(folder / "session.json")
                result = manager._read(folder / "result.json")
                if not metadata:
                    report["skipped"].append({"session": folder.name, "reason": "missing_metadata"})
                    continue
                if metadata.get("selftest") or result.get("selftest"):
                    report["excluded"]["selftest"] += 1
                    continue
                outcome = authoritative_outcome(result)
                if kind == "loss-decks" and outcome != "loss":
                    report["excluded"]["not_loss"] += 1
                    continue
                if kind == "replays":
                    recordings = recorded_replays(manager, folder)
                    files = [(f"_game{i + 1:02}.yrp" if len(recordings) > 1 else ".yrp", raw)
                             for i, (_, raw) in enumerate(recordings)]
                    reason = "no_finalized_replay"
                else:
                    deck = manager.opponent_decks.inspect(folder.name, refresh=True)
                    files = []
                    if deck["available"]:
                        decks = deck.get("items", [deck])
                        files = [(f"_game{i + 1:02}_opponent_main-extra.ydk" if len(decks) > 1
                                  else "_opponent_main-extra.ydk", row["ydk"].encode("utf-8"))
                                 for i, row in enumerate(decks)]
                    reason = deck.get("reason", "no_verified_opponent_deck")
                if not files:
                    report["skipped"].append({"session": folder.name, "reason": reason})
                    continue
            except (OSError, ValueError, KeyError, TypeError):
                report["skipped"].append({"session": folder.name, "reason": "invalid_or_missing_record"})
                continue
            for suffix, raw in files:
                total += len(raw)
                if total > MAX_ARCHIVE_BYTES:
                    raise ValueError("Export exceeds 256 MiB; no partial archive was returned")
                name = folder.name + "_" + outcome + suffix
                archive.writestr(name, raw)
                report["files"].append({"session": folder.name, "outcome": outcome, "file": name,
                                       "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()})
        if not report["files"]:
            raise ValueError("\u6682\u65e0\u53ef\u4e0b\u8f7d\u7684" +
                             {"replays": "\u5f55\u50cf", "decks": "\u5bf9\u65b9\u5361\u7ec4",
                              "loss-decks": "\u8f93\u5c40\u5bf9\u65b9\u5361\u7ec4"}[kind])
        report["exported"] = len(report["files"])
        archive.writestr("manifest.json", json.dumps(report, ensure_ascii=False, indent=2))
        archive.writestr("README.txt",
            "All historical external sessions, not just the visible page.\n"
            "loss-decks includes opponents only when the model has an authoritative loss.\n"
            "Unfinished replays, when available, are named _incomplete.yrp.\n"
            "YDK contains verified main and extra decks; Side Deck was not recorded.\n"
            "One file per duel. Repeated opponents/decks are kept with their session IDs.\n"
            "See manifest.json for exported files and skipped or excluded records.\n")
    check()
    size = output.tell()
    if size > MAX_ARCHIVE_BYTES:
        raise ValueError("Export exceeds 256 MiB; no partial archive was returned")
    output.seek(0)
    return {"bytes": size, "files": report["exported"], "skipped": len(report["skipped"])}
