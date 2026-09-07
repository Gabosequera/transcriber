"""Deshacer / rehacer del timeline (diseño docs/diseno-navegacion-editor.md §4). Puro.

`HistoryStack` es una pila de OPERACIONES con profundidad 50, por medio cargado (no se
persiste). Cada entrada guarda `before`/`after`: snapshots profundos de los documentos
que tocó, por `doc_id` (`autor`, `trims`, `plan`, `layer:<id>`, `lanes`), y `expect`:
la revisión que cada documento debe tener para que la entrada siga siendo válida. Una
operación puede tocar varios documentos y sigue siendo UNA entrada.

El que restaura (el controlador de capas) escribe por los mismos caminos de guardado de
siempre y después llama a `settle()` con las revisiones nuevas; si un documento cambió
por fuera (su revisión no es la esperada, por ejemplo la AI escribió mientras tanto) la
entrada se descarta con `Stale` en vez de pisar. Una acción nueva tras un undo descarta
la rama de redo, como en cualquier editor.
"""
from __future__ import annotations

import copy

DEPTH = 50


class Stale(Exception):
    def __init__(self, entry, doc_id, expected, actual):
        super().__init__(f"«{entry.label}»: {doc_id} cambió por fuera "
                         f"(revisión {actual}, se esperaba {expected})")
        self.entry, self.doc_id, self.expected, self.actual = entry, doc_id, expected, actual


class Entry:
    __slots__ = ("label", "before", "after", "expect")

    def __init__(self, label, before, after, expect):
        self.label = str(label)
        self.before = copy.deepcopy(before)
        self.after = copy.deepcopy(after)
        self.expect = dict(expect)

    @property
    def docs(self):
        return tuple(self.before)

    def __repr__(self):
        return f"Entry({self.label!r}, docs={self.docs})"


class HistoryStack:
    def __init__(self, depth: int = DEPTH):
        self.depth = max(1, int(depth))
        self._undo: list[Entry] = []
        self._redo: list[Entry] = []

    # ---- estado ----
    def __len__(self):
        return len(self._undo)

    def can_undo(self) -> bool:
        return bool(self._undo)

    def can_redo(self) -> bool:
        return bool(self._redo)

    def peek_undo(self) -> Entry | None:
        return self._undo[-1] if self._undo else None

    def peek_redo(self) -> Entry | None:
        return self._redo[-1] if self._redo else None

    def clear(self):
        self._undo.clear()
        self._redo.clear()

    # ---- registro ----
    def record(self, label, before: dict, after: dict, revisions: dict) -> Entry | None:
        """Registra una operación ya ESCRITA con éxito. `before`/`after` mapean
        doc_id → snapshot (None = el documento no existía); `revisions` son las
        revisiones actuales (tras escribir) de esos documentos. Si nada cambió
        (`before == after`) no se registra."""
        if set(before) != set(after):
            raise ValueError("before y after deben cubrir los mismos documentos")
        if before == after:
            return None
        entry = Entry(label, before, after, {doc: revisions.get(doc) for doc in before})
        self._undo.append(entry)
        self._redo.clear()
        del self._undo[:-self.depth]
        return entry

    # ---- deshacer / rehacer ----
    def _take(self, stack, revisions_now: dict) -> Entry:
        entry = stack.pop()
        for doc, expected in entry.expect.items():
            actual = revisions_now.get(doc)
            if expected != actual:
                raise Stale(entry, doc, expected, actual)
        return entry

    def undo(self, revisions_now: dict) -> Entry:
        """Saca la última operación si sus documentos siguen en la revisión esperada
        (si no, la descarta y lanza `Stale`). El que llama restaura `entry.before` y
        después `settle(entry, revisiones nuevas)`; la entrada pasa a la pila de redo."""
        if not self._undo:
            raise LookupError("nada que deshacer")
        entry = self._take(self._undo, revisions_now)
        self._redo.append(entry)
        return entry

    def redo(self, revisions_now: dict) -> Entry:
        if not self._redo:
            raise LookupError("nada que rehacer")
        entry = self._take(self._redo, revisions_now)
        self._undo.append(entry)
        return entry

    def settle(self, entry: Entry, revisions_now: dict):
        """Tras restaurar (por los caminos de guardado, que suben la revisión) fija
        las revisiones que la entrada esperará la próxima vez."""
        entry.expect = {doc: revisions_now.get(doc) for doc in entry.before}

    def discard(self, entry: Entry):
        """Una restauración que falló a medias: la entrada no vuelve a intentarse."""
        for stack in (self._undo, self._redo):
            if entry in stack:
                stack.remove(entry)
