"""Download and verify the pinned optional runtimes; never starts services."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import tarfile
import tempfile
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]


def digest(path):
    result = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def install(archive, destination, spec):
    if digest(archive) != spec["sha256"]:
        raise ValueError("Runtime archive SHA-256 does not match the published lock")
    destination = Path(destination).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="ygo-runtime-") as temporary:
        staged = Path(temporary)
        files, seen, total = [], set(), 0
        with tarfile.open(archive, "r:gz") as source:
            for member in source:
                raw = PurePosixPath(member.name)
                if raw.is_absolute() or ".." in raw.parts or "\\" in member.name:
                    raise ValueError("Unsafe runtime archive path")
                parts = raw.parts[spec.get("strip_components", 0):]
                if not parts:
                    continue
                relative = Path(*parts)
                if member.isdir():
                    continue
                if not member.isfile() or relative in seen or ":" in member.name:
                    raise ValueError("Runtime archive must contain unique regular files")
                total += member.size
                if total > 2 * 1024**3:
                    raise ValueError("Runtime archive exceeds the installation limit")
                seen.add(relative)
                target = staged / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                with source.extractfile(member) as incoming, target.open("wb") as outgoing:
                    shutil.copyfileobj(incoming, outgoing)
                target.chmod(0o755 if member.mode & 0o111 else 0o644)
                files.append(relative)
        for relative in files:
            target = destination / relative
            if not target.resolve().is_relative_to(destination) or target.is_symlink():
                raise ValueError("Runtime destination escapes through a symbolic link")
            if target.exists() and (not target.is_file()
                                    or digest(target) != digest(staged / relative)):
                raise ValueError(f"Existing file differs; use a fresh installation: {target}")
        for relative in files:
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                shutil.copy2(staged / relative, target)
    return len(files)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("runtime", choices=["model", "tournament"])
    parser.add_argument("--archive", type=Path, help="Use an already downloaded archive")
    parser.add_argument("--destination", type=Path)
    args = parser.parse_args()
    spec = json.loads((ROOT / "deploy/runtime-assets.lock.json").read_text("utf-8"))[args.runtime]
    archive = args.archive
    if archive is None:
        archive = ROOT / ".cache/runtime" / spec["filename"]
        archive.parent.mkdir(parents=True, exist_ok=True)
        if not archive.exists() or digest(archive) != spec["sha256"]:
            pending = archive.with_suffix(".download")
            try:
                with urlopen(spec["url"], timeout=120) as response, pending.open("wb") as stream:
                    shutil.copyfileobj(response, stream)
                if digest(pending) != spec["sha256"]:
                    raise ValueError("Downloaded runtime archive failed SHA-256 verification")
                os.replace(pending, archive)
            finally:
                pending.unlink(missing_ok=True)
    target = args.destination or (ROOT if args.runtime == "model" else ROOT / "runtime")
    count = install(archive, target, spec)
    print(json.dumps({"runtime": args.runtime, "files": count,
                      "destination": str(target.resolve()), "sha256": spec["sha256"]}))


if __name__ == "__main__":
    main()
