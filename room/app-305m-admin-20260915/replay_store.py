"""Durable session status and named replay copies; original YRP bytes stay intact."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import struct
import tempfile
import threading


LOCK = threading.RLock()
MAX_REPLAY_BYTES = 16 * 1024 * 1024
LABELS = {"recording": "\u5f55\u5236\u4e2d", "complete": "\u5df2\u5b8c\u6210",
          "incomplete": "\u672a\u5b8c\u6210"}


def now():
    return datetime.now(timezone.utc).isoformat()


def sync_directory(path):
    if os.name == "posix":
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def atomic_json(path, document):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".recording-", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(json.dumps(document, ensure_ascii=False, indent=2).encode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def read_json(path):
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(document, dict):
            raise ValueError("Expected a JSON object")
        return document
    except FileNotFoundError:
        return {}
    except (ValueError, UnicodeError) as error:
        return {"read_error": str(error)}


def process_stamp(pid):
    try:
        folder = Path("/proc") / str(pid)
        fields = (folder / "stat").read_text().rpartition(")")[2].split()
        if fields[0] == "Z":
            return None
        return {"pid": pid, "start": fields[19],
                "boot": Path("/proc/sys/kernel/random/boot_id").read_text().strip()}
    except (OSError, IndexError):
        return None


def begin_session(session):
    with LOCK:
        atomic_json(session / "replay-status.json", {
            "schema": 1, "session": session.name, "status": "recording",
            "label": LABELS["recording"], "started_at": now(),
        })


def track_worker(session, pid):
    with LOCK:
        path = session / "replay-status.json"
        document = read_json(path)
        document["worker"] = process_stamp(pid)
        atomic_json(path, document)


def inspect_replay(path):
    size = path.stat().st_size
    report = {"source": str(Path("replay") / path.name), "bytes": size,
              "header_valid": False, "structurally_complete": False, "responses": 0}
    if size > MAX_REPLAY_BYTES:
        raise ValueError("Replay exceeds the bounded inspection size")
    data = path.read_bytes()
    report["sha256"] = hashlib.sha256(data).hexdigest()
    try:
        if len(data) < 128 or data[:4] != b"yrp1":
            raise ValueError("Missing YRP1 header or duel metadata")
        flags = struct.unpack_from("<I", data, 8)[0]
        if flags != 0x10:
            raise ValueError("Expected native uncompressed uniform YRP1")
        offset = 128
        for _ in range(4):
            count = struct.unpack_from("<I", data, offset)[0]
            if count > 200:
                raise ValueError("Invalid recorded deck size")
            offset += 4 + count * 4
            if offset > len(data):
                raise ValueError("Truncated recorded deck")
        report["header_valid"] = True
        report["valid_prefix_bytes"] = offset
        while offset < len(data):
            length = data[offset]
            if length == 0 or offset + 1 + length > len(data):
                raise ValueError("Truncated or invalid response suffix")
            offset += 1 + length
            report["responses"] += 1
            report["valid_prefix_bytes"] = offset
        report["structurally_complete"] = True
    except (ValueError, struct.error) as error:
        report["error"] = str(error)
    return report


def archive_copy(source, target, expected_sha):
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        if hashlib.sha256(target.read_bytes()).hexdigest() != expected_sha:
            raise FileExistsError("An archived replay has different bytes: " + str(target))
        return
    temporary = None
    try:
        digest = hashlib.sha256()
        with source.open("rb") as src, tempfile.NamedTemporaryFile(
                dir=target.parent, prefix=".recording-", delete=False) as dst:
            temporary = Path(dst.name)
            for chunk in iter(lambda: src.read(1024 * 1024), b""):
                digest.update(chunk)
                dst.write(chunk)
            dst.flush()
            os.fsync(dst.fileno())
        if digest.hexdigest() != expected_sha:
            raise RuntimeError("Replay changed during finalization")
        os.replace(temporary, target)
        sync_directory(target.parent)
        sync_directory(target.parent.parent)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def finalize_session(session, reason="interrupted", data_root=None):
    session = Path(session)
    data_root = Path(data_root) if data_root is not None else session.parent.parent
    with LOCK:
        previous = read_json(session / "replay-status.json")
        if previous.get("finalized_at"):
            return previous
        metadata = read_json(session / "session.json")
        result = read_json(session / "result.json")
        files = sorted((session / "replay").glob("*.yrp"))
        if any(path.is_symlink() for path in files):
            raise ValueError("A replay source must not be a symlink")
        entries = [inspect_replay(path) for path in files]
        complete = (result.get("event") == "complete" and not result.get("truncated", False)
                    and bool(entries) and all(entry["structurally_complete"] for entry in entries))
        status = "complete" if complete else "incomplete"
        if complete:
            reason = "duel_complete"
        elif reason == "recovered_after_restart" and (session / "resigned.json").exists():
            reason = "surrender_or_leave"
        archive = data_root / "recordings" / session.name[:8]
        for index, (path, entry) in enumerate(zip(files, entries)):
            if entry["header_valid"]:
                target = archive / f"{session.name}__{LABELS[status]}_{index + 1}.yrp"
                archive_copy(path, target, entry["sha256"])
                entry["archive"] = str(target.relative_to(data_root))
        document = {
            **previous, "schema": 1, "session": session.name,
            "status": status, "label": LABELS[status], "reason": reason,
            "finalized_at": now(), "recording_available": any(e["header_valid"] for e in entries),
            "replays": entries, "result_available": bool(result),
            "checkpoint_steps": metadata.get("checkpoint_steps"),
            "checkpoint_sha256": metadata.get("checkpoint_sha256"),
            "client_profile": metadata.get("client_profile"),
            "strict_playback_verified": False,
            "metadata_errors": [name for name, value in
                                (("session.json", metadata), ("result.json", result))
                                if value.get("read_error")],
        }
        # Publish the archive index before the final marker so retries repair a torn exit.
        atomic_json(archive / (session.name + ".json"), document)
        sync_directory(data_root)
        atomic_json(session / "replay-status.json", document)
        return document


def recover_sessions(data_root):
    report = {"recovered": 0, "skipped_live": 0}
    for session in sorted((Path(data_root) / "sessions").glob("*")):
        if not session.is_dir() or session.is_symlink() or not (session / "session.json").is_file():
            continue
        document = read_json(session / "replay-status.json")
        if document.get("finalized_at"):
            continue
        worker = document.get("worker")
        if worker and worker == process_stamp(worker["pid"]):
            report["skipped_live"] += 1
            continue
        finalize_session(session, "recovered_after_restart", data_root)
        report["recovered"] += 1
    return report
