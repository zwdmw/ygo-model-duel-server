"""Move invitation endpoints without rotating tokens or changing other settings."""
import argparse
import json
import os
from pathlib import Path
import shutil
import tempfile
import time

from connector import validate_config


def write_atomic(path, data):
    descriptor, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as target:
            json.dump(data, target, ensure_ascii=True, indent=2)
            target.write("\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def migrate(endpoint, files, server_root=None):
    plans = []
    for path in dict.fromkeys(Path(file).resolve() for file in files):
        config = json.loads(path.read_text(encoding="utf-8"))
        previous, _ = validate_config(config)
        updated = {**config, "endpoint": endpoint}
        normalized, _ = validate_config(updated)
        updated["endpoint"] = normalized
        if previous != normalized:
            plans.append((path, updated))
    if server_root is not None:
        endpoint, _ = validate_config({"endpoint": endpoint, "token": "validation-only" * 3})
        if not Path(server_root).is_dir():
            raise ValueError("Server root must already exist")
    for path, config in plans:
        backup = path.with_name(path.name + f".before-endpoint-{time.time_ns()}.bak")
        shutil.copy2(path, backup)
        if os.name != "nt":
            backup.chmod(0o600)
        write_atomic(path, config)
    if server_root is not None:
        write_atomic(Path(server_root) / "public-endpoint.json", {"endpoint": endpoint})
    return {"changed_files": [str(path) for path, _ in plans], "tokens_rotated": False}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--file", type=Path, action="append", default=[])
    parser.add_argument("--server-root", type=Path)
    args = parser.parse_args()
    files = list(args.file)
    if args.server_root:
        files.extend(args.server_root.glob("invite-*.json"))
    try:
        print(json.dumps(migrate(args.endpoint, files, args.server_root)))
    except (OSError, ValueError, TypeError, KeyError):
        parser.exit(1, "Migration failed; check endpoint and private configuration files.\n")
