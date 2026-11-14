"""Run hash-pinned model-vs-model games through the native room server.

This runner is intended for the compute host, not for the live room service.
For every game it creates a private room data directory, loads model A as the
resident policy, starts model B through ``outbound.run``, and preserves the
native result, replay, network trace, and decision log. No result is accepted
from a scripted score or from a model-side claim without an engine terminal
message.

The current native room architecture is a resident-model versus one external
client. Therefore this tool is the isolated engine validation stage for model
matches. It does not write directly into a live tournament room; production
rooms continue to accept results only from the PvP gateway callback.

Example on the compute host:

    python3 tools/model_tournament.py \
      --app-root /root/autodl-tmp/agent195m-room-20260915/app-305m-admin-20260915 \
      --manifest /root/autodl-tmp/agent195m-room-20260915/app-305m-admin-20260915/model/compatible-experts.json \
      --deck /root/autodl-tmp/ygo-agent-main-gpu-20260824/experiments/expert-sky-striker-20260919/evaluations/official-3m/expert_target.ydk \
      --output /root/autodl-tmp/model-tournament \
      --bo2-count 1

The room server is forced to CPU and receives a private AGENT195M_DATA root.
It never uses or changes the running training/evaluation processes.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import signal
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import urlopen


BO2_GAMES = 2
MAX_BO2 = 10
DECK_SECTIONS = ("main", "extra", "side")
MODEL_SUFFIXES = {
    "checkpoint": ".flax_model",
    "sidecar": ".flax_model.json",
}


@dataclass(frozen=True)
class Model:
    model_id: str
    label: str
    steps: int
    checkpoint: Path
    sidecar: Path

    def descriptor(self, app_root: Path) -> dict[str, object]:
        relative = self.checkpoint.relative_to(app_root).as_posix()
        return {
            "model_label": self.label,
            "checkpoint_steps": self.steps,
            "checkpoint_sha256": self.model_id,
            "checkpoint_file": relative,
        }


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def parse_deck(path: Path) -> dict[str, list[int]]:
    sections = {name: [] for name in DECK_SECTIONS}
    section = "main"
    for line_number, raw in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        line = raw.strip()
        if line == "#main":
            section = "main"
        elif line == "#extra":
            section = "extra"
        elif line == "!side":
            section = "side"
        elif not line or line.startswith("#"):
            continue
        elif line.isascii() and line.isdecimal():
            card = int(line)
            if not 0 < card <= 0xFFFFFFFF:
                raise ValueError(f"invalid card ID at {path}:{line_number}")
            sections[section].append(card)
        else:
            raise ValueError(f"invalid deck line at {path}:{line_number}")
    if not 40 <= len(sections["main"]) <= 60:
        raise ValueError("fixed deck main section must contain 40-60 cards")
    if len(sections["extra"]) > 15 or len(sections["side"]) > 15:
        raise ValueError("fixed deck extra and side sections must contain at most 15 cards")
    return sections


def write_deck(path: Path, deck: dict[str, list[int]]) -> None:
    lines = ["#main"]
    lines.extend(str(card) for card in deck["main"])
    lines.append("#extra")
    lines.extend(str(card) for card in deck["extra"])
    lines.append("!side")
    lines.extend(str(card) for card in deck["side"])
    path.write_text("\n".join(lines) + "\n", encoding="ascii")


def canonical_deck_hash(deck: dict[str, list[int]]) -> str:
    payload = "\n".join(
        f"{section}:{','.join(str(card) for card in deck[section])}"
        for section in DECK_SECTIONS
    ).encode("ascii")
    return hashlib.sha256(payload).hexdigest()


def deck_from_file(path: Path) -> dict[str, list[int]]:
    return parse_deck(path)


def wait_health(url: str, timeout: float = 240.0) -> dict:
    deadline = time.monotonic() + timeout
    last_error = ""
    while time.monotonic() < deadline:
        try:
            with urlopen(url, timeout=3) as response:
                value = json.loads(response.read().decode("utf-8"))
            if value.get("state") == "ready":
                return value
            last_error = str(value)
        except (OSError, ValueError, HTTPError, URLError) as error:
            last_error = str(error)
        time.sleep(0.5)
    raise TimeoutError(f"room server did not become ready: {last_error}")


def terminate_process(process: subprocess.Popen[bytes] | None) -> None:
    if process is None or process.poll() is not None:
        return
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
        process.wait(timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
            process.wait(timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            pass


def manifest_model(manifest: dict, model_id: str, app_root: Path) -> Model:
    rows = manifest.get("items")
    if not isinstance(rows, list) or len(rows) != 5:
        raise ValueError("corrected Top5 manifest must contain exactly five items")
    row = next((item for item in rows if item.get("id") == model_id), None)
    if row is None:
        raise ValueError(f"model is not present in the corrected Top5 manifest: {model_id}")
    if not isinstance(row.get("checkpoint_steps"), int):
        raise ValueError("model checkpoint_steps is invalid")
    checkpoint = app_root / "model" / "catalog" / f"{model_id}.flax_model"
    sidecar = checkpoint.with_name(checkpoint.name + ".json")
    if not checkpoint.is_file() or checkpoint.is_symlink():
        raise FileNotFoundError(checkpoint)
    if not sidecar.is_file() or sidecar.is_symlink():
        raise FileNotFoundError(sidecar)
    if sha256(checkpoint) != model_id:
        raise ValueError(f"checkpoint hash mismatch: {checkpoint}")
    hashes = row.get("hashes") or {}
    if hashes.get("sidecar") and sha256(sidecar) != hashes["sidecar"]:
        raise ValueError(f"sidecar hash mismatch: {sidecar}")
    return Model(
        model_id=model_id,
        label=f"{row['checkpoint_steps'] // 1_000_000}M",
        steps=row["checkpoint_steps"],
        checkpoint=checkpoint,
        sidecar=sidecar,
    )


def model_from_manifest(manifest: dict, requested: str | None, app_root: Path, index: int) -> Model:
    rows = manifest.get("items") or []
    if requested:
        return manifest_model(manifest, requested, app_root)
    return manifest_model(manifest, rows[index]["id"], app_root)


def prepare_room_data(root: Path, deck: dict[str, list[int]], app_root: Path) -> Path:
    admin = root / "admin"
    admin.mkdir(parents=True, exist_ok=True)
    (admin / "control.key").write_text(secrets.token_urlsafe(32) + "\n", encoding="ascii")
    atomic_json(admin / "room.json", {
        "accepting": True,
        "admission_revision": 0,
        "spectating": False,
        "spectator_capacity": 1,
        "default_name": "Fixed Sky Striker",
        "default_deck": deck,
    })
    return admin


def start_room(
    app_root: Path,
    data_root: Path,
    model: Model,
    deck: dict[str, list[int]],
    port: int,
    health_port: int,
    spectator_port: int,
    human_seat: int,
    log_path: Path,
) -> subprocess.Popen[bytes]:
    prepare_room_data(data_root, deck, app_root)
    env = dict(os.environ)
    env.update({
        "AGENT195M_ROOT": str(app_root),
        "AGENT195M_DATA": str(data_root),
        "AGENT_MODEL_SNAPSHOT": json.dumps(model.descriptor(app_root)),
        "AGENT_ADMIN_KEY_FILE": str(data_root / "admin" / "control.key"),
        "JAX_PLATFORMS": "cpu",
        "CUDA_VISIBLE_DEVICES": "",
        "XLA_PYTHON_CLIENT_PREALLOCATE": "false",
        "OMP_NUM_THREADS": "2",
        "OPENBLAS_NUM_THREADS": "1",
        "PYTHONUNBUFFERED": "1",
        "PATH": os.pathsep.join([
            str(Path(sys.executable).resolve().parent),
            str(app_root / "bin"),
            env.get("PATH", os.defpath),
        ]),
        "PYTHONPATH": os.pathsep.join(
            [str(app_root), str(app_root / "project")]
        ),
    })
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log = log_path.open("wb")
    try:
        process = subprocess.Popen(
            [
                sys.executable,
                str(app_root / "server.py"),
                "--host", "127.0.0.1",
                "--port", str(port),
                "--health-port", str(health_port),
                "--spectator-port", str(spectator_port),
                "--human-seat", str(human_seat),
            ],
            cwd=app_root,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=(os.name == "posix"),
        )
    except BaseException:
        log.close()
        raise
    process._model_tournament_log = log  # type: ignore[attr-defined]
    return process


def start_outbound(
    app_root: Path,
    output: Path,
    model: Model,
    deck_path: Path,
    port: int,
    seed: int,
    preference: str,
    log_path: Path,
) -> subprocess.Popen[bytes]:
    output.mkdir(parents=True, exist_ok=True)
    copied_deck = output / "model.ydk"
    shutil.copyfile(deck_path, copied_deck)
    config = {
        "app": str(app_root),
        "output": str(output),
        "model": model.descriptor(app_root),
        "display_host": "127.0.0.1",
        "address": "127.0.0.1",
        "port": port,
        "deck_name": "Fixed Sky Striker",
        "deck_source": "local",
        "auth_mode": "none",
        "account_password": "",
        "preference": preference,
        "nickname": f"Model-{model.label}",
        "password": "",
        "version": 0x1362,
        "seed": seed,
        "selftest": False,
    }
    env = dict(os.environ)
    room_root = app_root.parent
    env.update({
        "AGENT195M_ROOT": str(app_root),
        "JAX_PLATFORMS": "cpu",
        "CUDA_VISIBLE_DEVICES": "",
        "XLA_PYTHON_CLIENT_PREALLOCATE": "false",
        "OMP_NUM_THREADS": "2",
        "OPENBLAS_NUM_THREADS": "1",
        "PYTHONUNBUFFERED": "1",
        "PYTHONPATH": os.pathsep.join(
            [str(room_root), str(app_root), str(app_root / "project")]
        ),
    })
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log = log_path.open("wb")
    try:
        process = subprocess.Popen(
            [sys.executable, "-u", "-m", "outbound.run"],
            cwd=app_root / "project",
            env=env,
            stdin=subprocess.PIPE,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=(os.name == "posix"),
        )
        assert process.stdin is not None
        process.stdin.write(json.dumps(config).encode("utf-8"))
        process.stdin.close()
    except BaseException:
        log.close()
        raise
    process._model_tournament_log = log  # type: ignore[attr-defined]
    return process


def close_log(process: subprocess.Popen[bytes] | None) -> None:
    if process is None:
        return
    stream = getattr(process, "_model_tournament_log", None)
    if stream is not None:
        stream.close()


def latest_session(data_root: Path, started_at: float) -> Path | None:
    candidates = []
    for path in (data_root / "sessions").glob("*"):
        if not path.is_dir() or not (path / "session.json").is_file():
            continue
        try:
            if path.stat().st_mtime >= started_at - 2:
                candidates.append(path)
        except OSError:
            continue
    return max(candidates, key=lambda path: path.stat().st_mtime) if candidates else None


def read_result(path: Path) -> dict:
    result_path = path / "result.json"
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if result_path.is_file():
            try:
                return read_json(result_path)
            except (OSError, ValueError):
                pass
        time.sleep(0.25)
    return {}


def run_game(
    app_root: Path,
    output: Path,
    deck_path: Path,
    model_a: Model,
    model_b: Model,
    game_number: int,
    port_base: int,
) -> dict:
    game_root = output / f"game-{game_number:03d}"
    data_root = game_root / "room-data"
    outbound_root = game_root / "outbound"
    game_root.mkdir(parents=True, exist_ok=False)
    seed = secrets.randbelow(2**31 - 1) + 1
    human_seat = 1 if game_number % 2 == 1 else 0
    preference = "second" if game_number % 2 == 1 else "first"
    port = port_base + game_number * 3
    health_port = port + 1
    spectator_port = port + 2
    started_at = time.time()
    room_process = None
    outbound_process = None
    deck = parse_deck(deck_path)
    deck_hash = sha256(deck_path)
    try:
        room_process = start_room(
            app_root, data_root, model_a, deck, port, health_port,
            spectator_port, human_seat, game_root / "room.log",
        )
        wait_health(f"http://127.0.0.1:{health_port}/health")
        outbound_process = start_outbound(
            app_root, outbound_root, model_b, deck_path, port, seed,
            preference, game_root / "outbound.log",
        )
        try:
            return_code = outbound_process.wait(timeout=1800)
        except subprocess.TimeoutExpired:
            terminate_process(outbound_process)
            return_code = None
        time.sleep(1.0)
        session = latest_session(data_root, started_at)
        outbound_result = read_result(outbound_root)
        room_result = read_result(session) if session else {}
        games = outbound_result.get("games") or []
        if outbound_result.get("event") != "complete" or not games:
            raise RuntimeError(
                f"outbound model did not receive an authoritative result: "
                f"returncode={return_code}, error={outbound_result.get('error')}"
            )
        game = games[-1]
        if game.get("winner") not in (0, 1, 2):
            raise RuntimeError("native game result has no valid MSG_WIN winner")
        replay_status = {}
        replay_status_path = outbound_root / "replay-status.json"
        if replay_status_path.is_file():
            replay_status = read_json(replay_status_path)
        native_replay = {}
        if session:
            native_replay_path = session / "replay-status.json"
            if native_replay_path.is_file():
                native_replay = read_json(native_replay_path)
        if not native_replay.get("recording_available"):
            raise RuntimeError("native room completed without a structurally valid replay")
        native_deck_hashes = {}
        if session:
            for name in ("human.ydk", "model.ydk"):
                path = session / name
                if path.is_file():
                    native_deck_hashes[name] = canonical_deck_hash(deck_from_file(path))
        expected_deck_hash = canonical_deck_hash(deck)
        if native_deck_hashes and any(
            value != expected_deck_hash for value in native_deck_hashes.values()
        ):
            raise RuntimeError("native room deck differs from the fixed deck")
        room_winner = room_result.get("winner_seat")
        if room_winner not in (0, 1) or room_winner != game.get("winner"):
            raise RuntimeError(
                f"resident and outbound engine results disagree: "
                f"{room_winner} != {game.get('winner')}"
            )
        result = {
            "game": game_number,
            "seed": seed,
            "human_seat": human_seat,
            "requested_first": "a" if preference == "second" else "b",
            "model_a": {
                "id": model_a.model_id,
                "steps": model_a.steps,
                "label": model_a.label,
                "role": "resident",
            },
            "model_b": {
                "id": model_b.model_id,
                "steps": model_b.steps,
                "label": model_b.label,
                "role": "outbound",
            },
            "deck": {
                "path": str(deck_path),
                "sha256": deck_hash,
                "canonical_sha256": expected_deck_hash,
            },
            "outbound_returncode": return_code,
            "outbound": outbound_result,
            "replay": replay_status,
            "native_replay": native_replay,
            "native_deck_hashes": native_deck_hashes,
            "room": room_result,
            "session_dir": str(session) if session else None,
            "replay_files": [
                str(path.relative_to(game_root))
                for path in game_root.rglob("*.yrp")
                if path.is_file()
            ],
        }
        atomic_json(game_root / "result.json", result)
        return result
    finally:
        terminate_process(outbound_process)
        terminate_process(room_process)
        close_log(outbound_process)
        close_log(room_process)


def validate_pair(results: list[dict]) -> None:
    for pair_start in range(0, len(results), BO2_GAMES):
        pair = results[pair_start:pair_start + BO2_GAMES]
        if len(pair) != BO2_GAMES:
            break
        actual = [
            "b" if row["outbound"].get("went_first") else "a"
            for row in pair
        ]
        if actual != ["a", "b"]:
            raise RuntimeError(
                f"BO2 {pair_start // BO2_GAMES + 1} did not swap actual first player: {actual}"
            )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app-root", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--deck", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model-a")
    parser.add_argument("--model-b")
    parser.add_argument("--bo2-count", type=int, default=1)
    parser.add_argument("--port-base", type=int, default=23100)
    args = parser.parse_args()
    if not 1 <= args.bo2_count <= MAX_BO2:
        parser.error(f"--bo2-count must be between 1 and {MAX_BO2}")
    app_root = args.app_root.resolve()
    manifest_path = args.manifest.resolve()
    deck_path = args.deck.resolve()
    if not app_root.is_dir() or not manifest_path.is_file() or not deck_path.is_file():
        parser.error("app root, manifest, and deck must exist")
    deck = parse_deck(deck_path)
    manifest = read_json(manifest_path)
    model_a = model_from_manifest(manifest, args.model_a, app_root, 0)
    model_b = model_from_manifest(manifest, args.model_b, app_root, 1)
    if model_a.model_id == model_b.model_id:
        parser.error("model-a and model-b must be different Top5 checkpoints")
    args.output.mkdir(parents=True, exist_ok=True)
    atomic_json(args.output / "manifest.json", {
        "manifest": str(manifest_path),
        "manifest_sha256": sha256(manifest_path),
        "deck": str(deck_path),
        "deck_sha256": sha256(deck_path),
        "deck_counts": {key: len(value) for key, value in deck.items()},
        "model_a": model_a.descriptor(app_root),
        "model_b": model_b.descriptor(app_root),
        "bo2_count": args.bo2_count,
        "created_at": time.time(),
        "native_result_source": "MSG_WIN",
    })
    results = []
    for game_number in range(1, args.bo2_count * BO2_GAMES + 1):
        result = run_game(
            app_root, args.output, deck_path, model_a, model_b,
            game_number, args.port_base,
        )
        results.append(result)
        print(json.dumps({
            "game": game_number,
            "outcome": result["outbound"].get("outcome"),
            "winner": result["outbound"].get("winner"),
            "went_first": result["outbound"].get("went_first"),
            "replay_received": result["native_replay"].get("recording_available"),
        }, ensure_ascii=False), flush=True)
    validate_pair(results)
    atomic_json(args.output / "results.json", {
        "status": "complete",
        "games": results,
        "bo2_count": args.bo2_count,
        "completed_at": time.time(),
    })
    print(json.dumps({
        "ok": True,
        "output": str(args.output),
        "games": len(results),
        "bo2_count": args.bo2_count,
    }, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
