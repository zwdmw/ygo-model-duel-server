"""Independent watchdog for the management agent; never supervises game processes."""
import argparse
import fcntl
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    root = args.config.resolve().parent
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    os.umask(0o077)
    with (root / "agent.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        (root / "agent-service.pid").write_text(str(os.getpid()))
        while not stop.is_set():
            child = subprocess.Popen([sys.executable, str(root / "agent.py"),
                                      "--config", str(args.config)], cwd=root)
            try:
                while not stop.wait(2) and child.poll() is None:
                    pass
            finally:
                if child.poll() is None:
                    child.terminate()
                try:
                    child.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()
            if stop.wait(5):
                break
        (root / "agent-service.pid").unlink(missing_ok=True)


if __name__ == "__main__":
    main()
