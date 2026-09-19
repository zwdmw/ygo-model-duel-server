"""Shared, bounded operations for the two administration services."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import time


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def safe_path(root, relative):
    root = Path(root).resolve()
    path = root / relative
    if path.is_symlink():
        raise ValueError("Symlinks are not allowed")
    resolved = path.resolve()
    if not resolved.is_relative_to(root) or resolved == root:
        raise ValueError("Path outside allowed storage")
    return resolved


def tail(path, limit=32768):
    try:
        with Path(path).open("rb") as stream:
            stream.seek(0, 2)
            length = stream.tell()
            stream.seek(max(0, length - limit))
            raw = stream.read(limit).decode("utf-8", errors="replace")
            if length > limit:
                raw = raw.partition("\n")[2]
    except FileNotFoundError:
        return ""
    raw = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+", r"\1[REDACTED]", raw)
    return re.sub(r'(?i)((?:password|token|secret|key)\s*["\']?\s*[:=]\s*["\']?)[^\s,"\']+',
                  r"\1[REDACTED]", raw)


class Metrics:
    def __init__(self, storage):
        self.storage = Path(storage)
        self.previous = None
        self.cpu = None
        self.sampled = 0

    def read(self):
        result = {"sampled_at": timestamp()}
        try:
            values = [int(x) for x in Path("/proc/stat").read_text().splitlines()[0].split()[1:9]]
            current = (sum(values), values[3] + values[4])
            if time.monotonic() - self.sampled >= 1:
                if self.previous and current[0] > self.previous[0]:
                    self.cpu = round(100 * (1 - (current[1] - self.previous[1]) /
                                            (current[0] - self.previous[0])), 1)
                self.previous, self.sampled = current, time.monotonic()
            memory = {line.split(":")[0]: int(line.split()[1]) * 1024
                      for line in Path("/proc/meminfo").read_text().splitlines()}
            result.update(cpu_percent=self.cpu, memory_total=memory["MemTotal"],
                          memory_available=memory["MemAvailable"], load=os.getloadavg()[0])
        except (OSError, ValueError, AttributeError):
            result["cpu_percent"] = None
        disk = shutil.disk_usage(self.storage)
        result.update(disk_total=disk.total, disk_free=disk.free)
        return result


class Audit:
    def __init__(self, path):
        self.path = Path(path)

    def add(self, action, success=True, target=None):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists() and self.path.stat().st_size > 8 * 1024 * 1024:
            os.replace(self.path, self.path.with_suffix(".previous.jsonl"))
        row = {"at": timestamp(), "action": action, "success": success}
        if target is not None:
            row["target"] = str(target)[:120]
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row, ensure_ascii=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())

    def recent(self):
        result = []
        for line in tail(self.path).splitlines():
            try:
                result.append(json.loads(line))
            except ValueError:
                pass
        return list(reversed(result[-100:]))
