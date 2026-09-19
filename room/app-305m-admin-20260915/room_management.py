"""Opt-in, loopback-only room controls. Changes never replace an active deck."""
import copy
import hmac
import json
from pathlib import Path
import secrets
import threading
import time

from deck_config import parse_ydk, selection_name, validate_selection
from model_snapshot import identity, validate_snapshot, verify_checkpoint
from replay_store import atomic_json, read_json


class RoomManagement:
    def __init__(self, directory, key_file, room_lock, spectators, model_root=None):
        self.directory = Path(directory)
        self.key = Path(key_file).read_text().strip()
        if len(self.key) < 32:
            raise ValueError("Management key is too short")
        self.room_lock, self.spectators = room_lock, spectators
        self.model_root = model_root
        self.lock = threading.RLock()
        self.types = None
        self.settings = {"accepting": True, "admission_revision": 0,
                         "spectating": True, "spectator_capacity": 8,
                         "default_name": "BestowedDragon", "default_deck": None}
        self.settings.update(read_json(self.directory / "room.json"))
        if "read_error" in self.settings:
            raise ValueError("Invalid persisted room settings")

    def authorize(self, header):
        return hmac.compare_digest(header or "", "Bearer " + self.key)

    def initialize(self, types, default):
        with self.lock:
            self.types, self.builtin = types, validate_selection(default, types)
            if self.settings["default_deck"] is not None:
                validate_selection(self.settings["default_deck"], types)
            self._visibility()

    def _visibility(self):
        self.spectators.configure(self.settings["spectating"], self.settings["spectator_capacity"])

    def status(self):
        with self.lock:
            return {key: value for key, value in self.settings.items() if key != "default_deck"} | {
                "management_api": 1, "occupied": self.room_lock.locked(),
                "restart_requested": (self.directory / "restart.json").exists(),
                "model_selection_api": 1 if self.model_root else 0,
                "model_switch": read_json(self.directory / "model-switch.json")}

    def default(self):
        with self.lock:
            return self.settings["default_name"], copy.deepcopy(
                self.settings["default_deck"] if self.settings["default_deck"] is not None else self.builtin)

    def enter(self):
        with self.lock:
            return self.settings["accepting"] and self.room_lock.acquire(blocking=False)

    def action(self, body):
        if not isinstance(body, dict):
            raise ValueError("Expected an object")
        with self.lock:
            if self.types is None:
                raise ValueError("Room is still warming up")
            action = body.get("action")
            proposed = copy.deepcopy(self.settings)
            if action == "settings":
                if set(body) - {"action", "accepting", "spectating", "spectator_capacity",
                                "expected_admission_revision"}:
                    raise ValueError("Unknown room setting")
                if "expected_admission_revision" in body:
                    revision = body["expected_admission_revision"]
                    if type(revision) is not int or revision < 0 or "accepting" not in body:
                        raise ValueError("Invalid admission revision")
                    if revision != proposed["admission_revision"]:
                        return self.status()
                for key in ("accepting", "spectating"):
                    if key in body:
                        if type(body[key]) is not bool:
                            raise ValueError("Expected a boolean")
                        proposed[key] = body[key]
                if "accepting" in body:
                    proposed["admission_revision"] += 1
                if "spectator_capacity" in body:
                    count = body["spectator_capacity"]
                    if type(count) is not int or not 1 <= count <= 8:
                        raise ValueError("Spectator limit must be between 1 and 8")
                    proposed["spectator_capacity"] = count
                if proposed["accepting"] and ((self.directory / "restart.json").exists()
                                              or (self.directory / "restart-processing.json").exists()):
                    raise ValueError("Cancel pending restart before reopening")
            elif action == "default_deck":
                proposed["default_name"] = selection_name(body.get("name"))
                proposed["default_deck"] = validate_selection(parse_ydk(body.get("ydk", "")), self.types)
            elif action == "reset_default":
                proposed.update(default_name="BestowedDragon", default_deck=None)
            elif action in ("restart", "switch_model"):
                switching = action == "switch_model"
                if body.get("confirmation") != ("SWITCH_MODEL" if switching else "RESTART"):
                    raise ValueError("Restart confirmation required")
                if ((self.directory / "restart.json").exists()
                        or (self.directory / "restart-processing.json").exists()):
                    raise ValueError("Another maintenance operation is pending")
                request = {"id": secrets.token_hex(12)}
                if switching:
                    if self.model_root is None:
                        raise ValueError("Model selection is not enabled")
                    model = validate_snapshot(body.get("model"))
                    verify_checkpoint(self.model_root, model)
                    request.update(model=model, resume_accepting=proposed["accepting"],
                                   admission_revision=proposed["admission_revision"])
                proposed["accepting"] = False
                # Persist the admission gate before the supervisor can see a restart.
                atomic_json(self.directory / "room.json", proposed)
                self.settings = proposed
                if switching:
                    atomic_json(self.directory / "model-switch.json", {
                        "state": "queued", "target": identity(model), "updated_at": time.time()})
                atomic_json(self.directory / "restart.json", request)
                return self.status()
            elif action == "cancel_restart":
                if (self.directory / "restart-processing.json").exists():
                    raise ValueError("Model is already loading")
                request = read_json(self.directory / "restart.json")
                try:
                    (self.directory / "restart.json").unlink()
                except FileNotFoundError:
                    if request:
                        raise ValueError("Maintenance has already started")
                else:
                    if request.get("model"):
                        atomic_json(self.directory / "model-switch.json", {
                            "state": "cancelled", "target": identity(request["model"]),
                            "updated_at": time.time()})
                return self.status()
            else:
                raise ValueError("Unknown room action")
            atomic_json(self.directory / "room.json", proposed)
            self.settings = proposed
            self._visibility()
            return self.status()
