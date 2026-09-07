"""Atajos configurables del editor (diseño docs/diseno-navegacion-editor.md §2).

Módulo PURO (sin Tk en la lógica; testeable en CI Linux):
  · `ACTIONS`: registro ordenado id → (etiqueta, grupo, acordes por defecto). Los ids
    son estables (`transport.play_pause`, `nav.next_edge`, `edit.split`, …).
  · Acordes como texto: "L", "Shift+L", "Ctrl+Shift+Z", "space", "comma", "Left",
    "KP_Add". `parse_chord`/`format_chord` normalizan mayúsculas y orden de
    modificadores (Ctrl, Alt, Shift).
  · `chord_from_event(keysym, state)` traduce el evento Tk: Shift 0x1, Control 0x4,
    Alt 0x20000 en Windows y 0x8 (Mod1) en Linux; NumLock y CapsLock se ignoran. Las
    letras se resuelven por `keysym.lower()` para que Shift+letra funcione igual con
    CapsLock. En Windows, AltGr (Ctrl+Alt) con un carácter que no es letra ni dígito
    es el modo normal de escribir `[`, `]` o `@` en teclados latinos: se trata como
    tecla sin modificadores.
  · `Keymap.load()`: defaults + `keymap.json` en `app_paths.CONFIG_DIR` (por máquina).
    Ids desconocidos se ignoran con aviso; conflictos (un acorde en dos acciones) se
    reportan y gana el primero en orden de registro. `resolve(chord)` es un lookup de
    diccionario: O(1) por tecla, sin regex por evento.
  · `save(overrides)` escribe solo las diferencias con los defaults; `reload()`
    recarga el singleton: los editores resuelven por el singleton en cada evento, así
    que un cambio en Ajustes aplica al instante sin re-bindear.
"""
from __future__ import annotations

import json
import sys
from collections import OrderedDict
from pathlib import Path

SCHEMA = "keymap/1"
MODIFIERS = ("Ctrl", "Alt", "Shift")
_MOD_ALIASES = {"control": "Ctrl", "ctrl": "Ctrl", "alt": "Alt", "option": "Alt",
                "shift": "Shift"}
_KEY_ALIASES = {" ": "space", "spacebar": "space", "esc": "Escape", "enter": "Return",
                "del": "Delete", "supr": "Delete", "backspace": "BackSpace", "tab": "Tab",
                "+": "plus", "-": "minus", "=": "equal", ",": "comma", ".": "period",
                "[": "bracketleft", "]": "bracketright", "up": "Up", "down": "Down",
                "left": "Left", "right": "Right", "home": "Home", "end": "End"}
# keysyms con nombre que NO son letras: se conservan tal cual (case-sensitive)
_NAMED = {"space", "comma", "period", "plus", "minus", "equal", "bracketleft",
          "bracketright", "semicolon", "slash", "backslash", "apostrophe", "grave",
          "less", "greater", "question", "colon", "quotedbl", "asterisk", "underscore",
          "exclam", "at", "numbersign", "dollar", "percent", "ampersand", "parenleft",
          "parenright", "braceleft", "braceright", "bar", "asciitilde", "asciicircum",
          "Left", "Right", "Up", "Down", "Home", "End", "Prior", "Next", "Insert", "Delete",
          "BackSpace", "Tab", "Return", "Escape", "Menu", "Pause", "Print",
          "KP_Add", "KP_Subtract", "KP_Multiply", "KP_Divide", "KP_Enter", "KP_Decimal",
          "KP_Insert", "KP_Delete", "KP_Home", "KP_End", "KP_Prior", "KP_Next",
          "KP_Left", "KP_Right", "KP_Up", "KP_Down"} | {f"F{i}" for i in range(1, 25)} \
         | {f"KP_{i}" for i in range(10)}
_MODIFIER_KEYSYMS = {"Shift_L", "Shift_R", "Control_L", "Control_R", "Alt_L", "Alt_R",
                     "Meta_L", "Meta_R", "Super_L", "Super_R", "Win_L", "Win_R",
                     "Caps_Lock", "Num_Lock", "Scroll_Lock", "ISO_Level3_Shift",
                     "Mode_switch", "Hyper_L", "Hyper_R"}
# teclas de navegación que sí admiten Ctrl+Alt como modificadores reales en Windows
_NAV_KEYS = {"Left", "Right", "Up", "Down", "Home", "End", "Prior", "Next", "Insert",
             "Delete", "BackSpace", "Tab", "Return", "Escape", "space"}

# Estados Tk
STATE_SHIFT = 0x1
STATE_CONTROL = 0x4
STATE_ALT_LINUX = 0x8          # Mod1 (en Windows 0x8 es NumLock)
STATE_ALT_WINDOWS = 0x20000


class Action:
    __slots__ = ("id", "label", "group", "defaults")

    def __init__(self, identifier, label, group, defaults):
        self.id, self.label, self.group = identifier, label, group
        self.defaults = tuple(normalize_chord(c) for c in defaults)

    def __repr__(self):
        return f"Action({self.id!r}, {self.defaults!r})"


ACTIONS: "OrderedDict[str, Action]" = OrderedDict()
GROUPS = ("Transporte", "Navegación", "Edición", "Herramientas", "Vista", "Marcas")


def _register(identifier, label, group, *defaults):
    if identifier in ACTIONS:
        raise ValueError(f"acción duplicada: {identifier}")
    ACTIONS[identifier] = Action(identifier, label, group, defaults)


# ---- acordes ----
def normalize_key(key: str) -> str:
    key = (key or "").strip()
    if not key:
        raise ValueError("tecla vacía")
    low = key.lower()
    if low in _KEY_ALIASES:
        return _KEY_ALIASES[low]
    if len(key) == 1:
        return key.upper() if key.isalpha() else key
    if key in _NAMED:
        return key
    for name in _NAMED:
        if name.lower() == low:
            return name
    if low.startswith("kp_") or low.startswith("f") and low[1:].isdigit():
        return key
    return key                      # keysym desconocido: se conserva tal cual


def parse_chord(text: str) -> tuple[frozenset, str]:
    """«Ctrl+Shift+Z» → ({'Ctrl','Shift'}, 'Z'). Tolera orden, mayúsculas y alias."""
    if not isinstance(text, str) or not text.strip():
        raise ValueError("acorde vacío")
    text = text.strip()
    if text == "+":
        return frozenset(), "plus"
    if text.endswith("++"):                    # «Ctrl++» = Ctrl + la tecla «+»
        parts = text[:-2].split("+") + ["+"]
    else:
        parts = text.split("+")
    parts = [p.strip() for p in parts]
    if any(p == "" for p in parts):
        raise ValueError(f"acorde inválido: {text}")
    mods, key = set(), None
    for index, part in enumerate(parts):
        alias = _MOD_ALIASES.get(part.lower())
        if alias and index < len(parts) - 1:
            mods.add(alias)
        elif alias:
            raise ValueError(f"acorde sin tecla: {text}")
        elif key is None:
            key = normalize_key(part)
        else:
            raise ValueError(f"acorde inválido: {text}")
    if key is None:
        raise ValueError(f"acorde sin tecla: {text}")
    return frozenset(mods), key


def format_chord(mods, key: str) -> str:
    ordered = [m for m in MODIFIERS if m in set(mods)]
    return "+".join(ordered + [key])


def normalize_chord(text: str) -> str:
    return format_chord(*parse_chord(text))


# ---- inventario (diseño §3; el orden de registro decide los conflictos) ----
_register("transport.play_pause", "Play / pausa", "Transporte", "space")
_register("transport.pause", "Pausa", "Transporte", "K")
_register("transport.faster", "Más rápido (×1→×2→×3→×4→×8; pausado = play a ×1)", "Transporte", "L")
_register("transport.slower", "Más lento (a ×1 pausa)", "Transporte", "J")
_register("transport.rate_1", "Velocidad ×1", "Transporte", "1")
_register("transport.rate_2", "Velocidad ×2", "Transporte", "2")
_register("transport.rate_3", "Velocidad ×3", "Transporte", "3")
_register("transport.rate_4", "Velocidad ×4", "Transporte", "4")
_register("transport.skim", "Skim ×8 (solo fotogramas clave)", "Transporte", "Shift+L")
_register("transport.play_from_item", "Play desde el item seleccionado (o el IN)", "Transporte", "Shift+space")

_register("nav.frame_prev", "Fotograma anterior", "Navegación", "comma")
_register("nav.frame_next", "Fotograma siguiente", "Navegación", "period")
_register("nav.frame_prev_10", "10 fotogramas atrás", "Navegación", "Shift+comma")
_register("nav.frame_next_10", "10 fotogramas adelante", "Navegación", "Shift+period")
_register("nav.step_prev", "Paso atrás (0,5 s)", "Navegación", "Left")
_register("nav.step_next", "Paso adelante (0,5 s)", "Navegación", "Right")
_register("nav.step_prev_5", "Paso atrás (5 s)", "Navegación", "Shift+Left")
_register("nav.step_next_5", "Paso adelante (5 s)", "Navegación", "Shift+Right")
_register("nav.home", "Inicio del medio", "Navegación", "Home")
_register("nav.end", "Fin del medio", "Navegación", "End")
_register("nav.prev_edge", "Borde anterior", "Navegación", "Up")
_register("nav.next_edge", "Borde siguiente", "Navegación", "Down")
_register("nav.prev_silence", "Silencio anterior", "Navegación", "Ctrl+Up")
_register("nav.next_silence", "Silencio siguiente", "Navegación", "Ctrl+Down")
_register("nav.goto", "Ir a tiempo…", "Navegación", "Ctrl+G")
_register("nav.sel_start", "Inicio de la selección", "Navegación", "Shift+I")
_register("nav.sel_end", "Fin de la selección", "Navegación", "Shift+O")

_register("edit.split", "Dividir en el playhead", "Edición", "S")
_register("edit.trim_start", "Recortar inicio al playhead", "Edición", "bracketleft")
_register("edit.trim_end", "Recortar fin al playhead", "Edición", "bracketright")
_register("edit.nudge_prev", "Empujar 1 fotograma atrás", "Edición", "Alt+Left")
_register("edit.nudge_next", "Empujar 1 fotograma adelante", "Edición", "Alt+Right")
_register("edit.nudge_prev_10", "Empujar 10 fotogramas atrás", "Edición", "Alt+Shift+Left")
_register("edit.nudge_next_10", "Empujar 10 fotogramas adelante", "Edición", "Alt+Shift+Right")
_register("edit.item_prev", "Item anterior del carril", "Edición", "Shift+Tab")
_register("edit.item_next", "Item siguiente del carril", "Edición", "Tab")
_register("edit.accept", "Aceptar (marca de revisión: se corta igual que un propuesto)", "Edición", "E")
_register("edit.accept_next", "Aceptar y pasar al siguiente", "Edición", "Shift+E")
_register("edit.toggle", "Desactivar (no se corta; en una marca cicla la decisión)", "Edición", "X")
_register("edit.activate", "Activar: vuelve a propuesto (se corta)", "Edición", "P")
_register("edit.delete", "Borrar", "Edición", "Delete", "BackSpace", "D")
_register("edit.edit", "Editar", "Edición", "Return", "F2")
_register("edit.deselect", "Deseleccionar", "Edición", "Escape")
_register("edit.undo", "Deshacer", "Edición", "Ctrl+Z")
_register("edit.redo", "Rehacer", "Edición", "Ctrl+R", "Ctrl+Shift+Z", "Ctrl+Y")

_register("tools.select", "Herramienta Selección", "Herramientas", "A", "V")
_register("tools.cut", "Herramienta Corte", "Herramientas", "B")
_register("tools.select_all", "Seleccionar todo el carril", "Herramientas", "Ctrl+A")
_register("layers.new_lane", "Añadir capa…", "Herramientas", "Ctrl+N")

_register("view.zoom_in", "Zoom +", "Vista", "plus", "equal", "KP_Add")
_register("view.zoom_out", "Zoom −", "Vista", "minus", "KP_Subtract")
_register("view.fit", "Ver todo", "Vista", "Shift+Z")
_register("view.zoom_sel", "Zoom a la selección", "Vista", "Z")
_register("view.follow", "Seguir al playhead", "Vista", "F")
_register("view.center", "Centrar el playhead", "Vista", "C")
_register("view.skip_trims", "Saltar recortes al reproducir", "Vista", "Shift+T")
_register("loop.set_in", "Repetir: entrada en el playhead (o click derecho en la regla)", "Vista")
_register("loop.set_out", "Repetir: salida en el playhead (o Ctrl+click derecho en la regla)", "Vista")
_register("loop.clear", "Repetir: quitar el rango (o Shift+click derecho en la regla)", "Vista")

_register("marks.point", "Marca puntual", "Marcas", "M")
_register("marks.in", "IN de región", "Marcas", "I")
_register("marks.out", "OUT de región", "Marcas", "O")


def chord_from_event(keysym: str, state: int, platform: str | None = None) -> str | None:
    """Acorde del evento Tk, o None para una tecla que no forma acorde (un modificador
    solo, keysym vacío)."""
    if not keysym or keysym in _MODIFIER_KEYSYMS or keysym == "??":
        return None
    platform = platform or sys.platform
    state = int(state or 0)
    shift = bool(state & STATE_SHIFT)
    ctrl = bool(state & STATE_CONTROL)
    alt = bool(state & (STATE_ALT_WINDOWS if platform.startswith("win") else STATE_ALT_LINUX))
    if len(keysym) == 1:
        key = keysym.upper() if keysym.isalpha() else keysym
    else:
        key = keysym
    if platform.startswith("win") and ctrl and alt:
        # AltGr en teclados latinos: `]`, `[`, `@`, `{`… llegan con Ctrl+Alt
        if not (len(key) == 1 and key.isalnum()) and key not in _NAV_KEYS \
                and not key.startswith("F") and not key.startswith("KP_"):
            ctrl = alt = False
    mods = set()
    if ctrl:
        mods.add("Ctrl")
    if alt:
        mods.add("Alt")
    if shift:
        mods.add("Shift")
    return format_chord(mods, key)


# ---- keymap ----
def config_path() -> Path:
    import app_paths
    return Path(app_paths.CONFIG_DIR) / "keymap.json"


class Keymap:
    def __init__(self, overrides: dict | None = None):
        self.overrides: dict[str, list[str]] = {}
        self.warnings: list[str] = []
        self.conflicts: list[tuple[str, str, str]] = []     # (acorde, gana, pierde)
        self._chords: dict[str, tuple[str, ...]] = {}
        self._bindings: dict[str, str] = {}
        for identifier, chords in (overrides or {}).items():
            if identifier not in ACTIONS:
                self.warnings.append(f"acción desconocida en keymap.json: {identifier}")
                continue
            if not isinstance(chords, list):
                self.warnings.append(f"{identifier}: los acordes deben ser una lista")
                continue
            clean = []
            for chord in chords:
                try:
                    clean.append(normalize_chord(chord))
                except ValueError as error:
                    self.warnings.append(f"{identifier}: {error}")
            if tuple(clean) != ACTIONS[identifier].defaults:
                self.overrides[identifier] = clean
        for identifier, action in ACTIONS.items():
            chords = tuple(self.overrides.get(identifier, action.defaults))
            self._chords[identifier] = chords
            for chord in chords:
                if chord in self._bindings:
                    self.conflicts.append((chord, self._bindings[chord], identifier))
                    continue
                self._bindings[chord] = identifier

    def resolve(self, chord: str | None) -> str | None:
        if chord is None:
            return None
        return self._bindings.get(chord)

    def chords(self, identifier: str) -> tuple[str, ...]:
        return self._chords.get(identifier, ())

    def chord_text(self, identifier: str) -> str:
        return " · ".join(self.chords(identifier))

    def owner(self, chord: str) -> str | None:
        return self._bindings.get(chord)

    def conflict_for(self, identifier: str) -> list[tuple[str, str]]:
        """[(acorde, id de la otra acción)] de los acordes de `identifier` que pierden."""
        return [(chord, winner) for chord, winner, loser in self.conflicts if loser == identifier]

    def as_overrides(self) -> dict[str, list[str]]:
        return {k: list(v) for k, v in self.overrides.items()}

    def with_chords(self, identifier: str, chords) -> "Keymap":
        overrides = self.as_overrides()
        overrides[identifier] = list(chords)
        return Keymap(overrides)

    @classmethod
    def load(cls, path: str | Path | None = None) -> "Keymap":
        path = Path(path) if path is not None else config_path()
        overrides = {}
        warnings = []
        try:
            if path.is_file():
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, dict) and isinstance(data.get("bindings"), dict):
                    overrides = data["bindings"]
                else:
                    warnings.append(f"{path.name}: formato desconocido; se usan los atajos por defecto")
        except (OSError, ValueError) as error:
            warnings.append(f"{path.name}: {error}; se usan los atajos por defecto")
        keymap = cls(overrides)
        keymap.warnings = warnings + keymap.warnings
        return keymap


def save(overrides: dict, path: str | Path | None = None) -> Path:
    """Escribe SOLO las diferencias con los defaults (una lista vacía = sin atajo)."""
    path = Path(path) if path is not None else config_path()
    diff = {}
    for identifier, chords in (overrides or {}).items():
        if identifier not in ACTIONS:
            continue
        clean = tuple(normalize_chord(c) for c in chords)
        if clean != ACTIONS[identifier].defaults:
            diff[identifier] = list(clean)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps({"schema": SCHEMA, "bindings": diff}, ensure_ascii=False, indent=2) + "\n"
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)
    return path


_CURRENT: Keymap | None = None


def current() -> Keymap:
    global _CURRENT
    if _CURRENT is None:
        _CURRENT = Keymap.load()
    return _CURRENT


def reload(path: str | Path | None = None) -> Keymap:
    global _CURRENT
    _CURRENT = Keymap.load(path)
    return _CURRENT


def resolve_event(keysym: str, state: int) -> str | None:
    """Atajo de UN evento Tk → id de acción (o None). Un lookup por tecla."""
    return current().resolve(chord_from_event(keysym, state))


# ---- menú / barra: el mismo inventario, para botones y click derecho ----
_PRETTY = {"period": ".", "comma": ",", "bracketleft": "[", "bracketright": "]", "plus": "+",
           "minus": "-", "equal": "=", "space": "Espacio", "Delete": "Supr", "BackSpace": "Retroceso",
           "Return": "Enter", "Escape": "Esc", "Left": "←", "Right": "→", "Up": "↑", "Down": "↓",
           "Home": "Inicio", "End": "Fin", "KP_Add": "Num +", "KP_Subtract": "Num −", "Prior": "RePág",
           "Next": "AvPág"}


def pretty_chord(chord: str) -> str:
    """Acorde legible para tooltips y menús: «Ctrl+→», «Supr», «.», «Espacio»."""
    mods, key = parse_chord(chord)
    return format_chord(mods, _PRETTY.get(key, key))


def pretty_chords(identifier: str, km: Keymap | None = None) -> str:
    km = km or current()
    return " · ".join(pretty_chord(c) for c in km.chords(identifier))


def menu_groups(available, km: Keymap | None = None) -> list:
    """[(grupo, [(id, etiqueta, acordes como texto)])] de las acciones `available`
    (ids), en el orden de registro. Es lo que dibujan el menú contextual y los
    tooltips de la barra: todo lo que hace una tecla se puede hacer con el mouse."""
    km = km or current()
    available = set(available)
    groups = []
    for group in GROUPS:
        rows = [(a.id, a.label, pretty_chords(a.id, km)) for a in ACTIONS.values()
                if a.group == group and a.id in available]
        if rows:
            groups.append((group, rows))
    return groups


def tooltip_text(identifier: str, km: Keymap | None = None) -> str:
    """«Etiqueta  ·  Acorde» para el tooltip de un botón (sin acorde si no tiene)."""
    km = km or current()
    action = ACTIONS.get(identifier)
    if action is None:
        return identifier
    chords = pretty_chords(identifier, km)
    return f"{action.label}  ·  {chords}" if chords else action.label
