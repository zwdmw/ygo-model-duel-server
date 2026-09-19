"""Activating a rebuilt external pool must not need an agent restart.

Regression: the pool was resolved once in Agent.__init__, so a rebuilt pool went
unnoticed until the service was restarted. Sessions then ran against the previous
pool and stopped with "Observed cards outside the model database" / "External pool
manifest changed" part-way through a duel.
"""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from .pool_assets import PoolCache


class PoolCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "card-pools/test-pool").mkdir(parents=True)
        self.manifest = self.root / "card-pools/test-pool/manifest.json"
        self.manifest.write_text('{"schema": 1}')
        self.pointer = self.root / "card-pools/current.json"
        self.pointer.write_text(json.dumps({"id": "test-pool", "sha256": "a" * 64}))

    def tearDown(self):
        self.tmp.cleanup()

    def test_missing_pointer_resolves_to_no_pool_once(self):
        self.pointer.unlink()
        cache = PoolCache(self.root, self.root)
        with patch("outbound.pool_assets.active_pool", return_value=None) as fake:
            self.assertIsNone(cache.resolve())
            self.assertIsNone(cache.resolve())
        self.assertEqual(fake.call_count, 1)
        self.assertEqual(cache.error, "")

    def test_manifest_and_pointer_changes_re_resolve(self):
        pool = object()
        cache = PoolCache(self.root, self.root)
        with patch("outbound.pool_assets.active_pool", return_value=pool) as fake:
            self.assertIs(cache.resolve(), pool)
            self.assertIs(cache.resolve(), pool)
            self.assertEqual(fake.call_count, 1, "an unchanged pool must be served from cache")
            self.manifest.write_text('{"schema": 1, "rebuilt": true}')
            self.assertIs(cache.resolve(), pool)
            self.assertEqual(fake.call_count, 2, "a rebuilt manifest must take effect immediately")
            self.pointer.write_text(json.dumps({"id": "test-pool", "sha256": "b" * 64}))
            self.assertIs(cache.resolve(), pool)
            self.assertEqual(fake.call_count, 3, "a repointed pool must take effect immediately")

    def test_damaged_pool_keeps_last_good_pool_and_is_reported(self):
        good = object()
        cache = PoolCache(self.root, self.root)
        with patch("outbound.pool_assets.active_pool", return_value=good):
            self.assertIs(cache.resolve(), good)
        self.manifest.write_text('{"schema": 1, "damaged": true}')
        with patch("outbound.pool_assets.active_pool",
                   side_effect=ValueError("External pool integrity failure: cards.cdb")):
            self.assertIs(cache.resolve(strict=False), good, "display must keep working")
            self.assertIn("integrity failure", cache.error)
            with self.assertRaises(ValueError):
                cache.resolve()
        self.manifest.write_text('{"schema": 1, "repaired": true}')
        with patch("outbound.pool_assets.active_pool", return_value=good):
            self.assertIs(cache.resolve(), good, "a repair must be picked up without a restart")
        self.assertEqual(cache.error, "")

    def test_on_change_fires_only_when_the_pool_changes(self):
        seen = []
        cache = PoolCache(self.root, self.root, on_change=seen.append)
        with patch("outbound.pool_assets.active_pool", return_value="pool-1"):
            cache.resolve()
            cache.resolve()
        self.assertEqual(seen, ["pool-1"])
        self.manifest.write_text('{"schema": 1, "v2": true}')
        with patch("outbound.pool_assets.active_pool", return_value="pool-2"):
            cache.resolve()
            cache.resolve()
        self.assertEqual(seen, ["pool-1", "pool-2"])


if __name__ == "__main__":
    unittest.main()
