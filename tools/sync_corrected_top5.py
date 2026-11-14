"""Publish the current Top 5 corrected Sky Striker experts atomically."""
import argparse
from collections import Counter
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import time


TOP_N = 5
VALIDATION_VERSION = 2
EVALUATION_REVISION = "native-own-deck-candidates-v2"
SIDECAR_REVISION = "sky-selfplay-own-deck-candidates-v2"
RUNTIME_REVISION = "native-own-deck-candidates-v2-protocol-v3"
ASSETS = (
    "assets/structured/frozen_semantics_v1.npz",
    "scripts/code_list.txt",
    "assets/locale/en/cards.cdb",
)
SUFFIXES = {
    "checkpoint": ".flax_model",
    "sidecar": ".flax_model.json",
    "result": ".evaluation.json",
    "status": ".evaluation-status.json",
    "provenance": ".evaluation-provenance.json",
    "loaded": ".evaluation-native.json",
}
SHA256 = re.compile(r"[0-9a-f]{64}")


@contextmanager
def single_writer(state):
    """The watcher and one-shot publisher share a Linux advisory lock."""
    import fcntl

    path = Path(str(state) + ".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def digest(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ValueError("Expected a regular file: " + str(path))
    before = path.stat()
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
    after = path.stat()
    if any(getattr(before, key) != getattr(after, key)
           for key in ("st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")):
        raise ValueError("File changed during hashing: " + str(path))
    return result.hexdigest()


def read_json(path, limit=4 * 1024 * 1024):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > limit:
        raise ValueError("Invalid JSON artifact: " + str(path))
    return json.loads(path.read_text(encoding="utf-8"))


def inside(path, root):
    path, root = Path(path), Path(root).resolve()
    if path.is_symlink() or not path.resolve().is_relative_to(root):
        raise ValueError("Artifact escaped its root: " + str(path))
    return path.resolve()


def resolved_file(path, root):
    """Allow a source asset link only when its final target stays in root."""
    path, root = Path(path), Path(root).resolve()
    target = path.resolve()
    if not target.is_relative_to(root) or not target.is_file():
        raise ValueError("Asset escaped its source root: " + str(path))
    return target


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent,
                                     delete=False) as stream:
        temporary = Path(stream.name)
        os.chmod(temporary, mode)
        json.dump(value, stream, ensure_ascii=True, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_copy(source, target, expected):
    source, target = Path(source), Path(target)
    if digest(source) != expected:
        raise ValueError("Source hash changed before copy: " + str(source))
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_file() and not target.is_symlink() and digest(target) == expected:
        return
    mode = target.stat().st_mode & 0o777 if target.exists() else 0o600
    with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as stream:
        temporary = Path(stream.name)
    try:
        with source.open("rb") as source_stream, temporary.open("wb") as target_stream:
            for chunk in iter(lambda: source_stream.read(1024 * 1024), b""):
                target_stream.write(chunk)
            target_stream.flush()
            os.fsync(target_stream.fileno())
        os.chmod(temporary, mode)
        if digest(temporary) != expected or digest(source) != expected:
            raise ValueError("Artifact changed during copy: " + str(source))
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def _int(value, name):
    if type(value) is not int:
        raise ValueError("Invalid integer: " + name)
    return value


def _summary(raw):
    summary = raw.get("summary") or (raw.get("structured_model") or {}).get("overall")
    if not isinstance(summary, dict):
        raise ValueError("Evaluation summary is missing")
    games = _int(summary.get("games"), "games")
    wins = _int(summary.get("wins"), "wins")
    losses = _int(summary.get("losses"), "losses")
    draws = _int(summary.get("draws"), "draws")
    if games != 64 or min(wins, losses, draws) < 0 or wins + losses + draws != games:
        raise ValueError("Evaluation summary is incomplete")
    rate = summary.get("strict_win_rate")
    if not isinstance(rate, (int, float)) or not 0 <= rate <= 1:
        raise ValueError("Invalid strict win rate")
    if rate != wins / games:
        raise ValueError("Strict win rate differs from wins")
    return {"games": games, "wins": wins, "losses": losses, "draws": draws,
            "win_rate": rate}


def _validate_games(raw, total):
    games = raw.get("games")
    if not isinstance(games, dict):
        raise ValueError("Evaluation game records are missing")
    counted = Counter()
    for seat, key in enumerate(("structured_going_first", "structured_going_second")):
        rows = games.get(key)
        if not isinstance(rows, list) or len(rows) != 32:
            raise ValueError("Evaluation seat is incomplete")
        if sorted(row.get("environment_index") for row in rows) != list(range(32)):
            raise ValueError("Evaluation seat contains duplicate games")
        for row in rows:
            reward = row.get("structured_return")
            terminal = row.get("terminal_to_play")
            environment_return = row.get("environment_return")
            expected = "win" if reward > 0 else "loss" if reward < 0 else "draw"
            if (row.get("structured_seat") != seat or row.get("truncated") is not False
                    or not 0 < row.get("length", 0) <= 1000
                    or terminal not in (0, 1) or reward not in (-1, 0, 1)
                    or reward != environment_return * (1 if terminal == seat else -1)
                    or row.get("outcome") != expected):
                raise ValueError("Evaluation game record is invalid")
            counted[expected] += 1
    actual = {"games": sum(counted.values()), "wins": counted["win"],
              "losses": counted["loss"], "draws": counted["draw"]}
    if any(actual[key] != total[key] for key in actual):
        raise ValueError("Evaluation game records differ from summary")


def _validate_runtime(app):
    native = read_json(Path(app) / "native-build.json", limit=64 * 1024)
    if native.get("observation_revision") != RUNTIME_REVISION:
        raise ValueError("Runtime native observation revision is not corrected")
    if native.get("own_deck_candidate_ids") != 2 or native.get("full_spectator_feed") != 1:
        raise ValueError("Runtime native module lacks corrected observation or spectator feed")
    return native


def candidate(row, experiment, app, runtime, asset_hashes):
    if not isinstance(row, dict) or row.get("status") != "complete":
        raise ValueError("Evaluation index row is incomplete")
    if row.get("truncated") != 0 or row.get("observation_revision") != EVALUATION_REVISION:
        raise ValueError("Evaluation index row is not corrected")
    steps = _int(row.get("checkpoint_steps"), "checkpoint_steps")
    if steps < 1 or not SHA256.fullmatch(row.get("sha256", "")):
        raise ValueError("Evaluation checkpoint identity is invalid")
    checkpoint = inside(row.get("checkpoint", ""), experiment)
    if checkpoint.suffix != ".flax_model" or not 0 < checkpoint.stat().st_size < 256 * 1024**2:
        raise ValueError("Invalid checkpoint file or size")
    evaluation = checkpoint.parent
    if evaluation.parent != Path(experiment).resolve() / "evaluations":
        raise ValueError("Checkpoint is outside the evaluation directory")
    sidecar = inside(str(checkpoint) + ".json", experiment)
    result = inside(evaluation / "result.json", experiment)
    status = inside(evaluation / "status.json", experiment)
    provenance = inside(evaluation / "provenance.json", experiment)
    loaded = inside(evaluation / "loaded-native.json", experiment)
    sha = row["sha256"]
    if digest(checkpoint) != sha:
        raise ValueError("Checkpoint hash differs from evaluation index")
    metadata = read_json(sidecar, limit=64 * 1024)
    if (metadata.get("schema") != "ygo-training-checkpoint/v2"
            or metadata.get("architecture") != "decision-v1"
            or metadata.get("global_step") != steps or metadata.get("sha256") != sha
            or metadata.get("observation_revision") != SIDECAR_REVISION
            or not isinstance(metadata.get("model_args"), dict)
            or metadata["model_args"].get("decision_layers") != 2
            or metadata["model_args"].get("structured") is not True
            or metadata["model_args"].get("oppo_info") is not False
            or metadata.get("switch") is not False
            or metadata.get("bfloat16") is not False):
        raise ValueError("Checkpoint architecture is not the corrected expert")
    raw = read_json(result)
    total = _summary(raw)
    if raw.get("models", {}).get("structured", {}).get("sha256") != sha:
        raise ValueError("Evaluation result identity is invalid")
    configuration = raw.get("configuration") or {}
    seed = _int(configuration.get("seed"), "seed")
    expected_config = {
        "policy": "deterministic_argmax", "max_options": 128, "max_steps": 1000,
        "paired_seeds": 32, "total_games": 64, "n_history_actions": 32,
        "mirrored_initial_observations_equal": True, "recording_enabled": False,
    }
    if any(configuration.get(key) != value for key, value in expected_config.items()):
        raise ValueError("Evaluation protocol differs from corrected expert")
    if (configuration.get("code_list_sha256") != asset_hashes["scripts/code_list.txt"]
            or configuration.get("expert_checkpoint_metadata") != metadata):
        raise ValueError("Evaluation assets or sidecar differ")
    _validate_games(raw, total)
    saved_status = read_json(status, limit=64 * 1024)
    if (saved_status.get("status") != "complete"
            or saved_status.get("checkpoint_steps") != steps
            or saved_status.get("sha256") != sha
            or saved_status.get("games") != 64
            or saved_status.get("truncated") != 0
            or saved_status.get("sealed_final") is not False
            or saved_status.get("observation_revision") != EVALUATION_REVISION):
        raise ValueError("Evaluation status is incomplete")
    if (saved_status.get("seed") != seed or
            any(summary.get(key) != total[key]
                for summary in (saved_status.get("summary", {}),
                                raw.get("structured_model", {}).get("overall", {}))
                for key in ("games", "wins", "losses", "draws"))):
        raise ValueError("Evaluation status summary differs")
    saved_provenance = read_json(provenance, limit=64 * 1024)
    saved_loaded = read_json(loaded, limit=64 * 1024)
    native_sha = saved_provenance.get("native_sha256")
    if (saved_provenance.get("both_players_corrected") is not True
            or saved_provenance.get("observation_revision") != EVALUATION_REVISION
            or saved_provenance.get("checkpoint_sha256") != sha
            or not SHA256.fullmatch(native_sha or "")
            or saved_loaded.get("sha256") != native_sha
            or saved_loaded.get("revision") != EVALUATION_REVISION):
        raise ValueError("Corrected native provenance is incomplete")
    official_sha = raw.get("models", {}).get("official", {}).get("sha256")
    if not SHA256.fullmatch(official_sha or ""):
        raise ValueError("Official model identity is invalid")
    return {
        "id": sha,
        "checkpoint_steps": steps,
        "win_rate": total["win_rate"],
        "wins": total["wins"],
        "losses": total["losses"],
        "draws": total["draws"],
        "games": total["games"],
        "finished_at": saved_status.get("finished_at"),
        "official_sha256": official_sha,
        "evaluation_native_sha256": native_sha,
        "evaluation_seed": seed,
        "assets": asset_hashes,
        "source": {
            "checkpoint": checkpoint,
            "sidecar": sidecar,
            "result": result,
            "status": status,
            "provenance": provenance,
            "loaded": loaded,
        },
        "metadata": metadata,
    }


def collect(experiment, app, runtime=None, top_n=TOP_N, results_bytes=None):
    experiment, app = Path(experiment).resolve(), Path(app).resolve()
    runtime = runtime or _validate_runtime(app)
    if results_bytes is None:
        results_bytes = (experiment / "evaluations/results.json").read_bytes()
    index = json.loads(results_bytes)
    if not isinstance(index.get("results"), list):
        raise ValueError("Evaluation index has no results list")
    training_project = experiment / "project"
    source_root = experiment.parent.parent
    asset_hashes = {}
    for relative in ASSETS:
        path = resolved_file(training_project / relative, source_root)
        asset_hashes[relative] = digest(path)
        if digest(app / "project" / relative) != asset_hashes[relative]:
            raise ValueError("Runtime asset differs from training asset: " + relative)
    candidates, rejected = [], []
    for row in index["results"]:
        try:
            candidates.append(candidate(row, experiment, app, runtime, asset_hashes))
        except (OSError, ValueError, KeyError, TypeError) as error:
            rejected.append({"checkpoint": row.get("checkpoint") if isinstance(row, dict) else None,
                             "reason": str(error)})
    unique = {row["id"]: row for row in candidates}
    protocols = {(row["evaluation_seed"], row["official_sha256"],
                  row["evaluation_native_sha256"]) for row in unique.values()}
    if len(protocols) > 1:
        raise ValueError("Corrected evaluations use different seeds, opponents or native modules")
    ordered = sorted(unique.values(),
                     key=lambda row: (-row["win_rate"], -row["checkpoint_steps"], row["id"]))
    if len(ordered) < top_n:
        raise ValueError(f"Only {len(ordered)} corrected evaluations are publishable")
    return ordered[:top_n], rejected, hashlib.sha256(results_bytes).hexdigest()


def _pin(row):
    paths = row["source"]
    return {
        "id": row["id"],
        "checkpoint_steps": row["checkpoint_steps"],
        "hashes": {key: digest(path) for key, path in paths.items()},
        "assets": row["assets"],
        "official_sha256": row["official_sha256"],
        "evaluation_native_sha256": row["evaluation_native_sha256"],
    }


def publish(experiment, app, state, top_n=TOP_N):
    experiment, app = Path(experiment).resolve(), Path(app).resolve()
    results_path = experiment / "evaluations/results.json"
    results_bytes = results_path.read_bytes()
    runtime = _validate_runtime(app)
    results_sha = hashlib.sha256(results_bytes).hexdigest()
    manifest_path = app / "model/compatible-experts.json"
    try:
        previous = read_json(manifest_path, limit=1024 * 1024)
    except (OSError, ValueError, TypeError):
        previous = None
    if (isinstance(previous, dict)
            and previous.get("schema") == "compatible-sky-experts/v1"
            and previous.get("validation_version") == VALIDATION_VERSION
            and previous.get("source_results_sha256") == results_sha
            and previous.get("top_n") == top_n
            and previous.get("runtime_observation_revision") == runtime["observation_revision"]):
        try:
            report = read_json(state, limit=1024 * 1024)
        except (OSError, ValueError, TypeError):
            report = {}
        if (isinstance(report, dict) and report.get("state") == "ok"
                and report.get("source_results_sha256") == results_sha
                and len(report.get("items", [])) == top_n):
            report.update(checked_at=time.time(), skipped=True,
                          runtime_native_sha256=runtime.get("sha256"))
            atomic_json(state, report)
            return report
    rows, rejected, results_sha = collect(experiment, app, runtime, top_n, results_bytes)
    pins = [_pin(row) for row in rows]
    catalog = app / "model/catalog"
    catalog.mkdir(parents=True, exist_ok=True)
    for row, pin in zip(rows, pins):
        for key, source in row["source"].items():
            target = catalog / (row["id"] + SUFFIXES[key])
            atomic_copy(source, target, pin["hashes"][key])
    manifest = {
        "schema": "compatible-sky-experts/v1",
        "validation_version": VALIDATION_VERSION,
        "items": pins,
        "source": str(results_path),
        "source_results_sha256": results_sha,
        "top_n": top_n,
        "evaluation_observation_revision": EVALUATION_REVISION,
        "runtime_observation_revision": runtime["observation_revision"],
        "updated_at": time.time(),
    }
    atomic_json(app / "model/compatible-experts.json", manifest)
    report = {
        "state": "ok",
        "updated_at": manifest["updated_at"],
        "source_results_sha256": results_sha,
        "items": [{"rank": rank, "id": row["id"],
                   "checkpoint_steps": row["checkpoint_steps"],
                   "win_rate": row["win_rate"], "wins": row["wins"],
                   "losses": row["losses"], "draws": row["draws"]}
                  for rank, row in enumerate(rows, 1)],
        "rejected_count": len(rejected),
        "rejected": rejected[-32:],
        "runtime_native_sha256": runtime.get("sha256"),
    }
    atomic_json(state, report)
    return report


def watch(experiment, app, state, interval):
    while True:
        try:
            report = publish(experiment, app, state)
            print(json.dumps(report, ensure_ascii=True), flush=True)
        except (OSError, ValueError, KeyError, TypeError) as error:
            failure = {"state": "error", "updated_at": time.time(), "error": str(error)}
            atomic_json(state, failure)
            print(json.dumps(failure, ensure_ascii=True), flush=True)
        time.sleep(interval)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--experiment", type=Path, required=True)
    parser.add_argument("--app", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--interval", type=int, default=60)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    if args.interval < 5:
        parser.error("--interval must be at least 5 seconds")
    try:
        with single_writer(args.state):
            if args.once:
                print(json.dumps(publish(args.experiment, args.app, args.state), ensure_ascii=True))
            else:
                watch(args.experiment, args.app, args.state, args.interval)
    except BlockingIOError:
        parser.exit(0, "Corrected Top 5 publisher is already running.\n")


if __name__ == "__main__":
    main()
