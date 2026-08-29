"""Rutas estables de la aplicación.

En desarrollo, los archivos persistentes siguen junto al código para no alterar el
flujo actual. Una instalación administrada define ``TRANSCRIPTOR_ROOT`` y separa
estrictamente releases inmutables de datos, modelos, cachés y herramientas.
"""
from __future__ import annotations

import os
from pathlib import Path


SOURCE_DIR = Path(__file__).resolve().parent
_ROOT_ENV = os.environ.get("TRANSCRIPTOR_ROOT", "").strip()
MANAGED_INSTALL = bool(_ROOT_ENV)
INSTALL_ROOT = Path(_ROOT_ENV).expanduser().resolve() if MANAGED_INSTALL else SOURCE_DIR

SHARED_DIR = Path(
    os.environ.get("TRANSCRIPTOR_SHARED_DIR", str(INSTALL_ROOT / "shared"))
).expanduser().resolve() if MANAGED_INSTALL else SOURCE_DIR

STATE_DIR = INSTALL_ROOT / "state" if MANAGED_INSTALL else SOURCE_DIR / ".state"
CONFIG_DIR = SHARED_DIR / "config" if MANAGED_INSTALL else SOURCE_DIR
LOGS_DIR = SHARED_DIR / "logs" if MANAGED_INSTALL else SOURCE_DIR / "logs"
CACHE_DIR = SHARED_DIR / "cache" if MANAGED_INSTALL else Path(
    os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache"))
) / "transcriptor"
MODELS_DIR = SHARED_DIR / "models" if MANAGED_INSTALL else SOURCE_DIR / "models"
TOOLS_DIR = SHARED_DIR / "tools" if MANAGED_INSTALL else SOURCE_DIR / "tools"
LLAMA_DIR = SHARED_DIR / "llama" if MANAGED_INSTALL else SOURCE_DIR / "llama"
COMPONENTS_DIR = SHARED_DIR / "components" if MANAGED_INSTALL else SOURCE_DIR
MEDIA_DIR = SHARED_DIR / "media" if MANAGED_INSTALL else SOURCE_DIR / "media"
MARKS_STORE_DIR = SHARED_DIR / "marcas_store" if MANAGED_INSTALL else SOURCE_DIR / "marcas_store"

CONFIG_FILE = CONFIG_DIR / "config.json"
PRESETS_FILE = CONFIG_DIR / "presets.json"
WIZARD_SOURCES_FILE = CONFIG_DIR / "wizard_fuentes.json"
MARK_SOURCES_FILE = CONFIG_DIR / "marcar_fuentes.json"
PIPELINE_HISTORY_FILE = CONFIG_DIR / "pipeline.history.json"

LAUGHTER_REPO = COMPONENTS_DIR / "LaughterSegmentation"
BREATH_REPO = COMPONENTS_DIR / "Respiro-en"


def configure_cache_environment() -> None:
    """Fija cachés persistentes antes de importar librerías de modelos.

    ``setdefault`` respeta overrides explícitos del usuario o del administrador.
    En desarrollo no se redirigen los cachés históricos del perfil del usuario.
    """
    if not MANAGED_INSTALL:
        return
    os.environ.setdefault("XDG_CACHE_HOME", str(CACHE_DIR))
    os.environ.setdefault("HF_HOME", str(CACHE_DIR / "huggingface"))
    os.environ.setdefault("HF_HUB_CACHE", str(CACHE_DIR / "huggingface" / "hub"))
    os.environ.setdefault("TORCH_HOME", str(CACHE_DIR / "torch"))
    os.environ.setdefault("UV_CACHE_DIR", str(CACHE_DIR / "uv"))


def ensure_persistent_dirs() -> None:
    """Crea únicamente directorios de estado; nunca toca una release."""
    for directory in (
        STATE_DIR, CONFIG_DIR, LOGS_DIR, CACHE_DIR, MODELS_DIR, TOOLS_DIR,
        LLAMA_DIR, COMPONENTS_DIR, MEDIA_DIR, MARKS_STORE_DIR,
    ):
        directory.mkdir(parents=True, exist_ok=True)


configure_cache_environment()
