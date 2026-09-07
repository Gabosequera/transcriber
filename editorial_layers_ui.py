"""Un solo editor de gestos y comentarios para capas propias y documentos heredados."""
from __future__ import annotations

import copy
import bisect
import tkinter as tk
from tkinter import font as tkfont
from tkinter import messagebox

import customtkinter as ctk
import editorial_chunks
import editorial_edits as edits
import editorial_history
import editorial_layers as layers
import editorial_trims
from editorial_io import digest_json, parse_time, format_time


def _font(size, **options):
    """Copia de la fuente por defecto de Tk con otro tamaño/peso. La barra dibuja en
    un canvas crudo (como el timeline), así que no pasa por el escalado de CTk."""
    font = tkfont.nametofont("TkDefaultFont").copy()
    font.configure(size=size, **options)
    return font


def _rounded(canvas, x1, y1, x2, y2, radius, *, fill, outline):
    """Rectángulo redondeado con primitivas exactas (el canvas no tiene una propia y
    el polígono suavizado deja el borde irregular): relleno en cruz + 4 sectores,
    borde de 4 arcos + 4 líneas de 1 px."""
    radius = min(radius, (x2 - x1) / 2, (y2 - y1) / 2)
    d = 2 * radius
    corners = ((x1, y1, 90), (x2 - d, y1, 0), (x2 - d, y2 - d, 270), (x1, y2 - d, 180))
    canvas.create_rectangle(x1 + radius, y1, x2 - radius, y2, fill=fill, width=0)
    canvas.create_rectangle(x1, y1 + radius, x2, y2 - radius, fill=fill, width=0)
    for cx, cy, start in corners:
        canvas.create_arc(cx, cy, cx + d, cy + d, start=start, extent=90, style="pieslice",
                          fill=fill, width=0)
    for cx, cy, start in corners:
        canvas.create_arc(cx, cy, cx + d, cy + d, start=start, extent=90, style="arc",
                          outline=outline)
    canvas.create_line(x1 + radius, y1, x2 - radius, y1, fill=outline)
    canvas.create_line(x1 + radius, y2, x2 - radius, y2, fill=outline)
    canvas.create_line(x1, y1 + radius, x1, y2 - radius, fill=outline)
    canvas.create_line(x2, y1 + radius, x2, y2 - radius, fill=outline)


class LayerDetailBar:
    """Barra de detalle del item de capa: UNA línea de altura constante bajo el
    timeline (fila libre del editor) con el item bajo el mouse o, si no hay, el
    seleccionado — color de origen/capa, capa, etiqueta, estado, tramos y el
    comentario para la AI recortado con «…» al ancho real. Dibuja en un canvas
    propio, así que cambiar el texto nunca altera el layout (antes el detalle iba
    al status del pie, que hacía wrap y movía timeline y preview con cada hover)."""

    COLORS = dict(bg="#111513", surface="#181d1a", raised="#202622", border="#303833",
                  muted="#8b9790", text="#eef3ef", text_soft="#c6cec9")
    STATES = {"proposed": ("PROPUESTO", "#c9974e"), "accepted": ("ACEPTADO", "#35a978"),
              "disabled": ("DESACTIVADO", "#8b9790")}
    ORIGINS = {"silence": "#527cad", "ai": "#9471bd", "user": "#c58e43"}
    HINT = "doble click edita · X activa/desactiva · Supr borra"
    EMPTY = ("Capas · pasa el mouse por un item del timeline para ver su detalle · "
             "arrastra en un carril para crear uno")

    def __init__(self, parent, *, row, colors=None, column=0, padx=4, pady=(0, 4)):
        self.colors = {**self.COLORS, **(colors or {})}
        self.f_label = _font(10, weight="bold")
        self.f_text = _font(10)
        self.f_small = _font(8)
        self.f_badge = _font(8, weight="bold")
        self.f_italic = _font(10, slant="italic")
        self.height = max(30, self.f_label.metrics("linespace") + 14)
        self.canvas = tk.Canvas(parent, height=self.height, bg=self.colors["bg"],
                                highlightthickness=0)
        self.canvas.grid(row=row, column=column, sticky="ew", padx=padx, pady=pady)
        self.canvas.bind("<Configure>", lambda e: self._draw())
        self._key = None
        self._current = None

    def show(self, layer, item, *, selected=False):
        key = (layer["layer_id"], item["item_id"], item["state"], item["label"], item["comment"],
               item.get("origin"), item.get("parent_id"),
               tuple((r["t_ini"], r["t_fin"]) for r in item["ranges"]), bool(selected))
        if key != self._key:
            self._key = key
            self._current = (layer, item, bool(selected))
            self._draw()

    def clear(self):
        if self._key is not None:
            self._key = self._current = None
            self._draw()

    def _draw(self):
        canvas, colors = self.canvas, self.colors
        canvas.delete("all")
        width, height = canvas.winfo_width(), self.height
        if width < 40:
            return
        _rounded(canvas, .5, .5, width - .5, height - .5, 6,
                 fill=colors["surface"], outline=colors["border"])
        cy = height / 2
        x = 12
        if self._current is None:
            self._text(x, cy, self.EMPTY, self.f_text, colors["muted"], width - x - 12)
            return
        layer, item, selected = self._current
        color = self.ORIGINS.get(item.get("origin")) or layer.get("color") or colors["muted"]
        canvas.create_oval(x, cy - 5, x + 10, cy + 5, fill=color,
                           outline="#ffffff" if selected else color, width=2 if selected else 1)
        x += 18
        x = self._text(x, cy, layer["name"].upper(), self.f_small, colors["muted"], width * .18) + 8
        label = ("↳ " if item.get("parent_id") else "") + (item["label"] or "(sin etiqueta)")
        x = self._text(x, cy, label, self.f_label, colors["text"], width * .3) + 10
        badge, badge_color = self.STATES.get(item["state"], (str(item["state"]).upper(), colors["muted"]))
        badge_width = self.f_badge.measure(badge) + 14
        _rounded(canvas, x, cy - 8, x + badge_width, cy + 8, 8,
                 fill=colors["raised"], outline=badge_color)
        canvas.create_text(x + badge_width / 2, cy, text=badge, fill=badge_color, font=self.f_badge)
        x += badge_width + 10
        x = self._text(x, cy, layers.ranges_summary(item["ranges"]), self.f_text,
                       colors["muted"], width * .3) + 12
        right = width - 12
        hint_width = self.f_small.measure(self.HINT)
        if right - x > hint_width + 180:
            canvas.create_text(right, cy, text=self.HINT, anchor="e", fill=colors["muted"],
                               font=self.f_small)
            right -= hint_width + 16
        comment = " ".join((item["comment"] or "").split())
        if comment:
            self._text(x, cy, comment, self.f_text, colors["text_soft"], right - x)
        else:
            self._text(x, cy, "sin comentario para la AI", self.f_italic, colors["muted"], right - x)

    def _text(self, x, cy, text, font, fill, max_width):
        text = layers.elide(font.measure, text, max_width)
        if not text:
            return x
        self.canvas.create_text(x, cy, text=text, anchor="w", fill=fill, font=font)
        return x + font.measure(text)


class LayersController:
    def __init__(self, workspace):
        self.w = workspace
        self.store = None
        self.selected = None  # layer_id, item_id, segment
        self.drag = None
        self.window = None
        self.detail = None    # LayerDetailBar del dueño (opcional)
        self._cache_key = None
        self._cache = []
        self._draw_indexes = {}
        # deshacer/rehacer (diseño §4): pila de operaciones por medio cargado
        self.history = editorial_history.HistoryStack()

    # ---- documentos: snapshot / revisión / restauración (diseño §4) ----
    # doc_id ∈ {"autor", "trims", "plan", "layer:<id>"} (Fase 7 añade "lanes").
    def _doc_snapshot(self, doc):
        w = self.w
        if doc == "autor":
            reg = w.editor.reg
            return copy.deepcopy(reg.marcas) if reg is not None else None
        if doc == "trims":
            return copy.deepcopy(w.trims)
        if doc == "plan":
            return copy.deepcopy(w.plan)
        if doc.startswith("layer:"):
            layer = self.store.layers.get(doc[6:]) if self.store else None
            return copy.deepcopy(layer)
        raise ValueError(f"documento desconocido: {doc}")

    def _doc_revision(self, doc):
        """«Revisión» que el historial compara para detectar cambios por fuera: el
        digest del CONTENIDO del documento sin sus campos volátiles (`revision`,
        `updated_at`). Un número de revisión no sirve: deshacer la entrada N sube la
        revisión y dejaría inválida a la N−1, cuyo `after` es el mismo contenido."""
        snapshot = self._doc_snapshot(doc)
        if snapshot is None:
            return None
        if isinstance(snapshot, dict):
            snapshot = {k: v for k, v in snapshot.items()
                        if k not in ("revision", "updated_at") and not (k == "deleted" and not v)}
        return digest_json(snapshot)

    def _doc_restore(self, doc, snapshot):
        """Restaura un documento POR SU CAMINO DE GUARDADO (nunca escribiendo archivos
        a mano): la revisión sube y las protecciones siguen valiendo."""
        w = self.w
        if doc == "autor":
            w.editor.reg.reemplazar(snapshot or [])
        elif doc == "trims":
            if snapshot is None:
                raise ValueError("los recortes no existían")
            document = copy.deepcopy(snapshot)
            editorial_trims.save_document(w.trims_path, document)
            w.trims = document
            w._review_stale = True
            w._reindex_trims()
            w._refresh_trims_status()
        elif doc == "plan":
            if snapshot is None:
                raise ValueError("el plan no existía; su importación no se deshace")
            plan = editorial_chunks.apply_plan(self.store.root, w._master_path(),
                                               copy.deepcopy(snapshot), persist_selection=True)
            w.plan = plan
        elif doc.startswith("layer:"):
            identifier = doc[6:]
            current = self.store.layers.get(identifier)
            if snapshot is None:               # la capa no existía: tumba persistente
                if current is not None:
                    self.store.save({**current, "deleted": True})
                return
            value = copy.deepcopy(snapshot)
            if current is not None and current.get("deleted") and not value.get("deleted"):
                value["deleted"] = False       # deshacer un borrado: la tumba se levanta
            if current is not None:            # la revisión nunca retrocede
                value["revision"] = max(int(value.get("revision", 0)), int(current.get("revision", 0)))
            self.store.save(value)
        else:
            raise ValueError(f"documento desconocido: {doc}")

    def transact(self, label, doc_ids, fn):
        """Ejecuta `fn()` (que escribe por los caminos de siempre) y registra la
        operación en el historial con los snapshots de antes y después de los
        documentos `doc_ids`. Si `fn` falla no se registra nada."""
        doc_ids = list(dict.fromkeys(doc_ids))
        before = {doc: self._doc_snapshot(doc) for doc in doc_ids}
        result = fn()
        after = {doc: self._doc_snapshot(doc) for doc in doc_ids}
        self.history.record(label, before, after, {doc: self._doc_revision(doc) for doc in doc_ids})
        return result

    def _lane_doc(self, lid):
        return {"autor": "autor", "recortes": "trims", "bloques": "plan"}.get(lid, f"layer:{lid}")

    def _after_write(self):
        """Tras escribir o restaurar: la selección solo sobrevive si el item existe."""
        if self.selected and self.selected[1]:
            try:
                _, item = self.find(*self.selected[:2])
            except StopIteration:
                item = None
            if item is None:
                self.selected = None
        self.snapshot()
        self.w._refresh_plan_buttons()
        self.w.editor.refrescar_layout()
        self.sync_detail()

    def undo(self, *, redo=False):
        """Ctrl+Z / Ctrl+R: restaura `before` (o `after`) por los caminos de guardado.
        Si un documento cambió por fuera, la entrada se descarta con aviso."""
        editor = self.w.editor
        if self.w.worker and self.w.worker.is_alive():
            editor.status("espera a que termine la operación del proyecto")
            return True
        stack = self.history
        entry = stack.peek_redo() if redo else stack.peek_undo()
        verb = "rehacer" if redo else "deshacer"
        if entry is None:
            editor.status(f"nada que {verb}")
            return True
        revisions = {doc: self._doc_revision(doc) for doc in entry.docs}
        try:
            entry = stack.redo(revisions) if redo else stack.undo(revisions)
        except editorial_history.Stale as error:
            editor.status(f"⚠ no se puede {verb} {error}; la entrada se descarta")
            return True
        snapshots = entry.after if redo else entry.before
        try:
            for doc, snapshot in snapshots.items():
                self._doc_restore(doc, snapshot)
        except Exception as error:
            stack.discard(entry)
            self._after_write()
            editor.status(f"✗ no se pudo {verb} «{entry.label}»: {error}")
            return True
        stack.settle(entry, {doc: self._doc_revision(doc) for doc in entry.docs})
        self._after_write()
        editor.status(("Rehecho: " if redo else "Deshecho: ") + entry.label)
        return True

    def all(self):
        if not self.store:
            return []
        reg = self.w.editor.reg
        key=(id(self.store),id(self.w.plan),id(self.w.trims),
             self.w.trims.get('revision') if self.w.trims else None,
             id(reg),reg.revision if reg else None,tuple(sorted(self.store.stamps.items())))
        if key != self._cache_key:
            self._cache_key=key
            self._cache=layers.adapters(self.store.master, plan=self.w.plan, trims=self.w.trims,
                                       marks=reg.marcas if reg else []) + self.store.visible()
            self._draw_indexes = {}
            for layer in self._cache:
                entries=sorted(((r['t_ini'],r['t_fin'],item,index)
                    for item in layer['items'] for index,r in enumerate(item['ranges'])),key=lambda r:r[0])
                maximum=-1
                ends=[]
                for _,end,_,_ in entries:
                    maximum=max(maximum,end)
                    ends.append(maximum)
                self._draw_indexes[layer['layer_id']]=(entries,[r[0] for r in entries],ends)
        return self._cache

    def visible_parts(self, layer, start, end):
        entries,starts,ends=self._draw_indexes[layer['layer_id']]
        return [(item,index) for a,b,item,index in entries[
            bisect.bisect_left(ends,start):bisect.bisect_right(starts,end)] if b>=start]

    def snapshot(self):
        return layers.write_snapshot(self.store.root, self.store.master, self.all(),master_digest=self.store.source_digest)

    def lanes(self):
        return [dict(nombre=layer["layer_id"], alto=34,
                     dibujar=lambda c, g, y, l=layer: self.draw(l, c, g, y),
                     gesto=lambda phase, e, g, y, lid=layer["layer_id"]: self.gesture(lid, phase, e, g, y))
                for layer in self.all()]

    def find(self, lid, iid=None):
        layer = next(l for l in self.all() if l["layer_id"] == lid)
        return layer, copy.deepcopy(next((i for i in layer["items"] if i["item_id"] == iid), None))

    def draw(self, layer, canvas, g, y):
        editor = self.w.editor
        start, span = editor.view
        canvas.create_rectangle(g[0], y, sum(g), y + 34, fill="#191f1c", outline="#343c37")
        parts=self.visible_parts(layer,start,start+span)
        occupied=set()
        for item, index in parts:
            selected = self.selected and self.selected[:2] == (layer["layer_id"], item["item_id"])
            for part in [item["ranges"][index]]:
                if part["t_fin"] < start or part["t_ini"] > start + span:
                    continue
                a, b = [editor._t2x(max(start, min(start + span, part[k])), g) for k in ("t_ini", "t_fin")]
                b = max(a + 3, b)
                color = ({"silence": "#527cad", "ai": "#9471bd", "user": "#c58e43"}.get(item.get("origin"))
                         or layer["color"])
                # LOD por píxel: nunca oculta la selección, ni multiplica miles de
                # rectángulos indistinguibles cuando se ve un VOD completo.
                if len(parts)>400 and not selected:
                    key=(int(a),int(b),color,item['state'])
                    if key in occupied:
                        continue
                    occupied.add(key)
                accepted = item["state"] == "accepted"
                canvas.create_rectangle(a, y + 12, b, y + 31,
                    fill=color if item["state"] != "disabled" else "",
                    outline="#ffffff" if selected else "#35a978" if accepted else color,
                    width=2 if selected or accepted else 1,
                    dash=(3, 2) if item["state"] == "disabled" else ())
                if b - a > 45:
                    canvas.create_text(a + 4, y + 21, anchor="w", fill="#ffffff",
                        text=("↳ " if item.get("parent_id") else "") + item["label"][:int((b-a)/7)],
                        font=("TkDefaultFont", 8))
                if selected:
                    for x in (a, b):
                        canvas.create_line(x, y + 11, x, y + 32, fill="white", width=3)
        canvas.create_text(g[0] + 4, y + 1, text=layer["name"], anchor="nw", fill="#aab6af",
                           font=("TkDefaultFont", 8))

    def hit(self, lid, x, g):
        editor = self.w.editor
        layer, _ = self.find(lid)
        hits = []
        for item in layer["items"]:
            for index, part in enumerate(item["ranges"]):
                a, b = editor._t2x(part["t_ini"], g), editor._t2x(part["t_fin"], g)
                if a - 5 <= x <= b + 5:
                    mode = "start" if abs(x-a) <= 6 else "end" if abs(x-b) <= 6 else "move"
                    hits.append((item, index, mode))
        if self.selected:
            active = next((h for h in hits if h[0]["item_id"] == self.selected[1]), None)
            if active:
                return active
        return hits[-1] if hits else None

    def gesture(self, lid, phase, e, g, y):
        if not self.store or (self.w.worker and self.w.worker.is_alive()):
            return True
        editor = self.w.editor
        t = max(0, min(self.store.master["media"]["duration"], editor._x2t(e.x, g)))
        if phase in ("press", "doble"):
            hit = self.hit(lid, e.x, g)
            self.selected = (lid, hit[0]["item_id"], hit[1]) if hit else (lid, None, 0)
            self.w.sel_cut = None
            editor._seleccionar(None)
            self.drag = dict(lid=lid, t=t, now=t, hit=copy.deepcopy(hit))
            if phase == "doble":
                self.edit_dialog()
            editor.redibujar()
            self.sync_detail()
            return True
        if phase == "motion" and self.drag:
            self.drag["now"] = t
            editor.status(f"{format_time(self.drag['t'])} → {format_time(t)} · suelta para guardar")
            canvas = editor.tl
            canvas.delete("layer-drag")
            canvas.create_rectangle(editor._t2x(min(t, self.drag["t"]), g), y + 10,
                                    editor._t2x(max(t, self.drag["t"]), g), y + 33,
                                    outline="white", dash=(3, 2), tags="layer-drag")
            return True
        if phase == "release" and self.drag:
            drag, self.drag = self.drag, None
            if abs(drag["now"] - drag["t"]) < .05:
                return True
            try:
                if drag["hit"]:
                    item, index, mode = drag["hit"]
                    part = item["ranges"][index]
                    delta = drag["now"] - drag["t"]
                    if mode == "move":
                        delta = max(-part["t_ini"], min(delta, self.store.master["media"]["duration"]-part["t_fin"]))
                        part["t_ini"] += delta
                        part["t_fin"] += delta
                    else:
                        part["t_ini" if mode == "start" else "t_fin"] = drag["now"]
                    self.persist(lid, item, label="mover item" if mode == "move" else "estirar item")
                else:
                    self.persist(lid, layers.new_item(min(drag["t"], drag["now"]), max(drag["t"], drag["now"])),
                                 create=True, label="crear item")
                    self.edit_dialog()
            except Exception as error:
                messagebox.showerror("No se guardó el rango", str(error), parent=self.w.f)
            editor.redibujar()
            return True
        return True

    def persist(self, lid, item, *, create=False, delete=False, label=None):
        """ÚNICO punto de escritura de un item (crear/editar/borrar) con las
        validaciones de siempre; registra la operación en el historial (§4)."""
        if self.w.worker and self.w.worker.is_alive():
            raise ValueError("espera a que termine la operación del proyecto")
        if label is None:
            label = ("crear" if create else "borrar" if delete else "editar") + " item"
        return self.transact(label, [self._lane_doc(lid)],
                             lambda: self._persist(lid, item, create=create, delete=delete))

    def _persist(self, lid, item, *, create=False, delete=False):
        # Trabajar sobre copias; el guardado fallido no modifica los documentos vivos.
        editor = self.w.editor
        layers.validate_items([dict(item, parent_id=None)], self.store.master["media"]["duration"], allow_points=lid=="autor")
        item = copy.deepcopy(item)
        item["edited"] = True
        if lid in ("autor", "recortes", "bloques") and len(item["ranges"]) != 1:
            raise ValueError("esta capa admite un rango por item")
        if lid == "autor":
            reg = editor.reg
            mark = next((m for m in reg.marcas if m["id"] == item["item_id"]), None)
            a, b = item["ranges"][0]["t_ini"], item["ranges"][0]["t_fin"]
            decision = {"accepted": "incluir", "disabled": "excluir"}.get(item["state"])
            if delete:
                reg.borrar(mark)
            elif create:
                mark = reg.agregar_region(a, b, decision=decision, prompt=item["comment"])
                reg.editar(mark,label=item['label'])
                item["item_id"] = mark["id"]
            elif a == b:
                if decision is not None:
                    reg.a_region(mark)
                    reg.editar(mark,prompt=item["comment"],label=item["label"],decision=decision)
                else:
                    reg.editar(mark, t=a, prompt=item["comment"], label=item["label"])
            else:
                reg.editar(mark, tipo="region", t_ini=a, t_fin=b, decision=decision,
                           prompt=item["comment"], label=item["label"])
        elif lid == "recortes":
            document = copy.deepcopy(self.w.trims)
            cut = next((c for c in document["cuts"] if c["cut_id"] == item["item_id"]), None)
            if delete:
                document["cuts"].remove(cut)
            else:
                part = item["ranges"][0]
                if create:
                    cut = editorial_trims.add_cut(document, part["t_ini"], part["t_fin"])
                    item["item_id"] = cut["cut_id"]
                cut.update(**part, reason=item["comment"], enabled=item["state"] != "disabled",
                           accepted=item["state"] == "accepted", edited=True)
            editorial_trims.save_document(self.w.trims_path, document)
            self.w.trims = document
            self.w._review_stale = True
            self.w._reindex_trims()
            self.w._refresh_trims_status()
        elif lid == "bloques":
            if create or delete or item["state"] == "disabled":
                raise ValueError("los bloques deben cubrir el medio; usa Revisar chunks para dividir el plan")
            plan = copy.deepcopy(self.w.plan)
            index = next(i for i,c in enumerate(plan["chunks"]) if c["chunk_id"] == item["item_id"])
            chunk = plan["chunks"][index]
            chunk.update(**item["ranges"][0], title=item["label"], comment=item["comment"], edited=True)
            if index > 0:
                plan["chunks"][index-1]["t_fin"] = chunk["t_ini"]
            if index + 1 < len(plan["chunks"]):
                plan["chunks"][index+1]["t_ini"] = chunk["t_fin"]
            plan = editorial_chunks.snap_plan_to_safe_boundaries(plan, self.store.master)
            editorial_chunks.apply_plan(self.store.root, self.w._master_path(), plan, persist_selection=True)
            self.w.plan = plan
        else:
            layer = copy.deepcopy(self.store.layers[lid])
            if create:
                layer["items"].append(item)
            elif delete:
                removed = {item["item_id"]}
                while True:
                    children = {i["item_id"] for i in layer["items"] if i.get("parent_id") in removed}
                    if children <= removed:
                        break
                    removed |= children
                layer["items"] = [i for i in layer["items"] if i["item_id"] not in removed]
                layer["deleted_item_ids"] = sorted(set(layer.get("deleted_item_ids", [])) | removed)
            else:
                layer["items"] = [item if i["item_id"] == item["item_id"] else i for i in layer["items"]]
            self.store.save(layer)
        self.selected = None if delete else (lid, item["item_id"], 0)
        self._after_write()
        return item

    # ---- acciones de edición sobre el item seleccionado (diseño §3, Fase 4) ----
    def _selected_item(self):
        if not self.selected or not self.selected[1]:
            raise ValueError("selecciona un item del timeline")
        lid, iid, segment = self.selected
        layer, item = self.find(lid, iid)
        if item is None:
            raise ValueError("el item ya no existe")
        return lid, layer, item, segment

    def split(self):
        """S: divide el tramo del item seleccionado en el playhead (recortes: dos
        cortes; bloques: nuevo límite validado; marcas: dos regiones; capas: dos items)."""
        lid, layer, item, segment = self._selected_item()
        t = self.w.editor.t_play
        doc = self._lane_doc(lid)

        def do():
            if lid == "recortes":
                document, twin = edits.split_cut(self.w.trims, item["item_id"], t)
                editorial_trims.save_document(self.w.trims_path, document)
                self.w.trims = document
                self.w._review_stale = True
                self.w._reindex_trims()
                self.w._refresh_trims_status()
                return twin["cut_id"]
            if lid == "bloques":
                plan, twin = edits.split_chunk(self.w.plan, item["item_id"], t)
                plan = editorial_chunks.snap_plan_to_safe_boundaries(plan, self.store.master)
                editorial_chunks.apply_plan(self.store.root, self.w._master_path(), plan,
                                            persist_selection=True)
                self.w.plan = plan
                return twin["chunk_id"]
            if lid == "autor":
                reg = self.w.editor.reg
                mark = next((m for m in reg.marcas if m["id"] == item["item_id"]), None)
                if mark is None or mark["tipo"] != "region":
                    raise ValueError("solo se divide una región")
                left, right = edits.split_range(mark, t)
                reg.editar(mark, t_fin=left["t_fin"])
                twin = reg.agregar_region(right["t_ini"], right["t_fin"], decision=mark.get("decision"),
                                          prompt=mark.get("prompt"))
                if mark.get("label"):
                    reg.editar(twin, label=mark["label"])
                return twin["id"]
            layer_doc, twin = edits.split_layer_item(self.store.layers[lid], item["item_id"], t,
                                                     segment=segment)
            layers.validate_items(layer_doc["items"], self.store.master["media"]["duration"])
            self.store.save(layer_doc)
            return twin["item_id"]
        new_id = self.transact("dividir item", [doc], do)
        self.selected = (lid, new_id, 0)
        self._after_write()
        self.w.editor.status("dividido en el playhead")
        return True

    def trim_edge(self, edge):
        """[ / ]: lleva el inicio o el fin del tramo seleccionado al playhead."""
        lid, layer, item, segment = self._selected_item()
        t = self.w.editor.t_play
        index = edits.range_at(item["ranges"], t, preferred=segment)
        if index is None:
            index = segment if 0 <= segment < len(item["ranges"]) else 0
        item["ranges"][index] = edits.trim_range(item["ranges"][index], edge, t)
        self.persist(lid, item, label="recortar " + ("inicio" if edge == "start" else "fin"))
        self.selected = (lid, item["item_id"], index)
        self.sync_detail()
        return True

    def nudge(self, frames):
        """Alt+←/→: empuja el item ±n fotogramas; el playhead lo sigue."""
        import editorial_nav
        lid, layer, item, segment = self._selected_item()
        fps = (self.w.info.get("video") or {}).get("fps") if self.w.info else None
        delta = frames * editorial_nav.frame_step(fps)
        item["ranges"] = edits.shift_ranges(item["ranges"], delta, self.store.master["media"]["duration"])
        self.persist(lid, item, label=f"empujar {abs(frames)} fotograma(s)")
        self.selected = (lid, item["item_id"], segment)
        self.w.editor._mover_playhead(item["ranges"][min(segment, len(item["ranges"]) - 1)]["t_ini"])
        return True

    def step_item(self, direction):
        """Tab / Shift+Tab: item anterior/siguiente dentro del carril seleccionado
        (o el carril bajo el último click); el playhead va a su inicio."""
        lid = self.selected[0] if self.selected else None
        if lid is None:
            lid = next((l["layer_id"] for l in self.all() if l["items"]), None)
        if lid is None:
            raise ValueError("no hay items en los carriles")
        layer, _ = self.find(lid)
        current = self.selected[1] if self.selected and self.selected[0] == lid else None
        item = edits.next_item(layer["items"], current, direction)
        if item is None:
            self.w.editor.status("no hay más items en el carril")
            return True
        self.selected = (lid, item["item_id"], 0)
        self.w.editor._mover_playhead(min(r["t_ini"] for r in item["ranges"]))
        self.w.editor.redibujar()
        self.sync_detail()
        return True

    def accept(self, *, then_next=False):
        """A: proposed/disabled → accepted; accepted → proposed (§3.1). Shift+A pasa
        al siguiente item del carril después."""
        lid, layer, item, segment = self._selected_item()
        if lid == "bloques":
            raise ValueError("los bloques no tienen aceptación")
        item["state"] = edits.toggle_accept(item["state"])
        self.persist(lid, item, label="aceptar" if item["state"] == "accepted" else "quitar aceptación")
        if then_next:
            self.step_item(+1)
        return True

    # ---- navegación por los carriles (Fase 3): índices con bisect, cacheados ----
    def edges(self):
        """Índice de bordes de TODOS los carriles visibles, reconstruido solo cuando
        cambian los documentos (misma clave que `all()`)."""
        import editorial_nav
        layers_ = self.all()
        key = self._cache_key
        if getattr(self, "_edges_key", None) != key:
            self._edges_key = key
            self._edges = editorial_nav.EdgeIndex(editorial_nav.edge_times(layers_))
        return self._edges

    def silences(self):
        import editorial_nav
        trims = self.w.trims
        key = (id(trims), trims.get("revision") if trims else None,
               len(trims["cuts"]) if trims else 0)
        if getattr(self, "_silences_key", None) != key:
            self._silences_key = key
            self._silences = editorial_nav.EdgeIndex(editorial_nav.silence_times(trims))
        return self._silences

    def selected_range(self):
        """[inicio, fin] del item seleccionado (todos sus tramos), o None."""
        if not self.selected or not self.selected[1]:
            return None
        try:
            _, item = self.find(*self.selected[:2])
        except StopIteration:
            return None
        if not item:
            return None
        return (min(r["t_ini"] for r in item["ranges"]), max(r["t_fin"] for r in item["ranges"]))

    def _jump(self, index, delta, what):
        editor = self.w.editor
        t = index.next(editor.t_play) if delta > 0 else index.prev(editor.t_play)
        if t is None:
            editor.status(f"no hay más {what} en esa dirección")
        else:
            editor._mover_playhead(t)
        return True

    def action(self, action, _e=None):
        """`acciones_extra` del editor: acciones del keymap que dependen de los
        carriles (el dueño va primero). True si se consumió; False deja que el
        editor use su propio fallback (marcas, IN/OUT)."""
        if not self.store:
            return False
        editor = self.w.editor
        if action == "edit.undo":
            return self.undo()
        if action == "edit.redo":
            return self.undo(redo=True)
        editing = {"edit.split": self.split,
                   "edit.trim_start": lambda: self.trim_edge("start"),
                   "edit.trim_end": lambda: self.trim_edge("end"),
                   "edit.nudge_prev": lambda: self.nudge(-1),
                   "edit.nudge_next": lambda: self.nudge(+1),
                   "edit.nudge_prev_10": lambda: self.nudge(-10),
                   "edit.nudge_next_10": lambda: self.nudge(+10),
                   "edit.item_prev": lambda: self.step_item(-1),
                   "edit.item_next": lambda: self.step_item(+1),
                   "edit.accept": self.accept,
                   "edit.accept_next": lambda: self.accept(then_next=True)}
        if action in editing:
            try:
                return editing[action]()
            except Exception as error:
                editor.status(f"⚠ {error}")
                return True
        if action in ("nav.prev_edge", "nav.next_edge"):
            return self._jump(self.edges(), +1 if action.endswith("next_edge") else -1, "bordes")
        if action in ("nav.prev_silence", "nav.next_silence"):
            return self._jump(self.silences(), +1 if action.endswith("next_silence") else -1,
                              "silencios")
        if action == "view.skip_trims":
            self.w.skip_check.toggle()
            editor.status("Saltar recortes al reproducir: "
                          + ("sí" if self.w.skip_check.get() else "no"))
            return True
        rango = self.selected_range()
        if action in ("nav.sel_start", "nav.sel_end", "view.zoom_sel", "transport.play_from_item"):
            if rango is None:
                return False                   # el editor prueba con IN/OUT o la marca
            if action == "nav.sel_start":
                editor._mover_playhead(rango[0])
            elif action == "nav.sel_end":
                editor._mover_playhead(rango[1])
            elif action == "view.zoom_sel":
                editor.zoom_a(*rango)
            else:
                editor._play(reiniciar=True, desde=rango[0])
            return True
        if not self.selected or not self.selected[1]:
            return False
        lid, iid, _ = self.selected
        _, item = self.find(lid, iid)
        if not item:
            return False
        try:
            if action == "edit.delete":
                self.persist(lid, item, delete=True)
            elif action == "edit.toggle":
                item["state"] = "proposed" if item["state"] == "disabled" else "disabled"
                self.persist(lid, item, label="desactivar item" if item["state"] == "disabled"
                             else "activar item")
            elif action == "edit.edit":
                self.edit_dialog()
            elif action == "edit.deselect":
                self.selected = None
                self.w.editor.redibujar()
                self.sync_detail()
            else:
                return False
        except Exception as error:
            self.w.editor.status(str(error))
        return True

    def sync_detail(self, hovered=None):
        """Refleja en la barra de detalle el item bajo el mouse (`hovered` = (capa,
        item)) o, si no hay, el seleccionado; sin ninguno muestra la ayuda. Nunca
        toca el status del pie: su wrap cambiaba la altura y movía todo el layout."""
        if self.detail is None:
            return
        if hovered is None and self.store and self.selected and self.selected[1]:
            try:
                layer, item = self.find(*self.selected[:2])
            except StopIteration:
                item = None
            hovered = (layer, item) if item else None
        if hovered is None:
            self.detail.clear()
            return
        layer, item = hovered
        self.detail.show(layer, item, selected=bool(
            self.selected and self.selected[:2] == (layer["layer_id"], item["item_id"])))

    def leave(self, _e=None):
        self.w.editor.tl.delete("layer-tooltip")
        self.sync_detail()

    def hover(self, e):
        if not self.store:
            return
        canvas = self.w.editor.tl
        canvas.delete("layer-tooltip")
        hit = self.w.editor._carril_en(e.y)
        g = self.w.editor._tl_geo()
        hovered = None
        if hit and g:
            item_hit = self.hit(hit[0]["nombre"], e.x, g)
            if item_hit:
                item = item_hit[0]
                hovered = (self.find(hit[0]["nombre"])[0], item)
                x = max(5, min(e.x, canvas.winfo_width() - 260))
                text = canvas.create_text(x + 5, max(2, e.y - 55), text=(
                    item['label'] + ' · ' + item['state'] + '\n' + item['comment'])[:400],
                    anchor="nw", width=250, fill="white", tags="layer-tooltip")
                bbox = canvas.bbox(text)
                if bbox:
                    bg = canvas.create_rectangle(bbox[0]-4,bbox[1]-3,bbox[2]+4,bbox[3]+3,
                                                 fill="#252d28", outline="#637368", tags="layer-tooltip")
                    canvas.tag_lower(bg, text)
        self.sync_detail(hovered)

    def menu(self, e):
        hit = self.w.editor._carril_en(e.y)
        g = self.w.editor._tl_geo()
        if not hit or not g or not self.store:
            return
        item_hit = self.hit(hit[0]["nombre"], e.x, g)
        if not item_hit:
            return
        self.selected = (hit[0]["nombre"], item_hit[0]["item_id"], item_hit[1])
        self.sync_detail()
        menu = tk.Menu(self.w.editor.tl, tearoff=False)
        menu.add_command(label="Editar comentario y rangos", command=self.edit_dialog)
        for label, action in (("Activar / desactivar", "edit.toggle"), ("Borrar item", "edit.delete")):
            menu.add_command(label=label, command=lambda a=action: self.action(a))
        menu.add_command(label="Ir al inicio", command=lambda: self.w.editor._set_playhead(item_hit[0]["ranges"][item_hit[1]]["t_ini"]))
        menu.tk_popup(e.x_root, e.y_root)

    def edit_dialog(self):
        if not self.selected or not self.selected[1]:
            return
        lid, iid, _ = self.selected
        _, item = self.find(lid, iid)
        if not item:
            return
        win = ctk.CTkToplevel(self.w.f)
        win.title("Editar item de capa")
        win.geometry("590x490")
        win.transient(self.w.f.winfo_toplevel())
        fields = {}
        for label, key, value in (("Etiqueta", "label", item["label"]),
                                   ("Tema padre (ID, vacío si es tema principal)", "parent_id", item.get("parent_id") or "")):
            ctk.CTkLabel(win, text=label).pack(anchor="w", padx=15)
            entry = ctk.CTkEntry(win)
            entry.pack(fill="x", padx=15)
            entry.insert(0, value)
            fields[key] = entry
        ctk.CTkLabel(win, text="Rangos: inicio – final en segundos o hh:mm:ss; un tramo por línea").pack()
        ranges = ctk.CTkTextbox(win, height=95)
        ranges.pack(fill="x", padx=15)
        ranges.insert("1.0", "\n".join(f"{r['t_ini']:.3f} - {r['t_fin']:.3f}" for r in item["ranges"]))
        ctk.CTkLabel(win, text="Comentario / pedido específico para la AI").pack(anchor="w", padx=15)
        comment = ctk.CTkTextbox(win, height=100)
        comment.pack(fill="x", padx=15)
        comment.insert("1.0", item["comment"])
        state = ctk.CTkOptionMenu(win, values=["Propuesto", "Aceptado / incluir", "Desactivado / excluir"])
        state.set(dict(zip(layers.STATES, state.cget("values")))[item["state"]])
        state.pack(pady=8)
        def save():
            try:
                updated = copy.deepcopy(item)
                updated.update(label=fields["label"].get(), parent_id=fields["parent_id"].get().strip() or None,
                               comment=comment.get("1.0", "end-1c"),
                               state=layers.STATES[state.cget("values").index(state.get())])
                updated["ranges"] = []
                for line in ranges.get("1.0", "end-1c").splitlines():
                    a, b = line.replace("–", "-").split("-")
                    updated["ranges"].append(dict(t_ini=parse_time(a), t_fin=parse_time(b)))
                self.persist(lid, updated, label="editar pedido")
                win.destroy()
            except Exception as error:
                messagebox.showerror("No se guardó", str(error), parent=win)
        ctk.CTkButton(win, text="Guardar", command=save).pack(pady=8)

    def manage(self):
        if not self.store:
            return
        win = ctk.CTkToplevel(self.w.f)
        win.title("Capas del proyecto")
        win.geometry("600x470")
        win.transient(self.w.f.winfo_toplevel())
        listing = tk.Listbox(win, bg="#19221c", fg="white", height=10)
        listing.pack(fill="both", expand=True, padx=15, pady=12)
        name = ctk.CTkEntry(win, placeholder_text="Nombre de la capa")
        name.pack(fill="x", padx=15)
        color = ctk.CTkEntry(win, placeholder_text="Color #RRGGBB")
        color.insert(0, "#d09947")
        color.pack(fill="x", padx=15, pady=5)
        current = []
        def refresh():
            current[:] = self.all()
            listing.delete(0, "end")
            for layer in current:
                listing.insert("end", f"{layer['name']} · {len(layer['items'])} items · {layer['layer_id']}")
            self.w.editor.refrescar_layout()
        def action(mode):
            try:
                if self.w.worker and self.w.worker.is_alive():
                    raise ValueError("espera a que termine la operación del proyecto")
                if mode == "new":
                    layer = layers.new_layer(self.store.master, name.get().strip(), master_digest=self.store.source_digest)
                else:
                    if not listing.curselection():
                        return
                    layer = current[listing.curselection()[0]]
                    if layer["kind"] not in ("user", "topics"):
                        raise ValueError("capa del proyecto: sus items se editan en el timeline")
                doc = "layer:" + layer["layer_id"]
                if mode == "delete":
                    self.transact("borrar capa", [doc], lambda: self.store.delete(layer["layer_id"]))
                    self.selected = None
                else:
                    layer.update(name=name.get().strip() or layer["name"], color=color.get().strip())
                    self.transact("crear capa" if mode == "new" else "renombrar capa", [doc],
                                  lambda: self.store.save(layer))
                self.snapshot()
                refresh()
            except Exception as error:
                messagebox.showerror("Capas", str(error), parent=win)
        for label, mode in (("Crear capa", "new"), ("Cambiar nombre / color", "save"), ("Borrar capa y sus items", "delete")):
            ctk.CTkButton(win, text=label, command=lambda m=mode: action(m)).pack(pady=4)
        ctk.CTkLabel(win, text="Arrastra un rango en el carril; doble click edita el pedido para la AI.").pack()
        refresh()
