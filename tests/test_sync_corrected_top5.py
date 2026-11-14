import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import sys

from tools import sync_corrected_top5 as sync

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "room/admin"))
import compatible_experts
from model_catalog import ModelCatalog


class CorrectedTop5SyncTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.experiment = self.root / "experiment"
        self.app = self.root / "app"
        self.evaluations = self.experiment / "evaluations"
        self.project = self.experiment / "project"
        self.evaluations.mkdir(parents=True)
        self.app.mkdir()
        for relative in sync.ASSETS:
            training = self.project / relative
            runtime = self.app / "project" / relative
            training.parent.mkdir(parents=True, exist_ok=True)
            runtime.parent.mkdir(parents=True, exist_ok=True)
            training.write_bytes(relative.encode())
            runtime.write_bytes(relative.encode())
        (self.app / "native-build.json").write_text(json.dumps({
            "sha256": "native",
            "observation_revision": sync.RUNTIME_REVISION,
            "own_deck_candidate_ids": 2,
            "full_spectator_feed": 1,
        }))
        self.rows = []
        for index, rate in enumerate((0.70, 0.95, 0.8125, 0.95, 0.60, 0.85), 1):
            self.add_evaluation(index, rate)
        (self.evaluations / "results.json").write_text(
            json.dumps({"results": self.rows}))

    def tearDown(self):
        self.temp.cleanup()

    def add_evaluation(self, index, rate):
        folder = self.evaluations / f"{index:09d}"
        folder.mkdir()
        checkpoint = folder / "model.flax_model"
        checkpoint.write_bytes(f"weights-{index}".encode())
        sha = hashlib.sha256(checkpoint.read_bytes()).hexdigest()
        steps = index * 3_000_000
        total = {"games": 64, "wins": int(rate * 64), "losses": 64 - int(rate * 64), "draws": 0}
        rate = total["wins"] / 64
        metadata = {
            "schema": "ygo-training-checkpoint/v2",
            "architecture": "decision-v1",
            "global_step": steps,
            "sha256": sha,
            "model_args": {"decision_layers": 2, "structured": True, "oppo_info": False},
            "observation_revision": sync.SIDECAR_REVISION,
            "switch": False,
            "bfloat16": False,
        }
        (folder / "model.flax_model.json").write_text(json.dumps(metadata))
        games = {}
        for seat, key in enumerate(("structured_going_first", "structured_going_second")):
            games[key] = []
            for game in range(32):
                won = seat * 32 + game < total["wins"]
                reward = 1.0 if won else -1.0
                games[key].append({
                    "environment_index": game,
                    "structured_seat": seat,
                    "length": 30,
                    "truncated": False,
                    "structured_return": reward,
                    "environment_return": reward,
                    "terminal_to_play": seat,
                    "outcome": "win" if won else "loss",
                })
        raw = {
            "status": "complete",
            "sealed_final": False,
            "checkpoint_steps": steps,
            "sha256": sha,
            "observation_revision": sync.EVALUATION_REVISION,
            "configuration": {
                "seed": 2026092094,
                "policy": "deterministic_argmax",
                "max_options": 128,
                "max_steps": 1000,
                "paired_seeds": 32,
                "total_games": 64,
                "n_history_actions": 32,
                "mirrored_initial_observations_equal": True,
                "recording_enabled": False,
                "code_list_sha256": sync.digest(self.project / "scripts/code_list.txt"),
                "expert_checkpoint_metadata": metadata,
            },
            "models": {
                "structured": {"sha256": sha},
                "official": {"sha256": "b" * 64},
            },
            "games": games,
            "summary": {**total, "strict_win_rate": rate},
        }
        # The production validator also checks this aggregate; keep the fixture
        # representative without duplicating all evaluator statistics.
        raw["structured_model"] = {"overall": total}
        (folder / "result.json").write_text(json.dumps(raw))
        status = {
            "status": "complete", "checkpoint_steps": steps, "sha256": sha,
            "games": 64, "truncated": 0, "sealed_final": False,
            "observation_revision": sync.EVALUATION_REVISION,
            "summary": total, "finished_at": index,
            "seed": 2026092094,
        }
        (folder / "status.json").write_text(json.dumps(status))
        (folder / "provenance.json").write_text(json.dumps({
            "both_players_corrected": True,
            "observation_revision": sync.EVALUATION_REVISION,
            "checkpoint_sha256": sha,
            "native_sha256": "c" * 64,
        }))
        (folder / "loaded-native.json").write_text(json.dumps({
            "sha256": "c" * 64, "revision": sync.EVALUATION_REVISION,
        }))
        self.rows.append({
            "status": "complete", "truncated": 0,
            "observation_revision": sync.EVALUATION_REVISION,
            "checkpoint_steps": steps, "checkpoint": str(checkpoint), "sha256": sha,
        })

    def test_collect_sorts_by_rate_then_steps_and_uses_five_rows(self):
        rows, rejected, _ = sync.collect(self.experiment, self.app)
        self.assertEqual([row["checkpoint_steps"] for row in rows],
                         [12_000_000, 6_000_000, 18_000_000, 9_000_000, 3_000_000])
        self.assertEqual(rejected, [])

    def test_publish_is_atomic_and_contains_only_top_five(self):
        state = self.root / "state.json"
        report = sync.publish(self.experiment, self.app, state)
        manifest = json.loads((self.app / "model/compatible-experts.json").read_text())
        self.assertEqual(len(manifest["items"]), 5)
        self.assertEqual(report["items"][0]["checkpoint_steps"], 12_000_000)
        for item in manifest["items"]:
            for suffix in sync.SUFFIXES.values():
                self.assertTrue((self.app / "model/catalog" / (item["id"] + suffix)).is_file())

    def test_publish_skips_unchanged_results(self):
        state = self.root / "state.json"
        sync.publish(self.experiment, self.app, state)
        report = sync.publish(self.experiment, self.app, state)
        self.assertTrue(report["skipped"])
        self.assertEqual(report["source_results_sha256"],
                         json.loads((self.app / "model/compatible-experts.json").read_text())
                         ["source_results_sha256"])

    def test_new_completed_evaluation_replaces_fifth_without_changing_active_model(self):
        state = self.root / "state.json"
        original = sync.publish(self.experiment, self.app, state)
        active = self.app / "active-model.json"
        active.write_text("unchanged-active-model")
        self.add_evaluation(7, 0.99)
        (self.evaluations / "results.json").write_text(json.dumps({"results": self.rows}))
        report = sync.publish(self.experiment, self.app, state)
        self.assertNotEqual(report["source_results_sha256"], original["source_results_sha256"])
        self.assertEqual(report["items"][0]["checkpoint_steps"], 21_000_000)
        self.assertEqual(len(report["items"]), 5)
        self.assertNotIn(original["items"][-1]["id"], [row["id"] for row in report["items"]])
        self.assertEqual(active.read_text(), "unchanged-active-model")

    def test_incomplete_duplicate_and_legacy_rows_cannot_displace_top_five(self):
        state = self.root / "state.json"
        original = sync.publish(self.experiment, self.app, state)
        self.add_evaluation(7, 1.0)
        self.rows[-1]["status"] = "running"
        self.add_evaluation(8, 1.0)
        self.rows[-1]["observation_revision"] = "legacy"
        self.add_evaluation(9, 1.0)
        path = Path(self.rows[-1]["checkpoint"]).parent / "result.json"
        raw = json.loads(path.read_text())
        raw["games"]["structured_going_first"][0]["environment_index"] = 1
        path.write_text(json.dumps(raw))
        (self.evaluations / "results.json").write_text(json.dumps({"results": self.rows}))
        report = sync.publish(self.experiment, self.app, state)
        self.assertEqual(report["items"], original["items"])
        self.assertEqual(report["rejected_count"], 3)

    def test_copy_failure_leaves_previous_manifest_intact(self):
        state = self.root / "state.json"
        sync.publish(self.experiment, self.app, state)
        manifest = self.app / "model/compatible-experts.json"
        previous = manifest.read_bytes()
        self.add_evaluation(7, 1.0)
        (self.evaluations / "results.json").write_text(json.dumps({"results": self.rows}))
        with patch.object(sync, "atomic_copy", side_effect=OSError("copy failed")):
            with self.assertRaisesRegex(OSError, "copy failed"):
                sync.publish(self.experiment, self.app, state)
        self.assertEqual(manifest.read_bytes(), previous)

    def test_protocol_drift_preserves_previous_manifest(self):
        state = self.root / "state.json"
        sync.publish(self.experiment, self.app, state)
        manifest = self.app / "model/compatible-experts.json"
        previous = manifest.read_bytes()
        self.add_evaluation(7, 1.0)
        folder = Path(self.rows[-1]["checkpoint"]).parent
        raw = json.loads((folder / "result.json").read_text())
        raw["configuration"]["seed"] += 1
        (folder / "result.json").write_text(json.dumps(raw))
        status = json.loads((folder / "status.json").read_text())
        status["seed"] += 1
        (folder / "status.json").write_text(json.dumps(status))
        (self.evaluations / "results.json").write_text(json.dumps({"results": self.rows}))
        with self.assertRaisesRegex(ValueError, "different seeds"):
            sync.publish(self.experiment, self.app, state)
        self.assertEqual(manifest.read_bytes(), previous)

    def test_recovered_state_restores_ranking_even_if_results_unchanged(self):
        state = self.root / "state.json"
        original = sync.publish(self.experiment, self.app, state)
        state.write_text(json.dumps({"state": "error", "error": "transient error"}))
        report = sync.publish(self.experiment, self.app, state)
        self.assertEqual(report["state"], "ok")
        self.assertEqual(report["items"], original["items"])

    def test_catalog_rereads_new_manifest_and_can_stage_corrected_expert(self):
        reference = self.experiment / "evaluations/periodic-5m/runs/305000000/result.json"
        reference.parent.mkdir(parents=True)
        reference.write_text(json.dumps({
            "status": "complete", "verified": True, "schema": "fixture", "protocol": {},
        }))
        sync.publish(self.experiment, self.app, self.root / "state.json")
        catalog = ModelCatalog(self.experiment, self.app)
        listing = catalog.listing()
        self.assertEqual([row["rank"] for row in listing["items"]], [1, 2, 3, 4, 5])
        self.assertTrue(all(row["group"] == "sky_corrected" for row in listing["items"]))
        self.add_evaluation(7, 1.0)
        (self.evaluations / "results.json").write_text(json.dumps({"results": self.rows}))
        sync.publish(self.experiment, self.app, self.root / "state.json")
        updated = catalog.listing()
        self.assertNotEqual(updated["items"][0]["id"], listing["items"][0]["id"])
        model = catalog.stage(updated["items"][0]["id"])
        self.assertEqual(model["checkpoint_steps"], 21_000_000)
        (self.app / "project/scripts/code_list.txt").write_text("different")
        with self.assertRaisesRegex(ValueError, "assets differ"):
            catalog.stage(updated["items"][0]["id"])

    def test_catalog_rejects_corrupted_published_artifact(self):
        report = sync.publish(self.experiment, self.app, self.root / "state.json")
        sha = report["items"][0]["id"]
        (self.app / "model/catalog" / (sha + ".evaluation.json")).write_text("{}")
        with self.assertRaisesRegex(ValueError, "artifact changed"):
            compatible_experts.entries(self.app, sync.digest)

    def test_bad_runtime_keeps_existing_manifest(self):
        manifest = self.app / "model/compatible-experts.json"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text('{"schema":"compatible-sky-experts/v1","items":[]}')
        native = json.loads((self.app / "native-build.json").read_text())
        native["observation_revision"] = "legacy"
        (self.app / "native-build.json").write_text(json.dumps(native))
        with self.assertRaisesRegex(ValueError, "Runtime native"):
            sync.publish(self.experiment, self.app, self.root / "state.json")
        self.assertEqual(json.loads(manifest.read_text())["items"], [])


if __name__ == "__main__":
    unittest.main()
