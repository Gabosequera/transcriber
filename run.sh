#!/usr/bin/env bash
# Lanza la app dejando las libs CUDA (cuBLAS/cuDNN) del venv en el path.
set -e
cd "$(dirname "$0")"

if [ ! -x ".venv/bin/python" ]; then
  echo "No hay venv. Crealo con:  python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"
  exit 1
fi

SITE="$(.venv/bin/python -c 'import site; print(site.getsitepackages()[0])')"
export LD_LIBRARY_PATH="$SITE/nvidia/cublas/lib:$SITE/nvidia/cudnn/lib:${LD_LIBRARY_PATH:-}"
exec .venv/bin/python app.py "$@"
