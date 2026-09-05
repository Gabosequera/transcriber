#!/usr/bin/env python3
"""Construye los assets deterministas de una release de Windows."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import zipfile
from pathlib import Path


PROJECT = Path(__file__).resolve().parents[1]
VERSION_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
ROOT_FILES = {
    "VERSION", "README.md", "INSTALL-WINDOWS.md", "CAMBIO-INTERFAZ-Y-DISTRIBUCION.md",
    "LICENSE.md", "COMMERCIAL-LICENSE.md",
    "requirements-windows.txt", "requirements.txt", "icon.png", "icon.svg",
    "update-channel.json",
}
INSTALLER_ROOT_FILES = {
    "setup-windows.bat", "setup-windows.ps1", "run.bat", "diagnostico.bat",
    "configurar-github.bat", "configurar-github.ps1",
}
INSTALLER_TOOL_FILES = {"tools/verify_windows_install.py"}
TORCH_VERSION = "2.8.0"
TORCH_INDEX = "https://download.pytorch.org/whl/cu128"


def canonical_json(value: dict) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()


def digest_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def source_files() -> list[Path]:
    files = [path for path in PROJECT.glob("*.py") if path.is_file()]
    files.extend(PROJECT / name for name in sorted(ROOT_FILES) if (PROJECT / name).is_file())
    files.extend((PROJECT / "skills").glob("*/SKILL.md"))
    return sorted(set(files), key=lambda path: path.name.casefold())


def build(version: str, output: Path, *, require_repository: bool = False) -> tuple[Path, Path]:
    if not VERSION_RE.fullmatch(version):
        raise SystemExit(f"versión inválida: {version!r}")
    declared = (PROJECT / "VERSION").read_text(encoding="utf-8").strip()
    if declared != version:
        raise SystemExit(f"VERSION declara {declared!r}, pero se solicitó {version!r}")
    channel = json.loads((PROJECT / "update-channel.json").read_text(encoding="utf-8"))
    if require_repository and not str(channel.get("repository") or "").strip():
        raise SystemExit("update-channel.json debe declarar repository antes de publicar")

    requirements = (PROJECT / "requirements-windows.txt").read_bytes()
    requirements_sha = digest_bytes(requirements)
    runtime_material = b"\0".join([
        b"python=3.13", f"torch={TORCH_VERSION}".encode(), TORCH_INDEX.encode(),
        requirements_sha.encode(), b"emotiefflib=no-deps",
    ])
    runtime_id = f"win-py313-{digest_bytes(runtime_material)[:16]}"
    entries = []
    payloads: dict[str, bytes] = {}
    for path in source_files():
        data = path.read_bytes()
        relative = path.relative_to(PROJECT).as_posix()
        payloads[relative] = data
        entries.append({"path": relative, "size": len(data), "sha256": digest_bytes(data)})
    release_manifest = {
        "schema": 1,
        "product": "transcriptor",
        "version": version,
        "platform": "windows-x86_64",
        "entrypoint": "app.py",
        "runtime": {
            "id": runtime_id,
            "python": "3.13",
            "requirements_file": "requirements-windows.txt",
            "requirements_sha256": requirements_sha,
            "torch": {"version": TORCH_VERSION, "index_url": TORCH_INDEX},
        },
        "files": entries,
    }
    release_manifest_bytes = canonical_json(release_manifest)
    output.mkdir(parents=True, exist_ok=True)
    archive = output / f"transcriptor-windows-v{version}.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as bundle:
        for relative, data in sorted(payloads.items()):
            info = zipfile.ZipInfo(relative, date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            bundle.writestr(info, data)
        info = zipfile.ZipInfo("release-manifest.json", date_time=(2026, 1, 1, 0, 0, 0))
        info.compress_type = zipfile.ZIP_DEFLATED
        info.external_attr = 0o100644 << 16
        bundle.writestr(info, release_manifest_bytes)
    update_manifest = {
        "schema": 1,
        "product": "transcriptor",
        "version": version,
        "asset": {"name": archive.name, "size": archive.stat().st_size,
                  "sha256": hashlib.sha256(archive.read_bytes()).hexdigest()},
        "release_manifest_sha256": digest_bytes(release_manifest_bytes),
    }
    manifest_path = output / f"transcriptor-update-v{version}.json"
    manifest_path.write_bytes(canonical_json(update_manifest))
    installer = output / f"transcriptor-installer-v{version}.zip"
    installer_payloads = dict(payloads)
    for name in INSTALLER_ROOT_FILES:
        path = PROJECT / name
        if not path.is_file():
            raise SystemExit(f"falta archivo del instalador: {name}")
        installer_payloads[name] = path.read_bytes()
    installer_payloads["tools/build_release.py"] = Path(__file__).read_bytes()
    for relative in INSTALLER_TOOL_FILES:
        path = PROJECT / relative
        if not path.is_file():
            raise SystemExit(f"falta herramienta del instalador: {relative}")
        installer_payloads[relative] = path.read_bytes()
    with zipfile.ZipFile(installer, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as bundle:
        for relative, data in sorted(installer_payloads.items()):
            info = zipfile.ZipInfo(relative, date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            bundle.writestr(info, data)
    return archive, manifest_path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", default=(PROJECT / "VERSION").read_text().strip())
    parser.add_argument("--output", type=Path, default=PROJECT / "dist")
    parser.add_argument("--require-repository", action="store_true")
    args = parser.parse_args()
    archive, manifest = build(args.version, args.output, require_repository=args.require_repository)
    print(archive)
    print(manifest)
    print(args.output / f"transcriptor-installer-v{args.version}.zip")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
