"""Persist model choices only after successful warmup; failed choices roll back."""
import json
from pathlib import Path
import time

from model_snapshot import identity, validate_identity, validate_snapshot, verify_checkpoint
from replay_store import atomic_json


class ModelSelection:
    def __init__(self, directory, app, fallback):
        self.directory, self.app = Path(directory), Path(app)
        active = self.directory / "active-model.json"
        self.active = validate_snapshot(json.loads(active.read_text()) if active.exists() else fallback)
        self.candidate = None
        self.request = None
        self.started = 0
        processing = self.directory / "restart-processing.json"
        if processing.exists():
            self.record("failed", error="Supervisor restarted during model switch; last committed model retained")
            processing.unlink()

    def record(self, state, **extra):
        atomic_json(self.directory / "model-switch.json",
                    {"state": state, "updated_at": time.time(),
                     "target": identity(self.candidate) if self.candidate else None, **extra})

    def claim(self):
        source = self.directory / "restart.json"
        processing = self.directory / "restart-processing.json"
        try:
            source.replace(processing)
        except FileNotFoundError:
            return False
        request = json.loads(processing.read_text())
        if "model" not in request:
            processing.unlink()
            return True
        try:
            candidate = validate_snapshot(request["model"])
            verify_checkpoint(self.app, candidate)
        except (ValueError, OSError, RuntimeError) as error:
            self.record("failed", error=str(error))
            processing.unlink()
            return False
        self.candidate, self.request, self.started = candidate, request, time.monotonic()
        self.record("loading")
        return True

    def model(self):
        return self.candidate or self.active

    def ready(self, health, resume):
        if self.candidate is None or health.get("state") != "ready":
            return
        validate_identity(health, identity(self.candidate))
        resume_accepting = self.request.get("resume_accepting")
        admission_revision = self.request["admission_revision"]
        atomic_json(self.directory / "active-model.json", self.candidate)
        self.active = self.candidate
        self.record("complete", active=identity(self.active))
        self.candidate = self.request = None
        (self.directory / "restart-processing.json").unlink(missing_ok=True)
        # The admission gate is only reopened after the new policy reports its hash.
        if resume_accepting:
            resume(admission_revision)

    def failed(self, error):
        if self.candidate is None:
            return False
        self.record("failed", error=str(error), rollback=identity(self.active))
        self.candidate = self.request = None
        (self.directory / "restart-processing.json").unlink(missing_ok=True)
        return True

    def check_timeout(self):
        if self.candidate is not None and time.monotonic() - self.started > 300:
            raise TimeoutError("New model did not become ready within 300 seconds")
