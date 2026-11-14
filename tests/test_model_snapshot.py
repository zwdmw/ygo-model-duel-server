import sys
import unittest
from pathlib import Path


ROOM = Path(__file__).resolve().parents[1] / "room"
APP = ROOM / "app-305m-admin-20260915"
sys.path.insert(0, str(APP))

from model_snapshot import SNAPSHOTS, snapshot, validate_snapshot


class ModelSnapshotTests(unittest.TestCase):
    def test_454m_identity_is_pinned(self):
        model = snapshot("454M")
        self.assertEqual(model["checkpoint_steps"], 454000640)
        self.assertEqual(
            model["checkpoint_sha256"],
            "71cab8ce0f247b54029cc4242bcd4a80831b49c67f0dede9093a650441b82d4f",
        )
        self.assertEqual(validate_snapshot(model), model)

    def test_454m_uses_catalog_path(self):
        self.assertTrue(
            SNAPSHOTS["454M"]["checkpoint_file"].startswith("model/catalog/")
        )


if __name__ == "__main__":
    unittest.main()
