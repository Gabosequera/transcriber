#!/usr/bin/env bash
set -euo pipefail

project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
capture_dir="$project_dir/capturas"
mkdir -p "$capture_dir"

xvfb-run -a -s "-screen 0 1400x900x24" bash -c '
    set -euo pipefail
    project_dir="$1"
    capture_dir="$2"
    cd "$project_dir"

    .venv/bin/python app.py >"$capture_dir/ui-app.log" 2>&1 &
    app_pid=$!

    cleanup() {
        kill "$app_pid" 2>/dev/null || true
        wait "$app_pid" 2>/dev/null || true
    }
    trap cleanup EXIT

    window_ready=false
    for _attempt in $(seq 1 60); do
        if xwininfo -root -tree 2>/dev/null | grep -q '"Transcriptor"'; then
            window_ready=true
            break
        fi
        sleep 0.25
    done

    if [[ "$window_ready" != true ]]; then
        echo "La ventana no apareció. Revisa $capture_dir/ui-app.log" >&2
        exit 1
    fi

    # La ventana se registra antes de que terminen de construirse las vistas manuales y
    # el editor automático. Esperar unos segundos evita capturar solo el header.
    sleep 5
    import -window root "$capture_dir/ui-latest.png"
' bash "$project_dir" "$capture_dir"

printf 'Captura guardada en: %s\n' "$capture_dir/ui-latest.png"
