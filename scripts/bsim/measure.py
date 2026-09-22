#!/usr/bin/env python3
"""Measure the Cornix split-link key latency from a BabbleSim run.

Reads the device logs produced by ``scripts/bsim/run.sh`` and reports, per
``CONFIG_ZMK_SPLIT_BLE_PREF_LATENCY`` variant, how long a key took to travel
from the peripheral's matrix to the central's keymap -- in *simulated* time.

Both ZMK processes are driven by the same BabbleSim clock, so their log
timestamps are on one timeline and can simply be subtracted; there is none of
the host-clock alignment guesswork that ``scripts/log/analyze_latency.py``
needs for real hardware.

Log lines this keys on (same sources as scripts/log/README.md):

  peripheral  ``Row: R, col: C, position: N, pressed: true|false``
              zmk/app/src/physical_layouts.c
  peripheral  ``split_peripheral_listener:`` (empty LOG_DBG)
              zmk/app/src/split/peripheral.c -- handed to the BLE transport
  central     ``[NOTIFICATION] data 0x... length N``
              zmk/app/src/split/bluetooth/central.c (GATT notify callback)
  central     ``Trigger key position state change of type N``
              zmk/app/src/split/bluetooth/central.c (system work queue)
  central     ``layer_id: L position: N, binding name: X``
              zmk/app/src/keymap.c -- the key is now acted upon
  both        ``interval I latency L timeout T`` / ``New connection params:``
  host        ``[HOST HID REPORT] n=..`` / ``[HOST PARAMS] ..`` / ``[HOST READY]``
              tests/bsim/host/src/main.c -- the simulated computer, present
              only in a three-device run (``scripts/bsim/run.sh --host``)

Usage:
  scripts/bsim/measure.py --log-dir .build/bsim/logs --latency 30,0 [--json F]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import sys

# "d_00: @00:00:15.044678  ..." -- BabbleSim device time, microseconds.
BSIM_TS = re.compile(r"@(\d+):(\d+):(\d+)\.(\d+)")
# "[00:00:15.044,677] <dbg> ..." -- Zephyr kernel uptime, same clock.
ZEPHYR_TS = re.compile(r"\[(\d+):(\d+):(\d+)\.(\d+),(\d+)\]")

POSITION = re.compile(r"Row: (\d+), col: (\d+), position: (\d+), pressed: (true|false)")
LISTENER = re.compile(r"split_peripheral_listener:")
NOTIFICATION = re.compile(r"\[NOTIFICATION\] data \S+ length (\d+)")
TRIGGER = re.compile(r"Trigger key position state change of type (\d+)")
KEYMAP = re.compile(r"layer_id: (\d+) position: (\d+), binding name: (\S+)")
SUBSCRIBED = re.compile(r"\[SUBSCRIBED\]")
CONN_PARAM = re.compile(r"interval (\d+) latency (\d+) timeout (\d+)")
NEW_CONN_PARAM = re.compile(r"New connection params: Interval: (\d+), Latency: (\d+), PHY: (\d+)")
# zmk/app/src/ble.c le_param_updated(), which fires for *every* connection the
# central holds, so the peer address is what tells the two links apart.
LE_PARAM_UPDATED = re.compile(
    r"le_param_updated: ([0-9A-Fa-f:]{17}) \(\w+\): interval (\d+) latency (\d+) timeout (\d+)")
SPLIT_PEER = re.compile(r"split_central_connected: Connected: ([0-9A-Fa-f:]{17})")
# tests/bsim/host/src/main.c
HOST_REPORT = re.compile(r"\[HOST HID REPORT\] n=(\d+) handle=(\d+) length=(\d+) data=(\S+)")
HOST_PARAMS = re.compile(r"\[HOST PARAMS\] interval (\d+) latency (\d+) timeout (\d+)")
HOST_PARAM_REQ = re.compile(
    r"\[HOST PARAM REQ\] keyboard asks for interval (\d+)-(\d+) latency (\d+) timeout (\d+)")
HOST_READY = re.compile(r"\[HOST READY\]")
# zmk/app/src/split/bluetooth/service.c send_position_state(): the 10-deep
# notify queue overflowed and the oldest state was discarded (a lost event).
QUEUE_FULL = re.compile(r"Position state message queue full")
NOTIFY_ERROR = re.compile(r"Error notifying (-?\d+)")
HOST_CONNECTED = re.compile(r"\[HOST CONNECTED\]")
BLE_HINT = re.compile(
    r"Connected|Disconnected|Security|SUBSCRIBED|split service|connection params|"
    r"interval \d+ latency|Failed to connect|BLUETOOTH FAILED|advertis",
    re.IGNORECASE,
)


def timestamp_us(line):
    """Simulated time of a log line, in microseconds, or None."""
    m = BSIM_TS.search(line)
    if m:
        h, mi, s, us = (int(x) for x in m.groups())
        return ((h * 3600 + mi * 60 + s) * 1_000_000) + us
    m = ZEPHYR_TS.search(line)
    if m:
        h, mi, s, ms, us = (int(x) for x in m.groups())
        return ((h * 3600 + mi * 60 + s) * 1_000_000) + ms * 1000 + us
    return None


def parse_log(path):
    """Return the events of interest from one device log."""
    out = {
        "positions": [],      # (t_us, position, pressed)
        "listener": [],       # t_us
        "notifications": [],  # t_us
        "triggers": [],       # t_us
        "keymap": [],         # (t_us, position, binding)
        "conn_params": [],    # (t_us, interval, latency, timeout|None)
        "subscribed": [],     # t_us -- central is subscribed to position state
        "param_updates": [],  # (t_us, peer_addr, interval, latency, timeout)
        "split_peer": None,   # address of the split peripheral, as the central saw it
        "host_reports": [],   # t_us -- HID notifications the simulated host received
        "host_params": [],    # (t_us, interval, latency, timeout)
        "host_param_reqs": [],# (t_us, min, max, latency, timeout)
        "host_ready": [],     # t_us -- host subscribed to every HID report
        "host_connected": [], # t_us
        "lines": 0,
    }
    with open(path, "r", errors="replace") as fh:
        for line in fh:
            out["lines"] += 1
            t = timestamp_us(line)
            if t is None:
                continue
            m = POSITION.search(line)
            if m:
                out["positions"].append((t, int(m.group(3)), m.group(4) == "true"))
                continue
            m = KEYMAP.search(line)
            if m:
                out["keymap"].append((t, int(m.group(2)), m.group(3)))
                continue
            if LISTENER.search(line):
                out["listener"].append(t)
                continue
            m = NOTIFICATION.search(line)
            if m:
                out["notifications"].append(t)
                continue
            if TRIGGER.search(line):
                out["triggers"].append(t)
                continue
            if SUBSCRIBED.search(line):
                out["subscribed"].append(t)
                continue
            m = HOST_REPORT.search(line)
            if m:
                out["host_reports"].append(t)
                continue
            m = HOST_PARAMS.search(line)
            if m:
                out["host_params"].append(
                    (t, int(m.group(1)), int(m.group(2)), int(m.group(3))))
                continue
            m = HOST_PARAM_REQ.search(line)
            if m:
                out["host_param_reqs"].append(
                    (t, int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))))
                continue
            if HOST_READY.search(line):
                out["host_ready"].append(t)
                continue
            if HOST_CONNECTED.search(line):
                out["host_connected"].append(t)
                continue
            m = SPLIT_PEER.search(line)
            if m:
                out["split_peer"] = m.group(1)
                continue
            m = NEW_CONN_PARAM.search(line)
            if m:
                out["conn_params"].append((t, int(m.group(1)), int(m.group(2)), None))
                continue
            m = LE_PARAM_UPDATED.search(line)
            if m:
                out["param_updates"].append(
                    (t, m.group(1), int(m.group(2)), int(m.group(3)), int(m.group(4))))
                continue
            m = CONN_PARAM.search(line)
            if m:
                out["conn_params"].append(
                    (t, int(m.group(1)), int(m.group(2)), int(m.group(3)))
                )
    return out


def pair_events(periph_positions, central_keymap, warmup_position, ready_us):
    """Pair peripheral key events with the central keymap events.

    Matching is per key position and in order: for one position the two
    devices see exactly the same alternating press/release sequence, so the
    n-th peripheral event for a position is the n-th central event for it.
    Falls back to a global in-order match when the central uses a different
    position numbering (it does not today, but a position-map change upstream
    would show up as a constant offset rather than as "no events matched").

    Peripheral events on ``warmup_position`` (the key the script presses only
    to pass the time while the split link comes up) and events that happen
    before the central has subscribed to the position-state characteristic
    are excluded: they are not a measurement of the connected link.
    """
    kept, dropped = [], 0
    for ev in periph_positions:
        if ev[1] == warmup_position or (ready_us is not None and ev[0] < ready_us):
            dropped += 1
            continue
        kept.append(ev)
    central = [k for k in central_keymap if k[1] != warmup_position]

    by_pos = {}
    for t, pos, _binding in central:
        by_pos.setdefault(pos, []).append(t)
    cursor = {}
    pairs, unmatched = [], []
    for t, pos, pressed in kept:
        queue = by_pos.get(pos, [])
        i = cursor.get(pos, 0)
        if i < len(queue):
            cursor[pos] = i + 1
            pairs.append((t, queue[i], pos, pressed))
        else:
            unmatched.append((t, pos, pressed))
    offset_note = None
    if not pairs and len(kept) == len(central) and central:
        offsets = {c[1] - p[1] for p, c in zip(kept, central)}
        if len(offsets) == 1:
            offset_note = offsets.pop()
            pairs = [(p[0], c[0], p[1], p[2]) for p, c in zip(kept, central)]
            unmatched = []
    return pairs, unmatched, offset_note, dropped


def next_after(events, t):
    """First timestamp in the sorted list `events` that is >= t, or None."""
    import bisect
    i = bisect.bisect_left(events, t)
    return events[i] if i < len(events) else None


def last_before(events, t):
    """Last timestamp in the sorted list `events` that is <= t, or None."""
    import bisect
    i = bisect.bisect_right(events, t)
    return events[i - 1] if i else None


def stats_ms(deltas_us):
    if not deltas_us:
        return None
    ms = sorted(d / 1000.0 for d in deltas_us)

    def pct(p):
        return ms[max(0, min(len(ms) - 1, int(round(p * (len(ms) - 1)))))]

    return {
        "count": len(ms),
        "min": ms[0],
        "median": statistics.median(ms),
        "p95": pct(0.95),
        "p99": pct(0.99),
        "max": ms[-1],
        "mean": statistics.fmean(ms),
    }


def fmt_stats(name, st):
    if st is None:
        return f"  {name:<34} (no samples)"
    return (
        f"  {name:<34} n={st['count']:<4d} min={st['min']:7.2f}  median={st['median']:7.2f}"
        f"  p95={st['p95']:7.2f}  p99={st['p99']:7.2f}  max={st['max']:7.2f}"
    )


def count_lines(path, pattern):
    try:
        with open(path, "r", errors="replace") as fh:
            return sum(1 for l in fh if pattern.search(l))
    except OSError:
        return 0


def tail_ble_lines(path, count=12):
    try:
        with open(path, "r", errors="replace") as fh:
            hits = [l.rstrip("\n") for l in fh if BLE_HINT.search(l)]
    except OSError as exc:
        return [f"    (cannot read {path}: {exc})"]
    return ["    " + l for l in hits[-count:]] or ["    (no BLE lines at all)"]


def analyse(log_dir, latency, warmup_position):
    periph_path = os.path.join(log_dir, f"peripheral-{latency}.log")
    central_path = os.path.join(log_dir, f"central-{latency}.log")
    host_path = os.path.join(log_dir, f"host-{latency}.log")
    for p in (periph_path, central_path):
        if not os.path.exists(p):
            raise SystemExit(f"error: missing log {p} (run scripts/bsim/run.sh first)")
    periph = parse_log(periph_path)
    central = parse_log(central_path)
    # The third device is optional: scripts/bsim/run.sh --no-host leaves no
    # host log behind, and everything below degrades to the two-device report.
    host = parse_log(host_path) if os.path.exists(host_path) else None
    # The link is only usable once the central has subscribed to the split
    # position-state characteristic; before that a notify fails with -ENOTCONN.
    ready_us = central["subscribed"][0] if central["subscribed"] else None
    pairs, unmatched, offset, dropped = pair_events(
        periph["positions"], central["keymap"], warmup_position, ready_us
    )

    result = {
        "latency": latency,
        "peripheral_log": periph_path,
        "central_log": central_path,
        "peripheral_key_events": len(periph["positions"]),
        "central_keymap_events": len(central["keymap"]),
        "matched": len(pairs),
        "unmatched": len(unmatched),
        # key events the central never acted on = lost on the split link
        # (the peripheral logs "Position state message queue full" when its
        # notify queue overflowed, see queue_full below)
        "dropped": len(unmatched),
        "queue_full": count_lines(periph_path, QUEUE_FULL),
        "notify_errors": count_lines(periph_path, NOTIFY_ERROR),
        "dropped_warmup": dropped,
        "link_ready_us": ready_us,
        "position_offset": offset,
        "conn_params": {
            "central": central["conn_params"][:4],
            "peripheral": periph["conn_params"][:4],
        },
        "links": link_report(central, host),
        "total": stats_ms([c - p for p, c, _pos, _pr in pairs]),
        "press": stats_ms([c - p for p, c, _pos, pr in pairs if pr]),
        "release": stats_ms([c - p for p, c, _pos, pr in pairs if not pr]),
        # Stage detail, matched by time rather than by index so that a lost
        # or pre-link event does not shift the whole column.
        "peripheral_scan_to_transport": stats_ms(
            [d for d in (
                (lambda b, a: None if b is None else b - a)(next_after(periph["listener"], a), a)
                for a in (p[0] for p in periph["positions"])
            ) if d is not None]
        ),
        "central_notify_to_keymap": stats_ms(
            [d for d in (
                (lambda a, b: None if a is None else b - a)(
                    last_before(central["notifications"], k[0]), k[0])
                for k in central["keymap"]
            ) if d is not None]
        ),
        "samples_ms": [round((c - p) / 1000.0, 3) for p, c, _pos, _pr in pairs],
    }
    # Only `&kp` bindings produce a HID report; the peripheral's script also
    # hits a `&mo` and a `&trans` position, which the central acts on but
    # never forwards to the computer.
    hid_positions = {pos for _t, pos, binding in central["keymap"] if binding == "key_press"}
    result.update(host_report(host, pairs, hid_positions, host_path))
    return result


def link_report(central, host):
    """What the central (and the host, if present) logged about each link.

    The central holds up to two connections and ``zmk/app/src/ble.c``'s
    ``le_param_updated()`` fires for both of them, so the peer address is the
    only thing that tells them apart. The split link's parameters are logged
    once at connection time by ``split_central_process_connection()``
    ("New connection params") instead.
    """
    split_peer = central["split_peer"]
    links = {"split": [], "host": []}
    for t, interval, lat, timeout in central["conn_params"]:
        links["split"].append(
            {"t_us": t, "interval": interval, "latency": lat, "timeout": timeout,
             "source": "central: New connection params"})
    for t, addr, interval, lat, timeout in central["param_updates"]:
        which = "split" if split_peer and addr == split_peer else "host"
        links[which].append(
            {"t_us": t, "peer": addr, "interval": interval, "latency": lat,
             "timeout": timeout, "source": "central: le_param_updated"})
    if host:
        for t, interval, lat, timeout in host["host_params"]:
            links["host"].append(
                {"t_us": t, "interval": interval, "latency": lat, "timeout": timeout,
                 "source": "host: le_param_updated"})
        for t, imin, imax, lat, timeout in host["host_param_reqs"]:
            links["host"].append(
                {"t_us": t, "interval": f"{imin}-{imax}", "latency": lat,
                 "timeout": timeout, "source": "host: keyboard's update request"})
    for v in links.values():
        v.sort(key=lambda d: d["t_us"])
    links["split_peer"] = split_peer
    return links


def host_report(host, pairs, hid_positions, host_path):
    """peripheral key -> HID report received by the simulated computer.

    Every `&kp` key position change on the right half makes the central's
    keymap act on it and send exactly one HID report, so the two sequences are
    1:1 and in order: the n-th such key event is the n-th host notification.
    Positions whose binding sends nothing (`&mo`, `&trans`) are left out.
    """
    if host is None:
        return {"host_present": False}

    reports = sorted(host["host_reports"])
    ready_us = host["host_ready"][0] if host["host_ready"] else None
    key_to_host, keymap_to_host = [], []
    matched, unmatched = 0, 0
    cursor = 0
    for p_us, c_us, pos, _pressed in pairs:
        if pos not in hid_positions:
            continue
        if ready_us is not None and c_us < ready_us:
            continue
        while cursor < len(reports) and reports[cursor] < c_us:
            cursor += 1
        if cursor >= len(reports):
            unmatched += 1
            continue
        r = reports[cursor]
        cursor += 1
        matched += 1
        key_to_host.append(r - p_us)
        keymap_to_host.append(r - c_us)
    return {
        "host_present": True,
        "host_log": host_path,
        "host_connected_us": host["host_connected"][0] if host["host_connected"] else None,
        "host_ready_us": ready_us,
        "host_reports": len(reports),
        "host_matched": matched,
        "host_unmatched": unmatched,
        "periph_key_to_host": stats_ms(key_to_host),
        "central_keymap_to_host": stats_ms(keymap_to_host),
        "host_samples_ms": [round(d / 1000.0, 3) for d in key_to_host],
    }


def print_link(name, entries):
    for e in entries:
        interval = e["interval"]
        if isinstance(interval, int):
            ms = interval * 1.25
            interval_txt = f"{interval} ({ms:.2f} ms)"
            worst = f"  -> may skip up to {(e['latency'] + 1) * ms:.1f} ms"
        else:  # a min-max request
            interval_txt = str(interval)
            worst = ""
        timeout = e.get("timeout")
        extra = f" timeout {timeout} ({timeout * 10} ms)" if timeout is not None else ""
        print(f"  [{e['t_us'] / 1e6:8.3f} s] {name:<11} interval {interval_txt}"
              f" latency {e['latency']}{extra}{worst}   [{e['source']}]")


def print_variant(res):
    print(f"=== CONFIG_ZMK_SPLIT_BLE_PREF_LATENCY = {res['latency']} ===")
    print_link("split link", res["links"]["split"])
    if res["links"]["host"]:
        print_link("host link", res["links"]["host"])
    elif res.get("host_present"):
        print("  host link: connected, but no parameter update was logged")
    if res["link_ready_us"] is not None:
        print(f"  split link subscribed at {res['link_ready_us'] / 1e6:.3f} s")
    if res.get("host_present"):
        ready = res["host_ready_us"]
        print(f"  host subscribed to the HID reports at "
              f"{'n/a' if ready is None else f'{ready / 1e6:.3f} s'}"
              f", received {res['host_reports']} reports")
    else:
        print("  no host device in this run (two-device simulation)")
    print(f"  peripheral key events: {res['peripheral_key_events']}, "
          f"central keymap events: {res['central_keymap_events']}, "
          f"matched: {res['matched']}, unmatched: {res['unmatched']}, "
          f"warm-up/pre-link dropped: {res['dropped_warmup']}")
    print(f"  lost on the split link: {res['dropped']}, peripheral 'queue full': "
          f"{res['queue_full']}, 'Error notifying': {res['notify_errors']}")
    if res["position_offset"]:
        print(f"  note: central positions are offset by {res['position_offset']}")
    print(fmt_stats("peripheral key -> central keymap", res["total"]))
    print(fmt_stats("  presses only", res["press"]))
    print(fmt_stats("  releases only", res["release"]))
    print(fmt_stats("peripheral scan -> BLE transport", res["peripheral_scan_to_transport"]))
    print(fmt_stats("central notify -> keymap", res["central_notify_to_keymap"]))
    if res.get("host_present"):
        print(fmt_stats("peripheral key -> host HID report", res["periph_key_to_host"]))
        print(fmt_stats("  central keymap -> host HID report", res["central_keymap_to_host"]))
    print()


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--log-dir", default=".build/bsim/logs",
                    help="directory with peripheral-<lat>.log / central-<lat>.log")
    ap.add_argument("--latency", default="30,0",
                    help="comma separated latency variants to report (default 30,0)")
    ap.add_argument("--warmup-position", type=int, default=47,
                    help="key position the peripheral script uses for its warm-up "
                         "press while the split link comes up; excluded from the "
                         "statistics (default 47)")
    ap.add_argument("--json", help="also write the numbers to this file")
    args = ap.parse_args(argv)

    variants = [int(x) for x in args.latency.split(",") if x.strip() != ""]
    results = [analyse(args.log_dir, lat, args.warmup_position) for lat in variants]

    for res in results:
        print_variant(res)

    failed = [r for r in results if not r["matched"]]
    if failed:
        print("ERROR: no key events matched between the two devices.")
        print("The split link probably never connected. Last BLE lines:")
        for r in failed:
            print(f"  --- {r['peripheral_log']}")
            print("\n".join(tail_ble_lines(r["peripheral_log"])))
            print(f"  --- {r['central_log']}")
            print("\n".join(tail_ble_lines(r["central_log"])))
        return 1

    def table(title, key):
        rows = [r for r in results if r.get(key)]
        if not rows:
            return
        print(f"{title}, simulated time (ms)")
        print(f"  {'PREF_LATENCY':>12} {'n':>4} {'min':>8} {'median':>8} {'p95':>8} {'p99':>8} {'max':>8}")
        for r in rows:
            st = r[key]
            print(f"  {r['latency']:>12} {st['count']:>4} {st['min']:>8.2f} "
                  f"{st['median']:>8.2f} {st['p95']:>8.2f} {st['p99']:>8.2f} {st['max']:>8.2f}")
        if len(rows) == 2:
            a, b = rows[0][key], rows[1][key]
            print(f"  {'delta':>12} {'':>4} {a['min'] - b['min']:>+8.2f} "
                  f"{a['median'] - b['median']:>+8.2f} {a['p95'] - b['p95']:>+8.2f} "
                  f"{a['p99'] - b['p99']:>+8.2f} {a['max'] - b['max']:>+8.2f}"
                  f"   ({rows[0]['latency']} minus {rows[1]['latency']})")
        print()

    table("peripheral key -> central keymap", "total")
    table("peripheral key -> host HID report", "periph_key_to_host")

    if args.json:
        with open(args.json, "w") as fh:
            json.dump({"variants": results}, fh, indent=2)
        print(f"\nwrote {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
