"""Portable installation boundaries and the documented local startup workflow."""
import hashlib
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
from urllib.request import ProxyHandler, build_opener

from tools.fetch_runtime import install

ROOT = Path(__file__).resolve().parents[1]


class RuntimeInstallationTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.archive = self.root / "fixture.tar.gz"

    def make_archive(self, name="runtime/file.txt", body=b"fixture"):
        with tarfile.open(self.archive, "w:gz") as archive:
            member = tarfile.TarInfo(name)
            member.size = len(body)
            archive.addfile(member, io.BytesIO(body))
        return {"sha256": hashlib.sha256(self.archive.read_bytes()).hexdigest()}

    def test_hash_mismatch_is_rejected_before_installation(self):
        spec = self.make_archive()
        spec["sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "SHA-256"):
            install(self.archive, self.root / "output", spec)
        self.assertFalse((self.root / "output").exists())

    def test_archive_cannot_write_outside_destination(self):
        spec = self.make_archive("../escaped.txt")
        with self.assertRaisesRegex(ValueError, "Unsafe"):
            install(self.archive, self.root / "output", spec)
        self.assertFalse((self.root / "escaped.txt").exists())

    def test_strip_component_preserves_existing_identical_files(self):
        spec = {**self.make_archive(), "strip_components": 1}
        target = self.root / "output"
        self.assertEqual(install(self.archive, target, spec), 1)
        self.assertEqual(install(self.archive, target, spec), 1)
        self.assertEqual((target / "file.txt").read_bytes(), b"fixture")

    def test_existing_different_file_is_not_overwritten(self):
        spec = self.make_archive()
        target = self.root / "output"
        existing = target / "runtime/file.txt"
        existing.parent.mkdir(parents=True)
        existing.write_bytes(b"original")
        with self.assertRaisesRegex(ValueError, "Existing file differs"):
            install(self.archive, target, spec)
        self.assertEqual(existing.read_bytes(), b"original")


@unittest.skipUnless(os.name == "posix", "POSIX process shutdown integration")
class LocalTournamentStartupTest(unittest.TestCase):
    def test_manual_workflow_starts_authenticates_and_stops(self):
        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary)
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", 0))
                port = probe.getsockname()[1]
            with (data / "launcher.log").open("wb") as log:
                process = subprocess.Popen(
                    [sys.executable, str(ROOT / "tools/run_tournament.py"), "--port", str(port),
                     "--data-dir", str(data)], cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
                try:
                    opener = build_opener(ProxyHandler({}))
                    deadline = time.monotonic() + 20
                    while time.monotonic() < deadline:
                        try:
                            with opener.open(f"http://127.0.0.1:{port}/health", timeout=1) as response:
                                self.assertEqual(json.load(response)["status"], "ok")
                            break
                        except OSError:
                            self.assertIsNone(process.poll())
                            time.sleep(0.1)
                    else:
                        self.fail("Documented launcher did not become ready")
                    with opener.open(f"http://127.0.0.1:{port}/api/config") as response:
                        self.assertTrue(json.load(response)["admin_required"])
                    key_file = data / "private/service-keys.json"
                    keys = json.loads(key_file.read_text())
                    self.assertEqual(len(set(keys.values())), 3)
                    self.assertEqual(key_file.stat().st_mode & 0o777, 0o600)
                finally:
                    process.terminate()
                    process.wait(timeout=20)
                self.assertEqual(process.returncode, 0)
                with socket.socket() as probe:
                    probe.settimeout(1)
                    self.assertNotEqual(probe.connect_ex(("127.0.0.1", port)), 0)


if __name__ == "__main__":
    unittest.main()
