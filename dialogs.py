"""
dialogs.py — Selectores de archivo NATIVOS del escritorio.

En KDE usa `kdialog`, en GNOME/otros `zenity` (ambos traen barra lateral con
ubicaciones ancladas, Recientes y barra de dirección editable). Si no hay
ninguno, cae al diálogo básico de Tk.

filters: lista de (nombre, [patrones]) → p.ej. [("Audio", ["*.wav","*.mp3"]), ("Todos", ["*"])]
`remember` es una clave corta: en KDE hace que reabra en la última carpeta usada
para ese propósito (comportamiento "recientes"). `start` (ruta real) lo sobreescribe.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

_DESKTOP = os.environ.get("XDG_CURRENT_DESKTOP", "").lower()


def _has(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def _backend() -> str:
    if "kde" in _DESKTOP and _has("kdialog"):
        return "kdialog"
    if _has("zenity"):
        return "zenity"
    if _has("kdialog"):
        return "kdialog"
    return "tk"


def _run(cmd) -> str | None:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            return None
        out = r.stdout.strip()
        return out or None
    except Exception:
        return None


def _kfilter(filters) -> str:
    return "\n".join(f"{' '.join(pats)}|{name}" for name, pats in filters)


def _zfilters(filters) -> list[str]:
    return [f"--file-filter={name} | {' '.join(pats)}" for name, pats in filters]


def _start(start, remember) -> str:
    """startDir para kdialog: ruta real, o token :remember (recuerda la última)."""
    if start:
        return start
    return f":transcriptor_{remember}" if remember else ""


# ------------------------------------------------------------------- públicas --
def open_file(title, filters, start=None, remember=None) -> str | None:
    b = _backend()
    if b == "kdialog":
        return _run(["kdialog", "--title", title, "--getopenfilename",
                     _start(start, remember), _kfilter(filters)])
    if b == "zenity":
        cmd = ["zenity", "--file-selection", f"--title={title}"] + _zfilters(filters)
        if start:
            cmd.append(f"--filename={start}")
        return _run(cmd)
    return _tk("open", title, filters, start)


def save_file(title, default=None, filters=None, start=None, remember=None) -> str | None:
    filters = filters or [("Todos", ["*"])]
    b = _backend()
    if b == "kdialog":
        sd = start or default or _start(None, remember)
        return _run(["kdialog", "--title", title, "--getsavefilename", sd, _kfilter(filters)])
    if b == "zenity":
        cmd = ["zenity", "--file-selection", "--save", "--confirm-overwrite",
               f"--title={title}"] + _zfilters(filters)
        if default:
            cmd.append(f"--filename={default}")
        return _run(cmd)
    return _tk("save", title, filters, default)


def open_dir(title, start=None, remember=None) -> str | None:
    b = _backend()
    if b == "kdialog":
        return _run(["kdialog", "--title", title, "--getexistingdirectory",
                     _start(start, remember)])
    if b == "zenity":
        cmd = ["zenity", "--file-selection", "--directory", f"--title={title}"]
        if start:
            cmd.append(f"--filename={start}")
        return _run(cmd)
    return _tk("dir", title, None, start)


# --------------------------------------------------------------- fallback Tk --
def _tk(kind, title, filters, hint):
    from tkinter import filedialog
    ftypes = [(n, " ".join(p)) for n, p in filters] if filters else None
    if kind == "dir":
        idir = hint if hint and Path(hint).is_dir() else None
        return filedialog.askdirectory(title=title, initialdir=idir) or None
    if kind == "save":
        kw = {}
        if hint:
            p = Path(hint); kw = {"initialdir": str(p.parent), "initialfile": p.name}
        return filedialog.asksaveasfilename(title=title, filetypes=ftypes, **kw) or None
    idir = hint if hint and Path(hint).is_dir() else (str(Path(hint).parent) if hint else None)
    return filedialog.askopenfilename(title=title, filetypes=ftypes, initialdir=idir) or None
