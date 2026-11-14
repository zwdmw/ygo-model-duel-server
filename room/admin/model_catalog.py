"""Read completed comparable evaluations; stage only their hash-pinned weights."""
import hashlib
import json
from pathlib import Path
import re
import shutil
import tempfile


def digest(path):
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


class ModelCatalog:
    def __init__(self, run, app, reference=305000000, paired_project=None):
        self.run, self.app = Path(run).resolve(), Path(app).resolve()
        self.runs = self.run / "evaluations/periodic-5m/runs"
        self.reference = self.runs / str(reference) / "result.json"
        self.paired_project = Path(paired_project).resolve() if paired_project else None

    def official_entries(self):
        """The frozen upstream (official) checkpoint, listed beside the Top 10.

        It is not one of our evaluations, so it carries no win-rate row; it is
        kept outside the ranking and marked with its own architecture so the
        runtime loads it through ``outbound/official_policy``.
        """
        import os
        path = Path(os.environ.get("AGENT195M_OFFICIAL_CHECKPOINT",
                                   str(self.app / "model/official.flax_model")))
        if not path.is_file():
            return []
        sha = digest(path)
        return [{"id": sha, "name": "official-22750M", "checkpoint_steps": int(
                     os.environ.get("AGENT195M_OFFICIAL_STEPS", "22750000000")),
                 "checkpoint_sha256": sha, "architecture": "official", "official": True,
                 "wins": 0, "losses": 0, "draws": 0, "games": 0, "win_rate": None,
                 "finished_at": None, "per_opponent": {}, "_source": path,
                 "_sidecar": self.app / "model/official-model.json"}]

    def entries(self):
        baseline = json.loads(self.reference.read_text())
        protocol = baseline["protocol"]
        if baseline.get("status") != "complete" or baseline.get("verified") is not True:
            raise ValueError("Reference evaluation is incomplete")
        rows = []
        for folder in self.runs.iterdir():
            if not folder.is_dir() or folder.is_symlink() or not re.fullmatch(r"[0-9]{1,12}", folder.name):
                continue
            try:
                result = json.loads((folder / "result.json").read_text())
                if (result.get("status") != "complete" or result.get("verified") is not True
                        or result.get("schema") != baseline["schema"] or result.get("protocol") != protocol):
                    continue
                total = result["windbot_total"]
                games = total["games"]
                opponents = protocol["opponents"]
                if (type(games) is not int or games != len(opponents) * protocol["games_per_opponent"]
                        or games <= 0 or set(result["windbot"]) != set(opponents)):
                    continue
                for key in ("wins", "losses", "draws"):
                    if (type(total[key]) is not int or total[key] < 0
                            or sum(result["windbot"][name][key] for name in opponents) != total[key]):
                        raise ValueError("Inconsistent evaluation totals")
                if (sum(total[key] for key in ("wins", "losses", "draws")) != games
                        or any(result["windbot"][name]["games"] != protocol["games_per_opponent"]
                               for name in opponents)):
                    continue
                threshold, steps, sha = (result[key] for key in
                                        ("nominal_threshold", "checkpoint_steps", "checkpoint_sha256"))
                if (threshold != int(folder.name) or type(steps) is not int or steps < threshold
                        or threshold % 1000000 or not re.fullmatch(r"[0-9a-f]{64}", sha)):
                    continue
                weights = list((folder / "checkpoints").glob("*.flax_model"))
                if len(weights) != 1 or weights[0].is_symlink():
                    continue
                source = weights[0].resolve()
                if not source.is_relative_to(folder.resolve()) or not 0 < source.stat().st_size < 256 * 1024**2:
                    continue
                sidecar = source.with_name(source.name + ".json")
                architecture = None
                if sidecar.is_file() and not sidecar.is_symlink() and sidecar.stat().st_size <= 64 * 1024:
                    try:
                        architecture = json.loads(sidecar.read_text()).get("architecture")
                    except (OSError, ValueError):
                        architecture = None
                rows.append({"id": sha, "name": f"{threshold // 1000000}M",
                             "checkpoint_steps": steps, "checkpoint_sha256": sha,
                             "architecture": architecture,
                             **{key: total[key] for key in ("wins", "losses", "draws", "games")},
                             "win_rate": total["wins"] / games, "finished_at": result["finished"],
                             "per_opponent": result["windbot"], "_source": source,
                             "_sidecar": sidecar if architecture else None})
            except (OSError, ValueError, KeyError, TypeError):
                continue
        rows.extend(self.official_entries())
        unique = {row["id"]: row for row in rows}
        ranked = [row for row in unique.values() if row.get("win_rate") is not None]
        ordered = sorted(ranked, key=lambda row: (-row["win_rate"], -row["checkpoint_steps"]))[:10]
        for rank, row in enumerate(ordered, 1):
            row.update(group="general", rank=rank)
        from compatible_experts import entries as compatible_entries
        ordered.extend(compatible_entries(self.app, digest))
        for row in unique.values():
            if row.get("architecture") == "official" and row not in ordered:
                row.update(group="official", rank=None)
                ordered.append(row)
        return ordered, protocol

    def listing(self):
        rows, protocol = self.entries()
        return {"items": [{key: value for key, value in row.items() if not key.startswith("_")}
                          for row in rows],
                "protocol": protocol, "source": self.run.name,
                "groups": [
                    {"id": "sky_corrected", "name": "Corrected Sky Striker Top 5", "limit": 5},
                    {"id": "general", "name": "General Top 10", "limit": 10},
                    {"id": "official", "name": "Official", "limit": None},
                ],
                "ranking": "win_rate_desc_steps_desc"}

    def stage(self, selected):
        if not isinstance(selected, str) or not re.fullmatch(r"[0-9a-f]{64}", selected):
            raise ValueError("Invalid model ID")
        rows, _ = self.entries()
        row = next((row for row in rows if row["id"] == selected), None)
        if row is None:
            raise ValueError("Model is no longer in the verified catalog; refresh the list")
        if row.get("group") == "sky_corrected":
            for name, expected in row["_runtime_assets"].items():
                path = self.app / "project" / name
                if path.is_symlink() or digest(path) != expected:
                    raise ValueError("Pinned expert assets differ from this runtime")
            return {"model_label": row["name"], "checkpoint_steps": row["checkpoint_steps"],
                    "checkpoint_sha256": selected,
                    "checkpoint_file": f"model/catalog/{selected}.flax_model"}
        if self.paired_project and row.get("architecture") != "official":
            for name in ("assets/structured/frozen_semantics_v1.npz", "scripts/code_list.txt",
                         "assets/locale/en/cards.cdb"):
                if digest(self.app / "project" / name) != digest(self.paired_project / name):
                    raise ValueError("Training card pool or semantic assets differ from this runtime")
        destination = self.app / "model/catalog" / (selected + ".flax_model")
        if not destination.parent.resolve().is_relative_to(self.app):
            raise ValueError("Catalog storage escaped the runtime")
        destination.parent.mkdir(parents=True, exist_ok=True)
        if not destination.exists() or digest(destination) != selected:
            with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as stream:
                temporary = Path(stream.name)
            try:
                shutil.copyfile(row["_source"], temporary)
                if digest(temporary) != selected:
                    raise ValueError("Evaluated checkpoint hash mismatch")
                temporary.replace(destination)
            finally:
                temporary.unlink(missing_ok=True)
        # The sidecar states the architecture and model arguments; without it the
        # runtime would build the legacy model and fail closed on the new weights.
        if row.get("_sidecar") is not None:
            target = destination.with_name(destination.name + ".json")
            with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as stream:
                sidecar_temporary = Path(stream.name)
            try:
                shutil.copyfile(row["_sidecar"], sidecar_temporary)
                sidecar_temporary.replace(target)
            finally:
                sidecar_temporary.unlink(missing_ok=True)
        return {"model_label": row["name"], "checkpoint_steps": row["checkpoint_steps"],
                "checkpoint_sha256": selected, "checkpoint_file": f"model/catalog/{selected}.flax_model"}
