"""Regression test for the Cornix split BLE key latency, on BabbleSim.

Runs ``scripts/bsim/run.sh`` -- two real ZMK instances (split BLE central and
split BLE peripheral) on Zephyr's ``nrf52_bsim`` board over BabbleSim's
simulated 2.4 GHz phy -- for two values of
``CONFIG_ZMK_SPLIT_BLE_PREF_LATENCY`` and checks the measured peripheral-key
-> central-keymap delay.

``SplitLatencyWithHostTest`` repeats the run with the third device from
``tests/bsim/host``, a plain Zephyr BLE central standing in for the computer
the keyboard types into, so that the left half has to schedule two
connections at once as it does on hardware.

What this test does and does not claim
--------------------------------------
On real hardware the right half felt laggy with ZMK's default
``CONFIG_ZMK_SPLIT_BLE_PREF_LATENCY=30`` (up to 31 x 7.5 ms = 232.5 ms of
skipped connection events) and was fixed by setting it to 0. The simulation
does **not** reproduce a 232 ms delay, and this test deliberately does not
pretend that it does: Zephyr's link layer breaks peripheral latency as soon as
the peripheral host queues a PDU (``ull_periph_latency_cancel()`` called from
``ll_tx_mem_enqueue()`` in
``zephyr/subsys/bluetooth/controller/ll_sw/ull_conn.c``), so on an otherwise
idle, error-free simulated link latency 30 costs only about one extra
connection interval. See tests/bsim/README.md.

What the test does assert is what the simulation reproduces reliably:

* with latency 0 the link delivers a key within ~2 connection intervals, and
* raising the latency to ZMK's default of 30 makes every percentile of that
  delay measurably worse, i.e. the setting is not free.

Run it with:  python3 -m unittest discover -s tests/bsim -v
Environment:  BSIM_TEST_NO_BUILD=1 reuses the existing executables,
              BSIM_TEST_SEED=<n> changes the BabbleSim random seed.
"""

import json
import os
import subprocess
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BSIM_DIR = os.environ.get("BSIM_OUT_PATH", os.path.join(REPO_ROOT, ".sim", "bsim"))
RUN_SH = os.path.join(REPO_ROOT, "scripts", "bsim", "run.sh")
SIM_WS = os.environ.get("SIM_WS", os.path.join(REPO_ROOT, ".sim", "ws"))

# One connection interval: CONFIG_ZMK_SPLIT_BLE_PREF_INT=6 -> 6 * 1.25 ms.
INTERVAL_MS = 7.5

# Calibrated over 8 BabbleSim seeds (7/11/23/57/101/199/333/5000), 40 key
# events per run (raw numbers in tests/bsim/README.md):
#   latency 0 : median 3.86..4.42, p95  7.02..8.26,  max  7.7..15.3 ms
#   latency 30: median 5.74..6.29, p95  9.02..10.24, max  9.7..17.0 ms
#   30 minus 0: median +1.56..+2.24, p95 +1.29..+2.64
# The max column is noisy (it can be larger for latency 0 on some seeds), so
# nothing is asserted on it. Thresholds keep at least a factor of two of
# headroom on the observed spread.
MIN_MATCHED_EVENTS = 30
LAT0_MEDIAN_MAX_MS = 8.0
LAT0_P95_MAX_MS = 2 * INTERVAL_MS          # 15 ms
LAT30_MEDIAN_EXCESS_MIN_MS = 0.75          # observed +1.78 .. +2.24
LAT30_P95_EXCESS_MIN_MS = 0.5              # observed +1.29 .. +2.64
LAT30_P95_SANITY_MAX_MS = 30.0             # the link is healthy, not stalled

# Three-device run, calibrated over seeds 11/23/101 with the macOS-like host
# parameters run.sh defaults to (interval 12 = 15 ms, latency 0, timeout 4 s);
# raw numbers in tests/bsim/README.md:
#   split link,  key -> central keymap:  median 4.6..6.3, p95  9.2..14.7 ms
#   host  link,  key -> host HID report: median 11.9..15.9, max 18.0..24.7 ms
# The thresholds keep a factor of ~4 of headroom, because the point of these
# assertions is only that adding the host link does not stall either link.
SPLIT3_P95_MAX_MS = 40.0
HOST3_MEDIAN_MAX_MS = 40.0
HOST3_MAX_MS = 120.0
HOST3_MIN_REPORTS = 30


def _missing_prerequisite():
    if not os.path.isdir(os.path.join(SIM_WS, ".west")):
        return (f"no west workspace at {SIM_WS}; run scripts/sim/bootstrap.sh "
                f"(or set SIM_WS)")
    if not os.access(os.path.join(BSIM_DIR, "bin", "bs_2G4_phy_v1"), os.X_OK):
        return (f"BabbleSim is not built at {BSIM_DIR}; run "
                f"scripts/bsim/bootstrap.sh (needs ~6 MB of Debian packages, "
                f"the BabbleSim repos and about a minute)")
    if not os.path.isdir(os.path.join(BSIM_DIR, "nrf_hw_models")):
        return (f"{BSIM_DIR}/nrf_hw_models is missing; run "
                f"scripts/bsim/bootstrap.sh")
    if not os.access(RUN_SH, os.X_OK):
        return f"{RUN_SH} is missing or not executable"
    return None


@unittest.skipIf(_missing_prerequisite() is not None, _missing_prerequisite() or "")
class SplitLatencyTest(unittest.TestCase):
    """Two devices: compare PREF_LATENCY=30 (ZMK default) against 0."""

    results = None
    output = ""

    @classmethod
    def setUpClass(cls):
        with tempfile.TemporaryDirectory() as tmp:
            json_path = os.path.join(tmp, "latency.json")
            # --no-host: this class is the original two-device simulation,
            # and its calibrated thresholds only hold without the third device.
            cmd = [RUN_SH, "--latency", "30,0", "--no-host", "--json", json_path]
            if os.environ.get("BSIM_TEST_NO_BUILD"):
                cmd.append("--no-build")
            if os.environ.get("BSIM_TEST_SEED"):
                cmd += ["--seed", os.environ["BSIM_TEST_SEED"]]
            proc = subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True, text=True)
            cls.output = proc.stdout + proc.stderr
            if proc.returncode != 0:
                raise AssertionError(
                    "scripts/bsim/run.sh failed (exit %d):\n%s" % (proc.returncode, cls.output)
                )
            with open(json_path) as fh:
                data = json.load(fh)
        cls.results = {v["latency"]: v for v in data["variants"]}

    def variant(self, latency):
        self.assertIn(latency, self.results,
                      "no results for latency %s:\n%s" % (latency, self.output))
        return self.results[latency]

    def test_both_variants_delivered_key_events(self):
        for latency in (30, 0):
            res = self.variant(latency)
            self.assertGreaterEqual(
                res["matched"], MIN_MATCHED_EVENTS,
                "latency %d: only %d of %d peripheral key events reached the "
                "central keymap - the split link is not healthy:\n%s"
                % (latency, res["matched"], res["peripheral_key_events"], self.output),
            )

    def test_requested_connection_parameters_were_applied(self):
        for latency in (30, 0):
            res = self.variant(latency)
            params = res["conn_params"]["central"]
            self.assertTrue(params, "latency %d: the central never logged its "
                                    "connection parameters:\n%s" % (latency, self.output))
            _t, interval, applied_latency, _timeout = params[0]
            self.assertEqual(interval, 6,
                             "expected CONFIG_ZMK_SPLIT_BLE_PREF_INT=6 (7.5 ms)")
            self.assertEqual(applied_latency, latency,
                             "the link did not use the requested peripheral latency")

    def test_latency_zero_is_within_two_connection_intervals(self):
        st = self.variant(0)["total"]
        self.assertLess(
            st["median"], LAT0_MEDIAN_MAX_MS,
            "with PREF_LATENCY=0 the median key delay should be well under one "
            "connection interval plus processing, got %.2f ms" % st["median"],
        )
        self.assertLess(
            st["p95"], LAT0_P95_MAX_MS,
            "with PREF_LATENCY=0 the p95 key delay should stay within two "
            "connection intervals (%.1f ms), got %.2f ms"
            % (LAT0_P95_MAX_MS, st["p95"]),
        )

    def test_default_latency_costs_measurably_more(self):
        fast = self.variant(0)["total"]
        slow = self.variant(30)["total"]
        self.assertGreater(
            slow["median"] - fast["median"], LAT30_MEDIAN_EXCESS_MIN_MS,
            "ZMK's default PREF_LATENCY=30 should cost extra delay compared to "
            "0, but the medians were %.2f ms vs %.2f ms. Either the controller "
            "stopped applying peripheral latency at all, or the measurement is "
            "broken:\n%s" % (slow["median"], fast["median"], self.output),
        )
        self.assertGreater(
            slow["p95"] - fast["p95"], LAT30_P95_EXCESS_MIN_MS,
            "PREF_LATENCY=30 should also cost at the p95, got %.2f ms vs %.2f ms"
            % (slow["p95"], fast["p95"]),
        )

    def test_simulated_latency_30_is_not_the_hardware_232_ms(self):
        """Guards the measurement, and records the sim/hardware gap.

        On hardware, latency 30 with a 7.5 ms interval allows up to 232.5 ms.
        In this simulation the peripheral controller cancels the latency as
        soon as the notification is queued, so the delay stays small. If this
        ever fails, the simulation has started reproducing the hardware
        symptom - that is a finding, not a bug: update tests/bsim/README.md
        and tighten the assertions above.
        """
        st = self.variant(30)["total"]
        self.assertLess(
            st["p95"], LAT30_P95_SANITY_MAX_MS,
            "latency 30 p95 was %.2f ms; see the docstring" % st["p95"],
        )


HOST_EXE = os.path.join(
    os.environ.get("BSIM_BUILD_DIR", os.path.join(REPO_ROOT, ".build", "bsim")),
    "host", "zephyr", "zephyr.exe")


def _missing_host_prerequisite():
    missing = _missing_prerequisite()
    if missing:
        return missing
    if os.environ.get("BSIM_TEST_NO_BUILD") and not os.access(HOST_EXE, os.X_OK):
        return (f"the simulated host is not built at {HOST_EXE}; run "
                f"scripts/bsim/run.sh once without --no-build")
    if not os.path.isdir(os.path.join(REPO_ROOT, "tests", "bsim", "host")):
        return "tests/bsim/host is missing"
    return None


@unittest.skipIf(_missing_host_prerequisite() is not None,
                 _missing_host_prerequisite() or "")
class SplitLatencyWithHostTest(unittest.TestCase):
    """Three devices: split peripheral + split central + simulated computer.

    The third device (``tests/bsim/host``) connects to the central's HID
    service and subscribes to its input reports, so the central's controller
    schedules two connections at once, as the left half does on hardware.

    What is asserted here is only what the simulation actually shows. It does
    **not** show the hardware's several-hundred-millisecond lag: see
    tests/bsim/README.md for the full matrix. If
    ``test_the_host_link_does_not_stall_either_link`` ever starts failing, the
    simulation has begun reproducing the hardware symptom -- that is a
    finding, not a bug.
    """

    results = None
    output = ""

    @classmethod
    def setUpClass(cls):
        with tempfile.TemporaryDirectory() as tmp:
            json_path = os.path.join(tmp, "latency-host.json")
            cmd = [RUN_SH, "--latency", "30,0", "--host",
                   "--host-interval", "12", "--host-latency", "0",
                   "--json", json_path]
            # Reuse the executables when they are all there: the two-device
            # class above has normally just built them.
            if os.environ.get("BSIM_TEST_NO_BUILD") or os.access(HOST_EXE, os.X_OK):
                cmd.append("--no-build")
            if os.environ.get("BSIM_TEST_SEED"):
                cmd += ["--seed", os.environ["BSIM_TEST_SEED"]]
            proc = subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True, text=True)
            cls.output = proc.stdout + proc.stderr
            if proc.returncode != 0:
                raise AssertionError(
                    "scripts/bsim/run.sh --host failed (exit %d):\n%s"
                    % (proc.returncode, cls.output))
            with open(json_path) as fh:
                data = json.load(fh)
        cls.results = {v["latency"]: v for v in data["variants"]}

    def variant(self, latency):
        self.assertIn(latency, self.results,
                      "no results for latency %s:\n%s" % (latency, self.output))
        return self.results[latency]

    def test_the_host_connected_and_received_hid_reports(self):
        for latency in (30, 0):
            res = self.variant(latency)
            self.assertTrue(res.get("host_present"),
                            "latency %d: no host log was produced:\n%s"
                            % (latency, self.output))
            self.assertIsNotNone(
                res["host_ready_us"],
                "latency %d: the host never finished subscribing to the HID "
                "reports:\n%s" % (latency, self.output))
            self.assertGreaterEqual(
                res["host_matched"], HOST3_MIN_REPORTS,
                "latency %d: only %d key events reached the host as HID "
                "reports:\n%s" % (latency, res["host_matched"], self.output))

    def test_both_links_use_the_expected_parameters(self):
        for latency in (30, 0):
            res = self.variant(latency)
            split = res["links"]["split"]
            self.assertTrue(split, "latency %d: no split link parameters logged" % latency)
            self.assertEqual(split[0]["interval"], 6,
                             "expected CONFIG_ZMK_SPLIT_BLE_PREF_INT=6 (7.5 ms)")
            self.assertEqual(split[0]["latency"], latency,
                             "the split link did not use the requested latency")
            host = [e for e in res["links"]["host"] if isinstance(e["interval"], int)]
            self.assertTrue(host, "latency %d: no host link parameters logged:\n%s"
                                  % (latency, self.output))
            self.assertEqual(
                host[-1]["interval"], 12,
                "the host link should settle on the 15 ms interval the host asked "
                "for, got %s" % host[-1]["interval"])

    def test_the_split_link_still_delivers_keys_promptly(self):
        for latency in (30, 0):
            res = self.variant(latency)
            self.assertGreaterEqual(
                res["matched"], MIN_MATCHED_EVENTS,
                "latency %d: only %d key events reached the central keymap with "
                "the host link up:\n%s" % (latency, res["matched"], self.output))
            self.assertLess(
                res["total"]["p95"], SPLIT3_P95_MAX_MS,
                "latency %d: adding the host link pushed the split-link p95 to "
                "%.2f ms:\n%s" % (latency, res["total"]["p95"], self.output))

    def test_the_host_link_does_not_stall_either_link(self):
        """Records the sim/hardware gap for the three-device case.

        On hardware, PREF_LATENCY=30 plus the host connection produced
        several hundred milliseconds of lag. With every host parameter set
        tried (see tests/bsim/README.md) the simulation stays far below that,
        because Zephyr's controller cancels peripheral latency the moment a
        PDU is queued.
        """
        for latency in (30, 0):
            st = self.variant(latency)["periph_key_to_host"]
            self.assertLess(
                st["median"], HOST3_MEDIAN_MAX_MS,
                "latency %d: median key -> host HID report was %.2f ms"
                % (latency, st["median"]))
            self.assertLess(
                st["max"], HOST3_MAX_MS,
                "latency %d: worst key -> host HID report was %.2f ms; see the "
                "docstring" % (latency, st["max"]))


FAST_PERIPH_EXE = os.path.join(
    os.environ.get("BSIM_BUILD_DIR", os.path.join(REPO_ROOT, ".build", "bsim")),
    "peripheral-fast", "zephyr", "zmk.exe")

# Fast-typing scenario (tests/bsim/split-latency/peripheral-fast): 36 presses
# with 2-key rollover 35-60 ms apart, a 4-key chord inside one connection
# interval, six 35 ms taps; 92 measured key events, all on &kp positions.
# Calibrated on 2026-09-22 over seeds 7/11/23/101/199 with the macOS-like host
# (raw numbers in tests/bsim/README.md, "Fast typing"):
#   split link, key -> central keymap: p50 3.8..7.0, p95 7.1..14.4, p99 7.5..15.2,
#                                      max 7.7..15.3 ms, 0 lost, 0 'queue full'
#   host link,  key -> host HID report: p50 9.3..17.9, max 18.3..26.7 ms
# i.e. the same as the slow script: a burst never costs more than two 7.5 ms
# connection intervals and never overflows the peripheral's 10-deep notify
# queue. The thresholds keep a factor of ~2.5 of headroom; the "no event
# lost" assertion has none on purpose, because one lost key is the finding
# this scenario exists to catch.
FAST_MIN_EVENTS = 90
FAST_SPLIT_P95_MAX_MS = 40.0
FAST_SPLIT_P99_MAX_MS = 45.0
FAST_HOST_MAX_MS = 120.0


def _missing_fast_prerequisite():
    missing = _missing_host_prerequisite()
    if missing:
        return missing
    if os.environ.get("BSIM_TEST_NO_BUILD") and not os.access(FAST_PERIPH_EXE, os.X_OK):
        return (f"the fast-typing peripheral is not built at {FAST_PERIPH_EXE}; run "
                f"scripts/bsim/run.sh --scenario fast once without --no-build")
    if not os.path.isdir(os.path.join(REPO_ROOT, "tests", "bsim", "split-latency", "peripheral-fast")):
        return "tests/bsim/split-latency/peripheral-fast is missing"
    return None


@unittest.skipIf(_missing_fast_prerequisite() is not None,
                 _missing_fast_prerequisite() or "")
class FastTypingTest(unittest.TestCase):
    """Three devices, fast-typing script on the peripheral.

    Checks the one thing a typist would notice that the slow script cannot
    show: that a burst of rollover presses does not overflow the peripheral's
    notify queue (``Position state message queue full`` in service.c, which
    discards the oldest state = a lost key) and does not stretch the tail of
    the split-link delay. On hardware lost keys were only ever seen together
    with a stalling link (scripts/log/FAST_TYPING.md); with an ideal radio
    the simulation must deliver every event.
    """

    results = None
    output = ""

    @classmethod
    def setUpClass(cls):
        with tempfile.TemporaryDirectory() as tmp:
            json_path = os.path.join(tmp, "latency-fast.json")
            cmd = [RUN_SH, "--scenario", "fast", "--latency", "30,0", "--host",
                   "--host-interval", "12", "--host-latency", "0",
                   "--json", json_path]
            if os.environ.get("BSIM_TEST_NO_BUILD") or (
                    os.access(HOST_EXE, os.X_OK) and os.access(FAST_PERIPH_EXE, os.X_OK)):
                cmd.append("--no-build")
            if os.environ.get("BSIM_TEST_SEED"):
                cmd += ["--seed", os.environ["BSIM_TEST_SEED"]]
            proc = subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True, text=True)
            cls.output = proc.stdout + proc.stderr
            if proc.returncode != 0:
                raise AssertionError(
                    "scripts/bsim/run.sh --scenario fast failed (exit %d):\n%s"
                    % (proc.returncode, cls.output))
            with open(json_path) as fh:
                data = json.load(fh)
        cls.results = {v["latency"]: v for v in data["variants"]}

    def variant(self, latency):
        self.assertIn(latency, self.results,
                      "no results for latency %s:\n%s" % (latency, self.output))
        return self.results[latency]

    def test_no_key_event_is_lost_in_a_burst(self):
        for latency in (30, 0):
            res = self.variant(latency)
            self.assertGreaterEqual(
                res["matched"], FAST_MIN_EVENTS,
                "latency %d: only %d of %d burst key events reached the central "
                "keymap:\n%s" % (latency, res["matched"], res["peripheral_key_events"], self.output))
            self.assertEqual(
                res["unmatched"], 0,
                "latency %d: %d key events of the burst never reached the central "
                "keymap - lost on the split link:\n%s" % (latency, res["unmatched"], self.output))
            self.assertEqual(
                res["queue_full"], 0,
                "latency %d: the peripheral's notify queue overflowed %d times "
                "('Position state message queue full'):\n%s"
                % (latency, res["queue_full"], self.output))
            self.assertEqual(
                res["host_matched"], res["matched"],
                "latency %d: %d key events reached the central but only %d HID "
                "reports reached the host:\n%s"
                % (latency, res["matched"], res["host_matched"], self.output))

    def test_a_burst_does_not_stretch_the_latency_tail(self):
        for latency in (30, 0):
            res = self.variant(latency)
            st = res["total"]
            self.assertLess(
                st["p95"], FAST_SPLIT_P95_MAX_MS,
                "latency %d: fast typing pushed the split-link p95 to %.2f ms:\n%s"
                % (latency, st["p95"], self.output))
            self.assertLess(
                st["p99"], FAST_SPLIT_P99_MAX_MS,
                "latency %d: fast typing pushed the split-link p99 to %.2f ms:\n%s"
                % (latency, st["p99"], self.output))
            self.assertLess(
                res["periph_key_to_host"]["max"], FAST_HOST_MAX_MS,
                "latency %d: worst key -> host HID report in the burst was %.2f ms"
                % (latency, res["periph_key_to_host"]["max"]))


if __name__ == "__main__":
    unittest.main()
