"""Barra de herramientas del editor: botones con icono, tooltip al pasar el mouse
(etiqueta y atajo vigente, leídos del keymap en el momento de mostrarlos) y menú
contextual con todas las acciones. Todo lo que hace una tecla se puede hacer con el
mouse desde aquí; el inventario es el mismo (`keymap.ACTIONS`)."""
from __future__ import annotations

import tkinter as tk

import customtkinter as ctk

import keymap

DELAY_MS = 450
COLORS = dict(bg="#252d28", border="#637368", text="#eef3ef")


class Tooltip:
    """Un tooltip por widget: aparece tras `DELAY_MS` con el mouse quieto encima y
    desaparece al salir o al hacer click. `text` puede ser una función (se evalúa
    al mostrar: los atajos cambian en Ajustes sin reiniciar)."""

    def __init__(self, widget, text):
        self.widget, self.text = widget, text
        self._after = None
        self._win = None
        # CTkSegmentedButton no implementa bind(): se ligan sus botones internos
        for target in [widget, *widget.winfo_children()]:
            try:
                target.bind("<Enter>", self._schedule, add=True)
                target.bind("<Leave>", self.hide, add=True)
                target.bind("<ButtonPress>", self.hide, add=True)
            except NotImplementedError:
                for child in target.winfo_children():
                    try:
                        child.bind("<Enter>", self._schedule, add=True)
                        child.bind("<Leave>", self.hide, add=True)
                        child.bind("<ButtonPress>", self.hide, add=True)
                    except NotImplementedError:
                        pass

    def current_text(self) -> str:
        return self.text() if callable(self.text) else str(self.text)

    def _schedule(self, _e=None):
        self._cancel()
        self._after = self.widget.after(DELAY_MS, self.show)

    def _cancel(self):
        if self._after is not None:
            try:
                self.widget.after_cancel(self._after)
            except tk.TclError:
                pass
            self._after = None

    def show(self, _e=None):
        self._after = None
        if self._win is not None:
            return
        text = self.current_text()
        if not text:
            return
        try:
            x = self.widget.winfo_rootx() + 8
            y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
        except tk.TclError:
            return
        win = tk.Toplevel(self.widget)
        win.wm_overrideredirect(True)
        try:
            win.wm_attributes("-topmost", True)
        except tk.TclError:
            pass
        label = tk.Label(win, text=text, bg=COLORS["bg"], fg=COLORS["text"], padx=8, pady=4,
                         font=("TkDefaultFont", 9), justify="left", wraplength=360,
                         highlightthickness=1, highlightbackground=COLORS["border"])
        label.pack()
        win.wm_geometry(f"+{x}+{y}")
        self._win = win

    def hide(self, _e=None):
        self._cancel()
        if self._win is not None:
            try:
                self._win.destroy()
            except tk.TclError:
                pass
            self._win = None


def tool_button(parent, glyph, action, run, *, width=34, tooltip=None, **kw):
    """Botón de icono que ejecuta `run(action)`; el tooltip dice la etiqueta y el
    atajo vigente de `action` (o `tooltip`, texto o función)."""
    button = ctk.CTkButton(parent, text=glyph, width=width, height=28, corner_radius=6,
                           fg_color="#242b27", hover_color="#303833",
                           font=ctk.CTkFont(size=13), command=lambda: run(action), **kw)
    button.tooltip = Tooltip(button, tooltip if tooltip is not None
                             else (lambda: keymap.tooltip_text(action)))
    return button


def build_menu(parent, available, run, *, before=None):
    """Menú contextual con TODAS las acciones disponibles agrupadas, cada una con su
    atajo como acelerador. `before(menu)` añade entradas propias del dueño (el item
    bajo el cursor) al principio."""
    menu = tk.Menu(parent, tearoff=False)
    if before is not None:
        try:
            if before(menu):
                menu.add_separator()
        except Exception:
            pass
    first = True
    for group, rows in keymap.menu_groups(available):
        if not first:
            menu.add_separator()
        first = False
        sub = tk.Menu(menu, tearoff=False)
        for identifier, label, chords in rows:
            sub.add_command(label=label, accelerator=chords, command=lambda a=identifier: run(a))
        menu.add_cascade(label=group, menu=sub)
    return menu
