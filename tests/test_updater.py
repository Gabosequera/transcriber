from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import release_state
import updater
from tools import build_release


class VersionAndConfigTests(unittest.TestCase):
    def test_versions_are_strict_and_orderable(self):
        self.assertEqual(updater.parse_version("v1.2.3"), (1, 2, 3))
        self.assertGreater(updater.parse_version("2.0.0"), updater.parse_version("1.99.99"))
        for invalid in ("1.2", "1.2.3-beta", "01.2.3", "../1.2.3"):
            with self.subTest(invalid=invalid), self.assertRaises(updater.UpdateError):
                updater.parse_version(invalid)

    def test_repository_validation_rejects_ambiguous_paths(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "update-channel.json").write_text(json.dumps({
                "schema": 1, "repository": "owner/../repo", "auto_update": True,
                "check_timeout_seconds": 5,
            }), encoding="utf-8")
            with self.assertRaises(updater.UpdateError):
                updater.load_update_config(root)


class BundleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.output = Path(cls.temp.name)
        cls.archive, cls.manifest_path = build_release.build("0.1.0", cls.output)
        cls.manifest = json.loads(cls.manifest_path.read_text(encoding="utf-8"))

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_verified_install_is_atomic_and_activates_release(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            result = updater.install_bundle(
                root, self.manifest, self.archive, prepare_runtime=False,
            )
            release = result["release_dir"]
            self.assertTrue((release / "app.py").is_file())
            self.assertEqual(release_state.load_current(root)["current"], "0.1.0")
            self.assertFalse(any((root / "updates" / "staging").iterdir()))

    def test_release_contains_code_but_no_machine_state_or_models(self):
        with zipfile.ZipFile(self.archive) as bundle:
            names = set(bundle.namelist())
        self.assertIn("app.py", names)
        self.assertIn("app_paths.py", names)
        self.assertNotIn("config.json", names)
        self.assertFalse(any(name.startswith("models/") for name in names))
        self.assertFalse(any(name.startswith(".venv/") for name in names))

    def test_archive_hash_mismatch_never_activates(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            corrupt = root / "corrupt.zip"
            corrupt.write_bytes(self.archive.read_bytes() + b"tampered")
            with self.assertRaises(updater.UpdateError):
                updater.install_bundle(root, self.manifest, corrupt, prepare_runtime=False)
            self.assertIsNone(release_state.load_current(root)["current"])

    def test_zip_traversal_is_rejected_before_extraction(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            archive = root / "unsafe.zip"
            with zipfile.ZipFile(archive, "w") as bundle:
                bundle.writestr("../escape.py", b"bad")
                bundle.writestr("release-manifest.json", b"{}")
            with self.assertRaises(updater.UpdateError):
                updater.extract_verified_archive(
                    archive, root / "release", version="0.1.0",
                    release_manifest_sha256=hashlib.sha256(b"{}").hexdigest(),
                )
            self.assertFalse((root / "escape.py").exists())

    def test_existing_release_is_reverified_before_reuse(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            result = updater.install_bundle(root, self.manifest, self.archive, prepare_runtime=False)
            (result["release_dir"] / "app.py").write_bytes(b"corrupt")
            with self.assertRaises(updater.UpdateError):
                updater.install_bundle(root, self.manifest, self.archive, prepare_runtime=False)


class ReleaseStateTests(unittest.TestCase):
    def test_health_confirmation_requires_matching_nonce(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            state = release_state.activate(root, "1.0.0", "win-py313-0123456789abcdef")
            env = {
                "TRANSCRIPTOR_ROOT": str(root),
                "TRANSCRIPTOR_RELEASE_VERSION": "1.0.0",
                "TRANSCRIPTOR_RELEASE_NONCE": state["pending"]["nonce"],
            }
            with mock.patch.dict(os.environ, env, clear=False):
                self.assertTrue(release_state.mark_healthy_from_environment())
            self.assertTrue(release_state.confirm_if_healthy(root))
            self.assertIsNone(release_state.load_current(root)["pending"])

    def test_rollback_restores_previous_runtime(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            runtime_a = "win-py313-0123456789abcdef"
            runtime_b = "win-py313-fedcba9876543210"
            for version, runtime in (("1.0.0", runtime_a), ("2.0.0", runtime_b)):
                release_state.write_json_atomic(root / "releases" / version / "release-manifest.json", {
                    "runtime": {"id": runtime},
                })
            release_state.activate(root, "1.0.0", runtime_a)
            release_state.activate(root, "2.0.0", runtime_b)
            state = release_state.rollback(root, "2.0.0", "smoke test falló")
            self.assertEqual(state["current"], "1.0.0")
            self.assertEqual(state["runtime_id"], runtime_a)
            self.assertIsNone(state["pending"])


if __name__ == "__main__":
    unittest.main()
