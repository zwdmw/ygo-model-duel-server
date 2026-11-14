"""Start an isolated local tournament; Ctrl+C stops only its child processes."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import time
from urllib.request import ProxyHandler, build_opener

ROOT = Path(__file__).resolve().parents[1]


def wait_ready(process, probe, label, timeout=90):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"{label} exited ({process.returncode}); inspect the logs")
        try:
            if probe():
                return
        except (OSError, ValueError):
            pass
        time.sleep(0.25)
    raise RuntimeError(f"{label} did not become ready; inspect the logs")


def tcp_ready(port):
    with socket.create_connection(("127.0.0.1", port), timeout=1):
        return True


def health(port):
    # A user's HTTP proxy must never intercept the local health checks.
    with build_opener(ProxyHandler({})).open(
        f"http://127.0.0.1:{port}/health", timeout=2
    ) as response:
        return json.load(response).get("status") == "ok"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["manual", "native"], default="manual")
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data/local-tournament")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--pvp-port", type=int, default=8790)
    parser.add_argument("--tcp-port", type=int, default=6009)
    parser.add_argument("--engine-port", type=int, default=7711)
    parser.add_argument("--aux-port-base", type=int, default=17911)
    parser.add_argument("--public-host", default="127.0.0.1")
    parser.add_argument("--tcp-host", default="127.0.0.1")
    parser.add_argument("--engine-dir", type=Path, default=ROOT / "runtime/evolution")
    parser.add_argument("--node", default=shutil.which("node"))
    args = parser.parse_args()
    data = args.data_dir.resolve()
    data.mkdir(parents=True, exist_ok=True)
    private = data / "private"
    private.mkdir(mode=0o700, exist_ok=True)
    credentials = private / "service-keys.json"
    if not credentials.exists():
        keys = {name: secrets.token_hex(32) for name in
                ("TOURNAMENT_ADMIN_KEY", "PVP_INTERNAL_KEY", "TOURNAMENT_INTERNAL_KEY")}
        with credentials.open("x", encoding="utf-8") as stream:
            os.chmod(credentials, 0o600)
            json.dump(keys, stream, indent=2)
    keys = json.loads(credentials.read_text("utf-8"))
    ports = [args.port]
    if args.mode == "native":
        ports += [args.pvp_port, args.tcp_port, args.engine_port,
                  *range(args.aux_port_base, args.aux_port_base + 5)]
        if sys.platform != "linux" or not args.node:
            parser.error("Native mode requires Linux x86-64 and Node.js 24.14.0")
        if not (args.engine_dir / "dist/src/index.js").is_file():
            parser.error("Run tools/fetch_runtime.py tournament first")
        version = subprocess.check_output([args.node, "--version"], text=True).strip()
        if version != "v24.14.0":
            parser.error(f"The published engine is pinned to Node.js v24.14.0; found {version}")
    if len(set(ports)) != len(ports) or any(not 0 < port < 65536 for port in ports):
        parser.error("Choose distinct TCP ports between 1 and 65535")
    for port in ports:
        with socket.socket() as probe:
            probe.bind(("0.0.0.0", port))
    logs, children = [], []
    env = {**os.environ, **keys, "PYTHONUNBUFFERED": "1"}
    for name in ("PVP_URL", "PVP_TOURNAMENT_URL", "PVP_FIXED_DECK", "PVP_ROOM_REGISTRY_DIR"):
        env.pop(name, None)

    def start(label, command, cwd=ROOT, extra=None):
        log = (data / f"{label}.log").open("ab")
        logs.append(log)
        process = subprocess.Popen(command, cwd=cwd, env={**env, **(extra or {})},
                                   stdout=log, stderr=subprocess.STDOUT,
                                   start_new_session=sys.platform != "win32")
        children.append(process)
        return process

    def interrupted(_signal, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupted)
    try:
        tournament_command = [sys.executable, "-m", "tournament.server", "--data-dir",
                              str(data / "tournament"), "--host", "127.0.0.1",
                              "--port", str(args.port)]
        if args.mode == "native":
            registry = data / "provisioning"
            registry.mkdir(exist_ok=True)
            engine_dir = args.engine_dir.resolve()
            # Copy only mutable engine output to the instance, keeping the installed
            # runtime and the production server entirely separate.
            engine_env = {
                "NODE_ENV": "production", "USE_REDIS": "false", "RANK_ENABLED": "false",
                "RATE_LIMIT_ENABLED": "false", "RESOURCES_DIR": str(engine_dir / "resources/current"),
                "MANIFEST_PATH": str(engine_dir / "resources.manifest.json"),
                "RANK_GROUPS_PATH": str(engine_dir / "config/rank-groups.json"),
                "ADMIN_API_KEY": keys["PVP_INTERNAL_KEY"],
                "YGO_PROVISIONING_DIR": str(registry), "YGOPRO_PORT": str(args.engine_port),
                "HOST_PORT": str(args.aux_port_base), "HTTP_PORT": str(args.aux_port_base + 1),
                "WEBSOCKET_PORT": str(args.aux_port_base + 2),
                "WEBSOCKET_DUEL_PORT": str(args.aux_port_base + 3),
                "YGOPRO_WEBSOCKET_PORT": str(args.aux_port_base + 4),
                "NODE_OPTIONS": "--max-old-space-size=256", "SIDE_TIMEOUT_MINUTES": "3",
            }
            engine = start("engine", [args.node, str(engine_dir / "dist/src/index.js")],
                           cwd=data, extra=engine_env)
            wait_ready(engine, lambda: tcp_ready(args.engine_port), "engine")
            gateway = start("pvp", [sys.executable, "-m", "pvp.server", "--data-dir",
                str(data / "pvp"), "--http-port", str(args.pvp_port), "--tcp-port",
                str(args.tcp_port), "--tcp-host", args.tcp_host, "--engine-port",
                str(args.engine_port), "--fixed-deck", str(ROOT / "decks/SkyStriker.ydk"),
                "--room-registry-dir", str(registry), "--tournament-url",
                f"http://127.0.0.1:{args.port}"], extra={
                    "PVP_REPLAY_BUILDER": str(ROOT / "tools/build_partial_series_replay.py")})
            wait_ready(gateway, lambda: health(args.pvp_port), "pvp")
            tournament_command += ["--pvp-url", f"http://127.0.0.1:{args.pvp_port}",
                                   "--pvp-public-host", args.public_host]
        tournament = start("tournament", tournament_command)
        wait_ready(tournament, lambda: health(args.port), "tournament")
        print(f"Ready: http://127.0.0.1:{args.port}/", flush=True)
        print(f"Administrator key: TOURNAMENT_ADMIN_KEY in {credentials}", flush=True)
        print(f"Logs and persistent state: {data}", flush=True)
        while True:
            for child in children:
                if child.poll() is not None:
                    raise RuntimeError(f"Child exited ({child.returncode}); inspect {data}")
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        for child in reversed(children):
            if child.poll() is None:
                if sys.platform == "win32":
                    child.terminate()
                else:
                    os.killpg(child.pid, signal.SIGTERM)
        for child in children:
            try:
                child.wait(timeout=15)
            except subprocess.TimeoutExpired:
                if sys.platform == "win32":
                    child.kill()
                else:
                    os.killpg(child.pid, signal.SIGKILL)
                child.wait()
        for log in logs:
            log.close()


if __name__ == "__main__":
    main()
