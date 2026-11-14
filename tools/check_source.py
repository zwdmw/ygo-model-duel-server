"""Reject deployment secrets and runtime binaries in the Git source tree."""
from pathlib import Path
import re
import subprocess
import sys


ROOT = Path(__file__).resolve().parent.parent
PRIVATE_NAMES = {
    "agent.json", "site.json", "user.json", "bridge.json", "models.json",
    "invitations.json", "room-codes.json", "panel-bootstrap.json",
}
RUNTIME_SUFFIXES = {".key", ".pem", ".so", ".dll", ".npz", ".cdb", ".flax_model",
                    ".yrp", ".evrp", ".db", ".sqlite", ".log", ".zip", ".gz", ".tgz", ".pyc"}
PATTERNS = [
    re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(rb"\bgh[pousr]_[A-Za-z0-9]{30,}\b"),
    re.compile(rb"\bgithub_pat_[A-Za-z0-9_]{50,}\b"),
    re.compile(rb"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(rb"\bLTAI[A-Za-z0-9]{16,}\b"),
]


def main():
    raw = subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT)
    files = [Path(name.decode("utf-8")) for name in raw.split(b"\0") if name]
    failures = []
    for relative in files:
        path = ROOT / relative
        if (set(relative.parts) & {"private", "data", "venv", ".venv", "__pycache__",
                                  "runtime", "artifacts", "node_modules", ".learnings"}
                or any(part.startswith(".tmp") for part in relative.parts)
                or path.suffix in RUNTIME_SUFFIXES or path.name in PRIVATE_NAMES
                or (path.name.startswith(".env") and not path.name.endswith(".example"))):
            failures.append(f"Excluded deployment artifact is tracked: {relative}")
            continue
        content = path.read_bytes()
        if len(content) > 5 * 1024 * 1024:
            failures.append(f"Unexpected large source file: {relative}")
        if path.suffix != ".png" and any(pattern.search(content) for pattern in PATTERNS):
            failures.append(f"Possible secret in {relative}")
    if failures:
        print("\n".join(failures), file=sys.stderr)
        return 1
    print(f"Source boundary check passed for {len(files)} tracked files.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
