"""Un solo editor de gestos y comentarios para capas propias y documentos heredados."""
from __future__ import annotations

import copy
import bisect
import re
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
    HINT = "doble click edita · E acepta · X activa/desactiva · Supr borra · click derecho: todo"
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
        origin = {"silence": "silencio", "ai": "AI", "user": "tuyo"}.get(item.get("origin"))
        if origin:
            x = self._text(x, cy, origin, self.f_small, self.ORIGINS[item["origin"]], width * .1) + 10
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
        # herramientas de mouse y selección múltiple (diseño §7/§8)
        self.tool = "select"
        self.selection = []   # [(layer_id, item_id, segment)] ordenada por tiempo, un carril
        self.on_tool_change = None
        self.lane_order = []      # views/lanes.json (solo presentación, reconstruible)
        self._lane_y = {}
        self._hover_state = None
        self._cursor = None

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
        if doc == "lanes":
            return list(self.lane_order)
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
        elif doc == "lanes":
            self._save_order(list(snapshot or []))
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
        if lid.startswith("trims:"):
            return "trims"
        return {"autor": "autor", "bloques": "plan"}.get(lid, f"layer:{layers.source_layer_id(lid)}")

    def _store_layer(self, lid):
        """Capa GUARDADA detrás de un carril de UI (`topics:<id>:<n>` → `<id>`)."""
        return self.store.layers[layers.source_layer_id(lid)]

    def order_ids(self):
        return [l["layer_id"] for l in self.all()]

    def _after_write(self):
        """Tras escribir o restaurar: la selección solo sobrevive si el item existe."""
        alive = []
        for key in self.selection:
            try:
                _, item = self.find(key[0], key[1])
            except StopIteration:
                item = None
            if item is not None:
                alive.append(key)
        if len(alive) != len(self.selection):
            self.selection = alive
        if self.selected and self.selected[1]:
            try:
                _, item = self.find(*self.selected[:2])
            except StopIteration:
                item = None
            if item is None:
                self.selected = alive[-1] if alive else None
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
             id(reg),reg.revision if reg else None,tuple(sorted(self.store.stamps.items())),
             tuple(self.lane_order))
        if key != self._cache_key:
            self._cache_key=key
            ui = layers.adapters(self.store.master, plan=self.w.plan, trims=self.w.trims,
                                 marks=reg.marcas if reg else [])
            for layer in self.store.visible():
                ui.extend(layers.split_by_depth(layer))      # temas/subtemas por profundidad (§9)
            self._cache = layers.order_layers(ui, self.lane_order)
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
        self._lane_y[layer["layer_id"]] = y
        canvas.create_rectangle(g[0], y, sum(g), y + 34, fill="#191f1c", outline="#343c37")
        parts=self.visible_parts(layer,start,start+span)
        occupied=set()
        for item, index in parts:
            selected = self.is_selected(layer["layer_id"], item["item_id"])
            primary = bool(self.selected) and self.selected[:2] == (layer["layer_id"], item["item_id"])
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
                # propuesto = relleno rayado (pendiente de revisar); aceptado = sólido con
                # borde verde y ✓ (revisado por la persona); desactivado = vacío y punteado.
                # Propuesto y aceptado se CORTAN igual: aceptar es solo la marca de revisión.
                canvas.create_rectangle(a, y + 12, b, y + 31,
                    fill=color if item["state"] != "disabled" else "",
                    stipple="gray50" if item["state"] == "proposed" else "",
                    outline="#ffffff" if selected else "#35a978" if accepted else color,
                    width=2 if selected or accepted else 1,
                    dash=(3, 2) if item["state"] == "disabled" else ())
                if b - a > 45:
                    canvas.create_text(a + 4, y + 21, anchor="w", fill="#ffffff",
                        text=("✓ " if accepted else "") + ("↳ " if item.get("parent_id") else "")
                             + item["label"][:int((b-a)/7)],
                        font=("TkDefaultFont", 8))
                elif accepted and b - a > 12:
                    canvas.create_text((a + b) / 2, y + 21, fill="#ffffff", text="✓",
                                       font=("TkDefaultFont", 8, "bold"))
                if selected:
                    for x in (a, b):
                        canvas.create_line(x, y + 11, x, y + 32, fill="white", width=3)
                    if primary and len(self.selection) > 1:   # el primario: punto en el borde superior
                        cx = (a + b) / 2
                        canvas.create_oval(cx - 3, y + 9, cx + 3, y + 15, fill="white", outline="")
        canvas.create_text(g[0] + 4, y + 1, text=layer["name"], anchor="nw", fill="#aab6af",
                           font=("TkDefaultFont", 8))

    # ---- herramientas de mouse (diseño §7/§8, Fase 6) ----
    EDGE_PX = 8            # zona de borde a cada lado del inicio/fin de un item
    NARROW_PX = 24         # por debajo, las zonas de borde son un tercio del ancho
    CLICK_PX = 4           # menos que esto es un click, no un arrastre
    TOOLS = ("select", "cut")

    def _redraw(self):
        """Selección, hover y herramienta cambian: redibujar el TIMELINE (el borde
        blanco vive ahí), no el preview."""
        self.w.editor._dibujar_timeline()

    def set_tool(self, tool):
        if tool not in self.TOOLS:
            raise ValueError(tool)
        if tool != self.tool:
            self.tool = tool
            self._hover_state = None
            self._set_cursor("arrow")
            if self.on_tool_change:
                try:
                    self.on_tool_change(tool)
                except Exception:
                    pass
        self.w.editor.status("Herramienta: " + ("Corte (B): arrastra una caja; Shift resta; Ctrl mueve"
                                                if tool == "cut" else "Selección (A)"))

    def _set_cursor(self, cursor):
        """El cursor del canvas cambia SOLO cuando cambia el estado, nunca por evento."""
        if cursor != self._cursor:
            self._cursor = cursor
            try:
                self.w.editor.tl.configure(cursor=cursor)
            except tk.TclError:
                pass

    @staticmethod
    def _lighter(color):
        try:
            r, g, b = (int(color[i:i + 2], 16) for i in (1, 3, 5))
            return "#%02x%02x%02x" % tuple(min(255, int(c + (255 - c) * .55)) for c in (r, g, b))
        except (ValueError, TypeError):
            return "#ffffff"

    def _parts(self, lid):
        """[(item_id, t_ini, t_fin)] de los items de UN rango del carril (los gestos de
        caja y la marquesina trabajan con un rango por item)."""
        layer, _ = self.find(lid)
        return [(i["item_id"], r["t_ini"], r["t_fin"]) for i in layer["items"]
                for r in i["ranges"][:1] if len(i["ranges"]) == 1]

    def _visible_parts_between(self, lid, t0, t1):
        layer, _ = self.find(lid)
        return [(item["item_id"], item["ranges"][index]["t_ini"], item["ranges"][index]["t_fin"])
                for item, index in self.visible_parts(layer, min(t0, t1), max(t0, t1))]

    def hit(self, lid, x, g):
        """Item bajo x en el carril, por el índice ordenado (`visible_parts`, bisect):
        O(log n + k) aunque haya miles de recortes. Devuelve (item, índice del tramo,
        modo) con modo `start`/`end` (a ≤ 8 px del borde; un tercio del ancho en items
        estrechos) o `move` (cuerpo). El seleccionado tiene prioridad."""
        editor = self.w.editor
        layer, _ = self.find(lid)
        t = editor._x2t(x, g)
        tol = (self.EDGE_PX + 2) * editor.view[1] / max(g[1], 1)
        hits = []
        for item, index in self.visible_parts(layer, t - tol, t + tol):
            part = item["ranges"][index]
            a, b = editor._t2x(part["t_ini"], g), editor._t2x(part["t_fin"], g)
            b = max(a + 3, b)
            if a - self.EDGE_PX <= x <= b + self.EDGE_PX:
                width = b - a
                zone = self.EDGE_PX if width >= self.NARROW_PX else max(1.0, width / 3)
                mode = "start" if abs(x - a) <= zone else "end" if abs(x - b) <= zone else "move"
                hits.append((item, index, mode))
        if self.selected:
            active = next((h for h in hits if h[0]["item_id"] == self.selected[1]), None)
            if active:
                return active
        return hits[-1] if hits else None

    # ---- selección múltiple (§8): `selection` lista ordenada por tiempo, un carril ----
    def _key_time(self, key):
        try:
            _, item = self.find(key[0], key[1])
            return min(r["t_ini"] for r in item["ranges"]) if item else 0.0
        except StopIteration:
            return 0.0

    def select(self, keys, primary=None):
        keys = [tuple(k) for k in keys]
        lanes = {k[0] for k in keys}
        if len(lanes) > 1:
            raise ValueError("solo se seleccionan items de un mismo carril")
        self.selection = sorted(dict.fromkeys(keys), key=self._key_time)
        if primary is None or tuple(primary) not in self.selection:
            primary = self.selection[-1] if self.selection else None
        self.selected = tuple(primary) if primary else (self.selected[0], None, 0) if self.selected else None

    def clear_selection(self, lid=None):
        self.selection = []
        self.selected = (lid, None, 0) if lid else None

    def is_selected(self, lid, item_id):
        return any(k[0] == lid and k[1] == item_id for k in self.selection) or (
            bool(self.selected) and self.selected[:2] == (lid, item_id))

    def selected_items(self):
        """[(lid, item copia, segmento)] de la selección (o del primario)."""
        keys = self.selection or ([self.selected] if self.selected and self.selected[1] else [])
        out = []
        for lid, iid, segment in keys:
            try:
                _, item = self.find(lid, iid)
            except StopIteration:
                item = None
            if item is not None:
                out.append((lid, item, segment))
        return out

    def select_all(self):
        lid = self.selected[0] if self.selected else None
        if lid is None:
            lid = next((l["layer_id"] for l in self.all() if l["items"]), None)
        if lid is None:
            return True
        layer, _ = self.find(lid)
        self.select([(lid, i["item_id"], 0) for i in layer["items"]])
        self._redraw()
        self.sync_detail()
        self.w.editor.status(f"{len(self.selection)} items seleccionados en «{layer['name']}»")
        return True

    # ---- gestos ----
    def gesture(self, lid, phase, e, g, y):
        if not self.store or (self.w.worker and self.w.worker.is_alive()):
            return True
        if phase == "doble":
            hit = self.hit(lid, e.x, g)
            if hit:
                self.select([(lid, hit[0]["item_id"], hit[1])])
                self.edit_dialog()
            return True
        if phase == "press":
            return self._press(lid, e, g, y)
        if phase == "motion":
            return self._motion(e, g)
        if phase == "release":
            return self._release(e, g)
        return True

    def _press(self, lid, e, g, y):
        editor = self.w.editor
        t = max(0.0, min(self.store.master["media"]["duration"], editor._x2t(e.x, g)))
        state = int(getattr(e, "state", 0) or 0)
        shift, ctrl = bool(state & 0x1), bool(state & 0x4)
        hit = self.hit(lid, e.x, g)
        self.w.sel_cut = None
        editor._seleccionar(None)
        self.drag = None
        if self.tool == "cut" and ctrl and not hit:
            return False                       # Ctrl + vacío = scrub (lo hace el editor)
        if self.tool == "cut" and not ctrl:
            if hit:
                self.select([(lid, hit[0]["item_id"], hit[1])])
            else:
                self.clear_selection(lid)
            self.drag = dict(kind="box", lid=lid, y=y, t0=t, t1=t, x0=e.x, x1=e.x,
                             subtract=shift, hit=hit)
        elif hit:
            item, index, mode = hit
            key = (lid, item["item_id"], index)
            if shift and self.tool == "select":
                keys = [k for k in self.selection if k[0] == lid]
                if key in keys:
                    keys.remove(key)
                else:
                    keys.append(key)
                self.select(keys, primary=key if key in keys else None)
            elif mode in ("start", "end"):
                if key not in self.selection:
                    self.select([key])
                else:
                    self.selected = key
                self.drag = dict(kind="edge", lid=lid, y=y, key=key, mode=mode, t0=t, t1=t,
                                 x0=e.x, x1=e.x, part=dict(item["ranges"][index]),
                                 color=self._item_color(self.find(lid)[0], item))
            else:
                if key not in self.selection:
                    self.select([key])
                else:
                    self.selected = key
                self.drag = dict(kind="move", lid=lid, y=y, t0=t, t1=t, x0=e.x, x1=e.x,
                                 items=[(k, copy.deepcopy(self.find(k[0], k[1])[1]["ranges"]))
                                        for k in self.selection])
        else:
            self.clear_selection(lid)
            self.drag = dict(kind="marquee", lid=lid, y=y, t0=t, t1=t, x0=e.x, x1=e.x,
                             y0=e.y, y1=e.y)
        self._redraw()
        self.sync_detail()
        return True

    def _drag_delta(self, d):
        """Delta de un arrastre de conjunto, acotado para que ningún item salga del medio."""
        delta = d["t1"] - d["t0"]
        duration = self.store.master["media"]["duration"]
        lo = min(r["t_ini"] for _, ranges in d["items"] for r in ranges)
        hi = max(r["t_fin"] for _, ranges in d["items"] for r in ranges)
        return max(-lo, min(delta, duration - hi))

    def _motion(self, e, g):
        d = self.drag
        if not d:
            return True
        editor = self.w.editor
        canvas = editor.tl
        t = max(0.0, min(self.store.master["media"]["duration"], editor._x2t(e.x, g)))
        d["t1"], d["x1"], d["y1"] = t, e.x, getattr(e, "y", d.get("y1", 0))
        canvas.delete("layer-drag")
        y = d["y"]
        if d["kind"] == "box":
            a, b = sorted((d["t0"], t))
            canvas.create_rectangle(editor._t2x(a, g), y + 10, editor._t2x(b, g), y + 33,
                                    outline="#ff6b6b" if d["subtract"] else "white",
                                    dash=(3, 2), tags="layer-drag")
            editor.status(f"{format_time(a)} → {format_time(b)} · "
                          + ("suelta para restar" if d["subtract"] else "suelta para crear o estirar"))
        elif d["kind"] == "marquee":
            canvas.create_rectangle(d["x0"], d["y0"], e.x, d["y1"], outline="white",
                                    dash=(3, 2), tags="layer-drag")
        elif d["kind"] == "move":
            delta = self._drag_delta(d)
            for key, ranges in d["items"]:
                ly = self._lane_y.get(key[0], y)
                for r in ranges:
                    canvas.create_rectangle(editor._t2x(r["t_ini"] + delta, g), ly + 10,
                                            editor._t2x(r["t_fin"] + delta, g), ly + 33,
                                            outline="white", dash=(3, 2), tags="layer-drag")
            editor.status(f"mover {len(d['items'])} item(s) {delta:+.2f} s · suelta para guardar")
        elif d["kind"] == "edge":
            part = d["part"]
            try:
                new = edits.trim_range(part, d["mode"], t)
            except ValueError:
                new = part
            canvas.create_rectangle(editor._t2x(new["t_ini"], g), y + 10,
                                    editor._t2x(new["t_fin"], g), y + 33,
                                    outline="white", dash=(3, 2), tags="layer-drag")
            old = part["t_ini" if d["mode"] == "start" else "t_fin"]
            new_t = new["t_ini" if d["mode"] == "start" else "t_fin"]
            text = f"{format_time(old)[3:-1]} → {format_time(new_t)[3:-1]} · {new_t - old:+.1f} s"
            x = max(4, min(e.x + 8, canvas.winfo_width() - 150))
            label = canvas.create_text(x, y - 2, text=text, anchor="sw", fill="white",
                                       font=("TkDefaultFont", 8), tags="layer-drag")
            bbox = canvas.bbox(label)
            if bbox:
                bg = canvas.create_rectangle(bbox[0] - 3, bbox[1] - 2, bbox[2] + 3, bbox[3] + 2,
                                             fill="#252d28", outline="#637368", tags="layer-drag")
                canvas.tag_lower(bg, label)
        return True

    def _release(self, e, g):
        d, self.drag = self.drag, None
        if not d:
            return True
        editor = self.w.editor
        editor.tl.delete("layer-drag")
        moved = abs(d.get("x1", d["x0"]) - d["x0"]) >= self.CLICK_PX
        try:
            if d["kind"] == "box" and moved:
                self.apply_box(d["lid"], min(d["t0"], d["t1"]), max(d["t0"], d["t1"]),
                               subtract=d["subtract"])
            elif d["kind"] == "marquee" and moved:
                self._marquee(d)
            elif d["kind"] == "move" and moved:
                delta = self._drag_delta(d)
                items = []
                for key, ranges in d["items"]:
                    _, item = self.find(key[0], key[1])
                    item["ranges"] = [{**r, "t_ini": round(r["t_ini"] + delta, 3),
                                       "t_fin": round(r["t_fin"] + delta, 3)} for r in ranges]
                    items.append(item)
                n = len(items)
                self.persist_many(d["lid"], items, label=f"mover {n} item(s)" if n > 1 else "mover item")
            elif d["kind"] == "move" and not moved and self.selected and len(self.selection) > 1:
                # click sin arrastre sobre un item del conjunto: se queda solo ese (un
                # arrastre habría movido el conjunto entero)
                self.select([self.selected])
            elif d["kind"] == "edge" and moved:
                lid, iid, index = d["key"]
                _, item = self.find(lid, iid)
                item["ranges"][index] = edits.trim_range(item["ranges"][index], d["mode"], d["t1"])
                self.persist(lid, item, label="estirar item")
                self.selected = (lid, iid, index)
        except Exception as error:
            editor.status(f"⚠ no se guardó: {error}")
        self._redraw()
        self.sync_detail()
        return True

    def _marquee(self, d):
        editor = self.w.editor
        y0, y1 = sorted((d["y0"], d.get("y1", d["y0"])))
        lanes = {}
        for lid, ly in self._lane_y.items():
            if ly <= y1 and ly + 34 >= y0:
                lanes[lid] = self._visible_parts_between(lid, d["t0"], d["t1"])
        if not lanes:
            lanes[d["lid"]] = self._visible_parts_between(d["lid"], d["t0"], d["t1"])
        lid, ids = edits.marquee_select(lanes, d["t0"], d["t1"])
        if not ids:
            self.clear_selection(d["lid"])
            editor.status("marquesina vacía")
            return
        self.select([(lid, iid, 0) for iid in ids])
        layer, _ = self.find(lid)
        editor.status(f"{len(ids)} item(s) seleccionados en «{layer['name']}»"
                      + (" (varios carriles: se eligió el que tenía más)" if len(lanes) > 1 else ""))

    def _item_color(self, layer, item):
        return ({"silence": "#527cad", "ai": "#9471bd", "user": "#c58e43"}.get(item.get("origin"))
                or layer["color"])

    # ---- escrituras en lote: UNA escritura por documento, UNA entrada de deshacer ----
    def persist_many(self, lid, items, *, delete=False, label=None):
        """Valida todos los items, aplica todo en una copia del documento, escribe una
        vez, refresca una vez y empuja una sola entrada al historial (§8). Si uno
        falla, no se escribe ninguno."""
        if self.w.worker and self.w.worker.is_alive():
            raise ValueError("espera a que termine la operación del proyecto")
        items = [copy.deepcopy(i) for i in items]
        if not items:
            return []
        if label is None:
            label = ("borrar" if delete else "editar") + f" {len(items)} items"
        duration = self.store.master["media"]["duration"]
        for item in items:
            layers.validate_items([dict(item, parent_id=None)], duration, allow_points=lid == "autor")
            item["edited"] = True
            if (lid in ("autor", "bloques") or lid.startswith("trims:")) and len(item["ranges"]) != 1:
                raise ValueError("esta capa admite un rango por item")
        if lid == "bloques":
            raise ValueError("los bloques no admiten operaciones en lote (cobertura continua)")

        def do():
            if lid == "autor":
                reg = self.w.editor.reg
                marks = copy.deepcopy(reg.marcas)
                by_id = {m["id"]: m for m in marks}
                for item in items:
                    mark = by_id.get(item["item_id"])
                    if mark is None:
                        continue
                    if delete:
                        marks.remove(mark)
                        continue
                    a, b = item["ranges"][0]["t_ini"], item["ranges"][0]["t_fin"]
                    decision = {"accepted": "incluir", "disabled": "excluir"}.get(item["state"])
                    if mark["tipo"] == "punto" and a == b:
                        mark.update(t=a, prompt=item["comment"] or None, label=item["label"])
                    else:
                        if mark["tipo"] == "punto":
                            mark.pop("t", None)
                            mark["tipo"] = "region"
                        mark.update(t_ini=a, t_fin=b, decision=decision, prompt=item["comment"] or None,
                                    label=item["label"])
                reg.reemplazar(marks)
            elif lid.startswith("trims:"):
                document = copy.deepcopy(self.w.trims)
                by_id = {c["cut_id"]: c for c in document["cuts"]}
                for item in items:
                    cut = by_id.get(item["item_id"])
                    if cut is None:
                        continue
                    if delete:
                        document["cuts"].remove(cut)
                        continue
                    part = item["ranges"][0]
                    cut.update(t_ini=part["t_ini"], t_fin=part["t_fin"], reason=item["comment"],
                               enabled=item["state"] != "disabled",
                               accepted=item["state"] == "accepted", edited=True)
                if not delete:
                    editorial_trims.coalesce(document, lane=layers.lane_of(lid))
                editorial_trims.save_document(self.w.trims_path, document)
                self.w.trims = document
                self.w._review_stale = True
                self.w._reindex_trims()
                self.w._refresh_trims_status()
            else:
                layer = copy.deepcopy(self._store_layer(lid))
                if delete:
                    removed = {i["item_id"] for i in items}
                    while True:
                        children = {i["item_id"] for i in layer["items"] if i.get("parent_id") in removed}
                        if children <= removed:
                            break
                        removed |= children
                    layer["items"] = [i for i in layer["items"] if i["item_id"] not in removed]
                    layer["deleted_item_ids"] = sorted(set(layer.get("deleted_item_ids", [])) | removed)
                else:
                    by_id = {i["item_id"]: i for i in items}
                    layer["items"] = [by_id.get(i["item_id"], i) for i in layer["items"]]
                self.store.save(layer)
        self.transact(label, [self._lane_doc(lid)], do)
        if delete:
            self.clear_selection(lid)
        else:
            self.select([(lid, i["item_id"], 0) for i in items],
                        primary=self.selected if self.selected and self.selected[0] == lid else None)
        self._after_write()
        return items

    def apply_box(self, lid, a, b, *, subtract=False):
        """Herramienta Corte: caja que crea/estira/funde (o resta con Shift) en UNA
        escritura del documento del carril (§7)."""
        if lid == "bloques":
            raise ValueError("en «Bloques» la herramienta Corte solo mueve límites (arrastra un borde)")
        layer, _ = self.find(lid)
        parts = self._parts(lid)
        ops = edits.box_subtract(parts, a, b) if subtract else edits.box_add(parts, a, b)
        if not ops:
            self.w.editor.status("la caja no toca ningún item")
            return None
        label = ("restar " if subtract else "dibujar ") + f"caja en «{layer['name']}»"
        created = []

        def do():
            if lid.startswith("trims:"):
                lane = layers.lane_of(lid)
                document = copy.deepcopy(self.w.trims)
                by_id = {c["cut_id"]: c for c in document["cuts"]}
                actor = None
                for op in ops:
                    if op[0] == "create":
                        cut = editorial_trims.add_cut(document, op[1], op[2], origin="user", edited=True,
                                                      lane=lane)
                        created.append(cut["cut_id"])
                        actor = cut["cut_id"]
                    elif op[0] == "update":
                        by_id[op[1]].update(t_ini=op[2], t_fin=op[3], edited=True)
                        actor = op[1]
                    elif op[0] == "delete":
                        document["cuts"].remove(by_id[op[1]])
                    elif op[0] == "split":
                        cut = by_id[op[1]]
                        cut.update(t_ini=op[2][0], t_fin=op[2][1], edited=True)
                        editorial_trims.add_cut(document, op[3][0], op[3][1], origin=cut["origin"],
                                                reason=cut.get("reason", ""), enabled=cut["enabled"],
                                                accepted=cut.get("accepted", False), edited=True,
                                                lane=cut.get("lane") or lane)
                    elif op[0] == "merge":
                        survivor = by_id[op[1]]
                        others = [by_id[i] for i in op[2]]
                        survivor.update(t_ini=op[3], t_fin=op[4], edited=True,
                                        reason=editorial_trims._join_reasons(survivor.get("reason"),
                                                                             *(c.get("reason") for c in others)))
                        for other in others:
                            document["cuts"].remove(other)
                        actor = op[1]
                if not subtract:
                    editorial_trims.coalesce(document, lane=lane, actor_id=actor)
                editorial_trims.save_document(self.w.trims_path, document)
                self.w.trims = document
                self.w._review_stale = True
                self.w._reindex_trims()
                self.w._refresh_trims_status()
            elif lid == "autor":
                reg = self.w.editor.reg
                marks = copy.deepcopy(reg.marcas)
                by_id = {m["id"]: m for m in marks}
                from datetime import datetime, timezone
                next_id = reg.next_id
                def new_mark(x, y_, base=None):
                    nonlocal next_id
                    mark = {"id": f"m{next_id:04d}", "tipo": "region", "t_ini": round(x, 3),
                            "t_fin": round(y_, 3), "decision": (base or {}).get("decision"),
                            "prompt": (base or {}).get("prompt"),
                            "creado": datetime.now(timezone.utc).isoformat(timespec="seconds")}
                    if base and base.get("label"):
                        mark["label"] = base["label"]
                    next_id += 1
                    marks.append(mark)
                    created.append(mark["id"])
                    return mark
                for op in ops:
                    if op[0] == "create":
                        new_mark(op[1], op[2])
                    elif op[0] == "update":
                        by_id[op[1]].update(t_ini=op[2], t_fin=op[3])
                    elif op[0] == "delete":
                        marks.remove(by_id[op[1]])
                    elif op[0] == "split":
                        mark = by_id[op[1]]
                        mark.update(t_ini=op[2][0], t_fin=op[2][1])
                        new_mark(op[3][0], op[3][1], base=mark)
                    elif op[0] == "merge":
                        survivor = by_id[op[1]]
                        others = [by_id[i] for i in op[2]]
                        survivor.update(t_ini=op[3], t_fin=op[4],
                                        prompt=editorial_trims._join_reasons(
                                            survivor.get("prompt"), *(m.get("prompt") for m in others)) or None)
                        for other in others:
                            marks.remove(other)
                reg.reemplazar(marks)
            else:
                layer_doc = copy.deepcopy(self._store_layer(lid))
                by_id = {i["item_id"]: i for i in layer_doc["items"]}
                removed = set()
                for op in ops:
                    if op[0] == "create":
                        item = layers.new_item(op[1], op[2])
                        layer_doc["items"].append(item)
                        created.append(item["item_id"])
                    elif op[0] == "update":
                        by_id[op[1]]["ranges"] = [dict(t_ini=op[2], t_fin=op[3])]
                        by_id[op[1]]["edited"] = True
                    elif op[0] == "delete":
                        layer_doc["items"].remove(by_id[op[1]])
                        removed.add(op[1])
                    elif op[0] == "split":
                        item = by_id[op[1]]
                        item["ranges"] = [dict(t_ini=op[2][0], t_fin=op[2][1])]
                        item["edited"] = True
                        twin = {**copy.deepcopy(item), "item_id": edits.new_item_id(),
                                "ranges": [dict(t_ini=op[3][0], t_fin=op[3][1])]}
                        layer_doc["items"].insert(layer_doc["items"].index(item) + 1, twin)
                    elif op[0] == "merge":
                        survivor = by_id[op[1]]
                        others = [by_id[i] for i in op[2]]
                        survivor["ranges"] = [dict(t_ini=op[3], t_fin=op[4])]
                        survivor["edited"] = True
                        survivor["comment"] = editorial_trims._join_reasons(
                            survivor.get("comment"), *(i.get("comment") for i in others))
                        for other in others:
                            layer_doc["items"].remove(other)
                            removed.add(other["item_id"])
                if removed:
                    layer_doc["deleted_item_ids"] = sorted(set(layer_doc.get("deleted_item_ids", [])) | removed)
                layers.validate_items(layer_doc["items"], self.store.master["media"]["duration"])
                self.store.save(layer_doc)
        self.transact(label, [self._lane_doc(lid)], do)
        survivors = [op[1] for op in ops if op[0] in ("update", "merge")] + created
        if survivors:
            self.select([(lid, survivors[-1], 0)])
        else:
            self.clear_selection(lid)
        self._after_write()
        # crear en una capa de PEDIDOS abre el diálogo con el foco en el pedido (§10);
        # en un carril de recortes queda creado sin diálogo
        if created and not lid.startswith("trims:") and lid != "autor" and \
                self.store.layers.get(layers.source_layer_id(lid), {}).get("kind") in ("user", "ai"):
            self.edit_dialog(focus="comment")
        return ops

    # ---- carriles: añadir, borrar, reordenar (§10) ----
    def _save_order(self, order):
        self.lane_order = [str(i) for i in order]
        layers.save_lane_order(self.store.root, self.lane_order)

    def create_lane(self, kind, name, color="#c58e43"):
        """«Añadir capa»: `kind` «trims» = un carril nuevo dentro de trims.json (dibujar
        una caja crea un corte sin diálogo); «user» = una capa de pedidos para la AI.
        Se inserta encima del carril seleccionado (o arriba de los de recortes)."""
        if self.w.worker and self.w.worker.is_alive():
            raise ValueError("espera a que termine la operación del proyecto")
        name = (name or "").strip()
        if not name:
            raise ValueError("ponle un nombre a la capa")
        selected = self.selected[0] if self.selected else None
        if kind == "trims":
            if self.w.trims is None or self.w.trims_path is None:
                raise ValueError("todavía no hay documento de recortes (importa o procesa el medio)")
            document = copy.deepcopy(self.w.trims)
            lane = editorial_trims.add_lane(document, name, color=color)
            new_id = layers.trims_lane_id(lane["lane_id"])

            def do():
                editorial_trims.save_document(self.w.trims_path, document)
                self.w.trims = document
                self.w._reindex_trims()
                self.w._refresh_trims_status()
                self._save_order(layers.insert_above(self.order_ids() + [new_id], new_id, selected))
            self.transact("añadir carril de recortes", ["trims", "lanes"], do)
        elif kind == "user":
            layer = layers.new_layer(self.store.master, name, master_digest=self.store.source_digest)
            layer["color"] = color
            new_id = layer["layer_id"]

            def do():
                self.store.save(layer)
                self._save_order(layers.insert_above(self.order_ids() + [new_id], new_id, selected))
            self.transact("crear capa", ["layer:" + new_id, "lanes"], do)
        else:
            raise ValueError(f"tipo de capa desconocido: {kind}")
        self.clear_selection(new_id)
        self._after_write()
        return new_id

    def delete_lane(self, lid, *, move_to_main=True):
        """Borra un carril de UI: uno de recortes del usuario (sus cortes van a «main»
        o se borran), una capa propia o de la AI (tumba persistente)."""
        if self.w.worker and self.w.worker.is_alive():
            raise ValueError("espera a que termine la operación del proyecto")
        if lid.startswith("trims:"):
            lane = layers.lane_of(lid)
            document = copy.deepcopy(self.w.trims)
            moved = editorial_trims.remove_lane(document, lane, move_to="main" if move_to_main else None)

            def do():
                editorial_trims.save_document(self.w.trims_path, document)
                self.w.trims = document
                self.w._review_stale = True
                self.w._reindex_trims()
                self.w._refresh_trims_status()
                self._save_order([i for i in self.lane_order if i != lid])
            self.transact("borrar carril de recortes", ["trims", "lanes"], do)
            self.clear_selection()
            self._after_write()
            return moved
        source = layers.source_layer_id(lid)
        layer = self.store.layers.get(source)
        if layer is None or layer["kind"] not in ("user", "topics", "ai"):
            raise ValueError("capa del proyecto: sus items se editan en el timeline")
        ids = [l["layer_id"] for l in self.all() if layers.source_layer_id(l["layer_id"]) == source]

        def do():
            self.store.delete(source)
            self._save_order([i for i in self.lane_order if i not in ids])
        self.transact("borrar capa", ["layer:" + source, "lanes"], do)
        self.clear_selection()
        self._after_write()
        return len(layer["items"])

    def rename_lane(self, lid, name=None, color=None):
        name = (name or "").strip() or None
        if lid.startswith("trims:"):
            lane = layers.lane_of(lid)
            document = copy.deepcopy(self.w.trims)
            entry = next(l for l in editorial_trims.lanes(document) if l["lane_id"] == lane)
            if name:
                entry["name"] = name
            if color:
                if not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
                    raise ValueError("color inválido (#RRGGBB)")
                entry["color"] = color
            document["lanes"] = [entry if l["lane_id"] == lane else l for l in editorial_trims.lanes(document)]

            def do():
                editorial_trims.save_document(self.w.trims_path, document)
                self.w.trims = document
                self.w._reindex_trims()
            self.transact("renombrar carril", ["trims"], do)
        else:
            source = layers.source_layer_id(lid)
            layer = copy.deepcopy(self.store.layers[source])
            if layer["kind"] not in ("user", "topics", "ai"):
                raise ValueError("capa del proyecto: sus items se editan en el timeline")
            layer.update(name=name or layer["name"], color=color or layer["color"])
            self.transact("renombrar capa", ["layer:" + source], lambda: self.store.save(layer))
        self._after_write()

    def move_lane(self, lid, delta):
        """▲ / ▼ en «Capas y comentarios»: reordena el carril (solo presentación)."""
        order = layers.move_in_order(self.order_ids(), lid, delta)
        self.transact("reordenar carriles", ["lanes"], lambda: self._save_order(order))
        self._after_write()

    def new_lane_dialog(self):
        """Ctrl+N / «Añadir capa»: selector de tipo (Recortes / Pedidos para la AI)."""
        if not self.store:
            return True
        win = ctk.CTkToplevel(self.w.f)
        win.title("Añadir capa")
        win.geometry("420x250")
        win.transient(self.w.f.winfo_toplevel())
        kind = ctk.CTkSegmentedButton(win, values=["Recortes", "Pedidos para la AI"])
        kind.set("Recortes")
        kind.pack(fill="x", padx=15, pady=(15, 6))
        ctk.CTkLabel(win, text="Recortes: dibujar una caja crea un corte que cuenta para la exportación. "
                               "Pedidos para la AI: cada caja abre el pedido para la AI.",
                     wraplength=380, justify="left", text_color="gray60").pack(padx=15, anchor="w")
        name = ctk.CTkEntry(win, placeholder_text="Nombre de la capa")
        name.pack(fill="x", padx=15, pady=(8, 4))
        color = ctk.CTkEntry(win, placeholder_text="Color #RRGGBB")
        color.insert(0, "#c58e43")
        color.pack(fill="x", padx=15, pady=(0, 8))

        def create(_e=None):
            try:
                self.create_lane("trims" if kind.get() == "Recortes" else "user", name.get(), color.get().strip())
                win.destroy()
                self.w.editor.tl.focus_set()
            except Exception as error:
                messagebox.showerror("Añadir capa", str(error), parent=win)
            return "break"
        ctk.CTkButton(win, text="Crear encima del carril seleccionado", command=create).pack(pady=6)
        win.bind("<Return>", create)
        win.bind("<Escape>", lambda e: (win.destroy(), self.w.editor.tl.focus_set()))
        win.update_idletasks()
        name.focus_force()
        return True

    def nudge_selection(self, frames):
        """Flechas con varios seleccionados / Alt+flechas: mueve el conjunto ±n
        fotogramas (una escritura) y el playhead lo sigue."""
        import editorial_nav
        items = self.selected_items()
        if not items:
            raise ValueError("selecciona un item del timeline")
        lid = items[0][0]
        fps = (self.w.info.get("video") or {}).get("fps") if self.w.info else None
        delta = frames * editorial_nav.frame_step(fps)
        duration = self.store.master["media"]["duration"]
        lo = min(r["t_ini"] for _, i, _ in items for r in i["ranges"])
        hi = max(r["t_fin"] for _, i, _ in items for r in i["ranges"])
        delta = max(-lo, min(delta, duration - hi))
        batch = []
        for _, item, _ in items:
            item["ranges"] = [{**r, "t_ini": round(r["t_ini"] + delta, 3), "t_fin": round(r["t_fin"] + delta, 3)}
                              for r in item["ranges"]]
            batch.append(item)
        primary = self.selected
        self.persist_many(lid, batch, label=f"empujar {len(batch)} item(s)")
        if primary and primary[1]:
            self.selected = primary
        first = next((i for i in batch if primary and i["item_id"] == primary[1]), batch[0])
        self.w.editor._mover_playhead(first["ranges"][0]["t_ini"])
        return True

    def batch(self, action):
        """X / A / Supr sobre el conjunto seleccionado: una escritura, una entrada."""
        items = self.selected_items()
        if len(items) < 2:
            return False
        lid = items[0][0]
        if action == "edit.delete":
            self.persist_many(lid, [i for _, i, _ in items], delete=True,
                              label=f"borrar {len(items)} items")
            return True
        new = edits.state_for(action)
        for _, item, _ in items:
            item["state"] = new
        verb = {"disabled": "desactivar", "accepted": "aceptar", "proposed": "activar"}[new]
        self.persist_many(lid, [i for _, i, _ in items], label=f"{verb} {len(items)} items")
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
        if (lid in ("autor", "bloques") or lid.startswith("trims:")) and len(item["ranges"]) != 1:
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
        elif lid.startswith("trims:"):
            lane = layers.lane_of(lid)
            document = copy.deepcopy(self.w.trims)
            cut = next((c for c in document["cuts"] if c["cut_id"] == item["item_id"]), None)
            if delete:
                document["cuts"].remove(cut)
            else:
                part = item["ranges"][0]
                if create:
                    cut = editorial_trims.add_cut(document, part["t_ini"], part["t_fin"], lane=lane)
                    item["item_id"] = cut["cut_id"]
                cut.update(**part, reason=item["comment"], enabled=item["state"] != "disabled",
                           accepted=item["state"] == "accepted", edited=True)
                editorial_trims.coalesce(document, lane=lane, actor_id=cut["cut_id"])   # solapes (§10)
            editorial_trims.save_document(self.w.trims_path, document)
            self.w.trims = document
            self.w._review_stale = True
            self.w._reindex_trims()
            self.w._refresh_trims_status()
        elif lid == "bloques":
            if create or delete or item["state"] == "disabled":
                raise ValueError("los bloques deben cubrir el medio; usa Revisar bloques para dividir el plan")
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
            layer = copy.deepcopy(self._store_layer(lid))
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
        if delete:
            self.clear_selection(lid)
        else:
            self.select([(lid, item["item_id"], 0)])
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
            if lid.startswith("trims:"):
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
            layer_doc, twin = edits.split_layer_item(self._store_layer(lid), item["item_id"], t,
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
        self._redraw()
        self.sync_detail()
        return True

    def accept(self, *, then_next=False):
        """A: proposed/disabled → accepted; accepted → proposed (§3.1). Shift+A pasa
        al siguiente item del carril después."""
        lid, layer, item, segment = self._selected_item()
        if lid == "bloques":
            raise ValueError("los bloques no tienen aceptación")
        if item["state"] != "accepted":
            item["state"] = "accepted"
            self.persist(lid, item, label="aceptar")
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
        if action == "tools.cut":
            self.set_tool("cut")
            return True
        if action == "tools.select":
            self.set_tool("select")
            return True
        if action == "tools.select_all":
            return self.select_all()
        if action == "layers.new_lane":
            return self.new_lane_dialog()
        multi = len(self.selection) > 1
        if multi and action in ("edit.toggle", "edit.activate", "edit.accept", "edit.delete"):
            try:
                return self.batch(action)
            except Exception as error:
                editor.status(f"⚠ {error}")
                return True
        if multi and action in ("nav.step_prev", "nav.step_next", "nav.step_prev_5", "nav.step_next_5"):
            # con varios seleccionados las flechas mueven el conjunto (§3); Shift ±10
            frames = (-1 if "prev" in action else 1) * (10 if action.endswith("_5") else 1)
            try:
                return self.nudge_selection(frames)
            except Exception as error:
                editor.status(f"⚠ {error}")
                return True
        if multi and action.startswith("edit.nudge_"):
            frames = (-1 if "prev" in action else 1) * (10 if action.endswith("_10") else 1)
            try:
                return self.nudge_selection(frames)
            except Exception as error:
                editor.status(f"⚠ {error}")
                return True
        if action == "edit.deselect" and self.selection:
            self.clear_selection(self.selected[0] if self.selected else None)
            self._redraw()
            self.sync_detail()
            return True
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
            elif action in ("edit.toggle", "edit.activate"):
                new = edits.state_for(action)
                if lid == "bloques" and new == "disabled":
                    raise ValueError("los bloques no se desactivan (cobertura continua)")
                if item["state"] != new:
                    item["state"] = new
                    self.persist(lid, item, label="desactivar item" if new == "disabled" else "activar item")
            elif action == "edit.edit":
                self.edit_dialog()
            elif action == "edit.deselect":
                self.clear_selection()
                self._redraw()
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
        self.w.editor.tl.delete("layer-edge")
        self._hover_state = None
        self._set_cursor("arrow")
        self.sync_detail()

    def hover(self, e):
        """Hover: cursor y borde resaltado SOLO cuando cambia el estado (vacío / cuerpo /
        borde / herramienta); el hit-test es el mismo `hit()` indexado que la barra de
        detalle. El tooltip se redibuja por evento como antes."""
        if not self.store:
            return
        editor = self.w.editor
        canvas = editor.tl
        canvas.delete("layer-tooltip")
        hit = editor._carril_en(e.y)
        g = editor._tl_geo()
        hovered = None
        item_hit = None
        state = ("empty", None, None, None)
        if hit and g:
            lid = hit[0]["nombre"]
            item_hit = self.hit(lid, e.x, g)
            if item_hit:
                state = (item_hit[2], lid, item_hit[0]["item_id"], item_hit[1])
            else:
                state = ("lane", lid, None, None)
        if self.drag is None and state != self._hover_state:
            self._hover_state = state
            ctrl = bool(int(getattr(e, "state", 0) or 0) & 0x4)
            if state[0] == "empty":
                cursor = "arrow"
            elif self.tool == "cut" and not ctrl:
                cursor = "crosshair"
            elif state[0] in ("start", "end"):
                cursor = "sb_h_double_arrow"
            elif state[0] == "move":
                cursor = "fleur"
            else:
                cursor = "arrow"
            self._set_cursor(cursor)
            canvas.delete("layer-edge")
            if state[0] in ("start", "end") and (self.tool == "select" or ctrl):
                item, index, mode = item_hit
                part = item["ranges"][index]
                x = editor._t2x(part["t_ini" if mode == "start" else "t_fin"], g)
                y = self._lane_y.get(state[1], hit[1])
                canvas.create_line(x, y + 10, x, y + 33, width=3, tags="layer-edge",
                                   fill=self._lighter(self._item_color(self.find(state[1])[0], item)))
        if hit and g:
            if item_hit:
                item = item_hit[0]
                hovered = (self.find(hit[0]["nombre"])[0], item)
                x = max(5, min(e.x, canvas.winfo_width() - 260))
                origin = {"silence": "silencio", "ai": "AI", "user": "tuyo"}.get(item.get("origin"))
                text = canvas.create_text(x + 5, max(2, e.y - 55), text=(
                    item['label'] + ' · ' + item['state'] + (' · ' + origin if origin else '')
                    + '\n' + item['comment'])[:400],
                    anchor="nw", width=250, fill="white", tags="layer-tooltip")
                bbox = canvas.bbox(text)
                if bbox:
                    bg = canvas.create_rectangle(bbox[0]-4,bbox[1]-3,bbox[2]+4,bbox[3]+3,
                                                 fill="#252d28", outline="#637368", tags="layer-tooltip")
                    canvas.tag_lower(bg, text)
        self.sync_detail(hovered)

    # acciones del keymap que este dueño atiende (para el menú contextual y la barra)
    SUPPORTED_ACTIONS = frozenset({
        "edit.undo", "edit.redo", "edit.split", "edit.trim_start", "edit.trim_end",
        "edit.nudge_prev", "edit.nudge_next", "edit.nudge_prev_10", "edit.nudge_next_10",
        "edit.item_prev", "edit.item_next", "edit.accept", "edit.accept_next", "edit.toggle", "edit.activate",
        "edit.delete", "edit.edit", "edit.deselect", "tools.cut", "tools.select",
        "tools.select_all", "layers.new_lane", "nav.prev_edge", "nav.next_edge",
        "nav.prev_silence", "nav.next_silence", "nav.sel_start", "nav.sel_end", "view.zoom_sel",
        "view.skip_trims", "transport.play_from_item"})

    def supported_actions(self):
        return self.SUPPORTED_ACTIONS if self.store else frozenset()

    def menu_items(self, e, menu):
        """Hook `menu_extra` del editor: click derecho sobre un item lo selecciona y
        añade sus entradas al principio del menú contextual. True si añadió algo."""
        editor = self.w.editor
        hit = editor._carril_en(e.y) if e is not None else None
        g = editor._tl_geo()
        if not hit or not g or not self.store:
            return False
        lid = hit[0]["nombre"]
        item_hit = self.hit(lid, e.x, g)
        layer, _ = self.find(lid)
        if not item_hit:
            menu.add_command(label=f"Carril «{layer['name']}»", state="disabled")
            menu.add_command(label="Añadir capa encima…", accelerator=self._chord("layers.new_lane"),
                             command=lambda: (self.clear_selection(lid), self.new_lane_dialog()))
            return True
        key = (lid, item_hit[0]["item_id"], item_hit[1])
        if key not in self.selection:
            self.select([key])
        else:
            self.selected = key
        self._redraw()
        self.sync_detail()
        item = item_hit[0]
        many = len(self.selection) > 1
        menu.add_command(label=(f"{len(self.selection)} items seleccionados" if many
                                else f"{layer['name']} · {item['label']}"), state="disabled")
        menu.add_command(label="Editar comentario y rangos", accelerator=self._chord("edit.edit"),
                         command=self.edit_dialog)
        for label, action in (("Aceptar (revisado; se corta)", "edit.accept"),
                              ("Activar (propuesto; se corta)", "edit.activate"),
                              ("Desactivar (no se corta)", "edit.toggle"),
                              ("Dividir en el playhead", "edit.split"),
                              ("Recortar inicio al playhead", "edit.trim_start"),
                              ("Recortar fin al playhead", "edit.trim_end"),
                              ("Borrar", "edit.delete")):
            menu.add_command(label=label, accelerator=self._chord(action),
                             command=lambda a=action: editor.ejecutar(a))
        start = item["ranges"][item_hit[1]]["t_ini"]
        menu.add_command(label="Ir al inicio", accelerator=self._chord("nav.sel_start"),
                         command=lambda: editor._mover_playhead(start))
        menu.add_command(label="Reproducir desde aquí", accelerator=self._chord("transport.play_from_item"),
                         command=lambda: editor._play(reiniciar=True, desde=start))
        return True

    @staticmethod
    def _chord(action):
        import keymap
        return keymap.pretty_chords(action)

    def edit_dialog(self, focus=None):
        """Diálogo del item. `focus="comment"`: el cursor queda en el campo del pedido
        sin tocar el mouse (crear en una capa de pedidos, §10). Escape y Ctrl+Enter
        guardan y cierran; cerrar con la X también guarda; el foco vuelve al timeline."""
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
        self.window = win
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
        def close():
            self.window = None
            try:
                win.destroy()
            finally:
                try:
                    self.w.editor.tl.focus_set()
                except tk.TclError:
                    pass

        def save(_e=None):
            try:
                updated = copy.deepcopy(item)
                updated.update(label=fields["label"].get(), parent_id=fields["parent_id"].get().strip() or None,
                               comment=comment.get("1.0", "end-1c"),
                               state=layers.STATES[state.cget("values").index(state.get())])
                updated["ranges"] = []
                for line in ranges.get("1.0", "end-1c").splitlines():
                    a, b = line.replace("–", "-").split("-")
                    updated["ranges"].append(dict(t_ini=parse_time(a), t_fin=parse_time(b)))
                if updated != item:
                    self.persist(lid, updated, label="editar pedido")
                close()
            except Exception as error:
                messagebox.showerror("No se guardó", str(error), parent=win)
            return "break"
        ctk.CTkButton(win, text="Guardar (Ctrl+Enter · Esc)", command=save).pack(pady=8)
        win.bind("<Escape>", save)
        win.bind("<Control-Return>", save)
        win.protocol("WM_DELETE_WINDOW", save)
        self._dialog_save = save
        if focus == "comment":
            target = getattr(comment, "_textbox", comment)
            win.update_idletasks()
            try:
                target.focus_force()
                win.after(60, target.focus_force)     # tras mapear la ventana
            except tk.TclError:
                pass

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
        def refresh(select=None):
            current[:] = self.all()
            listing.delete(0, "end")
            for layer in current:
                kind = {"recortes": "recortes", "topics": "temas", "ai": "AI", "user": "pedidos"}.get(layer["kind"], layer["kind"])
                listing.insert("end", f"{layer['name']} · {kind} · {len(layer['items'])} items · {layer['layer_id']}")
            if select in [l["layer_id"] for l in current]:
                index = [l["layer_id"] for l in current].index(select)
                listing.selection_set(index)
                listing.see(index)
            self.w.editor.refrescar_layout()
        def chosen():
            if not listing.curselection():
                raise ValueError("elige un carril de la lista")
            return current[listing.curselection()[0]]
        def action(mode):
            try:
                if self.w.worker and self.w.worker.is_alive():
                    raise ValueError("espera a que termine la operación del proyecto")
                if mode == "new":
                    self.new_lane_dialog()
                    return
                layer = chosen()
                lid = layer["layer_id"]
                if mode == "delete":
                    if lid.startswith("trims:"):
                        answer = messagebox.askyesnocancel(
                            "Borrar carril de recortes",
                            f"¿Mover los {len(layer['items'])} cortes de «{layer['name']}» al carril "
                            "«Recortes»? (No = borrarlos)", parent=win)
                        if answer is None:
                            return
                        self.delete_lane(lid, move_to_main=bool(answer))
                    else:
                        self.delete_lane(lid)
                elif mode in ("up", "down"):
                    self.move_lane(lid, -1 if mode == "up" else 1)
                    refresh(select=lid)
                    return
                else:
                    self.rename_lane(lid, name.get(), color.get().strip() or None)
                self.snapshot()
                refresh(select=lid)
            except Exception as error:
                messagebox.showerror("Capas", str(error), parent=win)
        row = ctk.CTkFrame(win, fg_color="transparent")
        row.pack(pady=4)
        for label, mode in (("▲ Subir", "up"), ("▼ Bajar", "down")):
            ctk.CTkButton(row, text=label, width=90, command=lambda m=mode: action(m)).pack(side="left", padx=4)
        for label, mode in (("Añadir capa… (Ctrl+N)", "new"), ("Cambiar nombre / color", "save"),
                            ("Borrar carril / capa", "delete")):
            ctk.CTkButton(win, text=label, command=lambda m=mode: action(m)).pack(pady=4)
        ctk.CTkLabel(win, text="Corte (B): dibuja una caja en el carril. Doble click edita el pedido para la AI.").pack()
        refresh()
