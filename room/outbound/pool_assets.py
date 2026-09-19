"""Verified immutable external assets, independent from the native room pool."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import threading
import time


FILES = ("cards.cdb", "code_list.txt", "semantics.npz")


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class PoolAssets:
    def __init__(self, path, app, expected=None):
        import numpy as np

        self.path, self.app = Path(path).resolve(), Path(app).resolve()
        manifest = self.path / "manifest.json"
        if manifest.is_symlink() or manifest.stat().st_size > 8 * 1024 * 1024:
            raise ValueError("Invalid external pool manifest")
        self.sha256 = sha(manifest)
        if expected is not None and expected != self.sha256:
            raise ValueError("External pool manifest changed")
        self.info = json.loads(manifest.read_text(encoding="utf-8"))
        if self.info.get("schema") != 1 or set(self.info["files"]) != set(FILES):
            raise ValueError("Unsupported external pool schema")
        for name in FILES:
            file = self.path / name
            if file.is_symlink() or file.stat().st_size > 128 * 1024 * 1024:
                raise ValueError("Invalid external pool asset")
            if sha(file) != self.info["files"][name]:
                raise ValueError("External pool integrity failure: " + name)
        for name, key in (("scripts/code_list.txt", "base_code_sha256"),
                          ("assets/structured/frozen_semantics_v1.npz", "base_semantic_sha256")):
            if sha(self.app / "project" / name) != self.info[key]:
                raise ValueError("External pool does not match the selected model base")
        def codes(path):
            lines = path.read_text(encoding="utf-8").splitlines()
            if any(not line.split() for line in lines):
                raise ValueError("Blank card index would change network semantic IDs")
            return [int(line.split()[0]) for line in lines]

        original = codes(self.app / "project/scripts/code_list.txt")
        extended = codes(self.path / "code_list.txt")
        if (extended[:len(original)] != original or len(set(extended)) != len(extended)
                or not 0 < len(extended) < 65536 or any(not 0 < code <= 0xFFFFFFFF for code in extended)
                or self.info["card_count"] != len(extended) or self.info["old_count"] != len(original)
                or self.info["added_count"] != len(extended) - len(original)
                or self.info["id"] != self.path.name):
            raise ValueError("External pool changed the append-only card identity contract")
        expected_hash = hashlib.sha256("\n".join(map(str, extended)).encode()).hexdigest()
        rows = len(extended) + 1
        with np.load(self.path / "semantics.npz", allow_pickle=False) as tables:
            metadata = json.loads(str(tables["metadata"].item()))
            if metadata.get("code_list_sha256") != expected_hash:
                raise ValueError("Semantic card indices do not match the external pool")
            for key, shape in (("cdb_exact", (rows, 78)), ("lua_effects", (rows, 16, 64)),
                               ("lua_mask", (rows, 16))):
                values = tables[key]
                if values.shape != shape or not np.isfinite(values).all() or np.any(values[0]):
                    raise ValueError("Invalid external semantic shape, values or hidden sentinel: " + key)
        self.pending = {row["code"] for row in self.info.get("missing_effect_scripts", []) if row["new"]}
        if not self.pending.issubset(set(extended[len(original):])):
            raise ValueError("Pending script list is not a subset of new cards")
        with closing(sqlite3.connect((self.path / "cards.cdb").as_uri() + "?mode=ro", uri=True)) as conn:
            self.types = {int(code): int(kind) for code, kind in conn.execute("SELECT id,type FROM datas")}
        if set(self.types) != set(extended):
            raise ValueError("External database and semantic card identities disagree")
        self.codes = extended

    def descriptor(self):
        return {"path": str(self.path), "sha256": self.sha256}

    def public(self):
        return {**{key: self.info[key] for key in
                   ("id", "built_at", "card_count", "old_count", "added_count",
                    "database_commit", "scripts_commit")},
                "pending_script_count": len(self.pending),
                "ready_count": self.info["card_count"] - len(self.pending), "sha256": self.sha256}


def active_pool(root, app):
    folder = Path(root) / "card-pools"
    pointer = folder / "current.json"
    if not pointer.exists():
        return None
    if pointer.is_symlink() or pointer.stat().st_size > 4096:
        raise ValueError("Invalid external pool pointer")
    current = json.loads(pointer.read_text())
    identity = current.get("id")
    if not isinstance(identity, str) or not re.fullmatch(r"[a-z0-9-]{1,80}", identity):
        raise ValueError("Invalid external pool identity")
    path = folder / identity
    if path.is_symlink() or not path.resolve().is_relative_to(folder.resolve()):
        raise ValueError("External pool escaped its storage")
    return PoolAssets(path, app, current["sha256"])


class PoolCache:
    """Resolve the active external pool whenever the pointer or manifest changes.

    The pool used to be resolved once, in the agent's constructor, so activating a
    rebuilt pool required restarting the service. Forgetting that surfaced as
    "Observed cards outside the model database" part-way through a duel, because
    the new session was still judged against the previous pool.

    The pointer and manifest are a few kB, so they are re-read on every access; the
    expensive asset validation runs only when either changed. A pool that fails
    validation is reported instead of being ignored: strict resolution refuses to
    start a duel, and non-strict resolution keeps the last good pool so the
    management page can still render. Failures are re-attempted after RETRY_SECONDS
    so a repaired pool is picked up without a restart.
    """

    RETRY_SECONDS = 5.0

    def __init__(self, root, app, on_change=None):
        self.root, self.app = Path(root), Path(app)
        self.lock = threading.Lock()
        self.on_change = on_change
        self.key = object()  # no resolution attempted yet
        self.pool = None
        self.error = ""
        self.retry_at = 0.0

    def _key(self):
        """Cheap change detector; never raises, returns None when nothing is active."""
        pointer = self.root / "card-pools/current.json"
        try:
            if pointer.is_symlink() or not pointer.is_file() or pointer.stat().st_size > 4096:
                return None
            current = json.loads(pointer.read_text())
            identity = current["id"]
            if not isinstance(identity, str) or not re.fullmatch(r"[a-z0-9-]{1,80}", identity):
                return None
            return (identity, current.get("sha256"),
                    sha(self.root / "card-pools" / identity / "manifest.json"))
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def resolve(self, strict=True):
        now = time.monotonic()
        with self.lock:
            key = self._key()
            if key == self.key and now < self.retry_at and (not strict or not self.error):
                return self.pool
            self.key = key
            try:
                pool = active_pool(self.root, self.app)
            except (ValueError, OSError) as error:
                self.error = str(error) or type(error).__name__
                self.retry_at = now + self.RETRY_SECONDS
                if strict:
                    raise
                return self.pool
            self.error, self.pool = "", pool
            self.retry_at = float("inf")
            if self.on_change is not None:
                self.on_change(pool)
            return pool
