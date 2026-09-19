"""Local deployment preparation. Never starts services or contacts game servers."""
import argparse
import getpass
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
from urllib.parse import urlsplit


REPO = Path(__file__).resolve().parent.parent
ROOM = REPO / "room"
APP = ROOM / "app-305m-admin-20260915"
MODULE = "project/ygoenv/ygopro/ygopro_ygoenv.cpython-311-x86_64-linux-gnu.so"
ASSETS = (
    "project/scripts/code_list.txt", "project/assets/locale/en/cards.cdb",
    "project/assets/structured/frozen_semantics_v1.npz", MODULE,
)


def digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def write_json(path, value, replace=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink() or (path.exists() and not replace):
        raise ValueError(f"Refusing to replace existing configuration: {path}")
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                     delete=False) as stream:
        temporary = Path(stream.name)
        os.chmod(temporary, 0o600)
        json.dump(value, stream, indent=2, ensure_ascii=True)
        stream.write("\n")
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def write_secret(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(value + "\n")


def endpoint(value, localhost=False):
    parsed = urlsplit(value)
    local_http = localhost and parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1"}
    if (not parsed.hostname or (parsed.scheme != "https" and not local_http)
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.path not in {"", "/"}):
        raise ValueError("Use a root HTTPS origin without credentials, query or path")
    return value.rstrip("/")


def runtime_assets():
    for name in ASSETS:
        path = APP / name
        if not path.is_file() or path.is_symlink():
            raise ValueError(f"Missing runtime asset: {path}")
    native = json.loads((APP / "native-build.json").read_text(encoding="utf-8"))
    if native.get("module") != MODULE or digest(APP / MODULE) != native.get("sha256"):
        raise ValueError("Native binary does not match native-build.json")
    contract = json.loads((ROOM / "outbound/native_contract.json").read_text(encoding="utf-8"))
    if native.get("source_hashes", {}).get("ygopro.h") != contract["source_sha256"]:
        raise ValueError("Native encoder and outbound encoder contracts do not match")
    deck = APP / "project/assets/deck/BestowedDragon.ydk"
    if not deck.is_file() or not any((APP / "project/script").glob("c*.lua")):
        raise ValueError("Missing default deck or Lua effect scripts")
    mono = APP / "bin/mono"
    if not mono.is_file():
        raise ValueError("Missing native child-process adapter bin/mono")
    return {name: digest(APP / name) for name in ASSETS}


def prepare_runtime(args):
    hashes = runtime_assets()
    for folder in ("project/assets", "project/script"):
        for path in sorted((APP / folder).rglob("*")):
            if path.is_file() and not path.is_symlink():
                hashes[path.relative_to(APP).as_posix()] = digest(path)
    write_json(APP / "manifest.json", {"schema": 1, "files": hashes}, replace=True)
    (APP / "bin/mono").chmod(0o755)
    print(json.dumps({"runtime_files": len(hashes), "app": str(APP)}))


def register_model(args):
    if args.steps < 1:
        raise ValueError("Training steps must be positive")
    if args.activate and ((ROOM / "service.pid").exists()
                          or (ROOM / "data/admin/active-model.json").exists()):
        raise ValueError("--activate is for initial offline setup; switch live models in the panel")
    label = args.label or f"{max(1, args.steps // 1000000)}M"
    if not re.fullmatch(r"[1-9][0-9]{0,5}M", label):
        raise ValueError("Label must be a training-step label such as 640M")
    source = args.checkpoint.resolve()
    if not source.is_file() or not 0 < source.stat().st_size < 256 * 1024**2:
        raise ValueError("Expected a nonempty compatible Flax checkpoint below 256 MiB")
    sidecar = args.sidecar or source.with_name(source.name + ".json")
    if not sidecar.is_file() or sidecar.is_symlink() or sidecar.stat().st_size > 65536:
        raise ValueError("A bounded training architecture sidecar is required")
    document = json.loads(sidecar.read_text(encoding="utf-8"))
    if (document.get("schema") != "ygo-training-checkpoint/v2"
            or not isinstance(document.get("model_args"), dict)
            or document.get("architecture") not in {"structured", "structured-v1", "decision-v1"}):
        raise ValueError("Expected a structured or decision-v1 v2 training sidecar")
    bound = runtime_assets()
    for name in ASSETS:
        if name == MODULE:
            continue
        trained = args.training_project / name.removeprefix("project/")
        if digest(trained) != bound[name]:
            raise ValueError("Training assets/native engine do not match runtime: " + name)
    identity = digest(source)
    if document.get("sha256", identity) != identity:
        raise ValueError("Training sidecar names a different checkpoint hash")
    if document.get("global_step", args.steps) != args.steps:
        raise ValueError("Training steps differ from the checkpoint sidecar")
    model = {"model_label": label, "checkpoint_steps": args.steps,
             "checkpoint_sha256": identity, "checkpoint_file": f"model/catalog/{identity}.flax_model"}
    destination = APP / model["checkpoint_file"]
    if not destination.resolve().is_relative_to(APP.resolve()) or destination.is_symlink():
        raise ValueError("Checkpoint storage escaped the runtime")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() and digest(destination) != identity:
        raise ValueError("Existing content-addressed checkpoint has invalid contents")
    if not destination.exists():
        with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as stream:
            temporary = Path(stream.name)
        try:
            shutil.copyfile(source, temporary)
            if digest(temporary) != identity:
                raise ValueError("Checkpoint changed during copy")
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)
    target = destination.with_name(destination.name + ".json")
    if target.is_symlink():
        raise ValueError("Architecture sidecar cannot be a symlink")
    if target.exists() and json.loads(target.read_text(encoding="utf-8")) != document:
        raise ValueError("Same weights already registered with another architecture sidecar")
    if not target.exists():
        write_json(target, document)
    # Store the actual canonical sidecar hash, not its source formatting.
    catalog_file = ROOM / "admin/models.json"
    catalog = json.loads(catalog_file.read_text()) if catalog_file.exists() else {"schema": 1, "models": []}
    row = {"model": model, "architecture": document["architecture"],
           "assets": bound, "sidecar_sha256": digest(target)}
    previous = next((item for item in catalog["models"]
                     if item["model"]["checkpoint_sha256"] == identity), None)
    if previous and previous != row:
        raise ValueError("Model already registered with different metadata")
    if previous is None:
        catalog["models"].append(row)
    write_json(catalog_file, catalog, replace=True)
    if args.activate:
        write_json(ROOM / "data/admin/active-model.json", model)
    print(json.dumps({"registered": identity, "label": label, "activated": args.activate}))


def init_compute(args):
    gateway = endpoint(args.gateway)
    files = [ROOM / "admin/control.key", ROOM / "admin/agent.json",
             ROOM / "invitations.json", ROOM / "private/panel-bootstrap.json"]
    if any(path.exists() for path in files):
        raise ValueError("Compute configuration already exists; refusing credential rotation")
    (ROOM / "data/admin").mkdir(parents=True, exist_ok=True)
    key, invite = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    write_secret(files[0], key)
    write_json(files[1], {
        "root": str(ROOM), "app": str(APP), "data": str(ROOM / "data"),
        "key_file": str(files[0]), "room_url": "http://127.0.0.1:7916",
        "port": 7918, "model_catalog": str(ROOM / "admin/models.json"),
    })
    write_json(files[2], {"public-bridge": {"sha256": hashlib.sha256(invite.encode()).hexdigest(),
                                          "enabled": True}})
    write_json(files[3], {"endpoint": gateway, "token": invite, "agent_key": key})
    print("Compute configuration created. Transfer room/private/panel-bootstrap.json securely.")


def init_panel(args):
    origin = endpoint(args.origin, localhost=True)
    bootstrap = json.loads(args.bootstrap.read_text(encoding="utf-8"))
    gateway = endpoint(bootstrap["endpoint"])
    if any(not isinstance(bootstrap.get(k), str) or len(bootstrap[k]) < 32 for k in ("token", "agent_key")):
        raise ValueError("Invalid private bootstrap")
    private = REPO / "panel/private"
    if any((private / name).exists() for name in ("site.json", "bridge.json", "user.json", "bridge.key")):
        raise ValueError("Panel configuration already exists; refusing credential rotation")
    password = getpass.getpass("New panel password (16-128 characters): ")
    if not 16 <= len(password) <= 128 or password != getpass.getpass("Confirm panel password: "):
        raise ValueError("Invalid or mismatched panel password")
    bridge_app = REPO / "panel/ygo-tcp-bridge/app"
    sys.path.insert(0, str(bridge_app))
    from configure_tcp_bridge import create_config
    config, codes = create_config(bootstrap, [
        ("model-ygo2", "YGOPro2", str(args.deck.resolve())),
        ("model-legacy", "legacy", str(args.deck.resolve())),
    ])
    sys.path.insert(0, str(REPO / "panel/ygo-admin/app"))
    from auth import password_hash
    key = secrets.token_urlsafe(32)
    data = REPO / "panel/data"
    data.mkdir(parents=True, exist_ok=True)
    write_secret(private / "bridge.key", key)
    config["management"] = {"key_file": str(private / "bridge.key"),
                            "state_file": str(data / "bridge-passwords.json"), "port": 6010}
    write_json(private / "bridge.json", config)
    write_json(private / "room-codes.json", codes)
    write_json(private / "user.json", {"username": args.username, "password": password_hash(password)})
    write_json(private / "site.json", {
        "origin": origin, "host": "127.0.0.1", "port": 8090,
        "helpers": str(bridge_app), "user_file": str(private / "user.json"), "data": str(data),
        "agent_url": gateway + "/control", "agent_key": bootstrap["agent_key"],
        "bridge_url": "http://127.0.0.1:6010", "bridge_key": key,
        "game_host": args.game_host, "game_port": 6008,
    })
    print("Panel configuration created. Credentials are only in panel/private/.")


def units(args):
    if not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", args.user):
        raise ValueError("Invalid Linux service account name")
    if re.search(r"[\s%\\\"']", str(REPO)) or sys.platform != "linux":
        raise ValueError("Install under a Linux path without spaces or systemd escaping characters")
    if args.role == "compute":
        python = ROOM / "venv/bin/python"
        commands = {
            "ygo-room": f"{python} {ROOM}/room_service.py --app-dir {APP}",
            "ygo-agent": f"{python} {ROOM}/admin/agent.py --config {ROOM}/admin/agent.json",
        }
        working = ROOM
    else:
        python = REPO / "panel/venv/bin/python"
        commands = {
            "ygo-bridge": f"{python} {REPO}/panel/ygo-tcp-bridge/app/tcp_bridge.py "
                          f"--config {REPO}/panel/private/bridge.json --host 0.0.0.0 --port 6008",
            "ygo-panel": f"{python} {REPO}/panel/ygo-admin/app/web_server.py "
                         f"--config {REPO}/panel/private/site.json",
        }
        working = REPO / "panel"
    output = REPO / "deploy/generated"
    output.mkdir(parents=True, exist_ok=True)
    for name, command in commands.items():
        path = output / f"{name}.service"
        if path.exists():
            raise ValueError(f"Unit already exists: {path}")
        path.write_text(
            f"[Unit]\nDescription={name}\nAfter=network-online.target\nWants=network-online.target\n\n"
            f"[Service]\nType=simple\nUser={args.user}\nWorkingDirectory={working}\n"
            f"ExecStart={command}\nRestart=always\nRestartSec=5\nTimeoutStopSec=40\n"
            "KillMode=control-group\nUMask=0077\nNoNewPrivileges=true\nPrivateTmp=true\n"
            "Environment=PYTHONUNBUFFERED=1\n\n[Install]\nWantedBy=multi-user.target\n",
            encoding="utf-8")
    print(f"Units written to {output}; no services were installed or started.")


def preflight(args):
    hashes = runtime_assets()
    catalog = ROOM / "admin/models.json"
    sys.path[:0] = [str(ROOM / "admin"), str(APP)]
    from registered_catalog import RegisteredCatalog
    registered = RegisteredCatalog(catalog, APP)
    models = registered.listing()["items"]
    for row in models:
        registered.stage(row["id"])
    manifest = json.loads((APP / "manifest.json").read_text())
    for name, expected in manifest["files"].items():
        path = (APP / name).resolve()
        if not path.is_relative_to(APP.resolve()) or digest(path) != expected:
            raise ValueError("Runtime manifest check failed: " + name)
    active = json.loads((ROOM / "data/admin/active-model.json").read_text())
    model = registered.stage(active["checkpoint_sha256"])
    if model != active:
        raise ValueError("Active model does not match registered metadata")
    print(json.dumps({"assets": len(hashes), "models": len(models), "active": model["model_label"]}))
    if args.warmup:
        python = ROOM / "venv/bin/python"
        environment = dict(os.environ, AGENT195M_ROOT=str(APP), AGENT195M_DATA=str(ROOM / "data"),
                           AGENT_MODEL_SNAPSHOT=json.dumps(model),
                           PYTHONPATH=os.pathsep.join([str(APP), str(APP / "project")]),
                           JAX_PLATFORMS="cpu", CUDA_VISIBLE_DEVICES="",
                           OMP_NUM_THREADS="2", OPENBLAS_NUM_THREADS="1")
        subprocess.run([str(python), str(APP / "worker.py"), "--warmup-only",
                        "--output", str(ROOM / "data/preflight")],
                       cwd=APP / "project", env=environment, check=True, timeout=300)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("prepare-runtime").set_defaults(func=prepare_runtime)
    register = commands.add_parser("register-model")
    register.add_argument("--checkpoint", type=Path, required=True)
    register.add_argument("--training-project", type=Path, required=True)
    register.add_argument("--sidecar", type=Path)
    register.add_argument("--steps", type=int, required=True)
    register.add_argument("--label")
    register.add_argument("--activate", action="store_true")
    register.set_defaults(func=register_model)
    compute = commands.add_parser("init-compute")
    compute.add_argument("--gateway", required=True)
    compute.set_defaults(func=init_compute)
    panel = commands.add_parser("init-panel")
    panel.add_argument("--bootstrap", type=Path, required=True)
    panel.add_argument("--origin", required=True)
    panel.add_argument("--game-host", required=True)
    panel.add_argument("--deck", type=Path, required=True)
    panel.add_argument("--username", default="admin")
    panel.set_defaults(func=init_panel)
    service = commands.add_parser("write-units")
    service.add_argument("--role", choices=["compute", "panel"], required=True)
    service.add_argument("--user", default="ygo")
    service.set_defaults(func=units)
    check = commands.add_parser("preflight")
    check.add_argument("--warmup", action="store_true")
    check.set_defaults(func=preflight)
    args = parser.parse_args()
    try:
        args.func(args)
    except (ValueError, OSError, KeyError, subprocess.SubprocessError) as error:
        parser.exit(1, f"Deployment preparation failed: {error}\n")


if __name__ == "__main__":
    main()
