"""Read the atomically published corrected-expert catalog on every request."""
from collections import Counter
import json
from pathlib import Path
import re


ASSETS = ("assets/structured/frozen_semantics_v1.npz", "scripts/code_list.txt",
          "assets/locale/en/cards.cdb")
SUFFIXES = {
    "checkpoint": ".flax_model", "sidecar": ".flax_model.json",
    "result": ".evaluation.json", "status": ".evaluation-status.json",
    "provenance": ".evaluation-provenance.json", "loaded": ".evaluation-native.json",
}


def read_json(path):
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 4 * 1024**2:
        raise ValueError("Invalid corrected expert artifact")
    return json.loads(path.read_text(encoding="utf-8"))


def inside(path, root):
    if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
        raise ValueError("Corrected expert artifact escaped the runtime")
    return path.resolve()


def validate_result(raw, status, provenance, loaded, metadata, pin):
    steps, sha = pin["checkpoint_steps"], pin["id"]
    settings = raw["configuration"]
    revision = "native-own-deck-candidates-v2"
    if (status["status"] != "complete" or status["checkpoint_steps"] != steps
            or status["sha256"] != sha or status["games"] != 64 or status["truncated"]
            or status["sealed_final"] is not False or status["observation_revision"] != revision
            or provenance["both_players_corrected"] is not True
            or provenance["observation_revision"] != revision
            or provenance["checkpoint_sha256"] != sha
            or provenance["native_sha256"] != pin["evaluation_native_sha256"]
            or loaded["sha256"] != provenance["native_sha256"] or loaded["revision"] != revision):
        raise ValueError("Corrected expert provenance differs")
    if (settings["seed"] != status["seed"] or settings["policy"] != "deterministic_argmax"
            or settings["max_options"] != 128 or settings["max_steps"] != 1000
            or settings["paired_seeds"] != 32 or settings["total_games"] != 64
            or settings["n_history_actions"] != 32
            or settings["mirrored_initial_observations_equal"] is not True
            or settings["recording_enabled"] is not False
            or settings["code_list_sha256"] != pin["assets"]["scripts/code_list.txt"]
            or settings["expert_checkpoint_metadata"] != metadata
            or raw["models"]["structured"]["sha256"] != sha
            or raw["models"]["official"]["sha256"] != pin["official_sha256"]):
        raise ValueError("Corrected expert evaluation identity differs")
    totals, per_seat = Counter(), {}
    for seat, key in enumerate(("structured_going_first", "structured_going_second")):
        rows = raw["games"][key]
        if len(rows) != 32 or sorted(row["environment_index"] for row in rows) != list(range(32)):
            raise ValueError("Incomplete or duplicate corrected evaluation")
        counts = Counter()
        for row in rows:
            reward = row["structured_return"]
            expected = "win" if reward > 0 else "loss" if reward < 0 else "draw"
            terminal = row["terminal_to_play"]
            if (row["structured_seat"] != seat or row["truncated"] is not False
                    or not 0 < row["length"] <= 1000 or terminal not in (0, 1)
                    or reward not in (-1, 0, 1)
                    or reward != row["environment_return"] * (1 if terminal == seat else -1)
                    or row["outcome"] != expected):
                raise ValueError("Invalid corrected terminal outcome")
            counts[expected] += 1
        totals.update(counts)
        per_seat[str(seat)] = {"games": 32, "wins": counts["win"], "losses": counts["loss"],
                              "draws": counts["draw"], "win_rate": counts["win"] / 32}
    total = {"games": 64, "wins": totals["win"], "losses": totals["loss"], "draws": totals["draw"]}
    if any(summary.get(k) != v for summary in (status["summary"], raw["structured_model"]["overall"])
           for k, v in total.items()):
        raise ValueError("Corrected summary differs from game records")
    return total, per_seat


def entries(app, digest):
    app = Path(app)
    manifest = app / "model/compatible-experts.json"
    if not manifest.exists():
        return []
    data = read_json(manifest)
    if data["schema"] != "compatible-sky-experts/v1" or len(data["items"]) > 16:
        raise ValueError("Invalid compatible expert catalog")
    dynamic = data.get("top_n") == 5
    if dynamic and len(data["items"]) != 5:
        raise ValueError("Corrected Top 5 catalog is incomplete")
    native = read_json(app / "native-build.json")
    if (native.get("observation_revision") != "native-own-deck-candidates-v2-protocol-v3"
            or native.get("own_deck_candidate_ids") != 2):
        raise ValueError("Runtime observation is not corrected")
    result, seen = [], set()
    for rank, pin in enumerate(data["items"], 1):
        sha, steps = pin["id"], pin["checkpoint_steps"]
        if (not re.fullmatch(r"[0-9a-f]{64}", sha) or sha in seen
                or type(steps) is not int or steps < 1000000
                or set(pin["assets"]) != set(ASSETS)):
            raise ValueError("Invalid pinned expert identity")
        seen.add(sha)
        files = {key: inside(app / "model/catalog" / (sha + suffix), app)
                 for key, suffix in SUFFIXES.items()}
        if not 0 < files["checkpoint"].stat().st_size < 256 * 1024**2:
            raise ValueError("Invalid pinned checkpoint size")
        for key, path in files.items():
            if digest(path) != pin["hashes"][key]:
                raise ValueError("Pinned expert artifact changed: " + key)
        if pin["hashes"]["checkpoint"] != sha:
            raise ValueError("Pinned checkpoint digest differs")
        metadata = read_json(files["sidecar"])
        if (metadata["schema"] != "ygo-training-checkpoint/v2"
                or metadata["architecture"] != "decision-v1" or metadata["global_step"] != steps
                or metadata["sha256"] != sha or metadata["model_args"]["decision_layers"] != 2
                or metadata["model_args"]["structured"] is not True
                or metadata["model_args"]["oppo_info"] is not False
                or metadata["switch"] is not False or metadata["bfloat16"] is not False
                or metadata["observation_revision"] != "sky-selfplay-own-deck-candidates-v2"):
            raise ValueError("Pinned expert architecture differs")
        raw, status, provenance, loaded = (read_json(files[k]) for k in
                                          ("result", "status", "provenance", "loaded"))
        total, per_seat = validate_result(raw, status, provenance, loaded, metadata, pin)
        result.append({
            "id": sha, "name": f"{steps // 1000000}M",
            "display_name": f"\u95ea\u5200\u4e13\u5bb6 {steps // 1000000}M (\u6b63\u786e\u8f93\u5165)",
            "group": "sky_corrected", "rank": rank if dynamic else None, "pinned": True,
            "checkpoint_steps": steps, "checkpoint_sha256": sha, "architecture": "decision-v1",
            **total, "win_rate": total["wins"] / total["games"], "per_seat": per_seat,
            "finished_at": status["finished_at"],
            "per_opponent": {"Official-22750M": dict(total)},
            "evaluation_protocol": {
                "id": "sky-corrected-official-v2", "seed": status["seed"],
                "policy": "deterministic_argmax", "games_per_opponent": 64,
                "opponents": ["Official-22750M"], "observation_revision": status["observation_revision"],
                "both_players_corrected": True, "not_a_network_win_rate": True,
            },
            "_source": files["checkpoint"], "_sidecar": files["sidecar"],
            "_sidecar_sha256": pin["hashes"]["sidecar"], "_project": None,
            "_runtime_assets": pin["assets"],
        })
    return result
