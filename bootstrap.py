"""Bootstrap estable: localiza el launcher de la release activa."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def main() -> int:
    root = Path(__file__).resolve().parent
    state_file = root / "state" / "current.json"
    try:
        state = json.loads(state_file.read_text(encoding="utf-8"))
        version = str(state["current"])
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f"Instalación incompleta: no se pudo leer {state_file}: {error}", file=sys.stderr)
        return 2
    launcher = root / "releases" / version / "launcher.py"
    if not launcher.is_file():
        print(f"Instalación incompleta: falta {launcher}", file=sys.stderr)
        return 2
    env = dict(os.environ)
    env["TRANSCRIPTOR_ROOT"] = str(root)
    # No escribir bytecode dentro de la release: su contenido se verifica contra el manifiesto.
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.call([sys.executable, "-B", str(launcher)], cwd=launcher.parent, env=env)


if __name__ == "__main__":
    raise SystemExit(main())
