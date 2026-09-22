"""Unit tests for scripts/log/analyze_drops.py (synthetic right/left pair)."""

import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(HERE, "..", "..", "scripts", "log")
sys.path.insert(0, os.path.abspath(SCRIPTS))

import analyze_drops as ad  # noqa: E402


def periph_line(host, dev, pos, pressed, row=0, col=7):
    return (f"[host {host}] [{dev}] <dbg> zmk: zmk_physical_layouts_kscan_process_msgq: "
            f"Row: {row}, col: {col}, position: {pos}, pressed: {'true' if pressed else 'false'}\n")


def central_notif(host, dev, positions):
    bits = [0] * 16
    for p in positions:
        bits[p // 8] |= 1 << (p % 8)
    hexs = " ".join("%02x" % b for b in bits[:8]) + "  " + " ".join("%02x" % b for b in bits[8:])
    return (f"[host {host}] [{dev}] <dbg> zmk: split_central_notify_func: [NOTIFICATION] data 0x20016558 length 16\n"
            f"[host {host}] [{dev}] <dbg> zmk: split_central_notify_func: data\n"
            f"[host {host}]                               {hexs} |........ ........\n")


# The right half taps position 6, then 7 twice (the second 7 tap is lost),
# then 8; the queue-full warning sits between the two lost events.
RIGHT = (
    "[host 09:58:00.000] *** Booting Zephyr OS build test ***\n"
    + periph_line("10:00:01.000", "00:00:01.000,000", 6, True)
    + periph_line("10:00:01.100", "00:00:01.100,000", 6, False)
    + periph_line("10:00:01.200", "00:00:01.200,000", 7, True)
    + periph_line("10:00:01.300", "00:00:01.300,000", 7, False)
    + periph_line("10:00:01.350", "00:00:01.350,000", 7, True)
    + "[host 10:00:01.450] [00:00:01.450,000] <wrn> zmk: Position state message queue full, popping first message and queueing again\n"
    + periph_line("10:00:01.450", "00:00:01.450,000", 7, False)
    + periph_line("10:00:02.000", "00:00:02.000,000", 8, True)
    + periph_line("10:00:02.100", "00:00:02.100,000", 8, False)
)
# The left half sees the first 7 tap and the 8 tap, each 5 ms after the key
# (its clock runs 100 s ahead), never the second 7 tap.
LEFT = (
    "[host 09:58:00.100] *** Booting Zephyr OS build test ***\n"
    "[host 09:58:00.200] [00:01:40.000,000] <dbg> zmk: split_central_device_found: [DEVICE]: C0:FF:EE:00:00:01 (random), AD evt type 1, AD data len 0, RSSI -61\n"
    "[host 09:58:00.300] [00:01:40.100,000] <dbg> zmk: split_central_connected: Connected: C0:FF:EE:00:00:01 (random)\n"
    + central_notif("10:00:01.050", "00:01:41.005,000", [6])
    + central_notif("10:00:01.150", "00:01:41.105,000", [])
    + central_notif("10:00:01.250", "00:01:41.207,000", [7])
    + central_notif("10:00:01.350", "00:01:41.304,000", [])
    + central_notif("10:00:02.050", "00:01:42.006,000", [8])
    + central_notif("10:00:02.150", "00:01:42.105,000", [])
)


class AnalyzeDropsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.right = os.path.join(self.tmp.name, "right.log")
        self.left = os.path.join(self.tmp.name, "left.log")
        with open(self.right, "w") as fh:
            fh.write(RIGHT)
        with open(self.left, "w") as fh:
            fh.write(LEFT)

    def tearDown(self):
        self.tmp.cleanup()

    def test_parse(self):
        pev, pmarks = ad.parse_periph(self.right)
        notifs, cmarks = ad.parse_central(self.left)
        self.assertEqual(len(pev), 8)
        self.assertEqual([m[2] for m in pmarks], ["boot", "queue full"])
        self.assertEqual(len(notifs), 6)
        self.assertEqual(notifs[0][2][0], 0x40)  # position 6 = byte 0 bit 6
        self.assertEqual([m[2] for m in cmarks], ["boot", "rssi"])
        self.assertEqual(cmarks[1][3], "-61")
        self.assertLess(cmarks[0][0], pev[0][0])  # boot marks share the events' day counter

    def test_match_finds_the_lost_tap(self):
        pev, _ = ad.parse_periph(self.right)
        notifs, _ = ad.parse_central(self.left)
        pairs, dropped, unmatched = ad.match(pev, notifs)
        self.assertEqual(len(pairs), 6)
        self.assertEqual([pev[k][2] for k in dropped], [7, 7])
        self.assertEqual(unmatched, [])
        lat = ad.latencies(pev, notifs, pairs)
        self.assertEqual(len(lat), 6)
        # every delivery took 4..7 ms on the two device clocks; above the
        # local minimum that is 0..3 ms
        self.assertTrue(all(0 <= x["ms"] <= 3.5 for x in lat), [x["ms"] for x in lat])

    def test_midnight_unwrap(self):
        self.assertEqual(ad.unwrap_host([86000.0, 100.0, 200.0]), [86000.0, 86500.0, 86600.0])
        # an 11 h gap forward and a 2 h step back are still one day and a wrap
        self.assertEqual(ad.unwrap_host([3600.0, 43200.0, 36000.0]), [3600.0, 43200.0, 122400.0])

    def test_cli_and_json(self):
        out = os.path.join(self.tmp.name, "out.json")
        rc = ad.main(["--peripheral", self.right, "--central", self.left, "--json", out])
        self.assertEqual(rc, 0)
        with open(out) as fh:
            res = json.load(fh)
        self.assertEqual((res["delivered"], res["lost"], res["lost_taps"], res["queue_full"]), (6, 2, 1, 1))
        self.assertEqual([e["position"] for e in res["lost_events"]], [7, 7])
        self.assertEqual(res["clean"]["count"], 6)

    def test_no_events(self):
        empty = os.path.join(self.tmp.name, "empty.log")
        open(empty, "w").close()
        self.assertEqual(ad.main(["--peripheral", empty, "--central", self.left]), 1)


if __name__ == "__main__":
    unittest.main()
