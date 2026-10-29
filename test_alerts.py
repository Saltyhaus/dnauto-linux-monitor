"""Alert rules for dnauto_gtk.py (no GTK display needed)."""
import tempfile
import unittest
from pathlib import Path

import dnauto_gtk as g
from dnauto_protocol import AmplifierState

LIM = {"temp_f": 140, "swr": 2.5, "swr_min_watts": 20, "hold_s": 2, "mismatch_s": 2.5,
       "current_a": 0, "link_lost_s": 10}


def amp(**kw):
    s = AmplifierState()
    for k, v in kw.items():
        setattr(s, k, v)
    return s


def ids(out, action="send"):
    return [o[1] for o in out if o[0] == action]


class AlertTests(unittest.TestCase):
    def test_mismatch_needs_grace_then_alerts_once_and_clears(self):
        a, s = g.Alerts(), amp(temperature_c=30)
        self.assertEqual(a.update(s, 100.0, LIM, True, 100.0, "20", "40", True), [])
        self.assertEqual(a.update(s, 102.0, LIM, True, 102.0, "20", "40", True), [])
        self.assertEqual(ids(a.update(s, 103.0, LIM, True, 103.0, "20", "40", True)), ["mismatch"])
        self.assertEqual(a.update(s, 104.0, LIM, True, 104.0, "20", "40", True), [])
        out = a.update(s, 105.0, LIM, True, 105.0, "40", "40", True)
        self.assertEqual(ids(out, "withdraw"), ["mismatch"])
        self.assertEqual(ids(out), ["mismatch:ok"])

    def test_no_mismatch_without_fresh_rig(self):
        a, s = g.Alerts(), amp()
        for t in range(10):
            self.assertEqual(a.update(s, float(t), LIM, True, float(t), "20", "40", False), [])

    def test_protection_flags(self):
        a = g.Alerts()
        out = a.update(amp(swr_alarm=True), 1.0, LIM, True, 1.0, None, None, False)
        self.assertEqual(ids(out), ["swr_alarm"])
        self.assertTrue(out[0][4])
        out = a.update(amp(swr_alarm=False), 2.0, LIM, True, 2.0, None, None, False)
        self.assertEqual(ids(out, "withdraw"), ["swr_alarm"])

    def test_temperature_with_hysteresis(self):
        a = g.Alerts()
        a.update(amp(temperature_c=65), 0.0, LIM, True, 0.0, None, None, False)          # 149 F
        self.assertEqual(ids(a.update(amp(temperature_c=65), 2.0, LIM, True, 2.0, None, None, False)), ["temp"])
        self.assertEqual(a.update(amp(temperature_c=59), 3.0, LIM, True, 3.0, None, None, False), [])  # 138 F: within 5
        self.assertEqual(ids(a.update(amp(temperature_c=55), 4.0, LIM, True, 4.0, None, None, False), "withdraw"),
                         ["temp"])

    def test_swr_only_while_transmitting(self):
        a = g.Alerts()
        lowpower = amp(power_scale_index=1, for_power_raw=10, ref_power_raw=5)        # 4 W fwd
        for t in (0.0, 3.0):
            self.assertEqual(a.update(lowpower, t, LIM, True, t, None, None, False), [])
        bad = amp(power_scale_index=5, for_power_raw=50, ref_power_raw=12)           # 400 W / 96 W -> SWR 2.9
        a.update(bad, 10.0, LIM, True, 10.0, None, None, False)
        self.assertEqual(ids(a.update(bad, 12.0, LIM, True, 12.0, None, None, False)), ["swr"])

    def test_link_lost_only_after_it_was_linked(self):
        a = g.Alerts()
        self.assertEqual(a.update(amp(), 100.0, LIM, False, 0.0, None, None, False), [])   # never talked
        a.update(amp(), 1.0, LIM, True, 1.0, None, None, False)
        self.assertEqual(ids(a.update(amp(), 12.0, LIM, False, 1.0, None, None, False)), ["link"])
        out = a.update(amp(), 13.0, LIM, True, 13.0, None, None, False)
        self.assertEqual(ids(out), ["link:ok"])

    def test_swr_math(self):
        self.assertAlmostEqual(g.swr_from(100, 0), 1.0)
        self.assertAlmostEqual(g.swr_from(100, 11.11), 2.0, places=1)
        self.assertIsNone(g.swr_from(0, 5))


class LimitsTests(unittest.TestCase):
    def test_defaults_written_and_bad_values_fall_back(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "x" / "limits.ini"
            self.assertEqual(g.load_limits(p)["temp_f"], 140)
            p.write_text("[alarms]\ntemp_f = 120   # cooler\nswr = oops\n")
            lim = g.load_limits(p)
            self.assertEqual((lim["temp_f"], lim["swr"]), (120, 2.5))


class HistoryTests(unittest.TestCase):
    def test_sampling_span_and_gaps(self):
        h = g.History(span=60, step=2)
        self.assertTrue(h.add(0.0, 90, None, 0))
        self.assertFalse(h.add(1.0, 91, None, 0))          # faster than one sample per 2 s
        for t in range(2, 100, 2):
            h.add(float(t), 90 + t / 10, 1.5 if t % 20 < 6 else None, 100)
        self.assertLessEqual(h.points[-1][0] - h.points[0][0], 60)   # only the last minute kept
        self.assertTrue(all(v is not None for _t, v in h.series(2)))  # SWR gaps dropped, not zero
        self.assertLess(len(h.series(2)), len(h.series(1)))

    def test_status_file_atomic(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "x" / "status.json"
            g.write_status({"time": 1, "linked": True}, p)
            import json
            self.assertEqual(json.loads(p.read_text())["linked"], True)


if __name__ == "__main__":
    unittest.main()
