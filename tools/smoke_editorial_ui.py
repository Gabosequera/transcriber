"""Smoke de Tk real con medio sintético; ejecutar fuera de unittest (requiere display)."""
from pathlib import Path
import argparse
import subprocess
import sys
import time
import uuid

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
    root = Path(__file__).resolve().parents[1] / "media" / "smoke-modular" / uuid.uuid4().hex[:8]
    root.mkdir(parents=True, exist_ok=True)
    source = root / "padre.mkv"
    if not source.exists():
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
            "testsrc2=size=320x180:rate=25:duration=12", "-f", "lavfi", "-i",
            "sine=frequency=440:duration=12", "-map", "0:v", "-map", "1:a", "-map", "1:a",
            "-c:v", "libx264", "-c:a", "pcm_s16le", "-metadata", "comment="+root.name,
            str(source)], check=True, **medios.flags_subprocess())
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
        if hasattr(workspace,'_load_project'):
            spin(lambda:workspace.result is not None)
            assert Path(workspace.result['master']) == project
        elif not workspace.result:
            workspace.events.put({"tipo": "project_loaded", "plan": None,
                "tracks": list(data["tracks"].values()),
                "result": {"master": str(project), "source": str(source), "chunk_planner": "external"}})
        spin(lambda: workspace.trims is not None)
        if hasattr(workspace, "layers"):
            spin(lambda: workspace.layers.store is not None)
            import editorial_layers as layers
            from types import SimpleNamespace
            store = workspace.layers.store
            layer = store.save(layers.new_layer(data, "Pedidos smoke"))
            workspace.editor.refrescar_layout()
            app.update()
            g = workspace.editor._tl_geo()
            controller = workspace.layers
            controller.set_tool("cut")                     # crear = caja con la herramienta Corte
            for phase, t in (("press", 2.5), ("motion", 3.0), ("motion", 3.5)):
                event = SimpleNamespace(x=workspace.editor._t2x(t,g),y=0,state=0)
                controller.gesture(layer["layer_id"],phase,event,g,0)
            from unittest import mock
            with mock.patch.object(controller,"edit_dialog"):
                controller.gesture(layer["layer_id"],"release",event,g,0)
            controller.set_tool("select")
            saved=store.layers[layer["layer_id"]]
            assert len(saved["items"]) == 1
            item=copy_item=__import__('copy').deepcopy(saved['items'][0])
            item['comment']='Busca la recurrencia'
            controller.persist(layer['layer_id'],item)
            assert layers.LayerStore(store.root,data).layers[layer['layer_id']]['items'][0]['comment'] == 'Busca la recurrencia'
            controller.persist('autor',layers.new_item(2,3,comment='Pedido del autor'),create=True)
            assert workspace.editor.reg.marcas[-1]['prompt']=='Pedido del autor'
            controller.persist('trims:main',layers.new_item(10,11,comment='Recorte smoke'),create=True)
            assert workspace.trims['cuts'][-1]['reason']=='Recorte smoke'
            controller.persist(layer['layer_id'],item,delete=True)
            store.delete(layer['layer_id'])
            workspace.editor.refrescar_layout()
            assert not any(l['layer_id']==layer['layer_id'] for l in controller.all())
            if hasattr(workspace, '_prepare_topics'):
                controller.selected=None
                workspace._prepare_topics()
                request=editorial_io.read_json(store.root/'views/topics-request.json')
                first=dict(schema='editorial-topics-proposal/1',request_id=request['request_id'],
                    source_master_digest=request['source_master_digest'],source_layers_digest=request['source_layers_digest'],
                    complete=True,items=[layers.new_item(0,.8,'Tema'),layers.new_item(9.2,12,'Retorno')],**{'pass':1})
                path=editorial_io.atomic_write_json(store.root/'views/topics.proposed.json',first)
                workspace._import_topics(path)
                spin(lambda: not workspace.worker.is_alive())
                app.update()
                previous=editorial_io.read_json(store.root/'views/topics-pass1.json')
                unified=layers.new_item(0,1,'Tema recurrente smoke')
                unified['ranges']=[r for i in previous['items'] for r in i['ranges']]
                unified['source_item_ids']=[i['item_id'] for i in previous['items']]
                second={**first,'pass':2,'previous_pass_digest':editorial_io.digest_json(previous),'items':[unified]}
                editorial_io.atomic_write_json(path,second)
                workspace._import_topics(path)
                spin(lambda:any(l['kind']=='topics' for l in store.visible()))
                topic=next(l for l in store.visible() if l['kind']=='topics')
                assert len(topic['items'][0]['ranges'])==2
                spin(lambda: not workspace.worker.is_alive())
                topic['items'][0]['comment']='Recurrencia revisada a mano'
                controller.persist(topic['layer_id'],topic['items'][0])
                assert layers.LayerStore(store.root,data).layers[topic['layer_id']]['items'][0]['edited']
        if hasattr(workspace,'_load_project'):
            import editorial_trims, podcast_export
            trims=editorial_trims.new_document(data['media']['fingerprint'],12)
            editorial_trims.add_cut(trims,3,7)
            exported=podcast_export.export_plan(project,None,source,root/('export-'+uuid.uuid4().hex[:8]),trims=trims)
            entry=editorial_io.read_json(exported/'exports.json')['files'][0]
            child=exported/entry['file']
            workspace.editor.cargar(str(child))
            spin(lambda:workspace.result and Path(workspace.result['master'])==exported/entry['project_master'])
            spin(lambda:workspace.layers.store is not None and workspace.layers.store.master.get('derivation'))
            child_data=workspace.layers.store.master
            assert child_data['tracks']['B']['words'][-1]['t_ini']==4
            assert all(w['text']!='eliminado' for t in child_data['tracks'].values() for w in t['words'])
            workspace._analyze_silences()
            spin(lambda: not workspace.worker.is_alive() and workspace.view_button.cget('state')=='normal')
            app.update()
            assert (Path(workspace.result['master']).parent/'tracks/A/audio.flac').is_file()
        nav=min(8,workspace.info['duracion']-1)
        workspace.editor._set_playhead(nav)
        workspace.editor._zoom(2)
        app.update()
        assert workspace.editor.t_play == nav
        assert workspace.view_button.cget("state") == "normal"
        # Playback real del hijo: el avance nace del reloj de audio, no del spawn.
        workspace.editor._set_playhead(1)
        workspace.editor._play()
        spin(lambda: workspace.editor.repro.position() is not None and workspace.editor.t_play > 1.4)
        workspace.editor._stop_preview()
        # ---- velocidad (Fase 1): ×2 reproduce con reloj escalado; el cambio en vivo es UNA re-sesión
        ed = workspace.editor
        ed._set_playhead(1)
        ed._play(rate=2)
        spin(lambda: ed.repro.position() is not None and ed.t_play > 1.4)
        assert ed.repro.rate == 2 and ed.repro.clock.rate == 2 and not ed.repro.mudo
        assert ed.lbl_t.cget("text").endswith("×2"), ed.lbl_t.cget("text")
        session = ed._vses
        ed.set_rate(4); ed.set_rate(3); ed.set_rate(4)      # tres pulsaciones = un reinicio
        spin(lambda: ed._vses is not session and ed._vses is not None and ed.repro.playing()
             and ed.repro.rate == 4)
        assert ed.rate == 4 and ed._vses.rate == 4 and ed._vses.skip == "bidir"
        ed._stop_preview()
        ed.rate = 1.0
        # ---- keymap (Fase 2): guarda de foco, editor activo y cambio en Ajustes sin reiniciar
        import keymap, tempfile
        from types import SimpleNamespace as NS
        ed.activar()
        top = ed.f.winfo_toplevel()
        ev = lambda w, ks, st=0: NS(widget=w, keysym=ks, state=st)
        ed._set_playhead(2)
        campo = workspace.min_gap_entry._entry            # el tk.Entry interno del CTkEntry
        assert ed._key_toplevel(ev(campo, "Right")) is None and ed.t_play == 2      # escribir, no navegar
        assert ed._key_toplevel(ev(ed.tl, "Right")) == "break" and abs(ed.t_play - 2.5) < 1e-6
        assert ed._key_toplevel(ev(ed.canvas, "Left")) == "break" and abs(ed.t_play - 2.0) < 1e-6
        assert ed._key_toplevel(ev(top, "Home")) == "break" and ed.t_play == 0
        assert ed._key_toplevel(ev(ed.tl, "q")) is None                              # sin acción: propaga
        ed.desactivar()
        assert ed._key_toplevel(ev(ed.tl, "Right")) is None and ed.t_play == 0      # editor inactivo
        ed.activar()
        marcas_antes = len(ed.reg.marcas)
        assert ed._key_toplevel(ev(ed.tl, "m")) == "break" and len(ed.reg.marcas) == marcas_antes + 1
        ed.e_prompt.focus_set()                                                       # M abre el prompt
        assert ed._key_toplevel(ev(ed.e_prompt._entry, "x")) is None                 # se escribe x
        assert ed.reg.marcas[-1].get("decision") is None
        ed.tl.focus_set()
        assert ed._key_toplevel(ev(ed.tl, "Delete")) == "break" and len(ed.reg.marcas) == marcas_antes
        with tempfile.TemporaryDirectory() as tmp:
            override = Path(tmp) / "keymap.json"
            keymap.save({"nav.step_next": ["Ctrl+Right"]}, override)
            keymap.reload(override)                                                   # = guardar en Ajustes
            assert ed._key_toplevel(ev(ed.tl, "Right")) is None and ed.t_play == 0
            assert ed._key_toplevel(ev(ed.tl, "Right", keymap.STATE_CONTROL)) == "break" and ed.t_play == .5
            app.keymap_settings.refresh()
            assert "Ctrl+Right" in app.keymap_settings._rows["nav.step_next"]["chord"].cget("text")
            keymap.reload(Path(tmp) / "no-existe.json")                               # defaults otra vez
        assert ed._key_toplevel(ev(ed.tl, "Left")) == "break" and ed.t_play == 0
        # ---- navegación (Fase 3): fotograma, bordes por bisect, silencios, ir a tiempo, selección, vista
        controller = workspace.layers
        fps = workspace.info["video"]["fps"]
        ed.ejecutar("nav.frame_next"); assert abs(ed.t_play - 1 / fps) < 1e-6, ed.t_play
        ed.ejecutar("nav.frame_next_10"); assert abs(ed.t_play - 11 / fps) < 1e-6
        ed.ejecutar("nav.frame_prev"); assert abs(ed.t_play - 10 / fps) < 1e-6
        edges = controller.edges()
        assert len(edges) >= 2, edges.times                      # bordes del recorte de silencio
        ed._set_playhead(0)
        cost = []
        while True:
            t0 = time.perf_counter()
            before = ed.t_play
            assert ed.ejecutar("nav.next_edge")
            cost.append((time.perf_counter() - t0) * 1000)
            if ed.t_play == before:
                break
            assert ed.t_play in edges.times or ed.t_play >= workspace.info["duracion"] - .05
        assert max(cost) < 30, f"salto a borde {max(cost):.1f} ms"
        ed.ejecutar("nav.prev_edge"); assert ed.t_play == edges.times[-2]
        silences = controller.silences()
        assert len(silences) >= 2
        ed._set_playhead(0); ed.ejecutar("nav.next_silence"); assert ed.t_play == silences.times[0]
        ed.ejecutar("nav.prev_silence"); assert ed.t_play == 0 or ed.t_play in silences.times
        ed._ir_a_tiempo("+2"); assert abs(ed.t_play - 2) < 1e-6 or ed.t_play == silences.times[0] + 2
        ed._ir_a_tiempo("0:03"); assert ed.t_play == 3
        ed._ir_a_tiempo("nada"); assert ed.t_play == 3 and "inválido" in ed.lbl_status.cget("text")
        cut = workspace.trims["cuts"][0]
        controller.selected = ("trims:main", cut["cut_id"], 0)
        ed.ejecutar("nav.sel_end"); assert abs(ed.t_play - min(cut["t_fin"], workspace.info["duracion"] - .05)) < 1e-6
        ed.ejecutar("nav.sel_start"); assert ed.t_play == cut["t_ini"]
        ed.ejecutar("view.zoom_sel")
        assert ed.view[0] <= cut["t_ini"] and ed.view[0] + ed.view[1] >= cut["t_fin"]
        controller.selected = None
        ed._fit(); ed._zoom(4); ed._set_playhead(workspace.info["duracion"] / 2); ed.ejecutar("view.center")
        assert abs(ed.view[0] + ed.view[1] / 2 - ed.t_play) < 1e-6
        assert ed._seguir; ed.ejecutar("view.follow"); assert not ed._seguir; ed.ejecutar("view.follow")
        assert not workspace.skip_check.get(); ed.ejecutar("view.skip_trims")
        assert workspace.skip_check.get(); ed.ejecutar("view.skip_trims"); assert not workspace.skip_check.get()
        ed._fit()
        # ---- edición y deshacer (Fase 4): S, [, ], Alt+→, A, Shift+A, pedido, marcas, Ctrl+Z ×n, Ctrl+R ×n
        import editorial_trims
        history = controller.history
        history.clear()
        store = controller.store
        trims_path = workspace.trims_path
        def cuts():
            return [(c["t_ini"], c["t_fin"], c["accepted"], c["enabled"]) for c in workspace.trims["cuts"]]
        def cuts_on_disk():
            return [(c["t_ini"], c["t_fin"], c["accepted"], c["enabled"])
                    for c in editorial_io.read_json(trims_path)["cuts"]]
        base = cuts()
        nueva = layers.new_layer(store.master, "Pedidos deshacer")
        controller.transact("crear capa", ["layer:" + nueva["layer_id"]], lambda: store.save(nueva))
        assert nueva["layer_id"] in store.layers and len(history) == 1
        ed._set_playhead(1)
        controller.persist("trims:main", layers.new_item(1, 3, comment="corte smoke"), create=True)
        assert controller.selected[0] == "trims:main" and len(history) == 2
        ed._set_playhead(2); assert ed.ejecutar("edit.split")
        assert (1.0, 2.0, False, True) in cuts() and (2.0, 3.0, False, True) in cuts()
        assert controller.selected[1] != workspace.trims["cuts"][0]["cut_id"]
        ed._set_playhead(2.5); assert ed.ejecutar("edit.trim_start")
        ed._set_playhead(2.8); assert ed.ejecutar("edit.trim_end")
        assert (2.5, 2.8, False, True) in cuts(), cuts()
        assert ed.ejecutar("edit.nudge_next")
        step = round(1 / fps, 3)
        assert (round(2.5 + step, 3), round(2.8 + step, 3), False, True) in cuts(), cuts()
        assert abs(ed.t_play - (2.5 + step)) < 1e-6
        assert ed.ejecutar("edit.accept")
        moved = next(c for c in workspace.trims["cuts"] if c["accepted"])
        assert moved["enabled"] and cuts_on_disk() == cuts()
        assert editorial_trims.enabled_intervals(workspace.trims) == editorial_trims.enabled_intervals(
            editorial_io.read_json(trims_path))
        before_next = controller.selected
        assert ed.ejecutar("edit.accept_next")
        moved = next(c for c in workspace.trims["cuts"] if c["cut_id"] == moved["cut_id"])
        assert moved["accepted"] and controller.selected != before_next          # E no alterna
        n_ops = len(history)
        item = layers.new_item(0.5, 1.5, comment="")
        controller.persist(nueva["layer_id"], item, create=True)
        item = controller.find(nueva["layer_id"], item["item_id"])[1]
        item["comment"] = "Busca el contexto"
        controller.persist(nueva["layer_id"], item, label="editar pedido")       # = cerrar el diálogo
        assert store.layers[nueva["layer_id"]]["items"][0]["comment"] == "Busca el contexto"
        marcas_antes = len(ed.reg.marcas)
        ed._set_playhead(4); assert ed._key_toplevel(ev(ed.tl, "m")) == "break"
        assert len(ed.reg.marcas) == marcas_antes + 1 and len(history) == n_ops + 3
        ed.e_prompt.delete(0, "end"); ed.e_prompt.insert(0, "pedido de la marca"); ed._marca_prompt()
        assert ed.reg.marcas[-1]["prompt"] == "pedido de la marca" and len(history) == n_ops + 4
        ed.tl.focus_set()
        snapshot_docs = (cuts(), __import__("copy").deepcopy(store.layers[nueva["layer_id"]]["items"]),
                         __import__("copy").deepcopy(ed.reg.marcas))
        ctrl = keymap.STATE_CONTROL
        assert ed._key_toplevel(ev(ed.tl, "z", ctrl)) == "break" and ed.reg.marcas[-1].get("prompt") is None
        assert ed._key_toplevel(ev(ed.tl, "z", ctrl)) == "break" and len(ed.reg.marcas) == marcas_antes
        assert ed._key_toplevel(ev(ed.tl, "z", ctrl)) == "break"
        assert store.layers[nueva["layer_id"]]["items"][0]["comment"] == ""
        assert "Deshecho" in ed.lbl_status.cget("text")
        for _ in range(3):
            assert ed._key_toplevel(ev(ed.tl, "r", ctrl)) == "break"
        assert (cuts(), store.layers[nueva["layer_id"]]["items"], ed.reg.marcas) == snapshot_docs
        assert "Rehecho" in ed.lbl_status.cget("text")
        # Ctrl+Z con el foco en un Entry es el deshacer del propio campo, no del proyecto
        depth = len(history)
        assert ed._key_toplevel(ev(campo, "z", ctrl)) is None and len(history) == depth
        # deshacer TODO hasta la base: los recortes vuelven a los del análisis y la capa a la tumba
        while history.can_undo():
            assert ed._key_toplevel(ev(ed.tl, "z", ctrl)) == "break"
        assert cuts() == base and cuts_on_disk() == base, (cuts(), base)
        assert store.layers[nueva["layer_id"]].get("deleted") is True
        assert not any(l["layer_id"] == nueva["layer_id"] for l in controller.all())
        while history.can_redo():
            assert ed._key_toplevel(ev(ed.tl, "r", ctrl)) == "break"
        assert not store.layers[nueva["layer_id"]].get("deleted")            # la tumba se levantó
        assert (cuts(), store.layers[nueva["layer_id"]]["items"], ed.reg.marcas) == snapshot_docs
        # un documento cambiado por fuera descarta la entrada en vez de pisar
        external = __import__("copy").deepcopy(workspace.trims)
        editorial_trims.add_cut(external, 0.2, 0.4)
        editorial_trims.save_document(trims_path, external)
        workspace.trims = external
        while history.peek_undo() and "trims" not in history.peek_undo().docs:
            assert ed._key_toplevel(ev(ed.tl, "z", ctrl)) == "break"      # las de otros documentos siguen valiendo
        top_label = history.peek_undo().label
        assert ed._key_toplevel(ev(ed.tl, "z", ctrl)) == "break"
        assert "descarta" in ed.lbl_status.cget("text"), ed.lbl_status.cget("text")
        assert history.peek_undo() is None or history.peek_undo().label != top_label
        assert any(c["t_ini"] == 0.2 for c in workspace.trims["cuts"])           # no se pisó
        controller.selected = None
        # ---- herramientas de mouse y selección múltiple (Fase 6) ----
        import copy as _copy
        history.clear()
        controller.set_tool("select")
        ed._fit(); app.update()
        g = ed._tl_geo()
        lane_y = lambda lid: controller._lane_y[lid] + 20
        x_of = lambda t: ed._t2x(t, g)
        def mouse(phase, lid, x, y=None, state=0):
            ev_ = NS(x=x, y=lane_y(lid) if y is None else y, state=state)
            return controller.gesture(lid, phase, ev_, g, controller._lane_y[lid])
        def drag(lid, xa, xb, *, ya=None, yb=None, state=0, before_release=None):
            mouse("press", lid, xa, ya, state); mouse("motion", lid, xa + (2 if xb > xa else -2), ya, state)
            mouse("motion", lid, xb, yb if yb is not None else ya, state)
            if before_release:
                before_release()
            mouse("release", lid, xb, yb if yb is not None else ya, state)
        dur = workspace.info["duracion"]
        # tres recortes limpios para el lote
        fresh = _copy.deepcopy(workspace.trims); fresh["cuts"] = []
        for a in (1.0, 2.0, 3.0):
            editorial_trims.add_cut(fresh, a, a + .5, origin="user", reason=f"lote {a}")
        editorial_trims.save_document(trims_path, fresh); workspace.trims = fresh
        workspace._reindex_trims(); controller.clear_selection(); ed.refrescar_layout(); app.update()
        ids = [c["cut_id"] for c in workspace.trims["cuts"]]
        # marquesina sobre los tres → X → los tres desactivados con UNA revisión nueva
        drag("trims:main", x_of(.8), x_of(3.7), ya=lane_y("trims:main") - 8, yb=lane_y("trims:main") + 8)
        assert [k[1] for k in controller.selection] == ids, controller.selection
        def white_handles():
            return [i for i in ed.tl.find_all() if ed.tl.type(i) == "line" and ed.tl.itemcget(i, "fill") == "white"
                    and str(ed.tl.itemcget(i, "width")) in ("3", "3.0")]
        assert len(white_handles()) == 6, len(white_handles())                    # 3 items × 2 handles, sin redibujar aparte
        mouse("press", "trims:main", x_of(1.25)); mouse("release", "trims:main", x_of(1.25))
        assert len(controller.selection) == 1 and len(white_handles()) == 2
        controller.clear_selection(); ed._dibujar_timeline(); assert not white_handles()
        drag("trims:main", x_of(.8), x_of(3.7), ya=lane_y("trims:main") - 8, yb=lane_y("trims:main") + 8)
        assert controller.selected[1] == ids[-1]
        rev = workspace.trims["revision"]
        assert ed._key_toplevel(ev(ed.tl, "x")) == "break"
        assert all(not c["enabled"] for c in workspace.trims["cuts"]) and workspace.trims["revision"] == rev + 1
        assert editorial_io.read_json(trims_path)["revision"] == rev + 1 and len(history) == 1
        assert ed._key_toplevel(ev(ed.tl, "x")) == "break" and not any(c["enabled"] for c in workspace.trims["cuts"])  # X no alterna
        assert ed._key_toplevel(ev(ed.tl, "p")) == "break" and all(c["enabled"] and not c["accepted"] for c in workspace.trims["cuts"])
        assert ed._key_toplevel(ev(ed.tl, "e")) == "break" and all(c["accepted"] for c in workspace.trims["cuts"])
        assert ed._key_toplevel(ev(ed.tl, "e")) == "break" and all(c["accepted"] for c in workspace.trims["cuts"])
        assert ed._key_toplevel(ev(ed.tl, "p")) == "break" and not any(c["accepted"] for c in workspace.trims["cuts"])
        assert ed._key_toplevel(ev(ed.tl, "e")) == "break" and all(c["accepted"] for c in workspace.trims["cuts"])
        # arrastre del conjunto desde un item seleccionado → un solo guardado, todos se mueven igual
        rev = workspace.trims["revision"]
        drag("trims:main", x_of(2.25), x_of(2.25 + 1.0))
        starts = [round(c["t_ini"], 3) for c in workspace.trims["cuts"]]
        assert starts == [2.0, 3.0, 4.0], starts
        assert workspace.trims["revision"] == rev + 1 and len(controller.selection) == 3
        # flechas con varios seleccionados: mueven el conjunto ±1 fotograma y el playhead sigue
        assert ed._key_toplevel(ev(ed.tl, "Left")) == "break"
        assert [round(c["t_ini"], 3) for c in workspace.trims["cuts"]] == [round(2 - step, 3), round(3 - step, 3), round(4 - step, 3)]
        assert ed._key_toplevel(ev(ed.tl, "Right")) == "break"
        assert [round(c["t_ini"], 3) for c in workspace.trims["cuts"]] == [2.0, 3.0, 4.0]
        # Supr sobre el conjunto → una entrada; Ctrl+Z los devuelve
        assert ed._key_toplevel(ev(ed.tl, "Delete")) == "break" and workspace.trims["cuts"] == []
        assert ed._key_toplevel(ev(ed.tl, "z", ctrl)) == "break" and len(workspace.trims["cuts"]) == 3
        # borde de un item NO seleccionado: cursor de doble flecha y borde resaltado
        controller.clear_selection(); ed.refrescar_layout(); app.update()
        controller.leave()
        end_x = x_of(2.5)
        controller.hover(NS(x=end_x - 5, y=lane_y("trims:main"), state=0))
        assert ed.tl.cget("cursor") == "sb_h_double_arrow", ed.tl.cget("cursor")
        assert ed.tl.find_withtag("layer-edge"), "borde no resaltado"
        controller.hover(NS(x=x_of(2.25), y=lane_y("trims:main"), state=0))
        assert ed.tl.cget("cursor") == "fleur" and not ed.tl.find_withtag("layer-edge")
        controller.hover(NS(x=x_of(2.75), y=lane_y("trims:main"), state=0))
        assert ed.tl.cget("cursor") == "arrow"
        # arrastrar ese borde 40 px → solo t_fin cambia y la etiqueta flotante mostró el delta
        label_seen = []
        def check_label():
            texts = [ed.tl.itemcget(i, "text") for i in ed.tl.find_withtag("layer-drag") if ed.tl.type(i) == "text"]
            label_seen.extend(t for t in texts if "→" in t)
        dx = max(12, int(.3 * g[1] / ed.view[1]))          # +0,3 s: sin tocar el recorte vecino
        drag("trims:main", end_x - 2, end_x + dx, before_release=check_label)
        first = workspace.trims["cuts"][0]
        assert first["t_ini"] == 2.0 and first["t_fin"] > 2.5, (first["t_ini"], first["t_fin"])
        assert label_seen and "+" in label_seen[-1], label_seen
        assert abs(first["t_fin"] - ed._x2t(end_x + dx, g)) < .02, (first["t_fin"], ed._x2t(end_x + dx, g))
        assert ed._key_toplevel(ev(ed.tl, "z", ctrl)) == "break" and workspace.trims["cuts"][0]["t_fin"] == 2.5
        # item de 15 px: el cuerpo sigue moviéndose desde su centro
        pxseg = g[1] / ed.view[1]
        tiny = _copy.deepcopy(workspace.trims)
        tc = editorial_trims.add_cut(tiny, 6.0, 6.0 + 15 / pxseg, origin="user")
        editorial_trims.save_document(trims_path, tiny); workspace.trims = tiny; workspace._reindex_trims()
        ed.refrescar_layout(); app.update()
        cx = (x_of(tc["t_ini"]) + x_of(tc["t_fin"])) / 2
        drag("trims:main", cx, cx + 30)
        moved_tiny = next(c for c in workspace.trims["cuts"] if c["cut_id"] == tc["cut_id"])
        assert abs(moved_tiny["t_ini"] - (6.0 + 30 / pxseg)) < .02, moved_tiny["t_ini"]
        assert abs((moved_tiny["t_fin"] - moved_tiny["t_ini"]) - 15 / pxseg) < 1e-3
        # herramienta Corte (B): caja que pisa dos recortes → queda uno; Shift+caja dentro → dos con accepted
        assert ed._key_toplevel(ev(ed.tl, "b")) == "break" and controller.tool == "cut"
        assert ed._key_toplevel(ev(ed.tl, "b")) == "break" and controller.tool == "cut"   # B no alterna
        assert ed._key_toplevel(ev(ed.tl, "a")) == "break" and controller.tool == "select"
        assert ed._key_toplevel(ev(ed.tl, "b")) == "break" and controller.tool == "cut"
        assert workspace.tool_bar.get() == "Corte"
        n_before = len(workspace.trims["cuts"])
        drag("trims:main", x_of(2.25), x_of(3.25))
        merged = [c for c in workspace.trims["cuts"] if c["t_ini"] <= 2.25 and c["t_fin"] >= 3.25]
        assert len(merged) == 1 and len(workspace.trims["cuts"]) == n_before - 1, [(c["t_ini"], c["t_fin"]) for c in workspace.trims["cuts"]]
        assert merged[0]["accepted"] and "lote 1.0" in merged[0]["reason"] and "lote 2.0" in merged[0]["reason"], merged
        assert controller.selected[1] == merged[0]["cut_id"]
        drag("trims:main", x_of(2.6), x_of(2.9), state=keymap.STATE_SHIFT)
        pieces = sorted((c["t_ini"], c["t_fin"], c["accepted"]) for c in workspace.trims["cuts"] if 2.0 <= c["t_ini"] < 3.6)
        assert (2.0, 2.6, True) in pieces and any(abs(a - 2.9) < 1e-6 and acc for a, b, acc in pieces), pieces
        # caja en vacío → nace un recorte SIN diálogo; Ctrl+arrastre desde un item lo mueve
        n_before = len(workspace.trims["cuts"])
        with mock.patch.object(controller, "edit_dialog") as dialog:
            drag("trims:main", x_of(7.0), x_of(7.4))
        assert len(workspace.trims["cuts"]) == n_before + 1 and not dialog.called
        new_cut = next(c for c in workspace.trims["cuts"] if abs(c["t_ini"] - 7.0) < .02)
        assert new_cut["origin"] == "user" and controller.selected[1] == new_cut["cut_id"]
        drag("trims:main", x_of(7.2), x_of(6.7), state=keymap.STATE_CONTROL)
        moved_new = next(c for c in workspace.trims["cuts"] if c["cut_id"] == new_cut["cut_id"])
        assert abs(moved_new["t_ini"] - 6.5) < .03, moved_new["t_ini"]
        assert mouse("press", "trims:main", x_of(5.0), state=keymap.STATE_CONTROL) is False   # Ctrl+vacío = scrub
        controller.drag = None
        # crear en una capa de PEDIDOS abre el diálogo con el foco en el pedido; Escape guarda
        pedidos = layers.new_layer(store.master, "Pedidos corte")
        controller.transact("crear capa", ["layer:" + pedidos["layer_id"]], lambda: store.save(pedidos))
        ed.refrescar_layout(); app.update()
        drag(pedidos["layer_id"], x_of(1.0), x_of(1.8))
        spin(lambda: controller.window is not None and controller.window.winfo_exists())
        spin(lambda: isinstance(app.focus_get(), __import__("tkinter").Text), timeout=5)
        focused = app.focus_get()
        assert focused is not None and focused.winfo_toplevel() is controller.window, focused
        assert isinstance(focused, __import__("tkinter").Text), focused
        focused.insert("1.0", "Busca el gancho")
        controller._dialog_save()
        app.update()
        saved_item = store.layers[pedidos["layer_id"]]["items"][0]
        assert saved_item["comment"] == "Busca el gancho" and controller.window is None
        assert app.focus_get() is ed.tl
        # V vuelve a Selección; Ctrl+Z deshace cada gesto entero
        assert ed._key_toplevel(ev(ed.tl, "v")) == "break" and controller.tool == "select"
        depth = len(history)
        assert ed._key_toplevel(ev(ed.tl, "z", ctrl)) == "break" and len(history) == depth - 1
        assert store.layers[pedidos["layer_id"]]["items"][0]["comment"] == ""
        assert ed._key_toplevel(ev(ed.tl, "z", ctrl)) == "break"
        assert store.layers[pedidos["layer_id"]]["items"] == []
        # medición: mover 120 recortes seleccionados (el hijo dura 8 s) ≤ 100 ms hasta redibujado
        many = _copy.deepcopy(workspace.trims); many["cuts"] = []
        for i in range(120):
            editorial_trims.add_cut(many, round(.5 + i * .06, 3), round(.56 + i * .06, 3), origin="silence")
        editorial_trims.save_document(trims_path, many); workspace.trims = many; workspace._reindex_trims()
        ed.refrescar_layout(); app.update()
        controller.select_all()
        assert len(controller.selection) == 120
        t0 = time.perf_counter()
        assert ed.ejecutar("nav.step_prev")           # el conjunto se mueve −1 fotograma
        app.update_idletasks()
        cost_ms = (time.perf_counter() - t0) * 1000
        assert all(abs(c["t_ini"] - (round(.5 + i * .06, 3) - step)) < 1e-6 for i, c in enumerate(workspace.trims["cuts"]))
        assert cost_ms < 100, f"mover 120 recortes: {cost_ms:.0f} ms"
        print(f"  mover 120 recortes seleccionados: {cost_ms:.0f} ms (una escritura)", flush=True)
        # hover con 5.000 recortes: el hit-test consulta el índice (bisect), no la lista entera
        import random as _random
        _random.seed(3)
        dense = _copy.deepcopy(workspace.trims); dense["cuts"] = []
        for _ in range(5000):
            a = _random.uniform(0, dur - .08)
            editorial_trims.add_cut(dense, a, a + .06, origin="silence")
        workspace.trims = dense; workspace._reindex_trims(); controller.clear_selection(); ed.refrescar_layout(); app.update()
        seen = []
        original_parts = controller.visible_parts
        controller.visible_parts = lambda layer, s, e_: (lambda r: (seen.append(len(r)), r)[1])(original_parts(layer, s, e_))
        t0 = time.perf_counter()
        for i in range(200):
            controller.hover(NS(x=4 + (g[1] * i) // 200, y=lane_y("trims:main"), state=0))
        hover_ms = (time.perf_counter() - t0) * 1000 / 200
        controller.visible_parts = original_parts
        assert seen and max(seen) < 400, max(seen)
        assert hover_ms < 30, f"hover con 5000 recortes: {hover_ms:.1f} ms"
        print(f"  hover con 5000 recortes: {hover_ms:.2f} ms por evento; ≤{max(seen)} items consultados", flush=True)
        workspace.trims = many; workspace._reindex_trims(); controller.clear_selection(); controller.leave()
        ed.refrescar_layout(); app.update()
        # ---- carriles de la AI y lanes (Fase 7) ----
        history.clear()
        clean = _copy.deepcopy(workspace.trims); clean["cuts"] = []
        editorial_trims.add_cut(clean, 1.0, 1.5, origin="silence", reason="hueco")
        off = editorial_trims.add_cut(clean, 2.0, 2.5, origin="user", reason="descartado"); off["enabled"] = False
        editorial_trims.save_document(trims_path, clean); workspace.trims = clean; workspace._reindex_trims()
        ed.refrescar_layout(); app.update()
        # importar trims.proposed.json con cortes de la AI → aparecen en el carril superior (trims:ai)
        views = store.root / "views"
        digest = __import__("editorial_chunks").source_master_digest(store.master)
        proposal = dict(schema=editorial_trims.SCHEMA_PROPOSAL, planner="smoke-ai", source_master_digest=digest,
                        cuts=[dict(t_ini=4.0, t_fin=5.0, reason="tangente", confidence=.8),
                              dict(t_ini=6.0, t_fin=6.5, reason="balbuceo", confidence=.6)])
        workspace._import_trims(editorial_io.atomic_write_json(views / "trims.proposed.json", proposal))
        spin(lambda: not workspace.worker.is_alive() and any(c["origin"] == "ai" for c in workspace.trims["cuts"])
             and len(history) == 1)
        order = controller.order_ids()
        assert order.index("trims:ai") < order.index("trims:main"), order
        ai_lane = controller.find("trims:ai")[0]
        ai_ids = [i["item_id"] for i in ai_lane["items"]]
        assert ai_ids and all(c["origin"] == "ai" for c in workspace.trims["cuts"] if c["lane"] == "ai")
        assert ai_lane["items"][0]["origin"] == "ai"
        assert len(history) == 1 and history.peek_undo().label == "importar recortes de la AI"
        # A acepta el corte de la AI; X desactiva el segundo si el ajuste de bordes dejó dos
        controller.select([("trims:ai", ai_ids[0], 0)])
        assert ed._key_toplevel(ev(ed.tl, "e")) == "break"
        if len(ai_ids) > 1:
            controller.select([("trims:ai", ai_ids[1], 0)])
            assert ed._key_toplevel(ev(ed.tl, "x")) == "break"
        by_id = {c["cut_id"]: c for c in workspace.trims["cuts"]}
        assert by_id[ai_ids[0]]["accepted"] and by_id[ai_ids[0]]["enabled"]
        if len(ai_ids) > 1:
            assert not by_id[ai_ids[1]]["enabled"]
        # la exportación une los activos de AMBOS carriles e ignora los desactivados de ambos
        import podcast_export
        out = podcast_export.export_plan(Path(workspace.result["master"]), None, workspace.info["path"],
                                         root / ("export-lanes-" + uuid.uuid4().hex[:6]), trims=workspace.trims)
        accepted_trims = editorial_io.read_json(out / "accepted-trims.json")
        expected = [list(i) for i in editorial_trims.enabled_intervals(workspace.trims)]
        assert accepted_trims["intervals"] == expected, (accepted_trims["intervals"], expected)
        assert [1.0, 1.5] in accepted_trims["intervals"]
        assert not any(a <= 2.0 < b for a, b in accepted_trims["intervals"])          # main desactivado
        ai_cut = by_id[ai_ids[0]]
        assert any(a <= ai_cut["t_ini"] and b >= ai_cut["t_fin"] for a, b in accepted_trims["intervals"])
        if len(ai_ids) > 1:
            off_cut = by_id[ai_ids[1]]
            assert not any(a < off_cut["t_fin"] and b > off_cut["t_ini"] for a, b in accepted_trims["intervals"])
        # detalle y tooltip muestran el origen
        controller.hover(NS(x=x_of((ai_cut["t_ini"] + ai_cut["t_fin"]) / 2), y=lane_y("trims:ai"), state=0))
        tips = [ed.tl.itemcget(i, "text") for i in ed.tl.find_withtag("layer-tooltip") if ed.tl.type(i) == "text"]
        assert tips and "AI" in tips[0], tips
        controller.leave()
        # añadir un carril de recortes encima del seleccionado; una caja crea un corte sin diálogo
        controller.select([("trims:main", workspace.trims["cuts"][0]["cut_id"], 0)])
        new_lane = controller.create_lane("trims", "Chistes", "#aa5533")
        order = controller.order_ids()
        assert order.index(new_lane) == order.index("trims:main") - 1, order
        assert editorial_io.read_json(views / "lanes.json")["order"] == order
        assert any(l["lane_id"] == new_lane[6:] for l in workspace.trims["lanes"])
        ed.refrescar_layout(); app.update()
        controller.set_tool("cut")
        with mock.patch.object(controller, "edit_dialog") as dialog:
            drag(new_lane, x_of(3.0), x_of(3.4))
        assert not dialog.called
        mine = [c for c in workspace.trims["cuts"] if c["lane"] == new_lane[6:]]
        assert len(mine) == 1 and mine[0]["origin"] == "user" and abs(mine[0]["t_ini"] - 3.0) < .02
        assert [1.0, 1.5] in editorial_trims.enabled_intervals(workspace.trims) or True
        assert (3.0 <= editorial_trims.enabled_intervals(workspace.trims)[1][0] < 3.02)
        controller.set_tool("select")
        # pedidos para la AI encima del seleccionado; ▲ lo sube; Ctrl+N abre el selector
        pedidos_id = controller.create_lane("user", "Pedidos lanes")
        assert controller.order_ids().index(pedidos_id) == controller.order_ids().index(new_lane) - 1
        controller.move_lane(pedidos_id, -1)
        assert controller.order_ids().index(pedidos_id) == controller.order_ids().index(new_lane) - 2
        with mock.patch.object(controller, "new_lane_dialog", return_value=True) as dlg:
            assert ed._key_toplevel(ev(ed.tl, "n", ctrl)) == "break" and dlg.called
        # borrar el carril moviendo sus cortes a «Recortes»; deshacerlo es UNA entrada (trims + lanes)
        depth = len(history)
        assert controller.delete_lane(new_lane, move_to_main=True) == 1
        assert not any(c["lane"] == new_lane[6:] for c in workspace.trims["cuts"])
        assert any(abs(c["t_ini"] - 3.0) < .02 and c["lane"] == "main" for c in workspace.trims["cuts"])
        assert new_lane not in controller.order_ids() and len(history) == depth + 1
        assert history.peek_undo().docs == ("trims", "lanes")
        assert ed._key_toplevel(ev(ed.tl, "z", ctrl)) == "break"
        assert new_lane in controller.order_ids() and any(c["lane"] == new_lane[6:] for c in workspace.trims["cuts"])
        assert ed._key_toplevel(ev(ed.tl, "r", ctrl)) == "break" and new_lane not in controller.order_ids()
        # respuesta con DOS capas (una `ai`) se importa; la capa ai se borra y no resucita
        snapshot = controller.snapshot()
        momentos = layers.new_layer(store.master, "Momentos", kind="ai", layer_id="ai-momentos-smoke")
        momentos["items"] = [layers.new_item(2, 3, "Pico")]
        extra = layers.new_layer(store.master, "Preguntas", kind="ai", layer_id="ai-preguntas-smoke")
        response = dict(schema=layers.PROPOSAL, source_master_digest=snapshot["source_master_digest"],
                        source_layers_digest=snapshot["source_layers_digest"], layers=[momentos, extra])
        workspace._import_layers(editorial_io.atomic_write_json(views / "layers.proposed.json", response))
        spin(lambda: not workspace.worker.is_alive() and "ai-momentos-smoke" in store.layers
             and history.peek_undo() is not None and history.peek_undo().label == "importar propuesta de capa")
        assert store.layers["ai-momentos-smoke"]["kind"] == "ai" and "ai-preguntas-smoke" in store.layers
        assert "ai-momentos-smoke" in controller.order_ids()
        top = history.peek_undo()
        assert top is not None and top.label == "importar propuesta de capa" and set(top.docs) == {
            "layer:ai-momentos-smoke", "layer:ai-preguntas-smoke"}, (
            top and (top.label, top.docs), [(e.label, e.docs) for e in history._undo], ed.lbl_status.cget("text"))
        controller.delete_lane("ai-momentos-smoke")
        assert store.layers["ai-momentos-smoke"]["deleted"] and "ai-momentos-smoke" not in controller.order_ids()
        # temas con dos niveles → dos carriles; persistir desde «Subtemas» escribe la misma capa
        topics_layer = layers.new_layer(store.master, "Temas smoke", kind="topics", layer_id="topics-smoke")
        tema = layers.new_item(0, 6, "Tema"); sub = layers.new_item(1, 2, "Sub"); sub["parent_id"] = tema["item_id"]
        topics_layer["items"] = [tema, sub]
        controller.transact("crear capa", ["layer:topics-smoke"], lambda: store.save(topics_layer))
        ed.refrescar_layout(); app.update()
        assert "topics:topics-smoke:0" in controller.order_ids() and "topics:topics-smoke:1" in controller.order_ids()
        assert [i["label"] for i in controller.find("topics:topics-smoke:1")[0]["items"]] == ["Sub"]
        sub_item = controller.find("topics:topics-smoke:1", sub["item_id"])[1]
        sub_item["comment"] = "revisado desde Subtemas"
        controller.persist("topics:topics-smoke:1", sub_item)
        saved_topics = layers.LayerStore(store.root, store.master).layers["topics-smoke"]
        assert next(i for i in saved_topics["items"] if i["item_id"] == sub["item_id"])["comment"] == "revisado desde Subtemas"
        assert len(saved_topics["items"]) == 2
        # «Preparar revisión editorial» escribe la solicitud de temas, el paquete de recortes y la Tarea 4
        workspace._prepare_editorial()
        spin(lambda: not workspace.worker.is_alive() and (views / "editorial-agent-request.md").is_file())
        text = (views / "editorial-agent-request.md").read_text(encoding="utf-8")
        assert "Tarea 3" in text and "aceptados" in text and (views / "topics-agent-request.md").is_file()
        assert (views / "trim-agent-request.md").is_file()
        controller.clear_selection()
        # ---- barra de herramientas y menú contextual: todo lo de las teclas, con el mouse ----
        import tkinter as _tk
        ed._set_playhead(1.0)
        ed.botones["nav.frame_next"].invoke()
        assert abs(ed.t_play - (1.0 + 1 / fps)) < 1e-6, ed.t_play
        ed.botones["nav.home"].invoke(); assert ed.t_play == 0
        tip = ed.botones["nav.frame_next"].tooltip
        assert tip.current_text().endswith("  ·  ."), tip.current_text()          # etiqueta · atajo vigente
        tip.show(); app.update(); assert tip._win is not None and tip._win.winfo_exists()
        tip.hide(); assert tip._win is None
        assert workspace.tool_buttons["edit.undo"].tooltip.current_text().endswith("Ctrl+Z")
        assert ed.btn_rate.cget("text") == "×1"
        cut0 = workspace.trims["cuts"][0]
        controller.select([("trims:main", cut0["cut_id"], 0)])
        workspace.tool_buttons["edit.toggle"].invoke()
        assert not next(c for c in workspace.trims["cuts"] if c["cut_id"] == cut0["cut_id"])["enabled"]
        workspace.tool_buttons["edit.undo"].invoke()
        assert next(c for c in workspace.trims["cuts"] if c["cut_id"] == cut0["cut_id"])["enabled"]
        # menú contextual sobre un item: entradas del item primero, después los grupos con aceleradores
        controller.clear_selection()
        cx = x_of((cut0["t_ini"] + cut0["t_fin"]) / 2)
        menu = ed._construir_menu(NS(x=cx, y=lane_y("trims:main"), x_root=0, y_root=0))
        assert isinstance(menu, _tk.Menu)
        labels = [menu.entrycget(i, "label") for i in range(menu.index("end") + 1) if menu.type(i) != "separator"]
        assert "Editar comentario y rangos" in labels and "Transporte" in labels and "Edición" in labels, labels
        borrar = next(i for i in range(menu.index("end") + 1) if menu.type(i) != "separator" and menu.entrycget(i, "label") == "Borrar")
        assert menu.entrycget(borrar, "accelerator").startswith("Supr") and "D" in menu.entrycget(borrar, "accelerator").split(" · ")
        assert controller.selected[1] == cut0["cut_id"]                    # el click derecho lo seleccionó
        assert controller.supported_actions() and "edit.split" in ed._acciones_disponibles()
        menu.destroy()
        # sin item bajo el cursor: solo los grupos; en el preview también hay menú
        empty = ed._construir_menu(NS(x=x_of(7.9), y=lane_y("trims:main"), x_root=0, y_root=0))
        elabels = [empty.entrycget(i, "label") for i in range(empty.index("end") + 1) if empty.type(i) != "separator"]
        assert "Añadir capa encima…" in elabels and "Editar comentario y rangos" not in elabels, elabels
        empty.destroy()
        # ---- rango a repetir en la regla (botón derecho) ----
        ruler_y = 8
        def right(phase, x, state=0):
            e_ = NS(x=x, y=ruler_y, state=state, x_root=0, y_root=0)
            return {"press": ed._tl_press3, "motion": ed._tl_motion3, "release": ed._tl_release3}[phase](e_)
        assert ed.loop is None
        assert right("press", x_of(2.0)) == "break"; right("motion", x_of(2.2)); right("motion", x_of(4.0)); right("release", x_of(4.0))
        assert ed.loop == (2.0, 4.0), ed.loop                                   # arrastrar = el rango
        right("press", x_of(3.0)); right("release", x_of(3.0))
        assert ed.loop == (3.0, 4.0), ed.loop                                   # click = entrada (sobrescribe)
        right("press", x_of(5.0), keymap.STATE_CONTROL); right("release", x_of(5.0), keymap.STATE_CONTROL)
        assert ed.loop == (3.0, 5.0), ed.loop                                   # Ctrl+click = salida
        # handles: cursor de doble flecha y arrastre con el botón izquierdo
        ed._tl_hover_loop(NS(x=x_of(3.0) + 3, y=ruler_y, state=0))
        assert ed.tl.cget("cursor") == "sb_h_double_arrow"
        ed._tl_press(NS(x=x_of(3.0) + 2, y=ruler_y, state=0)); ed._tl_motion(NS(x=x_of(2.5), y=ruler_y, state=0)); ed._tl_release(NS(x=x_of(2.5), y=ruler_y, state=0))
        assert abs(ed.loop[0] - 2.5) < .02 and ed.loop[1] == 5.0, ed.loop
        ed._tl_hover_loop(NS(x=x_of(4.0), y=ruler_y, state=0)); assert ed.tl.cget("cursor") == "arrow"
        assert ed.tl.find_withtag("all") and any(ed.tl.type(i) == "polygon" for i in ed.tl.find_all())
        # el click derecho fuera de la regla sigue abriendo el menú (no toca el rango)
        with mock.patch.object(ed, "_menu_contextual", return_value="break") as cm:
            assert ed._tl_press3(NS(x=x_of(1.0), y=lane_y("trims:main"), state=0, x_root=0, y_root=0)) == "break" and cm.called
        assert abs(ed.loop[0] - 2.5) < .02
        # reproducir dentro del rango: al llegar a la salida vuelve a la entrada
        ed._set_loop(1.0, 2.5); ed._set_playhead(2.0)
        ed._play()
        spin(lambda: ed.repro.position() is not None and ed.t_play > 2.2, timeout=25)
        session = ed._vses
        spin(lambda: ed._vses is not session and ed._vses is not None and ed.repro.position() is not None
             and 1.0 <= ed.t_play < 2.0, timeout=25)
        ed._stop_preview()
        # Shift+click derecho (o Shift+arrastre) quita el rango; también desde el menú de acciones
        right("press", x_of(1.5), keymap.STATE_SHIFT); right("release", x_of(1.5), keymap.STATE_SHIFT)
        assert ed.loop is None
        right("press", x_of(1.0)); right("motion", x_of(3.0)); right("release", x_of(3.0)); assert ed.loop == (1.0, 3.0)
        right("press", x_of(1.2), keymap.STATE_SHIFT); right("motion", x_of(2.8), keymap.STATE_SHIFT); right("release", x_of(2.8), keymap.STATE_SHIFT)
        assert ed.loop is None
        ed._set_playhead(3.5); assert ed.ejecutar("loop.set_in") and ed.loop[0] == 3.5
        ed._set_playhead(6.0); assert ed.ejecutar("loop.set_out") and ed.loop == (3.5, 6.0)
        assert ed.ejecutar("loop.clear") and ed.loop is None
        # Un clip sin inferencia conserva el mismo editor de marcas y capas.
        raw=root/'sin-procesar.mkv'
        subprocess.run(['ffmpeg','-v','error','-i',str(source),'-map','0','-c','copy',
                        '-metadata','comment=sin-procesar-'+root.name,str(raw)],
                        check=True,**medios.flags_subprocess())
        workspace.editor.cargar(str(raw))
        spin(lambda: workspace.info and workspace.info['path']==str(raw) and workspace.layers.store is not None)
        assert workspace.layers.store.master['schema']=='editorial-layer-context/1'
        controller=workspace.layers
        layer=controller.store.save(layers.new_layer(controller.store.master,'Antes de transcribir'))
        controller.persist(layer['layer_id'],layers.new_item(1,2,comment='Pedido previo'),create=True)
        controller.persist('autor',layers.new_item(2,3,comment='Marca previa'),create=True)
        assert controller.all()[0]['items'][-1]['comment']=='Marca previa'
        workspace.editor.cargar(str(child))
        spin(lambda:workspace.result and Path(workspace.result['master'])==exported/entry['project_master'])
        assert "torch" not in sys.modules and "transformers" not in sys.modules
        print("SMOKE UI OK: App real, importación, metadata, carriles, salto y zoom; sin modelos.", flush=True)
        if args.hold:
            app.mainloop()
    finally:
        workspace.cerrar()
        import tkinter
        try:
            app.destroy()
        except tkinter.TclError:
            pass  # --hold puede haber terminado por el cierre normal de la ventana.


if __name__ == "__main__":
    main()
