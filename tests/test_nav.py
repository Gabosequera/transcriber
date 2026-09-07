"""Navegación tipo editor (editorial_nav.py, puro): índice de bordes y «ir a tiempo»."""
import random
import time
import unittest

import editorial_nav as nav


class EdgeIndexTests(unittest.TestCase):
    def test_prev_next_are_strict_with_tolerance_and_unique(self):
        index = nav.EdgeIndex([5, 1, 3, 3.0, 3.0004, 9])
        self.assertEqual(index.times, [1, 3, 5, 9])
        self.assertEqual(index.next(0), 1)
        self.assertEqual(index.next(3), 5)              # sobre un borde salta al siguiente
        self.assertEqual(index.next(2.9995), 5)         # tolerancia de 1 ms
        self.assertEqual(index.prev(3), 1)
        self.assertEqual(index.prev(3.0005), 1)
        self.assertIsNone(index.prev(1))
        self.assertIsNone(index.next(9))
        self.assertEqual(index.prev(100), 9)
        self.assertEqual(index.nearest(4.9, radius=.15), 5)
        self.assertIsNone(index.nearest(4, radius=.5))
        self.assertEqual(len(nav.EdgeIndex([])), 0)
        self.assertIsNone(nav.EdgeIndex([]).next(0))

    def test_edges_come_from_every_lane_and_points_count_once(self):
        layers = [dict(items=[dict(ranges=[dict(t_ini=1, t_fin=2), dict(t_ini=7, t_fin=8)])]),
                  dict(items=[dict(ranges=[dict(t_ini=4, t_fin=4)])]),
                  dict(items=[])]
        self.assertEqual(sorted(nav.edge_times(layers)), [1, 2, 4, 7, 8])
        trims = dict(cuts=[dict(origin="silence", t_ini=1, t_fin=2, enabled=True),
                           dict(origin="ai", t_ini=3, t_fin=4, enabled=True),
                           dict(origin="silence", t_ini=5, t_fin=6, enabled=False)])
        self.assertEqual(nav.silence_times(trims), [1, 2, 5, 6])
        self.assertEqual(nav.silence_times(None), [])

    def test_jump_is_logarithmic_over_thousands_of_cuts(self):
        random.seed(1)
        times = [random.uniform(0, 10800) for _ in range(20000)]
        index = nav.EdgeIndex(times)
        start = time.perf_counter()
        t = 0.0
        hops = 0
        while (t := index.next(t)) is not None and hops < 5000:
            hops += 1
        elapsed = time.perf_counter() - start
        self.assertEqual(hops, 5000)
        self.assertLess(elapsed, 0.2, f"{elapsed:.3f}s para 5000 saltos")


class GotoTests(unittest.TestCase):
    def test_absolute_and_relative_forms_are_clamped(self):
        self.assertEqual(nav.parse_goto("1:23:45.6", 0, 10000), 5025.6)
        self.assertEqual(nav.parse_goto("5025", 0, 10000), 5025)
        self.assertEqual(nav.parse_goto("2:05", 0, 10000), 125)
        self.assertEqual(nav.parse_goto("+30", 100, 10000), 130)
        self.assertEqual(nav.parse_goto("-10", 100, 10000), 90)
        self.assertEqual(nav.parse_goto("+1:30", 100, 10000), 190)
        self.assertEqual(nav.parse_goto("-500", 100, 10000), 0)
        self.assertEqual(nav.parse_goto("99999", 100, 10000), 10000)
        self.assertEqual(nav.parse_goto("12,5", 0, 100), 12.5)
        for bad in ("", "abc", "1:99", "--5", "+"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                nav.parse_goto(bad, 0, 100)
        self.assertAlmostEqual(nav.frame_step(30), 1 / 30)
        self.assertAlmostEqual(nav.frame_step(None), 1 / 30)
        self.assertAlmostEqual(nav.frame_step(59.94), 1 / 59.94)


if __name__ == "__main__":
    unittest.main()
