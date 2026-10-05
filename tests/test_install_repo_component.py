import tempfile
import unittest
import zipfile
from pathlib import Path

from tools.install_repo_component import filesystem_path, install


class RepoComponentTests(unittest.TestCase):
    def test_long_paths_and_hidden_files_are_installed(self):
        with tempfile.TemporaryDirectory(dir=filesystem_path(Path(tempfile.gettempdir()))) as temporary:
            root = filesystem_path(Path(temporary))
            archive = root / "repo.zip"
            target = root / "component"
            relative = "/".join(["nested" * 6] * 7) + "/file.txt"
            with zipfile.ZipFile(archive, "w") as bundle:
                bundle.writestr("repo/.gitignore", "cache/")
                bundle.writestr("repo/train/model.py", "# model")
                bundle.writestr("repo/" + relative, "long path")
            install(archive, target, target / "train/model.py")
            self.assertEqual((target / relative).read_text(), "long path")
            self.assertEqual((target / ".gitignore").read_text(), "cache/")
            self.assertFalse(list(root.glob("repo-*")))

    def test_missing_marker_preserves_existing_component(self):
        with tempfile.TemporaryDirectory(dir=filesystem_path(Path(tempfile.gettempdir()))) as temporary:
            root = filesystem_path(Path(temporary))
            archive = root / "repo.zip"
            target = root / "component"
            target.mkdir()
            (target / "existing.txt").write_text("keep")
            with zipfile.ZipFile(archive, "w") as bundle:
                bundle.writestr("repo/README.md", "incomplete")
            with self.assertRaises(ValueError):
                install(archive, target, target / "train/model.py")
            self.assertEqual((target / "existing.txt").read_text(), "keep")
            self.assertFalse(list(root.glob("repo-*")))
