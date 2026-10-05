"""Extract a verified repository ZIP with Windows long-path support."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import tempfile
import zipfile


def filesystem_path(path: Path) -> Path:
    absolute = str(path.resolve())
    if os.name == "nt" and not absolute.startswith("\\\\?\\"):
        absolute = ("\\\\?\\UNC\\" + absolute[2:] if absolute.startswith("\\\\")
                    else "\\\\?\\" + absolute)
    return Path(absolute)


def install(archive: Path, target: Path, marker: Path) -> None:
    target = filesystem_path(target)
    relative_marker = filesystem_path(marker).relative_to(target)
    with tempfile.TemporaryDirectory(prefix="repo-", dir=target.parent) as temporary:
        staging = Path(temporary)
        with zipfile.ZipFile(archive) as bundle:
            # Only repository-relative paths may be written into staging.
            for entry in bundle.infolist():
                destination = (staging / entry.filename).resolve()
                if not destination.is_relative_to(staging.resolve()):
                    raise ValueError(f"Unsafe archive path: {entry.filename}")
            bundle.extractall(staging)
        roots = list(staging.iterdir())
        if len(roots) != 1 or not roots[0].is_dir():
            raise ValueError("Expected one repository directory in ZIP")
        source = roots[0]
        if not (source / relative_marker).is_file():
            raise ValueError(f"Component is missing {relative_marker}")
        if target.exists():
            shutil.rmtree(target)
        source.rename(target)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("target", type=Path)
    parser.add_argument("marker", type=Path)
    args = parser.parse_args()
    install(args.archive, args.target, args.marker)
