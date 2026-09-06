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
            for phase, t in (("press", 2.5), ("motion", 3.5)):
                event = SimpleNamespace(x=workspace.editor._t2x(t,g),y=0)
                controller.gesture(layer["layer_id"],phase,event,g,0)
            from unittest import mock
            with mock.patch.object(controller,"edit_dialog"):
                controller.gesture(layer["layer_id"],"release",event,g,0)
            saved=store.layers[layer["layer_id"]]
            assert len(saved["items"]) == 1
            item=copy_item=__import__('copy').deepcopy(saved['items'][0])
            item['comment']='Busca la recurrencia'
            controller.persist(layer['layer_id'],item)
            assert layers.LayerStore(store.root,data).layers[layer['layer_id']]['items'][0]['comment'] == 'Busca la recurrencia'
            controller.persist('autor',layers.new_item(2,3,comment='Pedido del autor'),create=True)
            assert workspace.editor.reg.marcas[-1]['prompt']=='Pedido del autor'
            controller.persist('recortes',layers.new_item(10,11,comment='Recorte smoke'),create=True)
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
