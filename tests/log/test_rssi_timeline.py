"""Unit tests for scripts/log/rssi_timeline.py (synthetic left-half log)."""

import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(HERE, "..", "..", "scripts", "log")
sys.path.insert(0, os.path.abspath(SCRIPTS))

import rssi_timeline as rt  # noqa: E402

PEER = "C0:FF:EE:00:00:01 (random)"
OTHER = "AA:BB:CC:DD:EE:FF (random)"


def link(host, dev, rssi, peer=PEER):
    """One src/split_rssi.c sample line."""
    return "[host %s] [%s] <inf> zmk: split rssi: %d dBm (peer %s)\n" % (host, dev, rssi, peer)


def scan(host, dev, rssi, peer=PEER):
    """One split_central_device_found() line."""
    return ("[host %s] [%s] <dbg> zmk: split_central_device_found: [DEVICE]: %s, "
            "AD evt type 1, AD data len 0, RSSI %d\n" % (host, dev, peer, rssi))


# Minute 0 is the good state, minute 1 is the bad one, with a reconnect in it.
LOG = (
    "[host 09:00:00.000] [00:00:00.000,000] <inf> zmk: Welcome to ZMK!\n"
    "[host 09:00:01.000] [00:00:01.000,000] <dbg> zmk: split_central_connected: Connected: %s\n" % PEER
    + link("09:00:05.000", "00:00:05.000,000", -63)
    + link("09:00:10.000", "00:00:10.000,000", -67)
    + link("09:00:15.000", "00:00:15.000,000", -70)
    + scan("09:00:20.000", "00:00:20.000,000", -61, OTHER)
    + link("09:01:05.000", "00:01:05.000,000", -85)
    + link("09:01:10.000", "00:01:10.000,000", -89)
    + scan("09:01:20.000", "00:01:20.000,000", -88)
    + scan("09:01:25.000", "00:01:25.000,000", -86)
)


class PatternTests(unittest.TestCase):
    def test_link_line(self):
        m = rt.LINK_RE.search("split rssi: -67 dBm (peer %s)" % PEER)
        self.assertEqual((int(m.group(1)), m.group(2)), (-67, PEER))

    def test_link_line_without_peer(self):
        m = rt.LINK_RE.search("split rssi: -67 dBm")
        self.assertEqual((int(m.group(1)), m.group(2)), (-67, None))

    def test_scan_line(self):
        m = rt.SCAN_RE.search("[DEVICE]: %s, AD evt type 1, AD data len 0, RSSI -85" % PEER)
        self.assertEqual((m.group(1), int(m.group(2))), (PEER, -85))

    def test_connected_line_is_the_split_one(self):
        self.assertEqual(rt.CONNECTED_RE.search("Connected: " + PEER).group(1), PEER)
        # the host link (zmk/app/src/ble.c) has no colon and must not match
        self.assertIsNone(rt.CONNECTED_RE.search("Connected " + PEER))

    def test_label(self):
        self.assertEqual(rt.label(0), "00:00")
        self.assertEqual(rt.label(90), "01:30")
        self.assertEqual(rt.label(3725), "1:02:05")


class TimelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.log = os.path.join(cls.tmp.name, "left.log")
        with open(cls.log, "w") as fh:
            fh.write(LOG)
        cls.result = rt.analyze(cls.log)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_peer_and_counts(self):
        self.assertEqual(self.result["peers"], [PEER])
        self.assertEqual(self.result["clock"], "host")
        self.assertEqual(self.result["link"]["n"], 5)
        # the OTHER advertiser is dropped, the two peer ones are kept
        self.assertEqual(self.result["scan"]["n"], 2)
        self.assertEqual(self.result["scan_ignored"], 1)

    def test_buckets(self):
        labels = [b["label"] for b in self.result["buckets"]]
        self.assertEqual(labels, ["00:00", "01:00"])
        good, bad = self.result["buckets"]
        self.assertEqual((good["link"]["n"], good["link"]["median"], good["link"]["min"]),
                         (3, -67, -70))
        self.assertEqual(good["scan"]["n"], 0)
        self.assertEqual((bad["link"]["n"], bad["link"]["median"], bad["link"]["min"]),
                         (2, -87, -89))
        self.assertEqual((bad["scan"]["n"], bad["scan"]["min"]), (2, -88))

    def test_overall(self):
        self.assertEqual((self.result["link"]["min"], self.result["link"]["max"]), (-89, -63))
        self.assertEqual(self.result["link"]["median"], -70)

    def test_bucket_width(self):
        wide = rt.analyze(self.log, bucket_s=300.0)
        self.assertEqual([b["label"] for b in wide["buckets"]], ["00:00"])
        self.assertEqual(wide["buckets"][0]["link"]["n"], 5)

    def test_report_text(self):
        text = rt.report_text(self.result)
        self.assertIn(PEER, text)
        self.assertIn("01:00", text)
        self.assertIn("total", text)

    def test_device_clock_only(self):
        """Without capture.py's host prefix the device uptime is used."""
        path = os.path.join(self.tmp.name, "dev_only.log")
        with open(path, "w") as fh:
            fh.write("[00:00:05.000,000] <inf> zmk: split rssi: -63 dBm (peer %s)\n" % PEER)
            fh.write("[00:01:05.000,000] <inf> zmk: split rssi: -85 dBm (peer %s)\n" % PEER)
        res = rt.analyze(path)
        self.assertEqual(res["clock"], "device")
        self.assertEqual([b["label"] for b in res["buckets"]], ["00:00", "01:00"])
        self.assertFalse(res["scan_peer_filtered"])

    def test_unknown_peer_keeps_every_advertiser(self):
        path = os.path.join(self.tmp.name, "no_connect.log")
        with open(path, "w") as fh:
            fh.write(scan("09:00:20.000", "00:00:20.000,000", -61, OTHER))
        res = rt.analyze(path)
        self.assertEqual((res["peers"], res["scan"]["n"], res["scan_ignored"]), ([], 1, 0))
        self.assertIn("unknown", rt.report_text(res))

    def test_cli_and_json(self):
        out = os.path.join(self.tmp.name, "out.json")
        self.assertEqual(rt.main([self.log, "--json", out]), 0)
        with open(out) as fh:
            res = json.load(fh)
        self.assertEqual(res["link"]["n"], 5)
        self.assertEqual(len(res["buckets"]), 2)

    def test_no_samples(self):
        empty = os.path.join(self.tmp.name, "empty.log")
        open(empty, "w").close()
        self.assertEqual(rt.main([empty]), 1)
        self.assertIn("no 'split rssi:'", rt.report_text(rt.analyze(empty)))


if __name__ == "__main__":
    unittest.main()
