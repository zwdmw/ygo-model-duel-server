"""Isolated Linux acceptance duel; never connects to a public opponent."""
import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import time
from urllib.request import ProxyHandler, build_opener


def main(root, deck, seed, seat=0, legacy=False):
    app = root / "app-305m-admin-20260915"
    remote = Path(__file__).resolve().parent.parent
    folder = remote.parent / f"selftest-{seed}"
    folder.mkdir(mode=0o700)
    with build_opener(ProxyHandler({})).open("http://127.0.0.1:7916/health") as response:
        before = json.load(response)
    policy_pid = before["policy_pid"]
    command = Path(f"/proc/{policy_pid}/cmdline").read_bytes().split(b"\0")
    if str(app / "worker.py").encode() not in command:
        raise RuntimeError("Serving policy identity changed")
    environ = Path(f"/proc/{policy_pid}/environ").read_bytes().split(b"\0")
    snapshot = next((row.split(b"=", 1)[1] for row in environ
                     if row.startswith(b"AGENT_MODEL_SNAPSHOT=")), None)
    if snapshot is None:
        sys.path.insert(0, str(app))
        os.environ["AGENT195M_ROOT"] = str(app)
        from model_snapshot import snapshot as current_snapshot
        snapshot = json.dumps(current_snapshot()).encode()
    model = json.loads(snapshot)
    if model["checkpoint_sha256"] != before["checkpoint_sha256"]:
        raise RuntimeError("Snapshot does not match serving policy")
    for port in (27915, 27916, 27917):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", port))
    env = dict(os.environ)
    env.update(AGENT195M_ROOT=str(app), AGENT195M_DATA=str(folder / "host"),
               AGENT_MODEL_SNAPSHOT=snapshot.decode(),
               PATH=str(Path(sys.executable).parent) + os.pathsep + env.get("PATH", ""),
               PYTHONPATH=os.pathsep.join((str(remote), str(app), str(app / "project"))),
               JAX_PLATFORMS="cpu", OMP_NUM_THREADS="2", OPENBLAS_NUM_THREADS="2")
    env.pop("AGENT_ADMIN_KEY_FILE", None)
    host = worker = None
    try:
        with (folder / "host.log").open("wb") as log:
            host = subprocess.Popen([
                sys.executable, "-u", str(app / "server.py"), "--host", "127.0.0.1",
                "--port", "27915", "--health-port", "27916", "--spectator-port", "27917",
                "--human-seat", str(seat)], cwd=app / "project", env=env, stdout=log,
                stderr=subprocess.STDOUT, start_new_session=True)
        opener = build_opener(ProxyHandler({}))
        for _ in range(180):
            if host.poll() is not None:
                raise RuntimeError("Isolated host exited; inspect host.log")
            try:
                with opener.open("http://127.0.0.1:27916/health", timeout=2) as response:
                    health = json.load(response)
                if health["state"] == "ready":
                    break
            except OSError:
                pass
            time.sleep(1)
        else:
            raise TimeoutError("Isolated host warmup exceeded 180 seconds")
        session = datetime.now().strftime("%Y%m%d-%H%M%S-") + f"{seed % 10000:04}"
        output = folder / "outbound/sessions" / session
        output.mkdir(parents=True)
        shutil.copyfile(deck, output / "model.ydk")
        config = dict(app=str(app), output=str(output), model=model, seed=seed,
                      display_host="isolated-selftest", address="127.0.0.1", port=27915,
                      deck_name="Tenpai acceptance", preference="auto", selftest=True,
                      nickname="ModelSelfTest", password="" if legacy else "YGOPro2",
                      version=0x133E if legacy else 0x1362)
        with (output / "worker.log").open("wb") as log:
            worker = subprocess.Popen([sys.executable, "-u", "-m", "outbound.run"],
                cwd=app / "project", env=env, stdin=subprocess.PIPE, stdout=log,
                stderr=subprocess.STDOUT, start_new_session=True)
        worker.communicate(json.dumps(config).encode(), timeout=240)
        result = json.loads((output / "result.json").read_text())
        print(json.dumps({"folder": str(folder), "result": result}), flush=True)
        if result.get("outcome") not in ("win", "loss", "draw"):
            raise RuntimeError("No completed controlled duel")
        if result.get("decisions", 0) < 1:
            raise RuntimeError("No real model decisions")
    finally:
        for process in (worker, host):
            if process is not None:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                    process.wait(timeout=15)
                except ProcessLookupError:
                    pass
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=10)
        with opener.open("http://127.0.0.1:7916/health", timeout=5) as response:
            after = json.load(response)
        for field in ("policy_pid", "checkpoint_sha256", "default_model_deck"):
            if before.get(field) != after.get(field):
                raise RuntimeError("Concurrent serving-game change: " + field)
        print("PRODUCTION_POLICY_UNCHANGED", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--deck", type=Path, required=True)
    parser.add_argument("--seed", type=int, default=9171)
    parser.add_argument("--seat", type=int, choices=(0, 1), default=0)
    parser.add_argument("--legacy", action="store_true")
    args = parser.parse_args()
    main(args.root, args.deck, args.seed, args.seat, args.legacy)
