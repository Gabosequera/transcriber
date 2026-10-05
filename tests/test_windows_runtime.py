"""El runtime creado por el updater debe arrancar tras moverse de staging."""
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import updater


@unittest.skipUnless(os.name == "nt", "runtime Windows")
class WindowsRuntimeTests(unittest.TestCase):
    def test_new_runtime_starts_and_existing_packages_survive_reuse(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            release = root / "releases" / "1.0.0"
            release.mkdir(parents=True)
            requirements = release / "requirements-windows.txt"
            requirements.write_text("", encoding="utf-8")
            uv = root / "shared" / "tools" / "uv.exe"
            uv.parent.mkdir(parents=True)
            uv.touch()
            manifest = {"runtime": {
                "id": "win-py313-test", "python": "3.13",
                "requirements_sha256": updater.sha256_file(requirements),
                "torch": {"version": "2.8.0", "index_url": "https://download.pytorch.org/whl/cu128"},
            }}
            real_popen = subprocess.Popen

            def launch(command, **kwargs):
                # La prueba crea un venv real; omite solo la descarga de paquetes.
                if command[0] == str(uv):
                    return SimpleNamespace(poll=lambda: 0, returncode=0)
                return real_popen(command, **kwargs)

            with mock.patch.object(updater.subprocess, "Popen", side_effect=launch):
                runtime = updater.ensure_runtime(root, manifest, release)
            result = subprocess.run([
                str(runtime / "Scripts" / "python.exe"), "-c",
                "import sys, tkinter; assert sys.version_info[:2] == (3, 13); print('OK')",
            ], check=True, capture_output=True, text=True)
            self.assertEqual(result.stdout.strip(), "OK")
            sentinel = runtime / "Lib" / "site-packages" / "keep.txt"
            sentinel.write_text("installed package", encoding="utf-8")
            with mock.patch.object(updater.subprocess, "Popen", side_effect=AssertionError("runtime recreated")):
                self.assertEqual(updater.ensure_runtime(root, manifest, release), runtime)
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "installed package")


if __name__ == "__main__":
    unittest.main()
