"""Unit tests for scripts/log/analyze_ble.py (synthetic fixtures)."""

import json
import os
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
FIX = os.path.join(HERE, "fixtures")
SCRIPTS = os.path.join(HERE, "..", "..", "scripts", "log")
sys.path.insert(0, os.path.abspath(SCRIPTS))

import analyze_ble as ab  # noqa: E402
import analyze_latency as al  # noqa: E402


class PatternTests(unittest.TestCase):
    def find(self, text):
        ln = al.parse_line("[00:00:01.000,000] <dbg> zmk: " + text)
        f = ab.extract([ln])
        self.assertEqual(len(f), 1, text)
        return f[0]

    def test_disconnect_variants(self):
        f = self.find("disconnected: Disconnected from AA:BB:CC:DD:EE:FF (public) (reason 0x3d)")
        self.assertEqual((f.name, f.fields["reason"], f.fields["split"]), ("disconnected", 0x3D, False))
        self.assertIn("MIC failure", ab.decode(f))
        f = self.find("split_central_disconnected: Disconnected: C0:FF:EE:00:00:01 (random) (reason 8)")
        self.assertEqual((f.fields["reason"], f.fields["split"]), (8, True))
        self.assertIn("supervision timeout", ab.decode(f))

    def test_connected_variants(self):
        self.assertFalse(self.find("connected: Connected AA:BB:CC:DD:EE:FF (public)").fields["split"])
        self.assertTrue(self.find("split_central_connected: Connected: AA:BB:CC:DD:EE:FF (random)").fields["split"])

    def test_security(self):
        f = self.find("security_changed: Security failed: AA:BB:CC:DD:EE:FF (public) level 2 err 2")
        self.assertEqual(f.fields["err"], 2)
        self.assertIn("PIN or key missing", ab.decode(f))
        f = self.find("security_changed: Security changed: AA:BB:CC:DD:EE:FF (public) level 2")
        self.assertEqual(f.name, "security_changed")

    def test_smp_and_keys(self):
        ln = al.parse_line("[00:00:01.000,000] <err> bt_smp: pairing failed (peer reason 0x5)")
        f = ab.extract([ln])[0]
        self.assertEqual((f.name, f.fields["reason"]), ("smp_failed", 5))
        self.assertIn("pairing not supported", ab.decode(f))
        ln = al.parse_line("[00:00:01.000,000] <err> bt_keys: Failed to save keys (err -28)")
        self.assertEqual(ab.extract([ln])[0].name, "keys_error")
        ln = al.parse_line("[00:00:01.000,000] <dbg> settings: settings_call_set_handler: set-value OK. key: bt/keys/abc")
        self.assertEqual(ab.extract([ln])[0].fields["key"], "bt/keys/abc")

    def test_fatal_and_banner(self):
        ln = al.parse_line("[00:00:08.000,000] <err> os: >>> ZEPHYR FATAL ERROR 2: Stack overflow on CPU 0")
        f = ab.extract([ln])[0]
        self.assertEqual((f.name, f.fields["code"], f.fields["reason"]), ("fatal", 2, "Stack overflow"))
        ln = al.parse_line("*** Booting Zephyr OS build v4.1.0 ***")
        f = ab.extract([ln])[0]
        self.assertEqual((f.name, f.fields["version"]), ("banner", "v4.1.0"))
        ln = al.parse_line("[00:00:00.000,000] ASSERTION FAIL [x] @ WEST_TOPDIR/zephyr/kernel/sched.c:123")
        self.assertEqual(ab.extract([ln])[0].name, "assert")


class FixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = ab.analyze([("central", os.path.join(FIX, "ble_pairing_failure.log"))])
        cls.f = cls.result["files"][0]

    def test_boot_and_identity(self):
        b = self.f["boot"]
        self.assertEqual(len(b["banners"]), 2)
        self.assertEqual(b["welcome_count"], 2)
        self.assertEqual(b["clock_resets"], 1)
        self.assertEqual(b["reboots"], 2)
        self.assertEqual(b["identity"], ["F1:E2:D3:C4:B5:A6 (random)"])
        self.assertTrue(any("HW Platform" in i for i in b["info"]))

    def test_fatal(self):
        self.assertEqual([x["event"] for x in self.f["fatal"]], ["fatal", "halt"])

    def test_connection_timeline(self):
        events = [(c["event"], c.get("addr")) for c in self.f["connections"]]
        self.assertEqual(len(events), 9)
        self.assertEqual(events[0], ("connected", "AA:BB:CC:DD:EE:FF (public)"))
        self.assertEqual(events[2][0], "security_failed")
        per = self.f["per_address"]
        self.assertEqual(per["AA:BB:CC:DD:EE:FF (public)"]["connected"], 2)
        self.assertEqual(per["AA:BB:CC:DD:EE:FF (public)"]["disconnected 0x3d"], 1)
        self.assertEqual(per["AA:BB:CC:DD:EE:FF (public)"]["disconnected 0x08"], 1)
        self.assertEqual(per["AA:BB:CC:DD:EE:FF (public)"]["security_failed err 2"], 1)
        self.assertEqual(per["C0:FF:EE:00:00:01 (random)"]["connect_failed"], 1)
        failed = [c for c in self.f["connections"] if c["event"] == "connect_failed"][0]
        self.assertIn("failed to be established", failed["decoded"])

    def test_pairing(self):
        names = [p["event"] for p in self.f["pairing"]]
        self.assertEqual(names, ["smp_refused_old_bond", "smp_failed"])

    def test_bonds(self):
        bo = self.f["bonds"]
        self.assertEqual(bo["profiles_from_settings"], {0: "AA:BB:CC:DD:EE:FF (public)"})
        self.assertEqual(bo["bond_count_from_settings_keys"], 1)
        self.assertEqual(bo["settings_keys_loaded"], 3)
        names = [e["event"] for e in bo["events"]]
        self.assertEqual(names, ["setting_ble", "profile_loaded", "settings_key_failed", "bonds_cleared", "keys_error"])

    def test_hints(self):
        text = "\n".join(self.f["hints"])
        for needle in ("2 boots", "fatal error", "Encryption fails", "old bond", "SMP pairing failed",
                       "supervision timeout", "never established", "bonds were wiped", "CONFIG_NVS=y"):
            self.assertIn(needle, text)
        self.assertNotIn("no stored bond", text)
        self.assertNotIn("No boot lines", text)

    def test_text_report(self):
        text = ab.report_text(self.result)
        self.assertIn("reboots=2", text)
        self.assertIn("MIC failure", text)
        self.assertIn("hints:", text)


class CliTests(unittest.TestCase):
    SCRIPT = os.path.join(SCRIPTS, "analyze_ble.py")

    def test_json(self):
        p = subprocess.run([sys.executable, self.SCRIPT, os.path.join(FIX, "ble_pairing_failure.log"), "--json", "-"],
                           capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        data = json.loads(p.stdout)
        self.assertEqual(data["files"][0]["boot"]["reboots"], 2)

    def test_no_findings_exit_code(self):
        p = subprocess.run([sys.executable, self.SCRIPT, os.path.join(FIX, "single_tio_ansi.log")],
                           capture_output=True, text=True)
        self.assertEqual(p.returncode, 1)
        self.assertIn("no matching BLE events", p.stderr)


if __name__ == "__main__":
    unittest.main()
