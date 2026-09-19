import argparse
from contextlib import redirect_stdout
import importlib
import io
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

from tools import deploy


SOURCE = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(SOURCE / "room/admin"),
                str(SOURCE / "room/app-305m-admin-20260915")]
from registered_catalog import RegisteredCatalog


class DeploymentTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.repo = Path(self.temp.name)
        self.room = self.repo / "room"
        self.app = self.room / "app-305m-admin-20260915"
        self.training = self.repo / "training"
        self.patch = patch.multiple(deploy, REPO=self.repo, ROOM=self.room, APP=self.app)
        self.patch.start()
        for name in deploy.ASSETS:
            path = self.app / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(name.encode())
            paired = self.training / name.removeprefix("project/")
            paired.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, paired)
        source_hash = "a" * 64
        (self.app / "native-build.json").write_text(json.dumps({
            "module": deploy.MODULE, "sha256": deploy.digest(self.app / deploy.MODULE),
            "source_hashes": {"ygopro.h": source_hash},
        }))
        (self.room / "outbound").mkdir()
        (self.room / "outbound/native_contract.json").write_text(json.dumps({"source_sha256": source_hash}))
        for name in ["project/assets/deck/BestowedDragon.ydk", "project/script/c1.lua", "bin/mono"]:
            path = self.app / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("fixture")
        self.weights = self.repo / "trained.flax_model"
        self.weights.write_bytes(b"synthetic-test-weights")
        self.sidecar = self.repo / "trained.flax_model.json"
        self.sidecar.write_text(json.dumps({
            "schema": "ygo-training-checkpoint/v2", "architecture": "decision-v1",
            "model_args": {"decision_layers": 2},
        }))
        self.args = argparse.Namespace(checkpoint=self.weights, sidecar=self.sidecar,
                                       training_project=self.training, steps=640679936,
                                       label=None, activate=True)

    def tearDown(self):
        self.patch.stop()
        self.temp.cleanup()

    def register(self):
        with redirect_stdout(io.StringIO()):
            deploy.register_model(self.args)
        return RegisteredCatalog(self.room / "admin/models.json", self.app)

    def test_register_stage_and_unranked_listing(self):
        catalog = self.register()
        listing = catalog.listing()
        self.assertEqual(listing["ranking"], "registered_unranked")
        self.assertIsNone(listing["items"][0]["win_rate"])
        model = catalog.stage(listing["items"][0]["id"])
        self.assertEqual(model["checkpoint_steps"], self.args.steps)
        self.assertEqual(model, json.loads((self.room / "data/admin/active-model.json").read_text()))

    def test_registration_is_idempotent_without_reactivation(self):
        self.register()
        self.args.activate = False
        catalog = self.register()
        self.assertEqual(len(catalog.entries()), 1)

    def test_refuses_runtime_training_card_pool_mismatch(self):
        (self.training / "scripts/code_list.txt").write_text("different")
        with self.assertRaisesRegex(ValueError, "Training assets"):
            self.register()
        self.assertFalse((self.room / "admin/models.json").exists())

    def test_refuses_unknown_architecture(self):
        document = json.loads(self.sidecar.read_text())
        document["architecture"] = "arbitrary-pytorch-policy"
        self.sidecar.write_text(json.dumps(document))
        with self.assertRaisesRegex(ValueError, "sidecar"):
            self.register()

    def test_refuses_live_initial_activation(self):
        (self.room / "service.pid").write_text("123")
        with self.assertRaisesRegex(ValueError, "initial offline"):
            self.register()

    def test_sidecar_checkpoint_identity_must_match(self):
        document = json.loads(self.sidecar.read_text())
        document["sha256"] = "0" * 64
        self.sidecar.write_text(json.dumps(document))
        with self.assertRaisesRegex(ValueError, "checkpoint hash"):
            self.register()

    def test_sidecar_training_steps_must_match(self):
        document = json.loads(self.sidecar.read_text())
        document["global_step"] = self.args.steps + 1
        self.sidecar.write_text(json.dumps(document))
        with self.assertRaisesRegex(ValueError, "Training steps"):
            self.register()

    def test_rejects_tampered_weights(self):
        catalog = self.register()
        row = catalog.entries()[0]
        (self.app / row["model"]["checkpoint_file"]).write_bytes(b"tampered")
        with self.assertRaisesRegex(RuntimeError, "integrity"):
            catalog.stage(row["model"]["checkpoint_sha256"])

    def test_rejects_tampered_sidecar(self):
        catalog = self.register()
        row = catalog.entries()[0]
        sidecar = self.app / (row["model"]["checkpoint_file"] + ".json")
        sidecar.write_text("{}")
        with self.assertRaisesRegex(ValueError, "sidecar"):
            catalog.stage(row["model"]["checkpoint_sha256"])

    def test_rejects_runtime_asset_drift(self):
        catalog = self.register()
        row = catalog.entries()[0]
        (self.app / "project/scripts/code_list.txt").write_text("changed")
        with self.assertRaisesRegex(ValueError, "Training assets"):
            catalog.stage(row["model"]["checkpoint_sha256"])

    def test_rejects_catalog_path_traversal(self):
        catalog = self.register()
        path = self.room / "admin/models.json"
        document = json.loads(path.read_text())
        document["models"][0]["model"]["checkpoint_file"] = "../../escape"
        path.write_text(json.dumps(document))
        with self.assertRaises(ValueError):
            catalog.entries()

    def test_prepare_manifest_contains_native_assets_and_scripts_not_weights(self):
        self.register()
        with redirect_stdout(io.StringIO()):
            deploy.prepare_runtime(argparse.Namespace())
        files = json.loads((self.app / "manifest.json").read_text())["files"]
        self.assertTrue(set(deploy.ASSETS).issubset(files))
        self.assertIn("project/script/c1.lua", files)
        self.assertFalse(any(name.endswith(".flax_model") for name in files))

    def test_native_contract_mismatch_fails_closed(self):
        (self.room / "outbound/native_contract.json").write_text(json.dumps({"source_sha256": "b" * 64}))
        with self.assertRaisesRegex(ValueError, "contracts"):
            deploy.runtime_assets()

    def test_init_compute_does_not_print_or_rotate_credentials(self):
        output = io.StringIO()
        args = argparse.Namespace(gateway="https://compute.example.org/")
        with redirect_stdout(output):
            deploy.init_compute(args)
        private = json.loads((self.room / "private/panel-bootstrap.json").read_text())
        self.assertNotIn(private["token"], output.getvalue())
        self.assertNotIn(private["agent_key"], output.getvalue())
        saved = (self.room / "admin/control.key").read_bytes()
        with self.assertRaisesRegex(ValueError, "rotation"):
            deploy.init_compute(args)
        self.assertEqual(saved, (self.room / "admin/control.key").read_bytes())

    def test_endpoint_rejects_plaintext_and_credential_urls(self):
        for value in ["http://example.org", "https://user:pass@example.org",
                      "https://example.org/path", "https://example.org/?token=secret"]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                deploy.endpoint(value)
        self.assertEqual(deploy.endpoint("http://127.0.0.1:8090", localhost=True),
                         "http://127.0.0.1:8090")

    def test_panel_bootstrap_generates_private_configuration(self):
        with redirect_stdout(io.StringIO()):
            deploy.init_compute(argparse.Namespace(gateway="https://compute.example.org"))
        deck = self.repo / "model.ydk"
        deck.write_text("#main\n" + "\n".join(map(str, range(1, 41))) + "\n#extra\n!side\n")
        args = argparse.Namespace(
            origin="https://admin.example.org", game_host="game.example.org",
            bootstrap=self.room / "private/panel-bootstrap.json", deck=deck, username="operator")
        imports = [str(SOURCE / "panel/ygo-tcp-bridge/app"),
                   str(SOURCE / "panel/ygo-admin/app")]
        password = "fixture-only-long-password"
        output = io.StringIO()
        with patch.object(sys, "path", [*imports, *sys.path]), patch(
                "getpass.getpass", side_effect=[password, password]), redirect_stdout(output):
            deploy.init_panel(args)
        site = json.loads((self.repo / "panel/private/site.json").read_text())
        user = json.loads((self.repo / "panel/private/user.json").read_text())
        bridge = json.loads((self.repo / "panel/private/bridge.json").read_text())
        self.assertEqual(site["agent_url"], "https://compute.example.org/control")
        self.assertEqual(user["username"], "operator")
        self.assertIsInstance(user["password"], dict)
        self.assertEqual(len(bridge["rooms"]), 2)
        self.assertNotIn(password, output.getvalue())
        self.assertNotIn(site["agent_key"], output.getvalue())
        self.assertNotIn(bridge["token"], output.getvalue())
        with self.assertRaisesRegex(ValueError, "rotation"):
            deploy.init_panel(args)

    def test_initial_preflight_verifies_registered_snapshot(self):
        self.register()
        with redirect_stdout(io.StringIO()):
            deploy.prepare_runtime(argparse.Namespace())
            deploy.preflight(argparse.Namespace(warmup=False))


if __name__ == "__main__":
    unittest.main()
