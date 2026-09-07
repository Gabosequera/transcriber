"""Atajos configurables (keymap.py, puro): parseo, evento Tk, conflictos y overrides."""
import json
import tempfile
import unittest
from pathlib import Path

import keymap


class ChordTests(unittest.TestCase):
    def test_parse_and_format_normalize_case_aliases_and_modifier_order(self):
        self.assertEqual(keymap.normalize_chord("shift+ctrl+z"), "Ctrl+Shift+Z")
        self.assertEqual(keymap.normalize_chord("Control+alt+left"), "Ctrl+Alt+Left")
        self.assertEqual(keymap.normalize_chord("l"), "L")
        self.assertEqual(keymap.normalize_chord(" space "), "space")
        self.assertEqual(keymap.normalize_chord("Esc"), "Escape")
        self.assertEqual(keymap.normalize_chord("kp_add"), "KP_Add")
        self.assertEqual(keymap.normalize_chord("Shift+,"), "Shift+comma")
        self.assertEqual(keymap.normalize_chord("]"), "bracketright")
        self.assertEqual(keymap.normalize_chord("+"), "plus")
        self.assertEqual(keymap.normalize_chord("Ctrl++"), "Ctrl+plus")
        for bad in ("", "Ctrl+", "Ctrl+Shift", "A+B"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                keymap.parse_chord(bad)

    def test_event_letters_ignore_capslock_and_shift_is_explicit(self):
        self.assertEqual(keymap.chord_from_event("l", 0, "win32"), "L")
        self.assertEqual(keymap.chord_from_event("L", keymap.STATE_SHIFT, "win32"), "Shift+L")
        # CapsLock activo: keysym mayúscula sin Shift → tecla simple
        self.assertEqual(keymap.chord_from_event("L", 0x2, "win32"), "L")
        # CapsLock + Shift: keysym minúscula con Shift → Shift+L
        self.assertEqual(keymap.chord_from_event("l", keymap.STATE_SHIFT | 0x2, "linux"), "Shift+L")
        self.assertEqual(keymap.chord_from_event("space", keymap.STATE_SHIFT, "win32"), "Shift+space")
        self.assertEqual(keymap.chord_from_event("Delete", 0, "win32"), "Delete")
        self.assertIsNone(keymap.chord_from_event("Shift_L", keymap.STATE_SHIFT, "win32"))
        self.assertIsNone(keymap.chord_from_event("", 0, "win32"))

    def test_alt_bit_depends_on_platform_and_numlock_is_ignored(self):
        self.assertEqual(keymap.chord_from_event("Left", keymap.STATE_ALT_WINDOWS, "win32"), "Alt+Left")
        self.assertEqual(keymap.chord_from_event("Left", keymap.STATE_ALT_LINUX, "linux"), "Alt+Left")
        # en Windows 0x8 es NumLock, no Alt
        self.assertEqual(keymap.chord_from_event("Left", keymap.STATE_ALT_LINUX, "win32"), "Left")
        self.assertEqual(keymap.chord_from_event("Left", keymap.STATE_ALT_WINDOWS, "linux"), "Left")
        self.assertEqual(keymap.chord_from_event("z", keymap.STATE_CONTROL | keymap.STATE_SHIFT, "win32"),
                         "Ctrl+Shift+Z")
        self.assertEqual(keymap.chord_from_event("Right", keymap.STATE_ALT_WINDOWS | keymap.STATE_SHIFT,
                                                 "win32"), "Alt+Shift+Right")

    def test_altgr_characters_on_windows_are_plain_keys(self):
        altgr = keymap.STATE_CONTROL | keymap.STATE_ALT_WINDOWS
        self.assertEqual(keymap.chord_from_event("bracketright", altgr, "win32"), "bracketright")
        self.assertEqual(keymap.chord_from_event("at", altgr, "win32"), "at")
        # Ctrl+Alt con letra, dígito o tecla de navegación siguen siendo modificadores reales
        self.assertEqual(keymap.chord_from_event("a", altgr, "win32"), "Ctrl+Alt+A")
        self.assertEqual(keymap.chord_from_event("Left", altgr, "win32"), "Ctrl+Alt+Left")
        self.assertEqual(keymap.chord_from_event("bracketright", altgr, "linux"), "Ctrl+bracketright")


class KeymapTests(unittest.TestCase):
    def test_defaults_resolve_and_cover_the_current_keys_one_to_one(self):
        km = keymap.Keymap()
        self.assertEqual(km.conflicts, [])
        self.assertEqual(km.warnings, [])
        expected = {"space": "transport.play_pause", "Left": "nav.step_prev", "Shift+Right": "nav.step_next_5",
                    "Home": "nav.home", "End": "nav.end", "plus": "view.zoom_in", "equal": "view.zoom_in",
                    "KP_Add": "view.zoom_in", "minus": "view.zoom_out", "KP_Subtract": "view.zoom_out",
                    "Shift+Z": "view.fit", "M": "marks.point", "I": "marks.in", "O": "marks.out",
                    "X": "edit.toggle", "P": "edit.activate", "Delete": "edit.delete", "BackSpace": "edit.delete", "D": "edit.delete",
                    "Return": "edit.edit", "F2": "edit.edit", "Escape": "edit.deselect",
                    "L": "transport.faster", "Shift+L": "transport.skim", "J": "transport.slower",
                    "K": "transport.pause", "1": "transport.rate_1", "4": "transport.rate_4",
                    "Ctrl+Z": "edit.undo", "Ctrl+R": "edit.redo", "Ctrl+Shift+Z": "edit.redo",
                    "Ctrl+Y": "edit.redo", "B": "tools.cut", "V": "tools.select", "A": "tools.select",
                    "Ctrl+A": "tools.select_all", "Ctrl+N": "layers.new_lane",
                    "bracketleft": "edit.trim_start", "bracketright": "edit.trim_end",
                    "S": "edit.split", "E": "edit.accept", "Shift+E": "edit.accept_next",
                    "Tab": "edit.item_next", "Shift+Tab": "edit.item_prev", "Up": "nav.prev_edge",
                    "Ctrl+Down": "nav.next_silence", "Ctrl+G": "nav.goto", "comma": "nav.frame_prev",
                    "Shift+period": "nav.frame_next_10", "Alt+Left": "edit.nudge_prev",
                    "Alt+Shift+Right": "edit.nudge_next_10", "Z": "view.zoom_sel", "F": "view.follow",
                    "C": "view.center", "Shift+T": "view.skip_trims"}
        for chord, action in expected.items():
            with self.subTest(chord=chord):
                self.assertEqual(km.resolve(chord), action)
        self.assertIsNone(km.resolve("Ctrl+Alt+Q"))
        self.assertIsNone(km.resolve(None))
        # todo id tiene etiqueta y grupo conocido
        for action in keymap.ACTIONS.values():
            self.assertIn(action.group, keymap.GROUPS)
            self.assertTrue(action.label)

    def test_partial_overrides_conflicts_and_unknown_ids(self):
        km = keymap.Keymap({"edit.split": ["Ctrl+S", "s"], "nav.next_edge": [],
                            "transport.pause": ["space"], "no.such": ["Q"], "view.fit": "Z"})
        self.assertEqual(km.resolve("Ctrl+S"), "edit.split")
        self.assertEqual(km.resolve("S"), "edit.split")
        self.assertIsNone(km.resolve("Down"))
        self.assertEqual(km.chords("nav.next_edge"), ())
        # el acorde en dos acciones: gana la primera registrada, el conflicto se reporta
        self.assertEqual(km.resolve("space"), "transport.play_pause")
        self.assertEqual(km.conflicts, [("space", "transport.play_pause", "transport.pause")])
        self.assertEqual(km.conflict_for("transport.pause"), [("space", "transport.play_pause")])
        self.assertTrue(any("no.such" in w for w in km.warnings))
        self.assertTrue(any("view.fit" in w for w in km.warnings))
        self.assertEqual(km.resolve("Shift+Z"), "view.fit")        # el override inválido no aplica
        self.assertEqual(set(km.as_overrides()), {"edit.split", "nav.next_edge", "transport.pause"})
        # un override igual al default no cuenta como override
        self.assertEqual(keymap.Keymap({"edit.split": ["s"]}).as_overrides(), {})
        changed = km.with_chords("transport.pause", ["K"])
        self.assertEqual(changed.conflicts, [])
        self.assertEqual(km.resolve("space"), "transport.play_pause")

    def test_save_writes_only_differences_and_load_reads_them_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "keymap.json"
            keymap.save({"edit.split": ["ctrl+s"], "view.fit": ["Shift+Z"], "nav.goto": []}, path)
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["schema"], keymap.SCHEMA)
            self.assertEqual(data["bindings"], {"edit.split": ["Ctrl+S"], "nav.goto": []})
            km = keymap.Keymap.load(path)
            self.assertEqual(km.resolve("Ctrl+S"), "edit.split")
            self.assertIsNone(km.resolve("S"))
            self.assertIsNone(km.resolve("Ctrl+G"))
            self.assertEqual(km.warnings, [])
            path.write_text("{no json", encoding="utf-8")
            broken = keymap.Keymap.load(path)
            self.assertEqual(broken.resolve("S"), "edit.split")
            self.assertTrue(broken.warnings)
            self.assertEqual(keymap.Keymap.load(Path(tmp) / "nada.json").as_overrides(), {})
            current = keymap.reload(path)
            self.assertIs(keymap.current(), current)
            keymap.reload(Path(tmp) / "nada.json")

    def test_resolve_is_a_single_dictionary_lookup(self):
        km = keymap.Keymap()
        self.assertIsInstance(km._bindings, dict)
        self.assertEqual(km._bindings["Ctrl+Shift+Z"], "edit.redo")


if __name__ == "__main__":
    unittest.main()


class MenuTests(unittest.TestCase):
    def test_menu_groups_follow_registration_order_and_show_chords(self):
        km = keymap.Keymap({"edit.split": ["Ctrl+S"]})
        groups = keymap.menu_groups({"edit.split", "transport.play_pause", "nav.home", "no.such"}, km)
        self.assertEqual([g for g, _ in groups], ["Transporte", "Navegación", "Edición"])
        self.assertEqual(groups[2][1], [("edit.split", keymap.ACTIONS["edit.split"].label, "Ctrl+S")])
        self.assertEqual(groups[0][1][0][2], "Espacio")
        self.assertEqual(keymap.pretty_chord("Ctrl+Right"), "Ctrl+→")
        self.assertEqual(keymap.pretty_chord("period"), ".")
        self.assertEqual(keymap.pretty_chords("edit.delete", keymap.Keymap()), "Supr · Retroceso · D")
        self.assertEqual(keymap.menu_groups(set(), km), [])
        self.assertEqual(keymap.tooltip_text("edit.split", km), f"{keymap.ACTIONS['edit.split'].label}  ·  Ctrl+S")
        self.assertEqual(keymap.tooltip_text("edit.split", keymap.Keymap({"edit.split": []})),
                         keymap.ACTIONS["edit.split"].label)
        self.assertEqual(keymap.tooltip_text("otro", km), "otro")
