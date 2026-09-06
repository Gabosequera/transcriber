"""Un solo editor de gestos y comentarios para capas propias y documentos heredados."""
from __future__ import annotations

import copy
import tkinter as tk
from tkinter import messagebox

import customtkinter as ctk
import editorial_chunks
import editorial_layers as layers
import editorial_trims
from editorial_io import parse_time, format_time


class LayersController:
    def __init__(self, workspace):
        self.w = workspace
        self.store = None
        self.selected = None  # layer_id, item_id, segment
        self.drag = None
        self.window = None

    def all(self):
        if not self.store:
            return []
        reg = self.w.editor.reg
        return layers.adapters(self.store.master, plan=self.w.plan, trims=self.w.trims,
                               marks=reg.marcas if reg else []) + self.store.visible()

    def snapshot(self):
        return layers.write_snapshot(self.store.root, self.store.master, self.all())

    def lanes(self):
        return [dict(nombre=layer["layer_id"], alto=34,
                     dibujar=lambda c, g, y, l=layer: self.draw(l, c, g, y),
                     gesto=lambda phase, e, g, y, lid=layer["layer_id"]: self.gesture(lid, phase, e, g, y))
                for layer in self.all()]

    def find(self, lid, iid=None):
        layer = next(l for l in self.all() if l["layer_id"] == lid)
        return layer, next((i for i in layer["items"] if i["item_id"] == iid), None)

    def draw(self, layer, canvas, g, y):
        editor = self.w.editor
        start, span = editor.view
        canvas.create_rectangle(g[0], y, sum(g), y + 34, fill="#191f1c", outline="#343c37")
        for item in layer["items"]:
            selected = self.selected and self.selected[:2] == (layer["layer_id"], item["item_id"])
            for part in item["ranges"]:
                if part["t_fin"] < start or part["t_ini"] > start + span:
                    continue
                a, b = [editor._t2x(max(start, min(start + span, part[k])), g) for k in ("t_ini", "t_fin")]
                b = max(a + 3, b)
                color = ({"silence": "#527cad", "ai": "#9471bd", "user": "#c58e43"}.get(item.get("origin"))
                         or layer["color"])
                canvas.create_rectangle(a, y + 12, b, y + 31,
                    fill=color if item["state"] != "disabled" else "",
                    outline="#ffffff" if selected else color, width=2 if selected else 1,
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
                    self.persist(lid, item)
                else:
                    self.persist(lid, layers.new_item(min(drag["t"], drag["now"]), max(drag["t"], drag["now"])), create=True)
                    self.edit_dialog()
            except Exception as error:
                messagebox.showerror("No se guardó el rango", str(error), parent=self.w.f)
            editor.redibujar()
            return True
        return True

    def persist(self, lid, item, *, create=False, delete=False):
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
                item["item_id"] = mark["id"]
            elif a == b:
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
                cut.update(**part, reason=item["comment"], enabled=item["state"] != "disabled", edited=True)
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
            else:
                layer["items"] = [item if i["item_id"] == item["item_id"] else i for i in layer["items"]]
            self.store.save(layer)
        self.selected = None if delete else (lid, item["item_id"], 0)
        self.snapshot()
        self.w._refresh_plan_buttons()
        editor.refrescar_layout()

    def keys(self, e):
        if not self.selected or not self.selected[1]:
            return False
        lid, iid, _ = self.selected
        _, item = self.find(lid, iid)
        if not item:
            return False
        try:
            if e.keysym in ("Delete", "BackSpace"):
                self.persist(lid, item, delete=True)
            elif e.keysym.lower() == "x":
                item["state"] = "proposed" if item["state"] == "disabled" else "disabled"
                self.persist(lid, item)
            elif e.keysym in ("Return", "F2"):
                self.edit_dialog()
            elif e.keysym == "Escape":
                self.selected = None
                self.w.editor.redibujar()
            else:
                return False
        except Exception as error:
            self.w.editor.status(str(error))
        return True

    def hover(self, e):
        if not self.store:
            return
        canvas = self.w.editor.tl
        canvas.delete("layer-tooltip")
        hit = self.w.editor._carril_en(e.y)
        g = self.w.editor._tl_geo()
        if hit and g:
            item_hit = self.hit(hit[0]["nombre"], e.x, g)
            if item_hit:
                item = item_hit[0]
                self.w.editor.status(f"{item['label']} · {len(item['ranges'])} tramo(s) · {item['comment']} · doble click para editar")
                x = max(5, min(e.x, canvas.winfo_width() - 260))
                text = canvas.create_text(x + 5, max(2, e.y - 55), text=(
                    item['label'] + ' · ' + item['state'] + '\n' + item['comment'])[:400],
                    anchor="nw", width=250, fill="white", tags="layer-tooltip")
                bbox = canvas.bbox(text)
                if bbox:
                    bg = canvas.create_rectangle(bbox[0]-4,bbox[1]-3,bbox[2]+4,bbox[3]+3,
                                                 fill="#252d28", outline="#637368", tags="layer-tooltip")
                    canvas.tag_lower(bg, text)

    def menu(self, e):
        hit = self.w.editor._carril_en(e.y)
        g = self.w.editor._tl_geo()
        if not hit or not g or not self.store:
            return
        item_hit = self.hit(hit[0]["nombre"], e.x, g)
        if not item_hit:
            return
        self.selected = (hit[0]["nombre"], item_hit[0]["item_id"], item_hit[1])
        menu = tk.Menu(self.w.editor.tl, tearoff=False)
        menu.add_command(label="Editar comentario y rangos", command=self.edit_dialog)
        from types import SimpleNamespace
        for label, key in (("Activar / desactivar", "x"), ("Borrar item", "Delete")):
            menu.add_command(label=label, command=lambda k=key: self.keys(SimpleNamespace(keysym=k)))
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
                self.persist(lid, updated)
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
                if mode == "new":
                    layer = layers.new_layer(self.store.master, name.get().strip())
                else:
                    if not listing.curselection():
                        return
                    layer = current[listing.curselection()[0]]
                    if layer["kind"] not in ("user", "topics"):
                        raise ValueError("capa del proyecto: sus items se editan en el timeline")
                if mode == "delete":
                    self.store.delete(layer["layer_id"])
                    self.selected = None
                else:
                    layer.update(name=name.get().strip() or layer["name"], color=color.get().strip())
                    self.store.save(layer)
                self.snapshot()
                refresh()
            except Exception as error:
                messagebox.showerror("Capas", str(error), parent=win)
        for label, mode in (("Crear capa", "new"), ("Cambiar nombre / color", "save"), ("Borrar capa y sus items", "delete")):
            ctk.CTkButton(win, text=label, command=lambda m=mode: action(m)).pack(pady=4)
        ctk.CTkLabel(win, text="Arrastra un rango en el carril; doble click edita el pedido para la AI.").pack()
        refresh()
