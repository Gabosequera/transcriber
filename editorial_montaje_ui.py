"""Modo Montaje del timeline (plan-montaje-ai.md §7.2/§7.3): carriles de pistas de
video `V1`, `V2`… con clips, gestos de la misma herramienta Selección/Corte, teclas del
keymap y preview clip a clip. La lógica vive en `editorial_montaje` (puro); esto solo
dibuja, traduce gestos y escribe por `save()` dentro de `LayersController.transact`
(documento de historial `montaje`)."""
from __future__ import annotations

import copy
import tkinter as tk
from pathlib import Path

import editorial_montaje as montaje
import editorial_nav
from editorial_io import format_time

LANE_H = 34
TOPICS_H = 16
COL_ORIGIN = {"ai": "#9471bd", "user": "#c58e43"}
EDGE_PX = 8
NARROW_PX = 24
CLICK_PX = 4
MAGNET_PX = 6
LANE_PREFIX = "montage:"
TOPICS_LANE = "montage:topics"


class MontageController:
    def __init__(self, workspace):
        self.w = workspace
        self.doc = None
        self.path = None
        self.mode = "source"
        self.selection = []          # clip_ids, en orden de secuencia
        self.selected = None         # clip_id primario
        self.drag = None
        self._lane_y = {}
        self._map = None
        self._map_key = None
        self._saved = {"source": None, "montage": None}      # (view, t_play) por modo
        self._hover_state = None
        self._cursor = None

    # ---- documento ----
    def reset(self):
        self.doc, self.path = None, None
        self.selection, self.selected, self.drag = [], None, None
        self._map = self._map_key = None
        self._lane_y = {}
        self._saved = {"source": None, "montage": None}
        if self.mode != "source":
            self.set_mode("source", quiet=True)

    def load(self, master_path, master):
        self.path = Path(master_path).parent / "views" / "montaje.json"
        self.doc = montaje.load_document(self.path, fingerprint=master["media"].get("fingerprint"),
                                         duration=float(master["media"]["duration"]))
        self._map = self._map_key = None

    def ensure_doc(self):
        if self.doc is None:
            store = self.w.layers.store
            if not store or self.path is None:
                raise ValueError("abre o genera primero la metadata del medio")
            master = store.master
            target = float(__import__("hardware").load().get("montage_target_minutes") or 15) * 60
            self.doc = montaje.new_document(master["media"]["fingerprint"], float(master["media"]["duration"]),
                                            target_seconds=target)
        return self.doc

    def has_clips(self) -> bool:
        return bool(self.doc and self.doc["clips"])

    def save(self, doc):
        """Único punto de escritura: persiste (revisión +1) y refresca el mapa."""
        if self.path is None:
            raise ValueError("no hay proyecto abierto")
        montaje.save_document(self.path, doc)
        self.doc = doc
        self._map = self._map_key = None

    def restore(self, snapshot):
        """Deshacer/rehacer: por el camino de guardado; None = el montaje no existía."""
        if snapshot is None:
            self.doc = None
            self._map = self._map_key = None
            if self.path and self.path.is_file():
                self.path.unlink()
            return
        self.save(copy.deepcopy(snapshot))

    def transact(self, label, fn):
        result = self.w.layers.transact("montaje: " + label, ["montaje"], fn)
        self._after_write()
        return result

    def _after_write(self):
        alive = {c["clip_id"] for c in (self.doc or {}).get("clips") or []}
        self.selection = [i for i in self.selection if i in alive]
        if self.selected not in alive:
            self.selected = self.selection[-1] if self.selection else None
        self.w.montage_changed()
        editor = self.w.editor
        if self.mode == "montage":
            editor.mapa_tiempo = self.map()
            editor._clamp_view()
        editor.refrescar_layout()
        self.sync_detail()

    def map(self):
        doc = self.doc or {"clips": [], "tracks": [{"track_id": "V1", "name": "V1"}]}
        key = (id(self.doc), (self.doc or {}).get("revision"), len(doc["clips"]))
        if key != self._map_key:
            self._map_key = key
            self._map = montaje.SequenceMap(doc) if self.doc else montaje.SequenceMap(
                {"clips": [], "tracks": [{"track_id": "V1", "name": "V1"}]})
        return self._map

    # ---- modo ----
    def set_mode(self, mode, *, quiet=False):
        if mode not in ("source", "montage") or mode == self.mode:
            return
        editor = self.w.editor
        if editor.info:
            editor._stop_preview()
            editor.btn_play.configure(text="▶")
            self._saved[self.mode] = (list(editor.view), editor.t_play, editor.loop)
        self.mode = mode
        if mode == "montage":
            editor.mapa_tiempo = self.map()
        else:
            editor.mapa_tiempo = None
        editor.loop = None
        if editor.info:
            saved = self._saved.get(mode)
            if saved:
                editor.view, editor.t_play, editor.loop = list(saved[0]), saved[1], saved[2]
            else:
                editor.view = [0.0, max(editor._dur(), 2.0)]
                editor.t_play = 0.0
            editor._clamp_view()
            editor.refrescar_layout()
            editor._set_playhead(min(editor.t_play, max(editor._dur() - 0.05, 0.0)))
        self._hover_state = None
        self.w.montage_changed()
        if not quiet:
            editor.status("Modo Montaje: la regla mide la secuencia; V1 abajo, V2 encima; arrastra un clip "
                          "hacia arriba para crear una pista" if mode == "montage" else "Modo Fuente")

    # ---- carriles ----
    def lanes(self):
        if self.mode != "montage":
            return []
        doc = self.doc
        tracks = montaje.track_ids(doc) if doc else ["V1"]
        out = []
        for tid in reversed(tracks):            # la más alta arriba
            out.append(dict(nombre=LANE_PREFIX + tid, alto=LANE_H,
                            dibujar=lambda c, g, y, t=tid: self.draw(t, c, g, y),
                            gesto=lambda phase, e, g, y, t=tid: self.gesture(t, phase, e, g, y)))
        out.append(dict(nombre=TOPICS_LANE, alto=TOPICS_H, dibujar=self.draw_topics))
        return out

    def _clips_of(self, track):
        return montaje.track_clips(self.doc, track) if self.doc else []

    def draw(self, track, canvas, g, y):
        editor = self.w.editor
        start, span = editor.view
        self._lane_y[track] = y
        canvas.create_rectangle(g[0], y, sum(g), y + LANE_H, fill="#171c1a", outline="#343c37")
        clips = self._clips_of(track)
        if not clips:
            canvas.create_text(g[0] + 40, y + LANE_H / 2, anchor="w", fill="#4d5751",
                               text="pista vacía: suelta un clip aquí" if track != "V1"
                               else "sin clips: Ctrl+Shift+A añade la selección de la fuente",
                               font=("TkDefaultFont", 8))
        for clip in clips:
            a_t, b_t = clip["seq_ini"], montaje.seq_fin(clip)
            if b_t < start or a_t > start + span:
                continue
            a = editor._t2x(max(start, a_t), g)
            b = max(a + 3, editor._t2x(min(start + span, b_t), g))
            selected = clip["clip_id"] in self.selection or clip["clip_id"] == self.selected
            accepted = clip["state"] == "accepted"
            color = COL_ORIGIN.get(clip["origin"], "#c58e43")
            canvas.create_rectangle(a, y + 4, b, y + LANE_H - 4,
                                    fill=color if clip["state"] != "disabled" else "",
                                    stipple="gray50" if clip["state"] == "proposed" else "",
                                    outline="#ffffff" if selected else "#35a978" if accepted else color,
                                    width=2 if selected or accepted else 1,
                                    dash=(3, 2) if clip["state"] == "disabled" else ())
            if b - a > 40:
                text = ("✓ " if accepted else "") + (clip["label"] or clip["clip_id"])
                canvas.create_text(a + 4, y + LANE_H / 2, anchor="w", fill="#ffffff",
                                   text=text[:int((b - a) / 7)], font=("TkDefaultFont", 8))
            if selected:
                for x in (a, b):
                    canvas.create_line(x, y + 3, x, y + LANE_H - 3, fill="white", width=3)
        canvas.create_text(g[0] + 4, y + 1, text=track, anchor="nw", fill="#aab6af", font=("TkDefaultFont", 8))

    def _topics_layer(self):
        store = self.w.layers.store
        if not store:
            return None
        return next((l for l in store.visible() if l.get("kind") == "topics"), None)

    def draw_topics(self, canvas, g, y):
        """Franja fina con los temas del rango fuente de cada tramo (plan §7.2)."""
        editor = self.w.editor
        start, span = editor.view
        self._lane_y[TOPICS_LANE] = y
        canvas.create_rectangle(g[0], y, sum(g), y + TOPICS_H, fill="#131816", outline="#2a312d")
        layer = self._topics_layer()
        if not layer or not self.doc:
            return
        by_id = {i["item_id"]: i for i in layer["items"]}
        for piece in montaje.flatten(self.doc, gaps=True):
            if piece.get("gap") or piece["seq_fin"] < start or piece["seq_ini"] > start + span:
                continue
            for item in layer["items"]:
                if item.get("parent_id"):
                    continue                     # solo temas principales en la franja
                for r in item["ranges"]:
                    lo, hi = max(r["t_ini"], piece["source_ini"]), min(r["t_fin"], piece["source_fin"])
                    if hi <= lo:
                        continue
                    sa = piece["seq_ini"] + (lo - piece["source_ini"])
                    sb = piece["seq_ini"] + (hi - piece["source_ini"])
                    a = editor._t2x(max(start, sa), g)
                    b = max(a + 2, editor._t2x(min(start + span, sb), g))
                    canvas.create_rectangle(a, y + 3, b, y + TOPICS_H - 3, fill=layer.get("color", "#9a70bc"),
                                            outline="")
                    if b - a > 50:
                        canvas.create_text(a + 3, y + TOPICS_H / 2, anchor="w", fill="#111513",
                                           text=item["label"][:int((b - a) / 6)], font=("TkDefaultFont", 7))
        del by_id

    # ---- hit test / hover ----
    def hit(self, track, x, g):
        editor = self.w.editor
        hits = []
        for clip in self._clips_of(track):
            a, b = editor._t2x(clip["seq_ini"], g), editor._t2x(montaje.seq_fin(clip), g)
            b = max(a + 3, b)
            if a - EDGE_PX <= x <= b + EDGE_PX:
                width = b - a
                zone = EDGE_PX if width >= NARROW_PX else max(1.0, width / 3)
                mode = "start" if abs(x - a) <= zone else "end" if abs(x - b) <= zone else "move"
                hits.append((clip, mode))
        if self.selected:
            active = next((h for h in hits if h[0]["clip_id"] == self.selected), None)
            if active:
                return active
        return hits[-1] if hits else None

    def _track_at(self, y):
        for track, ly in self._lane_y.items():
            if track != TOPICS_LANE and ly <= y < ly + LANE_H:
                return track
        return None

    def _set_cursor(self, cursor):
        if cursor != self._cursor:
            self._cursor = cursor
            try:
                self.w.editor.tl.configure(cursor=cursor)
            except tk.TclError:
                pass

    def hover(self, e):
        editor = self.w.editor
        g = editor._tl_geo()
        hit = editor._carril_en(e.y)
        state = ("empty", None)
        clip = None
        if hit and g and hit[0]["nombre"].startswith(LANE_PREFIX) and hit[0]["nombre"] != TOPICS_LANE:
            track = hit[0]["nombre"][len(LANE_PREFIX):]
            found = self.hit(track, e.x, g)
            if found:
                clip, mode = found
                state = (mode, clip["clip_id"])
            else:
                state = ("lane", track)
        if self.drag is None and state != self._hover_state:
            self._hover_state = state
            tool = self.w.layers.tool
            if state[0] == "empty" or state[0] == "lane":
                self._set_cursor("arrow")
            elif tool == "cut":
                self._set_cursor("crosshair")
            elif state[0] in ("start", "end"):
                self._set_cursor("sb_h_double_arrow")
            else:
                self._set_cursor("fleur")
        self.sync_detail(clip)

    def leave(self, _e=None):
        self._hover_state = None
        self._set_cursor("arrow")
        self.sync_detail()

    def _item_like(self, clip):
        seq_a, seq_b = clip["seq_ini"], montaje.seq_fin(clip)
        source = f"fuente {format_time(clip['source_ini'])[:-4]}–{format_time(clip['source_fin'])[:-4]}"
        topics = self._topic_labels(clip)
        comment = " · ".join(filter(None, (source, ("temas: " + ", ".join(topics)) if topics else "",
                                           clip.get("reason") or "", clip.get("junction_note") or "")))
        return {"item_id": clip["clip_id"], "label": clip["label"] or clip["clip_id"], "comment": comment,
                "state": clip["state"], "origin": clip["origin"], "parent_id": None,
                "ranges": [{"t_ini": seq_a, "t_fin": seq_b}]}

    def _topic_labels(self, clip):
        layer = self._topics_layer()
        if not layer:
            return []
        by_id = {i["item_id"]: i for i in layer["items"]}
        labels = [by_id[t]["label"] for t in clip.get("topic_ids") or [] if t in by_id]
        if labels:
            return labels
        out = []
        for item in layer["items"]:
            if any(r["t_ini"] < clip["source_fin"] and r["t_fin"] > clip["source_ini"] for r in item["ranges"]):
                out.append(item["label"])
        return out[:3]

    def sync_detail(self, hovered=None):
        detail = self.w.layers.detail
        if detail is None:
            return
        clip = hovered
        if clip is None and self.selected and self.doc:
            clip = next((c for c in self.doc["clips"] if c["clip_id"] == self.selected), None)
        if clip is None:
            detail.clear()
            return
        layer_like = {"layer_id": LANE_PREFIX + clip["track_id"], "name": f"Montaje {clip['track_id']}",
                      "color": COL_ORIGIN.get(clip["origin"], "#c58e43")}
        detail.show(layer_like, self._item_like(clip), selected=clip["clip_id"] == self.selected)

    # ---- selección ----
    def select(self, ids, primary=None):
        order = {c["clip_id"]: i for i, c in enumerate(montaje.ordered_clips(self.doc))} if self.doc else {}
        self.selection = sorted(dict.fromkeys(ids), key=lambda i: order.get(i, 0))
        if primary is None or primary not in self.selection:
            primary = self.selection[-1] if self.selection else None
        self.selected = primary

    def clear_selection(self):
        self.selection, self.selected = [], None

    def _selected_clip(self):
        if not self.doc or not self.selected:
            raise ValueError("selecciona un clip del montaje")
        return montaje.clip_by_id(self.doc, self.selected)

    def _redraw(self):
        self.w.editor._dibujar_timeline()

    # ---- gestos ----
    def gesture(self, track, phase, e, g, y):
        if self.w.worker and self.w.worker.is_alive():
            return True
        if phase == "doble":
            hit = self.hit(track, e.x, g)
            if hit:
                self.select([hit[0]["clip_id"]])
                self.reveal_source()
            return True
        if phase == "press":
            return self._press(track, e, g, y)
        if phase == "motion":
            return self._motion(e, g)
        if phase == "release":
            return self._release(e, g)
        return True

    def _press(self, track, e, g, y):
        editor = self.w.editor
        t = max(0.0, editor._x2t(e.x, g))
        state = int(getattr(e, "state", 0) or 0)
        shift, ctrl = bool(state & 0x1), bool(state & 0x4)
        hit = self.hit(track, e.x, g)
        self.drag = None
        tool = self.w.layers.tool
        if tool == "cut" and not ctrl:
            if hit:
                clip, _ = hit
                self.select([clip["clip_id"]])
                self._split_at(clip, t)
                return True
            return False                        # vacío con Corte: scrub
        if not hit:
            self.clear_selection()
            self._redraw()
            self.sync_detail()
            return False                        # el editor mueve el playhead
        clip, mode = hit
        cid = clip["clip_id"]
        if shift:
            ids = list(self.selection)
            if cid in ids:
                ids.remove(cid)
            else:
                ids.append(cid)
            self.select(ids, primary=cid if cid in ids else None)
        elif cid not in self.selection:
            self.select([cid])
        else:
            self.selected = cid
        if mode in ("start", "end") and not shift:
            self.drag = dict(kind="edge", clip=cid, mode=mode, t0=t, t1=t, x0=e.x, x1=e.x, y=y,
                             limits=montaje.edge_limits(self.doc, cid, mode))
        elif not shift:
            self.drag = dict(kind="move", ids=list(self.selection), t0=t, t1=t, x0=e.x, x1=e.x,
                             y0=e.y, y1=e.y, y=y, track=track)
        self._redraw()
        self.sync_detail()
        return True

    def _magnet(self, t, g):
        """Imán a bordes de otros clips y al playhead (±6 px)."""
        editor = self.w.editor
        radius = MAGNET_PX * editor.view[1] / max(g[1], 1)
        candidates = montaje.clip_edges(self.doc) + [editor.t_play]
        best = min(candidates, key=lambda x: abs(x - t), default=None)
        return best if best is not None and abs(best - t) <= radius else t

    def _motion(self, e, g):
        d = self.drag
        if not d:
            return True
        editor = self.w.editor
        canvas = editor.tl
        t = max(0.0, editor._x2t(e.x, g))
        d["t1"], d["x1"], d["y1"] = t, e.x, getattr(e, "y", d.get("y1", 0))
        canvas.delete("montage-drag")
        if d["kind"] == "move":
            delta = self._magnet_delta(d, g)
            target_track = self._drop_track(d)
            for cid in d["ids"]:
                clip = montaje.clip_by_id(self.doc, cid)
                ly = self._lane_y.get(target_track if len(d["ids"]) == 1 else clip["track_id"], d["y"])
                canvas.create_rectangle(editor._t2x(clip["seq_ini"] + delta, g), ly + 4,
                                        editor._t2x(montaje.seq_fin(clip) + delta, g), ly + LANE_H - 4,
                                        outline="white", dash=(3, 2), tags="montage-drag")
            editor.status(f"mover {len(d['ids'])} clip(s) {delta:+.2f} s → {target_track} · suelta para guardar")
        elif d["kind"] == "edge":
            clip = montaje.clip_by_id(self.doc, d["clip"])
            low, high = d["limits"]
            delta = max(low, min(t - d["t0"], high))
            a, b = clip["seq_ini"], montaje.seq_fin(clip)
            if d["mode"] == "start":
                a += delta
            else:
                b += delta
            ly = self._lane_y.get(clip["track_id"], d["y"])
            canvas.create_rectangle(editor._t2x(a, g), ly + 4, editor._t2x(b, g), ly + LANE_H - 4,
                                    outline="white", dash=(3, 2), tags="montage-drag")
            editor.status(f"{'inicio' if d['mode'] == 'start' else 'fin'} {delta:+.2f} s · "
                          f"clip de {b - a:.1f} s")
        return True

    def _magnet_delta(self, d, g):
        clip = montaje.clip_by_id(self.doc, d["ids"][0])
        raw = d["t1"] - d["t0"]
        snapped = self._magnet(clip["seq_ini"] + raw, g)
        return max(-clip["seq_ini"], snapped - clip["seq_ini"])

    def _drop_track(self, d):
        track = self._track_at(d.get("y1", d["y0"]))
        if track is None:
            top = self._lane_y and min(self._lane_y.items(), key=lambda kv: kv[1])
            if top and d.get("y1", d["y0"]) < top[1]:
                return top[0]                   # por encima de todo: la pista vacía de arriba
            return d["track"]
        return track

    def _release(self, e, g):
        d, self.drag = self.drag, None
        if not d:
            return True
        editor = self.w.editor
        editor.tl.delete("montage-drag")
        moved = abs(d.get("x1", d["x0"]) - d["x0"]) >= CLICK_PX or abs(d.get("y1", d["y0"]) - d["y0"]) >= LANE_H / 2
        try:
            if d["kind"] == "move" and moved:
                delta = self._magnet_delta(d, g)
                target = self._drop_track(d)
                if len(d["ids"]) == 1:
                    cid = d["ids"][0]
                    clip = montaje.clip_by_id(self.doc, cid)
                    new_doc, _ = montaje.move(self.doc, cid, clip["seq_ini"] + delta, target)
                    self.transact("mover clip", lambda: self.save(new_doc))
                else:
                    first = montaje.clip_by_id(self.doc, d["ids"][0])
                    track_delta = montaje.track_number(target) - montaje.track_number(first["track_id"])
                    new_doc, _ = montaje.shift_clips(self.doc, d["ids"], delta, track_delta)
                    self.transact(f"mover {len(d['ids'])} clips", lambda: self.save(new_doc))
            elif d["kind"] == "move" and not moved and len(self.selection) > 1:
                self.select([self.selected])
            elif d["kind"] == "edge" and moved:
                new_doc, _ = montaje.trim_edge(self.doc, d["clip"], d["mode"], d["t1"] - d["t0"])
                self.transact("recortar clip", lambda: self.save(new_doc))
        except Exception as error:
            editor.status(f"⚠ no se guardó: {error}")
        self._redraw()
        self.sync_detail()
        return True

    # ---- operaciones ----
    def _split_at(self, clip, seq_t):
        new_doc, parts = montaje.split(self.doc, clip["clip_id"], seq_t)
        self.transact("dividir clip", lambda: self.save(new_doc))
        self.select([parts[1]["clip_id"]])
        self.w.editor.status("clip dividido")

    def add_range(self, source_ini, source_fin, *, label="", topic_ids=None, origin="user"):
        doc = copy.deepcopy(self.ensure_doc())
        new_doc, clip = montaje.add_clip(doc, source_ini, source_fin, origin=origin, label=label,
                                         topic_ids=topic_ids or [], edited=True)
        self.transact("añadir clip", lambda: self.save(new_doc))
        self.select([clip["clip_id"]])
        return clip

    def add_selection(self):
        """Ctrl+Shift+A (plan §7.2): el item de capa seleccionado en modo Fuente (todos
        sus tramos), o el rango a repetir, al final de V1."""
        layers = self.w.layers
        ranges, label, topics = [], "", []
        if self.mode == "source" and layers.selected and layers.selected[1]:
            try:
                layer, item = layers.find(*layers.selected[:2])
            except StopIteration:
                item = None
            if item:
                ranges = [(r["t_ini"], r["t_fin"]) for r in item["ranges"]]
                label = item["label"]
                if layer.get("kind") == "topics" or str(layer["layer_id"]).startswith("topics:"):
                    topics = [item["item_id"]]
        if not ranges and self.w.editor.loop:
            ranges = [tuple(self.w.editor.loop)]
            label = "rango"
        if not ranges:
            raise ValueError("selecciona un item en modo Fuente o define un rango a repetir en la regla")
        added = 0
        for a, b in ranges:
            if b - a >= montaje.MIN_CLIP_SECONDS:
                self.add_range(a, b, label=label, topic_ids=topics)
                added += 1
        self.w.editor.status(f"{added} clip(s) añadidos al final de V1 ({label})")
        return True

    def add_topic(self):
        """Ctrl+Shift+T: el tema seleccionado (todos sus rangos, en orden)."""
        layers = self.w.layers
        if not (layers.selected and layers.selected[1] and str(layers.selected[0]).startswith("topics:")):
            raise ValueError("selecciona un tema o subtema en el carril de temas")
        return self.add_selection()

    def reveal_source(self):
        clip = self._selected_clip()
        self.w.set_mode("source")
        self.w.editor._mover_playhead(clip["source_ini"])
        self.w.editor.status(f"fuente del clip {clip['label'] or clip['clip_id']}: "
                             f"{format_time(clip['source_ini'])[:-4]}–{format_time(clip['source_fin'])[:-4]}")
        return True

    def edges_index(self):
        return editorial_nav.EdgeIndex(montaje.clip_edges(self.doc) if self.doc else [])

    def action(self, action, _e=None):
        """Acciones del keymap en modo Montaje (el dueño llama primero aquí)."""
        editor = self.w.editor
        if action == "montage.add_selection":
            return self.add_selection()
        if action == "montage.add_topic":
            return self.add_topic()
        if self.mode != "montage":
            return False
        if action.startswith("marks."):
            editor.status("las marcas del autor se editan en modo Fuente")
            return True
        if action in ("edit.undo", "edit.redo", "tools.cut", "tools.select", "tools.select_all",
                      "layers.new_lane"):
            if action == "tools.select_all" and self.doc:
                self.select([c["clip_id"] for c in self.doc["clips"]])
                self._redraw()
                return True
            if action == "layers.new_lane":
                editor.status("las capas se añaden en modo Fuente")
                return True
            return self.w.layers.action(action, _e)
        if action == "edit.deselect":
            self.clear_selection()
            self._redraw()
            self.sync_detail()
            return True
        if action in ("nav.prev_edge", "nav.next_edge", "nav.prev_silence", "nav.next_silence"):
            index = self.edges_index()
            t = index.next(editor.t_play) if action.endswith("next_edge") or action.endswith("next_silence") \
                else index.prev(editor.t_play)
            if t is None:
                editor.status("no hay más bordes de clip en esa dirección")
            else:
                editor._mover_playhead(t)
            return True
        if action == "view.skip_trims":
            editor.status("«Saltar recortes» actúa sobre la fuente; el montaje ya no los tiene")
            return True
        if action == "montage.reveal_source":
            return self.reveal_source()
        if action == "montage.export":
            self.w._export_montage()
            return True
        if action in ("edit.item_prev", "edit.item_next"):
            clips = montaje.ordered_clips(self.doc) if self.doc else []
            if not clips:
                raise ValueError("no hay clips en el montaje")
            ids = [c["clip_id"] for c in clips]
            if self.selected in ids:
                index = ids.index(self.selected) + (1 if action.endswith("next") else -1)
                if not 0 <= index < len(ids):
                    editor.status("no hay más clips")
                    return True
            else:
                index = 0 if action.endswith("next") else len(ids) - 1
            self.select([ids[index]])
            editor._mover_playhead(clips[index]["seq_ini"])
            self._redraw()
            self.sync_detail()
            return True
        if not self.selected and action in ("nav.sel_start", "nav.sel_end", "view.zoom_sel",
                                            "transport.play_from_item"):
            return False
        if action in ("nav.sel_start", "nav.sel_end", "view.zoom_sel", "transport.play_from_item"):
            clip = self._selected_clip()
            a, b = clip["seq_ini"], montaje.seq_fin(clip)
            if action == "nav.sel_start":
                editor._mover_playhead(a)
            elif action == "nav.sel_end":
                editor._mover_playhead(b)
            elif action == "view.zoom_sel":
                editor.zoom_a(a, b)
            else:
                editor._play(reiniciar=True, desde=a)
            return True
        handlers = {
            "edit.split": self._split_selected,
            "edit.delete": self._delete_selected,
            "edit.accept": lambda: self._state("accepted"),
            "edit.accept_next": lambda: (self._state("accepted"), self.action("edit.item_next"))[1],
            "edit.toggle": lambda: self._state("disabled"),
            "edit.activate": lambda: self._state("proposed"),
            "edit.trim_start": lambda: self._trim_to_playhead("start"),
            "edit.trim_end": lambda: self._trim_to_playhead("end"),
            "edit.nudge_prev": lambda: self._nudge(-1),
            "edit.nudge_next": lambda: self._nudge(+1),
            "edit.nudge_prev_10": lambda: self._nudge(-10),
            "edit.nudge_next_10": lambda: self._nudge(+10),
            "edit.edit": self.edit_dialog,
            "montage.move_up": lambda: self._move_track(+1),
            "montage.move_down": lambda: self._move_track(-1),
        }
        if len(self.selection) > 1 and action in ("nav.step_prev", "nav.step_next", "nav.step_prev_5",
                                                   "nav.step_next_5"):
            frames = (-1 if "prev" in action else 1) * (10 if action.endswith("_5") else 1)
            handlers[action] = lambda f=frames: self._nudge(f)
        if action in handlers:
            handlers[action]()
            return True
        return False

    def _split_selected(self):
        editor = self.w.editor
        t = editor.t_play
        clip = None
        if self.selected and self.doc:
            candidate = montaje.clip_by_id(self.doc, self.selected)
            if candidate["seq_ini"] < t < montaje.seq_fin(candidate):
                clip = candidate
        if clip is None:
            piece = self.map().to_source(t) if self.doc else None
            if piece is None:
                raise ValueError("el playhead no está sobre un clip")
            clip = montaje.clip_by_id(self.doc, piece["clip_id"])
        self._split_at(clip, t)

    def _delete_selected(self):
        ids = list(self.selection) or ([self.selected] if self.selected else [])
        if not ids:
            raise ValueError("selecciona un clip del montaje")
        doc = self.doc
        for cid in sorted(ids, key=lambda i: -montaje.clip_by_id(doc, i)["seq_ini"]):
            doc, _ = montaje.remove(doc, cid)
        new_doc = doc
        self.transact(f"borrar {len(ids)} clip(s)", lambda: self.save(new_doc))
        self.clear_selection()
        self.w.editor.status(f"{len(ids)} clip(s) borrado(s); los siguientes cierran el hueco")

    def _state(self, state):
        ids = list(self.selection) or ([self.selected] if self.selected else [])
        if not ids:
            raise ValueError("selecciona un clip del montaje")
        new_doc, touched = montaje.set_state(self.doc, ids, state)
        if touched:
            verb = {"disabled": "desactivar", "accepted": "aceptar", "proposed": "activar"}[state]
            self.transact(f"{verb} {len(touched)} clip(s)", lambda: self.save(new_doc))
        return True

    def _trim_to_playhead(self, edge):
        clip = self._selected_clip()
        t = self.w.editor.t_play
        delta = t - (clip["seq_ini"] if edge == "start" else montaje.seq_fin(clip))
        new_doc, _ = montaje.trim_edge(self.doc, clip["clip_id"], edge, delta)
        self.transact("recortar " + ("inicio" if edge == "start" else "fin"), lambda: self.save(new_doc))

    def _nudge(self, frames):
        ids = list(self.selection) or ([self.selected] if self.selected else [])
        if not ids:
            raise ValueError("selecciona un clip del montaje")
        fps = (self.w.info.get("video") or {}).get("fps") if self.w.info else None
        delta = frames * editorial_nav.frame_step(fps)
        new_doc, moved = montaje.shift_clips(self.doc, ids, delta)
        self.transact(f"empujar {len(ids)} clip(s)", lambda: self.save(new_doc))
        self.w.editor._mover_playhead(min(c["seq_ini"] for c in moved))

    def _move_track(self, delta):
        ids = list(self.selection) or ([self.selected] if self.selected else [])
        if not ids:
            raise ValueError("selecciona un clip del montaje")
        new_doc, _ = montaje.shift_clips(self.doc, ids, 0.0, delta)
        self.transact("cambiar de pista", lambda: self.save(new_doc))

    # ---- menú contextual y diálogo ----
    def supported_actions(self):
        if self.mode != "montage":
            return frozenset({"montage.add_selection", "montage.add_topic", "view.mode_montage"})
        return frozenset({
            "edit.undo", "edit.redo", "edit.split", "edit.trim_start", "edit.trim_end", "edit.nudge_prev",
            "edit.nudge_next", "edit.nudge_prev_10", "edit.nudge_next_10", "edit.item_prev", "edit.item_next",
            "edit.accept", "edit.accept_next", "edit.toggle", "edit.activate", "edit.delete", "edit.edit",
            "edit.deselect", "tools.cut", "tools.select", "tools.select_all", "nav.prev_edge", "nav.next_edge",
            "nav.sel_start", "nav.sel_end", "view.zoom_sel", "transport.play_from_item", "view.mode_montage",
            "montage.reveal_source", "montage.move_up", "montage.move_down", "montage.export",
            "montage.add_selection", "montage.add_topic"})

    def menu_items(self, e, menu):
        editor = self.w.editor
        hit = editor._carril_en(e.y) if e is not None else None
        g = editor._tl_geo()
        if not hit or not g or not hit[0]["nombre"].startswith(LANE_PREFIX) or hit[0]["nombre"] == TOPICS_LANE:
            return False
        track = hit[0]["nombre"][len(LANE_PREFIX):]
        found = self.hit(track, e.x, g)
        if not found:
            menu.add_command(label=f"Pista {track} (montaje)", state="disabled")
            return True
        clip = found[0]
        if clip["clip_id"] not in self.selection:
            self.select([clip["clip_id"]])
        else:
            self.selected = clip["clip_id"]
        self._redraw()
        self.sync_detail()
        import keymap
        menu.add_command(label=f"{track} · {clip['label'] or clip['clip_id']}", state="disabled")
        for label, action in (("Ver en la fuente", "montage.reveal_source"),
                              ("Editar etiqueta y motivo", "edit.edit"),
                              ("Aceptar", "edit.accept"), ("Activar", "edit.activate"),
                              ("Desactivar (no se reproduce ni exporta)", "edit.toggle"),
                              ("Dividir en el playhead", "edit.split"),
                              ("Subir de pista", "montage.move_up"), ("Bajar de pista", "montage.move_down"),
                              ("Borrar (los siguientes cierran el hueco)", "edit.delete")):
            menu.add_command(label=label, accelerator=keymap.pretty_chords(action),
                             command=lambda a=action: editor.ejecutar(a))
        return True

    def edit_dialog(self):
        import customtkinter as ctk
        from tkinter import messagebox
        clip = self._selected_clip()
        win = ctk.CTkToplevel(self.w.f)
        win.title("Editar clip del montaje")
        win.geometry("520x330")
        win.transient(self.w.f.winfo_toplevel())
        ctk.CTkLabel(win, text="Etiqueta").pack(anchor="w", padx=15, pady=(12, 0))
        label = ctk.CTkEntry(win)
        label.insert(0, clip["label"])
        label.pack(fill="x", padx=15)
        ctk.CTkLabel(win, text="Motivo / nota de junta").pack(anchor="w", padx=15, pady=(8, 0))
        reason = ctk.CTkTextbox(win, height=90)
        reason.insert("1.0", clip.get("reason") or "")
        reason.pack(fill="x", padx=15)
        ctk.CTkLabel(win, text=f"Fuente {format_time(clip['source_ini'])[:-4]}–{format_time(clip['source_fin'])[:-4]}"
                               f" · {clip['origin']} · pista {clip['track_id']}",
                     text_color="gray60").pack(anchor="w", padx=15, pady=(8, 0))
        state = ctk.CTkOptionMenu(win, values=["Propuesto", "Aceptado", "Desactivado"])
        state.set(dict(zip(montaje.STATES, state.cget("values")))[clip["state"]])
        state.pack(pady=8)

        def close():
            try:
                win.destroy()
            finally:
                try:
                    self.w.editor.tl.focus_set()
                except tk.TclError:
                    pass

        def save(_e=None):
            try:
                new_doc, _ = montaje.update_clip(
                    self.doc, clip["clip_id"], label=label.get().strip(), reason=reason.get("1.0", "end-1c"),
                    state=montaje.STATES[state.cget("values").index(state.get())])
                if new_doc["clips"] != self.doc["clips"]:
                    self.transact("editar clip", lambda: self.save(new_doc))
                close()
            except Exception as error:
                messagebox.showerror("No se guardó", str(error), parent=win)
            return "break"
        ctk.CTkButton(win, text="Guardar (Ctrl+Enter · Esc)", command=save).pack(pady=8)
        win.bind("<Escape>", save)
        win.bind("<Control-Return>", save)
        win.protocol("WM_DELETE_WINDOW", save)
        return True
