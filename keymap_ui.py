"""Ajustes → Atajos: tabla por grupo con etiqueta, acorde actual, «Grabar» (captura
la siguiente pulsación con la guarda de foco apagada), «×» (sin atajo), conflictos
en rojo con el nombre de la otra acción y «Restaurar predeterminados». Cada cambio
hace `keymap.save()` + `keymap.reload()`: los editores resuelven por el singleton en
cada evento, así que aplica al instante sin reiniciar (diseño §2)."""
from __future__ import annotations

import customtkinter as ctk

import keymap


class KeymapSettings:
    def __init__(self, parent, *, row: int, padx=6, pady=(0, 12)):
        self.frame = ctk.CTkFrame(parent)
        self.frame.grid(row=row, column=0, sticky="ew", padx=padx, pady=pady)
        self.frame.grid_columnconfigure(1, weight=1)
        self._recording: str | None = None
        self._rows: dict[str, dict] = {}
        head = ctk.CTkFrame(self.frame, fg_color="transparent")
        head.grid(row=0, column=0, columnspan=4, sticky="ew", padx=12, pady=(10, 2))
        head.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(head, text="Atajos del editor", font=ctk.CTkFont(size=14, weight="bold")).grid(
            row=0, column=0, sticky="w")
        ctk.CTkButton(head, text="Restaurar predeterminados", width=190, fg_color="#242b27",
                      hover_color="#303833", command=self.restore).grid(row=0, column=1, sticky="e")
        self.hint = ctk.CTkLabel(self.frame, text="", text_color="gray55", font=ctk.CTkFont(size=11),
                                 justify="left", anchor="w", wraplength=640)
        self.hint.grid(row=1, column=0, columnspan=4, sticky="w", padx=12, pady=(0, 6))
        self.table = ctk.CTkFrame(self.frame, fg_color="transparent")
        self.table.grid(row=2, column=0, columnspan=4, sticky="ew", padx=12, pady=(0, 10))
        self.table.grid_columnconfigure(0, weight=1)
        self._build_rows()
        self.refresh()

    # ---- tabla ----
    def _build_rows(self):
        row = 0
        for group in keymap.GROUPS:
            actions = [a for a in keymap.ACTIONS.values() if a.group == group]
            if not actions:
                continue
            ctk.CTkLabel(self.table, text=group.upper(), text_color="gray60",
                         font=ctk.CTkFont(size=10, weight="bold")).grid(
                row=row, column=0, sticky="w", pady=(8, 2))
            row += 1
            for action in actions:
                label = ctk.CTkLabel(self.table, text=action.label, anchor="w",
                                     font=ctk.CTkFont(size=12))
                label.grid(row=row, column=0, sticky="ew", padx=(8, 8), pady=1)
                chord = ctk.CTkLabel(self.table, text="", anchor="w", width=170,
                                     font=ctk.CTkFont(size=12, weight="bold"))
                chord.grid(row=row, column=1, sticky="w", padx=(0, 8), pady=1)
                record = ctk.CTkButton(self.table, text="Grabar", width=64, height=24,
                                       fg_color="#242b27", hover_color="#303833",
                                       command=lambda a=action.id: self.record(a))
                record.grid(row=row, column=2, padx=(0, 4), pady=1)
                clear = ctk.CTkButton(self.table, text="×", width=28, height=24,
                                      fg_color="#242b27", hover_color="#7a2e2e",
                                      command=lambda a=action.id: self.clear(a))
                clear.grid(row=row, column=3, pady=1)
                self._rows[action.id] = dict(chord=chord, record=record, clear=clear)
                row += 1

    def refresh(self):
        km = keymap.current()
        for identifier, widgets in self._rows.items():
            text = km.chord_text(identifier) or "(sin atajo)"
            lost = km.conflict_for(identifier)
            if lost:
                other = keymap.ACTIONS[lost[0][1]].label
                text += f"  ⚠ {lost[0][0]} ya es «{other}»"
                widgets["chord"].configure(text=text, text_color="#ff6b6b")
            elif self._recording == identifier:
                widgets["chord"].configure(text="pulsa una tecla… (Esc cancela)", text_color="#e6b85c")
            else:
                widgets["chord"].configure(text=text, text_color="gray85" if km.chords(identifier) else "gray50")
        warnings = list(km.warnings)
        if km.conflicts:
            warnings.append(f"{len(km.conflicts)} atajo(s) en conflicto: gana la acción que aparece primero.")
        self.hint.configure(text="\n".join(warnings) if warnings else
                            "Los atajos valen con el foco en el timeline o el preview; en un campo de texto "
                            "se escribe normal. Se guardan por máquina en keymap.json.")

    # ---- acciones ----
    def _apply(self, overrides):
        keymap.save(overrides)
        keymap.reload()
        self.refresh()

    def clear(self, identifier):
        self._recording = None
        overrides = keymap.current().as_overrides()
        overrides[identifier] = []
        self._apply(overrides)

    def restore(self):
        self._recording = None
        self._apply({})

    def record(self, identifier):
        """Captura la SIGUIENTE pulsación en el toplevel (guarda de foco apagada) y la
        asigna como único acorde de la acción; Escape cancela."""
        top = self.frame.winfo_toplevel()
        if self._recording is None:
            top.bind("<Key>", self._capture, add=True)
        self._recording = identifier
        top.focus_set()
        self.refresh()

    def _capture(self, e):
        if self._recording is None:
            return None
        chord = keymap.chord_from_event(getattr(e, "keysym", ""), getattr(e, "state", 0))
        if chord is None:
            return "break"                     # un modificador solo: seguir esperando
        identifier, self._recording = self._recording, None
        if chord != "Escape":
            overrides = keymap.current().as_overrides()
            overrides[identifier] = [chord]
            self._apply(overrides)
        else:
            self.refresh()
        return "break"
