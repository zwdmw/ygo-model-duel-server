"""Pinned model identities; deployment launchers select their model explicitly."""
import hashlib
import json
import os
from pathlib import Path
import re


SNAPSHOTS = {
    "195M": {
        "checkpoint_steps": 195035136,
        "checkpoint_sha256": "7f93135e84bd4f7258c5251d28c8c58e81017e15114c1afa33de5dc72eac0ac9",
        "checkpoint_file": "model/195M_step195035136.flax_model",
    },
    "285M": {
        "checkpoint_steps": 285212672,
        "checkpoint_sha256": "c9b6dd64fc98144d0be59fb98e0f3fa2ed2c328b1dd61e26284431170c678833",
        "checkpoint_file": "model/285M_step285212672.flax_model",
    },
    "305M": {
        "checkpoint_steps": 305135616,
        "checkpoint_sha256": "0c0f877f7013b3e416dbb1f65ce4b23ad9757388dcae172d214cfa10715c8a91",
        "checkpoint_file": "model/305M_step305135616.flax_model",
    },
}


def snapshot(name=None):
    if name is None and os.environ.get("AGENT_MODEL_SNAPSHOT"):
        return validate_snapshot(json.loads(os.environ["AGENT_MODEL_SNAPSHOT"]))
    name = name if name is not None else os.environ.get("AGENT_CHECKPOINT", "195M")
    if name not in SNAPSHOTS:
        raise ValueError(f"Unknown pinned model: {name}")
    return {"model_label": name, **SNAPSHOTS[name]}


def validate_snapshot(model):
    keys = {"model_label", "checkpoint_steps", "checkpoint_sha256", "checkpoint_file"}
    if not isinstance(model, dict) or set(model) != keys:
        raise ValueError("Invalid model descriptor")
    label, steps, sha = (model[key] for key in ("model_label", "checkpoint_steps", "checkpoint_sha256"))
    if (not isinstance(label, str) or not re.fullmatch(r"(official-)?[1-9][0-9]{0,5}M", label)
            or type(steps) is not int or steps < 1
            or not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{64}", sha)):
        raise ValueError("Invalid model identity")
    builtin = {"model_label": label, **SNAPSHOTS[label]} if label in SNAPSHOTS else None
    if model != builtin and model["checkpoint_file"] != f"model/catalog/{sha}.flax_model":
        raise ValueError("Model path is not an immutable catalog checkpoint")
    return dict(model)


def identity(model):
    return {key: model[key] for key in
            ("model_label", "checkpoint_steps", "checkpoint_sha256")}


def validate_identity(actual, expected):
    if any(actual.get(key) != value for key, value in expected.items()):
        raise ValueError("Unexpected model identity; checkpoint steps and hash must match")


def verify_checkpoint(root, model):
    root = Path(root).resolve()
    path = (root / model["checkpoint_file"]).resolve()
    if not path.is_relative_to(root):
        raise ValueError("Model path escaped the runtime")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    if digest.hexdigest() != model["checkpoint_sha256"]:
        raise RuntimeError(f"Checkpoint integrity failure: {path.name}")
    return path
