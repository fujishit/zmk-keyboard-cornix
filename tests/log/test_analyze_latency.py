"""Unit tests for scripts/log/analyze_latency.py (synthetic fixtures)."""

import json
import os
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
FIX = os.path.join(HERE, "fixtures")
SCRIPTS = os.path.join(HERE, "..", "..", "scripts", "log")
sys.path.insert(0, os.path.abspath(SCRIPTS))

import analyze_latency as al  # noqa: E402


class ParseLineTests(unittest.TestCase):
    def test_host_prefix_and_zephyr_line(self):
        ln = al.parse_line("[host 12:00:05.001] [00:00:05.000,500] <dbg> zmk: zmk_physical_layouts_kscan_process_msgq: "
                           "Row: 1, col: 2, position: 20, pressed: true", 7)
        self.assertEqual(ln.no, 7)
        self.assertAlmostEqual(ln.t_host, 12 * 3600 + 5.001)
        self.assertAlmostEqual(ln.t_dev, 5.0005)
        self.assertEqual(ln.level, "dbg")
        self.assertEqual(ln.module, "zmk")
        self.assertEqual(ln.func, "zmk_physical_layouts_kscan_process_msgq")
        self.assertTrue(ln.msg.startswith("Row: 1"))
        self.assertTrue(ln.text.startswith("zmk_physical_layouts_kscan_process_msgq: Row"))

    def test_tio_prefix_and_ansi(self):
        ln = al.parse_line("[12:00:05.000] \x1b[0m[00:00:05.100,000] <dbg> zmk: kscan_matrix_read: "
                           "Sending event at 3,1 state off\x1b[0m")
        self.assertAlmostEqual(ln.t_host, 12 * 3600 + 5.0)
        self.assertAlmostEqual(ln.t_dev, 5.1)
        self.assertEqual(ln.func, "kscan_matrix_read")
        self.assertEqual(ln.msg, "Sending event at 3,1 state off")

    def test_empty_dbg_message_keeps_function(self):
        ln = al.parse_line("[00:00:05.000,700] <dbg> zmk: split_peripheral_listener: ")
        self.assertEqual(ln.func, "split_peripheral_listener")
        self.assertEqual(ln.msg, "")

    def test_wrn_line_is_not_split_on_colon(self):
        ln = al.parse_line("[00:00:06.000,000] <wrn> zmk: Disconnected: something happened")
        self.assertIsNone(ln.func)
        self.assertEqual(ln.msg, "Disconnected: something happened")

    def test_timestamp_only_and_plain_lines(self):
        ln = al.parse_line("[host 12:00:00.000] [00:00:00.000,000] *** Booting Zephyr OS build v4.1.0 ***")
        self.assertAlmostEqual(ln.t_dev, 0.0)
        self.assertIsNone(ln.level)
        self.assertIn("Booting", ln.text)
        plain = al.parse_line("[host 12:00:00.000] --- capture: start ---")
        self.assertIsNone(plain.t_dev)
        self.assertAlmostEqual(plain.t_host, 12 * 3600)
        self.assertIsNone(al.parse_line("   \n"))

    def test_segments_detect_clock_reset(self):
        lines = [al.parse_line(s, i) for i, s in enumerate([
            "[00:00:10.000,000] <inf> zmk: a", "[00:00:11.000,000] <inf> zmk: b",
            "[00:00:00.100,000] <inf> zmk: c"], 1)]
        self.assertEqual(al.assign_segments(lines), 2)
        self.assertEqual([l.seg for l in lines], [0, 0, 1])


class PeripheralTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = al.analyze([("peripheral", os.path.join(FIX, "right_peripheral.log"))])

    def stage(self, name):
        return self.result["stages"]["peripheral: " + name]

    def test_role_and_counts(self):
        f = self.result["files"][0]
        self.assertEqual(f["role_hint"], "peripheral")
        self.assertEqual(f["events"], 6)

    def test_kscan_to_position(self):
        s = self.stage("kscan -> position")
        self.assertEqual(s["count"], 2)
        self.assertAlmostEqual(s["min"], 0.5, places=3)
        self.assertAlmostEqual(s["max"], 1.0, places=3)
        self.assertAlmostEqual(s["median"], 0.75, places=3)
        self.assertAlmostEqual(s["p95"], 1.0, places=3)

    def test_position_to_split_listener(self):
        s = self.stage("position -> split_listener")
        self.assertEqual([round(x["ms"], 3) for x in s["samples"]], [0.2, 0.3])
        s2 = self.stage("kscan -> split_listener")
        self.assertEqual([round(x["ms"], 3) for x in s2["samples"]], [0.7, 1.3])

    def test_ble_params(self):
        ble = self.result["ble"]
        self.assertEqual(len(ble), 1)
        self.assertEqual(ble[0]["kind"], "param_update")
        self.assertEqual((ble[0]["interval"], ble[0]["latency"], ble[0]["timeout"]), (6, 30, 400))
        self.assertAlmostEqual(ble[0]["interval_ms"], 7.5)
        self.assertAlmostEqual(ble[0]["max_peripheral_latency_ms"], 232.5)
        self.assertEqual(ble[0]["timeout_ms"], 4000)

    def test_warnings(self):
        texts = [(w["level"], w["example"]) for w in self.result["warnings"]]
        self.assertIn(("wrn", "Position state message queue full, popping first message and queueing again"), texts)
        self.assertTrue(any(l == "dbg" and "Error notifying" in t for l, t in texts))

    def test_clock_offset(self):
        off = self.result["files"][0]["clock_offsets"][0]
        self.assertAlmostEqual(off["offset_s"], 12 * 3600, places=3)
        self.assertGreaterEqual(off["flush_delay_ms"]["max"], off["flush_delay_ms"]["min"])


class CentralTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = al.analyze([("central", os.path.join(FIX, "left_central.log"))])

    def samples(self, name):
        return [round(x["ms"], 3) for x in self.result["stages"]["central: " + name]["samples"]]

    def test_role(self):
        self.assertEqual(self.result["files"][0]["role_hint"], "central")

    def test_peripheral_path_stages(self):
        self.assertEqual(self.samples("notification -> trigger"), [0.2, 0.1])
        self.assertEqual(self.samples("trigger -> keymap"), [0.2, 0.2])
        self.assertEqual(self.samples("notification -> keymap"), [0.4, 0.3])
        self.assertEqual(self.samples("notification -> hid"), [0.6, 0.5])
        self.assertEqual(self.samples("trigger -> hid"), [0.4, 0.4])

    def test_keymap_to_hid_includes_local_key_and_skips_layer_key(self):
        self.assertEqual(self.samples("keymap -> hid"), [0.2, 0.2, 0.2])
        self.assertEqual(self.result["unmatched"].get("central: keymap (no immediate hid)"), 1)
        self.assertEqual(self.result["unmatched"].get("central: keymap"), 1)

    def test_local_key_path(self):
        self.assertEqual(self.samples("kscan -> position"), [0.4])
        self.assertEqual(self.samples("local position -> keymap"), [0.2])
        self.assertEqual(self.samples("local kscan -> keymap"), [0.6])
        self.assertEqual(self.samples("local kscan -> hid"), [0.8])
        self.assertEqual(self.samples("local position -> hid"), [0.4])

    def test_conn_params_and_error(self):
        kinds = [b["kind"] for b in self.result["ble"]]
        self.assertEqual(kinds, ["conn_params"])
        self.assertEqual(self.result["ble"][0]["phy"], 1)
        self.assertTrue(any(w["level"] == "err" and "FAILED TO SEND OVER USB" in w["example"]
                            for w in self.result["warnings"]))


class CrossDeviceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = al.analyze([("peripheral", os.path.join(FIX, "right_peripheral.log")),
                                 ("central", os.path.join(FIX, "left_central.log"))])

    def samples(self, name):
        return [round(x["ms"], 3) for x in self.result["stages"]["cross: " + name]["samples"]]

    def test_aligned_position_to_keymap(self):
        self.assertEqual(self.samples("peripheral position -> central keymap (aligned)"), [9.9, 9.3])

    def test_raw_host_stamps(self):
        self.assertEqual(self.samples("peripheral position -> central keymap (raw host stamps)"), [11.0, 10.0])

    def test_chains_to_hid_and_kscan(self):
        self.assertEqual(self.samples("peripheral kscan -> central keymap (aligned)"), [10.4, 10.3])
        self.assertEqual(self.samples("peripheral position -> central hid (aligned)"), [10.1, 9.5])
        self.assertEqual(self.samples("peripheral kscan -> central hid (aligned)"), [10.6, 10.5])

    def test_per_device_stages_still_present(self):
        self.assertIn("peripheral: kscan -> position", self.result["stages"])
        self.assertIn("central: notification -> hid", self.result["stages"])

    def test_text_report_mentions_stages_and_ble(self):
        text = al.report_text(self.result, show_hist=True, verbose=False)
        self.assertIn("cross: peripheral position -> central keymap (aligned)", text)
        self.assertIn("interval 7.50 ms, latency 30 (worst-case 232.5 ms)", text)
        self.assertIn("warnings / errors", text)


class SingleFileTests(unittest.TestCase):
    def test_tio_and_ansi_file(self):
        r = al.analyze([("device", os.path.join(FIX, "single_tio_ansi.log"))])
        st = r["stages"]
        self.assertAlmostEqual(st["device: kscan -> position"]["samples"][0]["ms"], 1.0, places=3)
        self.assertAlmostEqual(st["device: local position -> keymap"]["samples"][0]["ms"], 0.25, places=3)
        self.assertAlmostEqual(st["device: keymap -> hid"]["samples"][0]["ms"], 0.75, places=3)
        self.assertAlmostEqual(st["device: local kscan -> hid"]["samples"][0]["ms"], 2.0, places=3)
        self.assertEqual(r["unmatched"].get("device: kscan"), 1)  # release never transformed
        self.assertEqual([b["kind"] for b in r["ble"]], ["param_update_failed"])
        self.assertEqual(r["ble"][0]["err"], -5)
        self.assertTrue(any("Keyboard message queue full" in w["example"] for w in r["warnings"]))

    def test_noise_only_reports_no_events(self):
        r = al.analyze([("device", os.path.join(FIX, "noise_only.log"))])
        self.assertEqual(r["stages"], {})
        self.assertIn("no matching events found", r["message"])
        self.assertEqual(r["files"][0]["lines"], 4)
        text = al.report_text(r, False, False)
        self.assertIn("check that the debug snippet is enabled", text)


class CliTests(unittest.TestCase):
    SCRIPT = os.path.join(SCRIPTS, "analyze_latency.py")

    def test_json_output(self):
        p = subprocess.run([sys.executable, self.SCRIPT, "--peripheral", os.path.join(FIX, "right_peripheral.log"),
                            "--central", os.path.join(FIX, "left_central.log"), "--json", "-", "--no-samples"],
                           capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        data = json.loads(p.stdout)
        self.assertEqual(data["stages"]["cross: peripheral position -> central keymap (aligned)"]["count"], 2)
        self.assertNotIn("samples", data["stages"]["peripheral: kscan -> position"])

    def test_noise_exit_code(self):
        p = subprocess.run([sys.executable, self.SCRIPT, os.path.join(FIX, "noise_only.log")],
                           capture_output=True, text=True)
        self.assertEqual(p.returncode, 1)
        self.assertIn("no matching events found", p.stdout)

    def test_usage_without_files(self):
        p = subprocess.run([sys.executable, self.SCRIPT], capture_output=True, text=True)
        self.assertEqual(p.returncode, 2)


if __name__ == "__main__":
    unittest.main()
