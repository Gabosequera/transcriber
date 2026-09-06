"""Actualizador transaccional para releases publicadas en GitHub.

Sólo usa la biblioteca estándar para que pueda ejecutarse desde el entorno bootstrap.
Las releases son inmutables: se descargan a staging, se validan por SHA-256 archivo por
archivo y sólo entonces se cambia el puntero de versión activa.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable, Iterator

import release_state


API_VERSION = "2022-11-28"
MANIFEST_SCHEMA = 1
MAX_MANIFEST_BYTES = 1 * 1024 * 1024
MAX_ARCHIVE_BYTES = 250 * 1024 * 1024
MAX_EXTRACTED_BYTES = 150 * 1024 * 1024
REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
VERSION_RE = re.compile(r"^(?:v)?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
Progress = Callable[[str, int | None, int | None], None]


class UpdateError(RuntimeError):
    """Fallo controlado: la release activa debe continuar sin cambios."""


def _progress(callback: Progress | None, message: str, done=None, total=None) -> None:
    if callback:
        callback(message, done, total)


def parse_version(value: str) -> tuple[int, int, int]:
    match = VERSION_RE.fullmatch(value.strip())
    if not match:
        raise UpdateError(f"versión estable inválida: {value!r}")
    return tuple(int(part) for part in match.groups())


def normalized_version(value: str) -> str:
    return ".".join(str(part) for part in parse_version(value))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_update_config(root: Path, source_dir: Path | None = None) -> dict:
    override = os.environ.get("TRANSCRIPTOR_UPDATE_CONFIG", "").strip()
    candidates = [Path(override)] if override else []
    candidates.append(root / "update-channel.json")
    if source_dir:
        candidates.append(source_dir / "update-channel.json")
    config = None
    for candidate in candidates:
        try:
            config = json.loads(candidate.read_text(encoding="utf-8"))
            break
        except FileNotFoundError:
            continue
        except (OSError, ValueError) as error:
            raise UpdateError(f"configuración de actualizaciones inválida en {candidate}: {error}") from error
    if config is None:
        return {"schema": 1, "repository": "", "auto_update": False, "check_timeout_seconds": 5}
    if not isinstance(config, dict) or config.get("schema") != 1:
        raise UpdateError("update-channel.json usa un schema no compatible")
    repository = str(config.get("repository") or "").strip()
    if repository and (not REPOSITORY_RE.fullmatch(repository) or ".." in repository):
        raise UpdateError("repository debe tener la forma owner/repositorio")
    timeout = config.get("check_timeout_seconds", 5)
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not 1 <= timeout <= 30:
        raise UpdateError("check_timeout_seconds debe estar entre 1 y 30")
    return {
        "schema": 1,
        "repository": repository,
        "channel": "stable",
        "auto_update": bool(config.get("auto_update", True)),
        "check_timeout_seconds": float(timeout),
    }


@dataclass(frozen=True)
class UpdateCandidate:
    version: str
    release_url: str
    archive_asset: dict
    update_manifest: dict


class GitHubClient:
    def __init__(self, repository: str, timeout: float = 5.0, token: str | None = None):
        if not REPOSITORY_RE.fullmatch(repository) or ".." in repository:
            raise UpdateError("repositorio GitHub inválido")
        self.repository = repository
        self.timeout = timeout
        self.token = token or os.environ.get("TRANSCRIPTOR_GITHUB_TOKEN", "").strip() or None

    def _headers(self, accept: str = "application/vnd.github+json") -> dict[str, str]:
        headers = {
            "Accept": accept,
            "X-GitHub-Api-Version": API_VERSION,
            "User-Agent": "Transcriptor-Updater/1",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def _read(self, url: str, *, limit: int, accept="application/vnd.github+json") -> bytes:
        request = urllib.request.Request(url, headers=self._headers(accept))
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                declared = response.headers.get("Content-Length")
                if declared:
                    try:
                        if int(declared) > limit:
                            raise UpdateError("GitHub devolvió un recurso mayor que el límite permitido")
                    except ValueError as error:
                        raise UpdateError("GitHub devolvió un Content-Length inválido") from error
                payload = response.read(limit + 1)
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise UpdateError(f"no se pudo consultar GitHub: {error}") from error
        if len(payload) > limit:
            raise UpdateError("GitHub devolvió un recurso mayor que el límite permitido")
        return payload

    def latest_release(self) -> dict:
        url = f"https://api.github.com/repos/{self.repository}/releases/latest"
        try:
            data = json.loads(self._read(url, limit=MAX_MANIFEST_BYTES))
        except ValueError as error:
            raise UpdateError("GitHub devolvió JSON inválido") from error
        if not isinstance(data, dict) or data.get("draft") or data.get("prerelease"):
            raise UpdateError("GitHub no devolvió una release estable válida")
        return data

    def read_asset(self, asset: dict, *, limit: int) -> bytes:
        url = asset.get("url") if self.token else asset.get("browser_download_url")
        if not isinstance(url, str) or not url.startswith("https://"):
            raise UpdateError("la release contiene una URL de asset inválida")
        accept = "application/octet-stream" if self.token else "application/vnd.github.raw"
        return self._read(url, limit=limit, accept=accept)

    def download_asset(self, asset: dict, destination: Path, *, expected_size: int,
                       progress: Progress | None = None) -> None:
        if not 0 < expected_size <= MAX_ARCHIVE_BYTES:
            raise UpdateError("tamaño de paquete inválido")
        url = asset.get("url") if self.token else asset.get("browser_download_url")
        if not isinstance(url, str) or not url.startswith("https://"):
            raise UpdateError("la release contiene una URL de descarga inválida")
        request = urllib.request.Request(
            url, headers=self._headers("application/octet-stream" if self.token else "application/vnd.github.raw")
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        written = 0
        try:
            with urllib.request.urlopen(request, timeout=max(self.timeout, 30)) as response, destination.open("wb") as output:
                declared = response.headers.get("Content-Length")
                if declared and int(declared) != expected_size:
                    raise UpdateError("el tamaño HTTP no coincide con el manifiesto")
                while True:
                    block = response.read(1024 * 1024)
                    if not block:
                        break
                    written += len(block)
                    if written > expected_size or written > MAX_ARCHIVE_BYTES:
                        raise UpdateError("la descarga excedió el tamaño declarado")
                    output.write(block)
                    _progress(progress, "Descargando actualización", written, expected_size)
                output.flush()
                os.fsync(output.fileno())
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            destination.unlink(missing_ok=True)
            raise UpdateError(f"falló la descarga de la actualización: {error}") from error
        if written != expected_size:
            destination.unlink(missing_ok=True)
            raise UpdateError("la descarga quedó incompleta")


def _asset_by_name(release: dict, name: str) -> dict:
    matches = [asset for asset in release.get("assets", []) if asset.get("name") == name]
    if len(matches) != 1:
        raise UpdateError(f"la release debe contener exactamente un asset {name!r}")
    return matches[0]


def check_for_update(client: GitHubClient, current_version: str) -> UpdateCandidate | None:
    current = parse_version(current_version)
    release = client.latest_release()
    version = normalized_version(str(release.get("tag_name") or ""))
    if parse_version(version) <= current:
        return None
    manifest_name = f"transcriptor-update-v{version}.json"
    manifest_asset = _asset_by_name(release, manifest_name)
    manifest_bytes = client.read_asset(manifest_asset, limit=MAX_MANIFEST_BYTES)
    if manifest_asset.get("size") != len(manifest_bytes):
        raise UpdateError("el tamaño del manifiesto no coincide con GitHub")
    digest = str(manifest_asset.get("digest") or "")
    if digest.startswith("sha256:") and hashlib.sha256(manifest_bytes).hexdigest() != digest[7:].lower():
        raise UpdateError("el digest GitHub del manifiesto no coincide")
    try:
        manifest = json.loads(manifest_bytes)
    except ValueError as error:
        raise UpdateError("el manifiesto de actualización no es JSON válido") from error
    if not isinstance(manifest, dict) or manifest.get("schema") != MANIFEST_SCHEMA:
        raise UpdateError("schema de actualización no compatible")
    if normalized_version(str(manifest.get("version") or "")) != version:
        raise UpdateError("la versión del manifiesto no coincide con el tag")
    asset_info = manifest.get("asset") or {}
    asset_name = asset_info.get("name")
    if asset_name != f"transcriptor-windows-v{version}.zip":
        raise UpdateError("nombre de paquete inesperado en el manifiesto")
    archive_asset = _asset_by_name(release, asset_name)
    size = asset_info.get("size")
    if not isinstance(size, int) or isinstance(size, bool) or size <= 0 or size > MAX_ARCHIVE_BYTES:
        raise UpdateError("tamaño inválido en el manifiesto")
    if archive_asset.get("size") != size:
        raise UpdateError("el tamaño del asset GitHub no coincide con el manifiesto")
    expected_hash = str(asset_info.get("sha256") or "").lower()
    if not re.fullmatch(r"[0-9a-f]{64}", expected_hash):
        raise UpdateError("SHA-256 del paquete inválido")
    github_digest = str(archive_asset.get("digest") or "")
    if github_digest.startswith("sha256:") and github_digest[7:].lower() != expected_hash:
        raise UpdateError("el digest publicado por GitHub no coincide con el manifiesto")
    return UpdateCandidate(
        version=version,
        release_url=str(release.get("html_url") or ""),
        archive_asset=archive_asset,
        update_manifest=manifest,
    )


def _safe_relative_path(raw: str) -> PurePosixPath:
    if not isinstance(raw, str) or not raw or "\\" in raw or "\x00" in raw:
        raise UpdateError(f"ruta de release inválida: {raw!r}")
    path = PurePosixPath(raw)
    if path.is_absolute() or any(part in ("", ".", "..") for part in path.parts):
        raise UpdateError(f"ruta de release insegura: {raw!r}")
    if path.parts[0].endswith(":"):
        raise UpdateError(f"ruta de release insegura: {raw!r}")
    return path


def _validate_release_manifest(manifest: dict, expected_version: str) -> dict[str, dict]:
    if not isinstance(manifest, dict) or manifest.get("schema") != MANIFEST_SCHEMA:
        raise UpdateError("release-manifest.json no es compatible")
    if normalized_version(str(manifest.get("version") or "")) != expected_version:
        raise UpdateError("la versión interna del paquete no coincide")
    if manifest.get("product") != "transcriptor":
        raise UpdateError("producto inválido en el manifiesto interno")
    if manifest.get("platform") != "windows-x86_64" or manifest.get("entrypoint") != "app.py":
        raise UpdateError("plataforma o entrypoint inválido")
    runtime = manifest.get("runtime") or {}
    if not re.fullmatch(r"win-py313-[0-9a-f]{16}", str(runtime.get("id") or "")):
        raise UpdateError("identificador de runtime inválido")
    if runtime.get("python") != "3.13" or runtime.get("requirements_file") != "requirements-windows.txt":
        raise UpdateError("contrato Python/requirements no compatible")
    requirements_sha = str(runtime.get("requirements_sha256") or "")
    if not re.fullmatch(r"[0-9a-f]{64}", requirements_sha):
        raise UpdateError("hash de requirements inválido")
    torch = runtime.get("torch") or {}
    torch_version = str(torch.get("version") or "")
    index_url = str(torch.get("index_url") or "")
    if not re.fullmatch(r"\d+\.\d+\.\d+", torch_version):
        raise UpdateError("versión de Torch inválida")
    if index_url not in {"https://download.pytorch.org/whl/cu128"}:
        raise UpdateError("índice de Torch no autorizado")
    runtime_material = b"\0".join([
        b"python=3.13", f"torch={torch_version}".encode(), index_url.encode(),
        requirements_sha.encode(), b"emotiefflib=no-deps",
    ])
    expected_runtime_id = f"win-py313-{hashlib.sha256(runtime_material).hexdigest()[:16]}"
    if runtime["id"] != expected_runtime_id:
        raise UpdateError("el runtime_id no corresponde a sus dependencias")
    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        raise UpdateError("el manifiesto interno no declara archivos")
    expected: dict[str, dict] = {}
    folded: set[str] = set()
    total = 0
    for item in files:
        if not isinstance(item, dict):
            raise UpdateError("entrada de archivo inválida")
        path = str(_safe_relative_path(item.get("path")))
        key = path.casefold()
        if key in folded:
            raise UpdateError("el paquete contiene rutas duplicadas para Windows")
        folded.add(key)
        size, digest = item.get("size"), str(item.get("sha256") or "").lower()
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise UpdateError(f"tamaño inválido para {path}")
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise UpdateError(f"SHA-256 inválido para {path}")
        total += size
        if total > MAX_EXTRACTED_BYTES:
            raise UpdateError("la release excede el tamaño descomprimido permitido")
        expected[path] = item
    if "app.py" not in expected or "VERSION" not in expected:
        raise UpdateError("la release no contiene sus archivos esenciales")
    return expected


def _is_bytecode_artifact(relative: Path) -> bool:
    """Caché de bytecode que Python puede generar al importar módulos de la release.

    No forma parte del manifiesto y no debe invalidar la instalación."""
    return "__pycache__" in relative.parts or relative.suffix in {".pyc", ".pyo"}


def verify_installed_release(directory: Path, expected_version: str,
                             release_manifest_sha256: str | None = None, *,
                             strict: bool = False) -> dict:
    """Comprueba que la release instalada coincide con su manifiesto. `strict=False` (arranque)
    tolera la caché de bytecode que Python pueda haber dejado; `strict=True` (instalación) la
    considera un archivo sobrante → la release se reconstruye limpia desde el paquete."""
    manifest_path = directory / "release-manifest.json"
    try:
        manifest_bytes = manifest_path.read_bytes()
        manifest = json.loads(manifest_bytes)
    except (OSError, ValueError) as error:
        raise UpdateError(f"la release instalada no tiene un manifiesto válido: {error}") from error
    if release_manifest_sha256 and hashlib.sha256(manifest_bytes).hexdigest() != release_manifest_sha256:
        raise UpdateError("la release instalada no coincide con el manifiesto publicado")
    expected = _validate_release_manifest(manifest, expected_version)
    actual = {
        path.relative_to(directory).as_posix()
        for path in directory.rglob("*")
        if path.is_file() and (strict or not _is_bytecode_artifact(path.relative_to(directory)))
    }
    declared = set(expected) | {"release-manifest.json"}
    if actual != declared:
        missing = sorted(declared - actual)
        unexpected = sorted(actual - declared)

        def summary(label: str, paths: list[str]) -> str | None:
            if not paths:
                return None
            visible = ", ".join(paths[:3])
            suffix = f" (+{len(paths) - 3})" if len(paths) > 3 else ""
            return f"{label}: {visible}{suffix}"

        details = filter(None, (
            summary("faltan", missing),
            summary("sobran", unexpected),
        ))
        raise UpdateError(
            "la release instalada no coincide con su manifiesto (" + "; ".join(details) + ")"
        )
    for relative, item in expected.items():
        path = directory.joinpath(*PurePosixPath(relative).parts)
        if path.stat().st_size != item["size"] or sha256_file(path) != item["sha256"]:
            raise UpdateError(f"la release instalada está corrupta: {relative}")
    return manifest


def _extract_release_to_staging(root: Path, archive: Path, *, version: str,
                                release_manifest_sha256: str) -> tuple[Path, dict]:
    stage_parent = root / "updates" / "staging"
    stage_parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=f"{version}-", dir=stage_parent))
    shutil.rmtree(stage)
    try:
        manifest = extract_verified_archive(
            archive,
            stage,
            version=version,
            release_manifest_sha256=release_manifest_sha256,
        )
        return stage, manifest
    except Exception:
        shutil.rmtree(stage, ignore_errors=True)
        raise


def _remove_path(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


def _replace_release_atomically(final: Path, stage: Path) -> None:
    """Activa una copia verificada y restaura la anterior si el intercambio falla."""
    final.parent.mkdir(parents=True, exist_ok=True)
    if not final.exists():
        os.replace(stage, final)
        return

    backup_parent = final.parents[1] / "updates" / "staging"
    backup = backup_parent / f".{final.name}.{os.getpid()}.replaced"
    if backup.exists():
        _remove_path(backup)
    os.replace(final, backup)
    try:
        os.replace(stage, final)
    except Exception:
        os.replace(backup, final)
        raise
    try:
        _remove_path(backup)
    except OSError:
        pass


def extract_verified_archive(archive: Path, destination: Path, *, version: str,
                             release_manifest_sha256: str) -> dict:
    try:
        bundle = zipfile.ZipFile(archive)
    except (OSError, zipfile.BadZipFile) as error:
        raise UpdateError(f"el paquete no es un ZIP válido: {error}") from error
    with bundle:
        infos = [info for info in bundle.infolist() if not info.is_dir()]
        names: dict[str, zipfile.ZipInfo] = {}
        folded: set[str] = set()
        for info in infos:
            path = str(_safe_relative_path(info.filename))
            key = path.casefold()
            if key in folded:
                raise UpdateError("el ZIP contiene rutas duplicadas para Windows")
            folded.add(key)
            mode = info.external_attr >> 16
            if stat.S_ISLNK(mode):
                raise UpdateError("el ZIP contiene enlaces simbólicos")
            names[path] = info
        manifest_info = names.get("release-manifest.json")
        if not manifest_info or manifest_info.file_size > MAX_MANIFEST_BYTES:
            raise UpdateError("falta release-manifest.json o es demasiado grande")
        manifest_bytes = bundle.read(manifest_info)
        if hashlib.sha256(manifest_bytes).hexdigest() != release_manifest_sha256:
            raise UpdateError("el hash del manifiesto interno no coincide")
        try:
            manifest = json.loads(manifest_bytes)
        except ValueError as error:
            raise UpdateError("release-manifest.json no es JSON válido") from error
        expected = _validate_release_manifest(manifest, version)
        if set(names) != set(expected) | {"release-manifest.json"}:
            raise UpdateError("el ZIP contiene archivos ausentes o no declarados")
        destination.mkdir(parents=True, exist_ok=False)
        try:
            for relative, item in expected.items():
                info = names[relative]
                if info.file_size != item["size"]:
                    raise UpdateError(f"tamaño incorrecto dentro del ZIP: {relative}")
                target = destination.joinpath(*PurePosixPath(relative).parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                digest = hashlib.sha256()
                written = 0
                with bundle.open(info) as source, target.open("wb") as output:
                    while True:
                        block = source.read(1024 * 1024)
                        if not block:
                            break
                        written += len(block)
                        if written > item["size"]:
                            raise UpdateError(f"archivo expandido excede su tamaño: {relative}")
                        digest.update(block)
                        output.write(block)
                    output.flush()
                    os.fsync(output.fileno())
                if written != item["size"] or digest.hexdigest() != item["sha256"]:
                    raise UpdateError(f"falló la verificación de {relative}")
            (destination / "release-manifest.json").write_bytes(manifest_bytes)
        except Exception:
            shutil.rmtree(destination, ignore_errors=True)
            raise
    return manifest


@contextlib.contextmanager
def update_lock(root: Path) -> Iterator[None]:
    lock_path = root / "state" / "update.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    stream = lock_path.open("a+b")
    try:
        if os.name == "nt":
            import msvcrt
            stream.seek(0, os.SEEK_END)
            if stream.tell() == 0:
                stream.write(b"0")
                stream.flush()
            stream.seek(0)
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as error:
                raise UpdateError("otra actualización ya está en curso") from error
        else:
            import fcntl
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as error:
                raise UpdateError("otra actualización ya está en curso") from error
        yield
    finally:
        try:
            if os.name == "nt":
                import msvcrt
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        stream.close()


def _runtime_python(runtime_dir: Path) -> Path:
    return runtime_dir / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def ensure_runtime(root: Path, manifest: dict, release_dir: Path,
                   progress: Progress | None = None) -> Path:
    runtime = manifest["runtime"]
    runtime_id = runtime["id"]
    final = root / "runtimes" / runtime_id
    metadata = release_state.read_json(final / "runtime.json", {})
    if _runtime_python(final).is_file() and metadata.get("id") == runtime_id:
        return final
    if os.name != "nt":
        raise UpdateError("la creación automática del runtime sólo está habilitada en Windows")
    uv = root / "shared" / "tools" / "uv.exe"
    if not uv.is_file():
        raise UpdateError("falta shared/tools/uv.exe; ejecute setup-windows.bat")
    requirements = release_dir / runtime.get("requirements_file", "requirements-windows.txt")
    if not requirements.is_file() or sha256_file(requirements) != runtime.get("requirements_sha256"):
        raise UpdateError("requirements-windows.txt no coincide con el manifiesto")
    final.parent.mkdir(parents=True, exist_ok=True)
    staging = final.parent / f".{runtime_id}.{os.getpid()}.tmp"
    shutil.rmtree(staging, ignore_errors=True)
    env = dict(os.environ)
    env.setdefault("UV_CACHE_DIR", str(root / "shared" / "cache" / "uv"))

    def run(command: list[str], label: str) -> None:
        _progress(progress, label)
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW") else 0
        process = subprocess.Popen(command, cwd=release_dir, env=env, creationflags=flags)
        while process.poll() is None:
            time.sleep(0.25)
            _progress(progress, label)
        if process.returncode:
            raise UpdateError(f"falló {label} (código {process.returncode})")

    try:
        run([str(uv), "venv", "--python", str(runtime.get("python", "3.13")), str(staging)],
            "Creando runtime nuevo")
        python = _runtime_python(staging)
        torch = runtime.get("torch") or {}
        run([
            str(uv), "pip", "install", "--python", str(python),
            f"torch=={torch['version']}", f"torchaudio=={torch['version']}",
            "--index-url", str(torch["index_url"]),
        ], "Instalando Torch del runtime")
        run([str(uv), "pip", "install", "--python", str(python), "-r", str(requirements)],
            "Instalando dependencias del runtime")
        run([str(uv), "pip", "install", "--python", str(python), "--no-deps", "emotiefflib"],
            "Instalando EmotiEffLib")
        release_state.write_json_atomic(staging / "runtime.json", {
            "schema": 1, "id": runtime_id, "created_at": release_state.utc_now(),
        })
        try:
            os.replace(staging, final)
        except FileExistsError:
            shutil.rmtree(staging, ignore_errors=True)
        if not _runtime_python(final).is_file():
            raise UpdateError("el runtime terminó sin un intérprete utilizable")
        return final
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def install_bundle(root: Path, update_manifest: dict, archive: Path, *,
                   prepare_runtime: bool = True, progress: Progress | None = None) -> dict:
    version = normalized_version(str(update_manifest.get("version") or ""))
    asset = update_manifest.get("asset") or {}
    expected_size = asset.get("size")
    if archive.stat().st_size != expected_size or sha256_file(archive) != asset.get("sha256"):
        raise UpdateError("el paquete descargado no coincide con su manifiesto")
    internal_hash = str(update_manifest.get("release_manifest_sha256") or "").lower()
    if not re.fullmatch(r"[0-9a-f]{64}", internal_hash):
        raise UpdateError("hash de release-manifest inválido")
    root = root.resolve()
    with update_lock(root):
        final = root / "releases" / version
        manifest = None
        if final.exists():
            try:
                manifest = verify_installed_release(final, version, internal_hash, strict=True)
            except UpdateError:
                _progress(progress, "Reparando archivos de la release")
        if manifest is None:
            _progress(progress, "Verificando archivos de la release")
            stage = None
            try:
                stage, manifest = _extract_release_to_staging(
                    root,
                    archive,
                    version=version,
                    release_manifest_sha256=internal_hash,
                )
                _replace_release_atomically(final, stage)
            except Exception:
                if stage is not None:
                    shutil.rmtree(stage, ignore_errors=True)
                raise
        runtime_dir = None
        if prepare_runtime:
            runtime_dir = ensure_runtime(root, manifest, final, progress)
        runtime_id = manifest["runtime"]["id"]
        state = release_state.activate(root, version, runtime_id)
        _progress(progress, f"Release {version} lista")
        return {"version": version, "release_dir": final, "runtime_dir": runtime_dir, "state": state}


def download_and_install(root: Path, client: GitHubClient, candidate: UpdateCandidate,
                         progress: Progress | None = None) -> dict:
    updates = root / "updates" / "downloads"
    updates.mkdir(parents=True, exist_ok=True)
    final_archive = updates / candidate.update_manifest["asset"]["name"]
    partial = final_archive.with_suffix(final_archive.suffix + ".part")
    partial.unlink(missing_ok=True)
    client.download_asset(
        candidate.archive_asset, partial,
        expected_size=candidate.update_manifest["asset"]["size"], progress=progress,
    )
    expected_hash = candidate.update_manifest["asset"]["sha256"]
    if sha256_file(partial) != expected_hash:
        partial.unlink(missing_ok=True)
        raise UpdateError("el SHA-256 de la descarga no coincide")
    os.replace(partial, final_archive)
    return install_bundle(root, candidate.update_manifest, final_archive, progress=progress)


def configured_repository(root: Path, source_dir: Path | None = None) -> str:
    return load_update_config(root, source_dir)["repository"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Actualizador seguro de Transcriptor")
    subparsers = parser.add_subparsers(dest="command", required=True)
    install = subparsers.add_parser("install-bundle", help="instala un bundle local verificado")
    install.add_argument("--root", type=Path, required=True)
    install.add_argument("--manifest", type=Path, required=True)
    install.add_argument("--archive", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == "install-bundle":
        try:
            manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
            install_bundle(
                args.root, manifest, args.archive,
                progress=lambda message, _done, _total: print(f"[setup] {message}", flush=True),
            )
        except (OSError, ValueError, UpdateError) as error:
            print(f"[ERROR] {error}", file=sys.stderr)
            return 2
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
