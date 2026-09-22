"""Unit tests for scripts/model/split_latency_model.py.

Run with:  python3 -m unittest discover -s tests/model -v

Every test uses an explicit seed, so the numbers below are reproducible.  The
bounds are deliberately loose enough to survive a change of the processing
defaults but tight enough to catch a broken connection-event assignment.
"""

import contextlib
import io
import json
import os
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
FIX = os.path.join(HERE, "fixtures")
SCRIPTS = os.path.abspath(os.path.join(HERE, "..", "..", "scripts", "model"))
SCRIPT = os.path.join(SCRIPTS, "split_latency_model.py")
sys.path.insert(0, SCRIPTS)

import split_latency_model as m  # noqa: E402


def run_cli(*argv):
    """Call main() in-process and return (exit code, stdout)."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = m.main(list(argv))
    return rc, buf.getvalue()


def cross(result):
    return result["stages"][m.MODEL_STAGE]


# --------------------------------------------------------------------------
# parameter parsing
# --------------------------------------------------------------------------
class DistTests(unittest.TestCase):
    def test_kinds_inferred_from_the_number_of_values(self):
        self.assertEqual(m.Dist.parse("3").kind, "fixed")
        self.assertEqual(m.Dist.parse("50,2000").kind, "uniform")
        self.assertEqual(m.Dist.parse("1,2,3").kind, "list")
        self.assertEqual(m.Dist.parse("uniform:50,2000").kind, "uniform")
        self.assertEqual(m.Dist.parse("list:5,6").kind, "list")

    def test_fixed_and_list_sampling(self):
        rng = __import__("random").Random(0)
        self.assertEqual(m.Dist.parse("fixed:4").sample(rng), 4.0)
        d = m.Dist.parse("list:10,20,30")
        self.assertEqual([d.sample(rng) for _ in range(4)], [10.0, 20.0, 30.0, 10.0])

    def test_uniform_stays_inside_its_range(self):
        rng = __import__("random").Random(3)
        d = m.Dist.parse("uniform:2,5")
        for _ in range(200):
            v = d.sample(rng)
            self.assertGreaterEqual(v, 2.0)
            self.assertLessEqual(v, 5.0)

    def test_rejected_specs(self):
        for bad in ("", "uniform:5", "uniform:9,1", "fixed:1,2", "weird:1", "-1", "abc"):
            with self.assertRaises(Exception, msg=bad):
                m.Dist.parse(bad)

    def test_parse_int_list(self):
        self.assertEqual(m.parse_int_list("0,1,2,30"), [0, 1, 2, 30])
        with self.assertRaises(Exception):
            m.parse_int_list("0,-1")


class ParamsTests(unittest.TestCase):
    def test_defaults_match_zmk(self):
        """CONFIG_ZMK_SPLIT_BLE_PREF_INT/_LATENCY/_TIMEOUT (zmk app/src/split/bluetooth/Kconfig)."""
        p = m.Params()
        self.assertEqual((p.interval_units, p.latency, p.timeout_units), (6, 30, 400))
        self.assertAlmostEqual(p.interval_ms, 7.5)
        self.assertAlmostEqual(p.worst_case_wait_ms, 232.5)
        self.assertAlmostEqual(p.supervision_timeout_ms, 4000.0)

    def test_rejected_parameters(self):
        for kwargs in ({"interval_units": 5}, {"interval_units": 4000}, {"latency": -1},
                       {"latency": 500}, {"wake_on_data": "sometimes"}, {"loss_prob": 1.0},
                       {"loss_prob": -0.1}, {"events": 0}):
            with self.assertRaises(ValueError, msg=str(kwargs)):
                m.Params(**kwargs)

    def test_supervision_timeout_warning(self):
        """The BLE spec requires timeout > 2 x (L+1) x I; the model says so instead of pretending."""
        r = m.simulate(m.Params(latency=30, timeout_units=40, events=20))
        self.assertTrue(any("supervision timeout" in w for w in r["warnings"]))
        self.assertEqual(m.simulate(m.Params(events=20))["warnings"], [])


# --------------------------------------------------------------------------
# the core claim: latency 30 with no early wake is the 232.5 ms pathology
# --------------------------------------------------------------------------
class NoEarlyWakeTests(unittest.TestCase):
    def test_latency30_max_approaches_the_worst_case_window(self):
        p = m.Params(latency=30, wake_on_data="none", events=2000, seed=1)
        s = cross(m.simulate(p))
        worst = p.worst_case_wait_ms                      # (30+1) x 7.5 = 232.5 ms
        self.assertAlmostEqual(worst, 232.5)
        # With 2000 samples the maximum gets within a few ms of the window, and it
        # may only exceed it by the fixed processing terms (queue + air + central).
        self.assertGreater(s["max"], worst - 10.0)
        self.assertLess(s["max"], worst + 3.0)

    def test_latency30_median_is_about_half_the_window(self):
        p = m.Params(latency=30, wake_on_data="none", events=2000, seed=1)
        s = cross(m.simulate(p))
        half = p.worst_case_wait_ms / 2.0                 # 116.25 ms
        self.assertGreater(s["median"], half - 20.0)
        self.assertLess(s["median"], half + 20.0)
        self.assertLess(s["min"], 10.0)                   # a lucky key still gets through fast

    def test_no_sample_can_exceed_the_window_plus_processing(self):
        """The invariant that a broken event assignment breaks first."""
        for seed in (1, 2, 7):
            p = m.Params(latency=30, wake_on_data="none", events=500, seed=seed)
            r = m.simulate(p)
            slack = p.prepare_ms + p.air_ms + p.central.max + p.queue.max
            for x in r["stages"][m.MODEL_STAGE]["samples"]:
                self.assertLessEqual(x["ms"], p.worst_case_wait_ms + slack)

    def test_latency0_max_is_one_interval_plus_processing(self):
        p = m.Params(latency=0, wake_on_data="none", events=500, seed=1)
        s = cross(m.simulate(p))
        bound = p.interval_ms + p.prepare_ms + p.air_ms + p.central.max + p.queue.max
        self.assertLessEqual(s["max"], bound)
        self.assertLess(s["max"], 12.0)
        self.assertLess(s["median"], p.interval_ms)

    def test_lowering_latency_is_a_large_win(self):
        kw = dict(wake_on_data="none", events=1000, seed=4)
        hi = cross(m.simulate(m.Params(latency=30, **kw)))
        lo = cross(m.simulate(m.Params(latency=0, **kw)))
        self.assertGreater(hi["median"], 10 * lo["median"])
        self.assertGreater(hi["p95"], 10 * lo["p95"])


class ImmediateWakeTests(unittest.TestCase):
    def test_latency_has_no_effect_on_delay(self):
        """What Zephyr's ull_periph_latency_cancel() does: the numbers must not move."""
        kw = dict(wake_on_data="immediate", events=500, seed=5)
        base = cross(m.simulate(m.Params(latency=0, **kw)))
        for L in (1, 2, 8, 30, 100):
            s = cross(m.simulate(m.Params(latency=L, **kw)))
            for key in ("count", "min", "median", "p95", "max"):
                self.assertAlmostEqual(s[key], base[key], places=9,
                                       msg="latency %d changed %s" % (L, key))

    def test_immediate_matches_latency0_of_the_sleeping_model(self):
        kw = dict(events=300, seed=6)
        a = cross(m.simulate(m.Params(latency=30, wake_on_data="immediate", **kw)))
        b = cross(m.simulate(m.Params(latency=0, wake_on_data="none", **kw)))
        self.assertAlmostEqual(a["median"], b["median"], places=9)
        self.assertAlmostEqual(a["max"], b["max"], places=9)

    def test_latency_still_saves_radio_wakes(self):
        """The trade-off the sweep exists for: same delay, fewer wake-ups."""
        kw = dict(wake_on_data="immediate", events=300, seed=6)
        lo = m.simulate(m.Params(latency=0, **kw))["wake"]["events_per_s"]
        hi = m.simulate(m.Params(latency=30, **kw))["wake"]["events_per_s"]
        self.assertGreater(lo, 10 * hi)


class LossTests(unittest.TestCase):
    def test_loss_adds_retransmissions_and_delay(self):
        kw = dict(latency=0, wake_on_data="immediate", events=800, seed=8)
        clean = m.simulate(m.Params(loss_prob=0.0, **kw))
        lossy = m.simulate(m.Params(loss_prob=0.3, **kw))
        self.assertEqual(clean["retransmissions"], 0)
        self.assertGreater(lossy["retransmissions"], 0)
        self.assertGreater(cross(lossy)["max"], cross(clean)["max"])

    def test_loss_hurts_far_more_with_high_latency(self):
        """A retransmission costs one interval when awake, a whole window when asleep."""
        kw = dict(events=800, seed=9, loss_prob=0.3)
        awake = cross(m.simulate(m.Params(latency=30, wake_on_data="immediate", **kw)))
        asleep = cross(m.simulate(m.Params(latency=30, wake_on_data="none", **kw)))
        self.assertGreater(asleep["max"], 5 * awake["max"])


class DeterminismTests(unittest.TestCase):
    def test_same_seed_same_numbers(self):
        a = m.simulate(m.Params(events=100, seed=42))
        b = m.simulate(m.Params(events=100, seed=42))
        self.assertEqual(json.dumps(a, sort_keys=True), json.dumps(b, sort_keys=True))

    def test_different_seed_different_numbers(self):
        a = cross(m.simulate(m.Params(events=100, seed=42)))
        b = cross(m.simulate(m.Params(events=100, seed=43)))
        self.assertNotAlmostEqual(a["median"], b["median"])

    def test_sweep_reuses_the_same_key_presses(self):
        p = m.Params(events=200, seed=11)
        rows = m.run_sweep(p, [0, 30])
        direct = m.simulate(p.with_latency(30))
        self.assertAlmostEqual(rows[1]["median"], cross(direct)["median"])
        self.assertEqual(p.latency, 30, "with_latency() must not mutate the original params")


# --------------------------------------------------------------------------
# sweep
# --------------------------------------------------------------------------
class SweepTests(unittest.TestCase):
    LATENCIES = [0, 1, 2, 4, 8, 16, 30]

    def test_monotonic_in_latency_without_early_wake(self):
        rows = m.run_sweep(m.Params(wake_on_data="none", events=1500, seed=2), self.LATENCIES)
        self.assertEqual([r["latency"] for r in rows], self.LATENCIES)
        for a, b in zip(rows, rows[1:]):
            self.assertLess(a["worst_case_ms"], b["worst_case_ms"])
            self.assertLess(a["median"], b["median"])
            self.assertLess(a["p95"], b["p95"])
            self.assertLess(a["max"], b["max"])
            # the battery side of the trade-off goes the other way
            self.assertGreater(a["idle_wakes_per_s"], b["idle_wakes_per_s"])
            self.assertGreater(a["sim_wakes_per_s"], b["sim_wakes_per_s"])

    def test_worst_case_column_is_exactly_the_formula(self):
        rows = m.run_sweep(m.Params(events=100, seed=2), self.LATENCIES)
        for r in rows:
            self.assertAlmostEqual(r["worst_case_ms"], (r["latency"] + 1) * 7.5)
            self.assertLessEqual(r["max"], r["worst_case_ms"] + 3.0)
            self.assertAlmostEqual(r["idle_wakes_per_s"], 1000.0 / r["worst_case_ms"])

    def test_simulated_wake_rate_tracks_the_idle_rate(self):
        """With keys far apart the radio wake rate is dominated by the idle grid."""
        rows = m.run_sweep(m.Params(wake_on_data="none", events=1500, seed=2), self.LATENCIES)
        for r in rows:
            self.assertGreaterEqual(r["sim_wakes_per_s"], r["idle_wakes_per_s"])
            self.assertLess(r["sim_wakes_per_s"], r["idle_wakes_per_s"] * 1.2 + 1.0)

    def test_delay_is_flat_under_immediate_wake(self):
        rows = m.run_sweep(m.Params(wake_on_data="immediate", events=800, seed=2), self.LATENCIES)
        for r in rows[1:]:
            self.assertAlmostEqual(r["median"], rows[0]["median"], places=9)
            self.assertAlmostEqual(r["max"], rows[0]["max"], places=9)
        for a, b in zip(rows, rows[1:]):
            self.assertGreater(a["sim_wakes_per_s"], b["sim_wakes_per_s"])


# --------------------------------------------------------------------------
# JSON schema
# --------------------------------------------------------------------------
class JsonSchemaTests(unittest.TestCase):
    def test_top_level_keys(self):
        r = m.simulate(m.Params(events=30, seed=3))
        for key in ("params", "derived", "stages", "wake", "retransmissions", "warnings"):
            self.assertIn(key, r)

    def test_params_and_derived_fields(self):
        r = m.simulate(m.Params(events=30, seed=3))
        for key in ("interval_units", "interval_ms", "latency", "timeout_units", "events", "seed",
                    "key_gap_ms", "wake_on_data", "scan_ms", "queue_ms", "air_ms", "central_ms",
                    "prepare_ms", "loss_prob", "supervision_timeout_ms"):
            self.assertIn(key, r["params"])
        for key in ("interval_ms", "worst_case_wait_ms", "mean_wait_no_wake_ms",
                    "idle_wakes_per_s", "supervision_timeout_ms", "grid_t0_ms"):
            self.assertIn(key, r["derived"])
        for key in ("attended_events", "idle_wakes", "span_s", "events_per_s"):
            self.assertIn(key, r["wake"])

    def test_stage_summaries_match_the_analyzer_shape(self):
        """Same keys as scripts/log/analyze_latency.py summarize(), so tables line up."""
        r = m.simulate(m.Params(events=30, seed=3))
        self.assertIn(m.MODEL_STAGE, r["stages"])
        for name, s in r["stages"].items():
            for key in ("count", "min", "median", "p95", "max", "unit", "samples"):
                self.assertIn(key, s, name)
            self.assertEqual(s["unit"], "ms")
            self.assertEqual(s["count"], 30)
            self.assertEqual(len(s["samples"]), 30)
            self.assertLessEqual(s["min"], s["median"])
            self.assertLessEqual(s["median"], s["p95"])
            self.assertLessEqual(s["p95"], s["max"])
            for x in s["samples"]:
                for key in ("ms", "key", "t_press_ms", "event", "retries"):
                    self.assertIn(key, x)

    def test_json_output_round_trips(self):
        rc, out = run_cli("--events", "25", "--seed", "3", "--json", "-")
        self.assertEqual(rc, 0)
        data = json.loads(out)
        self.assertEqual(data["params"]["latency"], 30)
        self.assertAlmostEqual(data["derived"]["worst_case_wait_ms"], 232.5)

    def test_no_samples_strips_the_per_event_lists(self):
        rc, out = run_cli("--events", "25", "--no-samples", "--json", "-")
        self.assertEqual(rc, 0)
        for s in json.loads(out)["stages"].values():
            self.assertNotIn("samples", s)

    def test_sweep_and_compare_land_in_the_json(self):
        rc, out = run_cli("--events", "40", "--sweep", "0,30",
                          "--compare", os.path.join(FIX, "measured_latency30.json"), "--json", "-")
        self.assertEqual(rc, 0)
        data = json.loads(out)
        self.assertEqual([r["latency"] for r in data["sweep"]], [0, 30])
        for key in ("latency", "worst_case_ms", "median", "p95", "max",
                    "idle_wakes_per_s", "sim_wakes_per_s"):
            self.assertIn(key, data["sweep"][0])
        self.assertIn("compare", data)


# --------------------------------------------------------------------------
# --compare against analyze_latency.py JSON
# --------------------------------------------------------------------------
class CompareTests(unittest.TestCase):
    def test_reads_the_cross_stage_of_a_real_analyzer_json(self):
        measured = m.load_measured(os.path.join(FIX, "measured_latency30.json"))
        r = m.simulate(m.Params(events=300, seed=1))
        c = m.compare(r, measured)
        self.assertEqual(c["measured_stage"], m.MEASURED_STAGE)
        self.assertEqual(c["measured"]["count"], 60)
        for key in ("min", "median", "p95", "max"):
            self.assertIn(key, c["delta_model_minus_measured"])
            self.assertAlmostEqual(c["delta_model_minus_measured"][key],
                                   c["model"][key] - c["measured"][key])
        self.assertEqual(c["ble"][0]["latency"], 30)
        self.assertEqual(c["ble"][0]["interval"], 6)

    def test_the_default_model_matches_a_latency30_capture(self):
        """The point of the whole exercise: no early wake explains the measurements."""
        measured = m.load_measured(os.path.join(FIX, "measured_latency30.json"))
        c = m.compare(m.simulate(m.Params(latency=30, wake_on_data="none", events=500, seed=1)), measured)
        self.assertLess(abs(c["delta_model_minus_measured"]["median"]), 25.0)
        self.assertLess(abs(c["delta_model_minus_measured"]["max"]), 25.0)

    def test_a_latency0_capture_needs_the_latency0_model(self):
        measured = m.load_measured(os.path.join(FIX, "measured_latency0.json"))
        good = m.compare(m.simulate(m.Params(latency=0, events=500, seed=1)), measured)
        bad = m.compare(m.simulate(m.Params(latency=30, events=500, seed=1)), measured)
        self.assertLess(abs(good["delta_model_minus_measured"]["median"]), 5.0)
        self.assertGreater(abs(bad["delta_model_minus_measured"]["median"]), 50.0)

    def test_missing_cross_stage_explains_itself(self):
        measured = m.load_measured(os.path.join(FIX, "measured_no_cross.json"))
        c = m.compare(m.simulate(m.Params(events=20, seed=1)), measured)
        self.assertIsNone(c["measured"])
        self.assertIn("not in the JSON", c["message"])
        self.assertIn("peripheral: kscan -> position", c["message"])

    def test_rejects_a_json_that_is_not_analyzer_output(self):
        path = os.path.join(FIX, "not_analyzer_output.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"hello": "world"}, fh)
        try:
            with self.assertRaises(ValueError):
                m.load_measured(path)
        finally:
            os.remove(path)


# --------------------------------------------------------------------------
# text report
# --------------------------------------------------------------------------
class ReportTests(unittest.TestCase):
    def test_table_header_is_identical_to_the_analyzer(self):
        """So a model run and an analyze_latency.py run can be read side by side."""
        sys.path.insert(0, os.path.abspath(os.path.join(HERE, "..", "..", "scripts", "log")))
        import analyze_latency as al
        expected = "  %-58s %6s %8s %8s %8s %8s" % ("stage", "count", "min", "median", "p95", "max")
        self.assertEqual(m.TABLE_HEADER, expected)
        self.assertEqual(al.fmt_ms(12.345), m.fmt_ms(12.345))

    def test_report_mentions_the_key_numbers(self):
        rc, out = run_cli("--events", "50", "--sweep", "0,30")
        self.assertEqual(rc, 0)
        self.assertIn("232.5", out)
        self.assertIn("wake-on-data=none", out)
        self.assertIn(m.MODEL_STAGE, out)
        self.assertIn("sweep over peripheral latency", out)
        self.assertIn("idle wake/s", out)

    def test_hist_and_verbose_add_lines(self):
        _, plain = run_cli("--events", "40")
        _, hist = run_cli("--events", "40", "--hist")
        _, verbose = run_cli("--events", "40", "--verbose")
        self.assertGreater(len(hist.splitlines()), len(plain.splitlines()))
        self.assertGreater(len(verbose.splitlines()), len(hist.splitlines()))

    def test_bad_parameters_exit_2_without_a_traceback(self):
        proc = subprocess.run([sys.executable, SCRIPT, "--interval-units", "1"],
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("error:", proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)

    def test_runs_as_a_script(self):
        proc = subprocess.run([sys.executable, SCRIPT, "--events", "20", "--sweep"],
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("split-latency model:", proc.stdout)
        self.assertEqual(proc.stderr, "")


if __name__ == "__main__":
    unittest.main()
