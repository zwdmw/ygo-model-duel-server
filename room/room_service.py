"""Supervise only this room's processes, with CPU affinity and an RSS ceiling."""
import fcntl
import argparse
import ctypes
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener


ROOT = Path(__file__).resolve().parent
STOP = threading.Event()


def process_info(pid):
    try:
        folder = Path("/proc") / str(pid)
        fields = (folder / "stat").read_text().rpartition(")")[2].split()
        rss = next((line.split()[1] for line in (folder / "status").read_text().splitlines()
                    if line.startswith("VmRSS:")), "0")
        return {"parent": int(fields[1]), "born": fields[19], "rss": int(rss) * 1024}
    except (OSError, ValueError, IndexError):
        return None


def descendants():
    table = {int(path.name): process_info(int(path.name))
             for path in Path("/proc").iterdir() if path.name.isdigit()}
    owned = {os.getpid()}
    while True:
        extra = {pid for pid, info in table.items()
                 if info and info["parent"] in owned} - owned
        if not extra:
            break
        owned.update(extra)
    return {pid: table[pid] for pid in owned if table.get(pid)}


def stop_owned(children, tracked):
    for child in children:
        if child.poll() is None:
            child.terminate()
    for child in children:
        try:
            child.wait(timeout=12)
        except subprocess.TimeoutExpired:
            pass
    # Verify both PID and kernel start time before touching any remaining child.
    for sig in (signal.SIGTERM, signal.SIGKILL):
        for pid, previous in tracked.items():
            if pid == os.getpid():
                continue
            current = process_info(pid)
            if current and current["born"] == previous["born"]:
                try:
                    os.kill(pid, sig)
                except ProcessLookupError:
                    pass
        if sig == signal.SIGTERM:
            time.sleep(1)
    for child in children:
        child.wait()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--tcp-config", type=Path)
    parser.add_argument("--tcp-port", type=int, default=6008)
    parser.add_argument("--model", choices=("195M", "285M", "305M", "454M"), default="195M")
    parser.add_argument("--app-dir", type=Path, default=ROOT / "app-305m-admin-20260915")
    args = parser.parse_args()
    app = args.app_dir.resolve()
    sys.path.insert(0, str(app))
    from model_selection import ModelSelection
    from model_snapshot import snapshot
    os.umask(0o077)
    with (ROOT / "service.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        selection = ModelSelection(ROOT / "data/admin", app, snapshot(args.model))
        signal.signal(signal.SIGTERM, lambda *_: STOP.set())
        signal.signal(signal.SIGINT, lambda *_: STOP.set())
        cpus = sorted(os.sched_getaffinity(0))[-4:]
        os.sched_setaffinity(0, cpus)
        os.nice(10)
        # Keep orphaned native workers inside this supervisor's process tree.
        if ctypes.CDLL(None, use_errno=True).prctl(36, 1, 0, 0, 0) != 0:
            raise OSError(ctypes.get_errno(), "Cannot supervise orphaned room workers")
        (ROOT / "service.pid").write_text(str(os.getpid()))
        env = dict(os.environ, AGENT195M_ROOT=str(app),
                   AGENT_CHECKPOINT=args.model,
                   AGENT195M_DATA=str(ROOT / "data"), PYTHONUNBUFFERED="1",
                   PYTHONPATH=str(app) + ":" + str(app / "project"),
                   PATH=str(ROOT / "venv/bin") + ":" + os.defpath,
                   JAX_PLATFORMS="cpu", CUDA_VISIBLE_DEVICES="",
                   XLA_PYTHON_CLIENT_PREALLOCATE="false",
                   JAX_COMPILATION_CACHE_DIR=str(ROOT / "data/jax-cache"),
                   OMP_NUM_THREADS="2", OPENBLAS_NUM_THREADS="1")
        if (ROOT / "admin/control.key").exists():
            env["AGENT_ADMIN_KEY_FILE"] = str(ROOT / "admin/control.key")
        python = str(ROOT / "venv/bin/python")
        commands = [
            [python, str(app / "server.py"), "--host", "127.0.0.1"],
            [python, str(ROOT / "gateway.py"), "--invites", str(ROOT / "invitations.json")],
        ]
        names = ["room", "gateway"]
        if args.tcp_config is not None:
            commands.append([
                python, str(ROOT / "tcp_bridge/tcp_bridge.py"),
                "--config", str(args.tcp_config.resolve()),
                "--host", "0.0.0.0", "--port", str(args.tcp_port),
            ])
            names.append("tcp-bridge")
        print(json.dumps({"event": "service_start", "cpus": cpus,
                          "model": args.model,
                          "rss_limit_GiB": 4, "gpu_enabled": False}), flush=True)
        attempt = 0
        while attempt < 3:
            children, logs, tracked = [], [], {}
            requested_restart = False
            try:
                env["AGENT_MODEL_SNAPSHOT"] = json.dumps(selection.model())
                for name, command in zip(names, commands):
                    log = (ROOT / f"{name}.log").open("a")
                    logs.append(log)
                    children.append(subprocess.Popen(command, cwd=ROOT, env=env,
                                                     stdout=log, stderr=subprocess.STDOUT))
                while not STOP.wait(2):
                    selection.check_timeout()
                    current = descendants()
                    tracked.update(current)
                    if sum(info["rss"] for info in current.values()) > 4 * 2**30:
                        raise RuntimeError("Room process tree exceeded its 4 GiB RSS limit")
                    if any(child.poll() is not None for child in children):
                        raise RuntimeError("A room service process exited")
                    try:
                        with build_opener(ProxyHandler({})).open(
                            "http://127.0.0.1:7916/health", timeout=2
                        ) as response:
                            health = json.load(response)
                            if health.get("state") == "failed":
                                raise RuntimeError("Resident inference failed")
                            def resume(admission_revision):
                                key = (ROOT / "admin/control.key").read_text().strip()
                                request = Request("http://127.0.0.1:7916/manage",
                                    data=json.dumps({"action": "settings", "accepting": True,
                                        "expected_admission_revision": admission_revision}).encode(),
                                    headers={"Content-Type": "application/json",
                                             "Authorization": "Bearer " + key})
                                with build_opener(ProxyHandler({})).open(request, timeout=5):
                                    pass
                            selection.ready(health, resume)
                            if (health.get("restart_requested") and not health.get("occupied", True)
                                    and not health.get("accepting", True) and health.get("state") == "ready"):
                                if selection.claim():
                                    requested_restart = True
                                    print(json.dumps({"event": "admin_idle_restart",
                                                      "model": selection.model()["model_label"]}), flush=True)
                                    break
                    except HTTPError as error:
                        if error.code == 503:
                            raise RuntimeError("Resident inference failed") from error
                    except (URLError, TimeoutError):
                        pass
            except Exception as error:
                if selection.failed(error):
                    requested_restart = True
                else:
                    attempt += 1
                print(json.dumps({"event": "service_error", "attempt": attempt + 1,
                                  "error": str(error)}), flush=True)
            finally:
                tracked.update(descendants())
                stop_owned(children, tracked)
                for log in logs:
                    log.close()
            if STOP.wait(0.5 if requested_restart else 10):
                break
        (ROOT / "service.pid").unlink(missing_ok=True)


if __name__ == "__main__":
    main()
