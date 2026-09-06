"""Smoke de Tk real con medio sintético; ejecutar fuera de unittest (requiere display)."""
from pathlib import Path
import argparse
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))

import editorial_io
import editorial_master
import medios
from test_projects import fixture


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--hold", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1] / "media" / "smoke-modular"
    root.mkdir(parents=True, exist_ok=True)
    source = root / "padre.mkv"
    if not source.exists():
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
            "testsrc2=size=320x180:rate=25:duration=12", "-f", "lavfi", "-i",
            "sine=frequency=440:duration=12", "-map", "0:v", "-map", "1:a", "-map", "1:a",
            "-c:v", "libx264", "-c:a", "pcm_s16le", str(source)], check=True, **medios.flags_subprocess())
    data = fixture()
    data["media"].update(path=str(source), t0=0, fingerprint=medios.fingerprint(source))
    project = editorial_master.write_package(root / "padre" / "editorial", data)["master"]
    from app import App
    app = App()
    app.title("Transcriptor — smoke modular")
    failures = []
    app.report_callback_exception = lambda *error: failures.append(str(error))
    workspace = app.automatico
    workspace.editor.cargar(str(source))

    def spin(predicate, timeout=20):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            app.update()
            if failures:
                raise AssertionError(failures)
            if predicate():
                return
            time.sleep(.02)
        raise AssertionError("timeout en smoke de UI")

    try:
        spin(lambda: workspace.info is not None and len(workspace.track_widgets) == 2)
        if not workspace.result:
            workspace.events.put({"tipo": "project_loaded", "plan": None,
                "tracks": list(data["tracks"].values()),
                "result": {"master": str(project), "source": str(source), "chunk_planner": "external"}})
        spin(lambda: workspace.trims is not None)
        workspace.editor._set_playhead(8)
        workspace.editor._zoom(2)
        app.update()
        assert workspace.editor.t_play == 8
        assert workspace.view_button.cget("state") == "normal"
        assert "torch" not in sys.modules and "transformers" not in sys.modules
        print("SMOKE UI OK: App real, importación, metadata, carriles, salto y zoom; sin modelos.", flush=True)
        if args.hold:
            app.mainloop()
    finally:
        workspace.cerrar()
        app.destroy()


if __name__ == "__main__":
    main()
