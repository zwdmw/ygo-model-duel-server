"""An explicit, unranked catalog for models exported by other training jobs."""
import json
from pathlib import Path
import re

from model_catalog import digest
from model_snapshot import validate_snapshot, verify_checkpoint


ASSETS = (
    "project/scripts/code_list.txt",
    "project/assets/locale/en/cards.cdb",
    "project/assets/structured/frozen_semantics_v1.npz",
    "project/ygoenv/ygopro/ygopro_ygoenv.cpython-311-x86_64-linux-gnu.so",
)


class RegisteredCatalog:
    def __init__(self, manifest, app):
        self.manifest, self.app = Path(manifest), Path(app).resolve()

    def entries(self):
        if self.manifest.is_symlink() or self.manifest.stat().st_size > 1024 * 1024:
            raise ValueError("Invalid registered catalog")
        document = json.loads(self.manifest.read_text(encoding="utf-8"))
        if document.get("schema") != 1 or not isinstance(document.get("models"), list):
            raise ValueError("Unsupported registered catalog schema")
        rows, ids = [], set()
        for row in document["models"]:
            model = validate_snapshot(row["model"])
            identity = model["checkpoint_sha256"]
            if identity in ids or set(row["assets"]) != set(ASSETS):
                raise ValueError("Duplicate model or incomplete training-asset binding")
            if any(not isinstance(value, str) or not re.fullmatch("[0-9a-f]{64}", value)
                   for value in row["assets"].values()):
                raise ValueError("Invalid training-asset hash")
            if not re.fullmatch("[0-9a-f]{64}", row.get("sidecar_sha256", "")):
                raise ValueError("Invalid architecture sidecar hash")
            ids.add(identity)
            rows.append(row)
        return rows

    def listing(self):
        rows = self.entries()
        items = []
        for rank, row in enumerate(rows, 1):
            model = row["model"]
            items.append({
                "id": model["checkpoint_sha256"], "name": model["model_label"],
                "checkpoint_steps": model["checkpoint_steps"],
                "checkpoint_sha256": model["checkpoint_sha256"],
                "architecture": row["architecture"], "registered": True,
                "wins": 0, "losses": 0, "draws": 0, "games": 0,
                "win_rate": None, "finished_at": None, "per_opponent": {}, "rank": rank,
            })
        return {"items": items, "protocol": {}, "source": "registered",
                "ranking": "registered_unranked"}

    def stage(self, selected):
        row = next((item for item in self.entries()
                    if item["model"]["checkpoint_sha256"] == selected), None)
        if row is None:
            raise ValueError("Model is not in the registered catalog")
        for name, expected in row["assets"].items():
            path = self.app / name
            if path.is_symlink() or digest(path) != expected:
                raise ValueError("Training assets differ from the runtime: " + name)
        path = verify_checkpoint(self.app, row["model"])
        sidecar = path.with_name(path.name + ".json")
        if sidecar.is_symlink() or digest(sidecar) != row["sidecar_sha256"]:
            raise ValueError("Registered model architecture sidecar changed")
        return dict(row["model"])
