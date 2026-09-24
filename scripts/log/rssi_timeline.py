#!/usr/bin/env python3
"""Per-minute split-link RSSI timeline from a left-half (central) log.

Answers "was the radio link bad, and when?" for a capture from the left half.
Two sources, both in the same table:

  link   ``split rssi: -67 dBm (peer C0:FF:EE:00:00:01 (random))`` --
         boards/shields/cornix_indicator/src/split_rssi.c, one INF line every
         CONFIG_CORNIX_INDICATOR_SPLIT_RSSI_PERIOD_MS while the half is
         USB-powered. This is the RSSI of the live *connection*, read out of
         the controller with HCI Read RSSI.
  scan   ``split_central_device_found: [DEVICE]: <addr>, AD evt type N,
         AD data len N, RSSI -85`` -- zmk/app/src/split/bluetooth/central.c,
         one per advertising packet seen while the link is *down*, i.e. the
         signal strength at reconnect time. Only packets from a peer the log
         also has a ``split_central_connected: Connected: <addr>`` line for
         are counted, so other people's keyboards stay out of the table. With
         no such line in the log, every advertiser is counted and the report
         says so.

Reference numbers measured on this keyboard: -63..-70 dBm in the good state,
-85..-89 dBm in the bad one (supervision timeouts every ~40 s). The firmware
paints the connectivity LED on the same -70 / -80 edges, green / yellow / red.

Usage:
  python3 scripts/log/rssi_timeline.py logs/left-*.log
  python3 scripts/log/rssi_timeline.py logs/left-*.log --bucket 300 --json out.json

Buckets are counted from the first sample of each file, on one clock for the
whole file: the capture host clock (``[host ..]``, written by capture.py) when
the file has one, the device uptime otherwise. A reboot in the middle of a
device-clock log therefore folds back onto earlier buckets; capture with
capture.py to avoid that.

Standard library only.
"""

from __future__ import annotations

import re
import statistics
import sys

from analyze_latency import doc_parser, emit_json, parse_file  # noqa: E402  (same directory)

# Same spelling as analyze_ble.py: six hex octets plus Zephyr's "(random)" /
# "(public)" suffix from bt_addr_le_to_str().
ADDR = r"([0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5}(?: \([a-z\-]+\))?)"

# src/split_rssi.c: LOG_INF("split rssi: %d dBm (peer %s)", rssi, addr)
LINK_RE = re.compile(r"split rssi:\s*(-?\d+)\s*dBm(?:\s*\(peer " + ADDR + r"\))?")
# zmk/app/src/split/bluetooth/central.c split_central_device_found()
SCAN_RE = re.compile(r"\[DEVICE\]: " + ADDR + r".*?RSSI\s*(-?\d+)")
# zmk/app/src/split/bluetooth/central.c split_central_connected(). The host
# link logs "Connected %s" without the colon, so this only matches the split.
CONNECTED_RE = re.compile(r"\bConnected: " + ADDR)


def collect(path: str) -> dict:
    """Raw samples of one file, before any peer filtering or clock choice.

    Every sample keeps both stamps - (t_host, t_dev, rssi, addr) - because the
    two must not be mixed inside one table: they differ by the whole uptime of
    the board. analyze() picks one for the file.
    """
    peers: list[str] = []
    link: list[tuple] = []
    scan: list[tuple] = []

    for ln in parse_file(path):
        m = LINK_RE.search(ln.text)
        if m:
            link.append((ln.t_host, ln.t_dev, int(m.group(1)), m.group(2) or ""))
            continue
        m = CONNECTED_RE.search(ln.text)
        if m:
            if m.group(1) not in peers:
                peers.append(m.group(1))
            continue
        m = SCAN_RE.search(ln.text)
        if m:
            scan.append((ln.t_host, ln.t_dev, int(m.group(2)), m.group(1)))

    return {"peers": peers, "link": link, "scan": scan}


def label(offset: float) -> str:
    """mm:ss, or h:mm:ss once the capture is longer than an hour."""
    secs = int(offset)
    if secs >= 3600:
        return "%d:%02d:%02d" % (secs // 3600, secs // 60 % 60, secs % 60)
    return "%02d:%02d" % (secs // 60, secs % 60)


def summarize(values: list[int]) -> dict:
    return {"n": len(values),
            "median": statistics.median(values) if values else None,
            "min": min(values) if values else None,
            "max": max(values) if values else None}


def analyze(path: str, bucket_s: float = 60.0) -> dict:
    raw = collect(path)
    peers = raw["peers"]
    # Without a Connected: line the peripheral address is unknown; keeping
    # every advertiser is more useful than an empty table, as long as the
    # report says which it did.
    scan = [s for s in raw["scan"] if not peers or s[3] in peers]
    ignored = len(raw["scan"]) - len(scan)

    link = raw["link"]
    # One clock for the whole file: the capture host clock when capture.py
    # wrote one (it is on every line then), the device uptime otherwise.
    use_host = any(s[0] is not None for s in link + scan)
    clock = 0 if use_host else 1
    stamps = [s[clock] for s in link + scan if s[clock] is not None]

    result = {"file": path, "bucket_s": bucket_s, "peers": peers,
              "clock": "host" if use_host else "device",
              "scan_peer_filtered": bool(peers), "scan_ignored": ignored,
              "buckets": [],
              "link": summarize([s[2] for s in link]),
              "scan": summarize([s[2] for s in scan])}
    if not stamps:
        return result

    t0 = min(stamps)
    by_bucket: dict[int, dict[str, list[int]]] = {}
    for kind, samples in (("link", link), ("scan", scan)):
        for sample in samples:
            if sample[clock] is None:
                continue
            slot = by_bucket.setdefault(int((sample[clock] - t0) // bucket_s),
                                        {"link": [], "scan": []})
            slot[kind].append(sample[2])

    for idx in sorted(by_bucket):
        slot = by_bucket[idx]
        result["buckets"].append({"start_s": idx * bucket_s,
                                  "label": label(idx * bucket_s),
                                  "link": summarize(slot["link"]),
                                  "scan": summarize(slot["scan"])})
    return result


def _cell(value, fmt: str = "%d") -> str:
    return "-" if value is None else fmt % value


def report_text(result: dict) -> str:
    out = ["split-link RSSI timeline: %s" % result["file"]]
    if result["peers"]:
        out.append("peer(s): %s" % ", ".join(result["peers"]))
    else:
        out.append("peer(s): unknown (no 'Connected:' line) - every advertiser counted")
    out.append("%g s buckets on the %s clock, %d link sample(s), %d scan sample(s)%s"
               % (result["bucket_s"], result.get("clock", "device"),
                  result["link"]["n"], result["scan"]["n"],
                  ", %d from other peers ignored" % result["scan_ignored"]
                  if result["scan_ignored"] else ""))
    if not result["buckets"]:
        out.append("")
        out.append("no 'split rssi:' or '[DEVICE]: ... RSSI' lines found - is the left half")
        out.append("running a cornix_indicator build with CONFIG_CORNIX_INDICATOR_SPLIT_RSSI=y,")
        out.append("USB-powered, and captured at INF level?")
        return "\n".join(out)

    head = "%-10s %5s %8s %6s %6s %6s %8s" % (
        "time", "n", "median", "min", "max", "scan", "scan min")
    out += ["", head, "-" * len(head)]
    for b in result["buckets"]:
        out.append("%-10s %5d %8s %6s %6s %6s %8s"
                   % (b["label"], b["link"]["n"], _cell(b["link"]["median"], "%.1f"),
                      _cell(b["link"]["min"]), _cell(b["link"]["max"]),
                      b["scan"]["n"] or "-", _cell(b["scan"]["min"])))
    out.append("-" * len(head))
    out.append("%-10s %5d %8s %6s %6s %6s %8s"
               % ("total", result["link"]["n"], _cell(result["link"]["median"], "%.1f"),
                  _cell(result["link"]["min"]), _cell(result["link"]["max"]),
                  result["scan"]["n"] or "-", _cell(result["scan"]["min"])))
    return "\n".join(out)


def build_parser():
    p = doc_parser(__doc__)
    p.add_argument("files", nargs="*", help="left-half (central) log file(s)")
    p.add_argument("--bucket", type=float, default=60.0,
                   help="bucket width in seconds (default 60)")
    p.add_argument("--json", metavar="FILE",
                   help="also write the full result as JSON ('-' for stdout)")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.files:
        build_parser().print_usage(sys.stderr)
        return 2

    results = [analyze(f, args.bucket) for f in args.files]
    text = "\n\n".join(report_text(r) for r in results)
    payload = results[0] if len(results) == 1 else {"files": results}
    emit_json(args.json, payload, text)
    return 0 if any(r["buckets"] for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
