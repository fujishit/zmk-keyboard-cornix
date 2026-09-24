#!/usr/bin/env python3
"""Lost keys and latency tail on the split link, from a right/left log pair.

Complements analyze_latency.py for one question: did every key event the
right half (split peripheral) scanned reach the left half (split central),
and how late? It works on the *state* the split link carries instead of on
individual log lines:

  right  ``Row: R, col: C, position: P, pressed: true|false``
         (zmk/app/src/physical_layouts.c) -- one event per key transition
  left   ``[NOTIFICATION] data 0x.. length 16`` followed by the 16-byte hex
         dump (zmk/app/src/split/bluetooth/central.c, LOG_HEXDUMP_DBG) -- the
         peripheral's whole position bitmap as received

Consecutive bitmaps on the left are XOR-ed into per-position transitions and
paired, per position and in order, with the right half's events, inside a
host-timestamp window (capture.py's ``[host ..]`` prefix; both logs must come
from the same capture session). A right-half event that never produces a
transition on the left was lost on the link: that is what the peripheral's
``Position state message queue full, popping first message`` warning does
(zmk/app/src/split/bluetooth/service.c, send_position_state()). A tap is
"lost" when both its press and its release were dropped.

Latency is the difference of the two device clocks minus a local baseline
(the minimum over the surrounding +-30 s, drift-corrected), so it is the
delay *above the best case*, with about 1 ms of floor not shown. Events are
split by the gap to the previous key event to see whether fast typing pays.

Usage:
  python3 scripts/log/analyze_drops.py --peripheral logs/right-X.log --central logs/left-Y.log
  python3 scripts/log/analyze_drops.py ... --burst-ms 60 --session 10:30-10:50 --json out.json

Standard library only.
"""

from __future__ import annotations

import argparse
import bisect
import heapq
import json
import os
import re
import statistics
import sys
from collections import defaultdict, deque

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import analyze_latency as al  # noqa: E402

# The line shapes this tool shares with analyze_latency.py (same log lines, so
# the pattern lives in one place).  TS_ONLY_RE carries a sixth group with the
# rest of the line, which always matches, so it finds the same timestamps.
HOST = al.HOST_RE
ZTS = al.TS_ONLY_RE
POS = al.EVENT_RE["position"]
HEX = re.compile(r"^\s*((?:[0-9a-f]{2}\s+){15}[0-9a-f]{2})\s*\|")
NOTIF = re.compile(r"\[NOTIFICATION\] data \S+ length 16")
BOOT = re.compile(r"Booting Zephyr")
QFULL = re.compile(r"Position state message queue full")
ERRN = re.compile(r"Error notifying (-?\d+)")
DISC = re.compile(r"Disconnected.*reason (0x[0-9a-f]+|\d+)")
DEVICE = re.compile(r"\[DEVICE\]: ([0-9A-Fa-f:]{17}).*RSSI (-?\d+)")
SPLIT_PEER = re.compile(r"split_central_connected: Connected: ([0-9A-Fa-f:]{17})")

# A right-half line normally reaches the host 0-1.5 s *before* the matching
# left-half line (both are deferred logs flushed every 50 ms, plus the COM
# reader); allow 8 s of real link delay and 0.4 s of reversed jitter.
WIN_BEFORE = 8.0
WIN_AFTER = 0.4


def host_s(m):
    return al.host_seconds(*m.groups())


def dev_s(line):
    m = ZTS.search(line)
    if not m:
        return None
    h, mi, s, ms, us = (int(m.group(i)) for i in range(1, 6))
    return h * 3600 + mi * 60 + s + ms / 1e3 + us / 1e6


def unwrap_host(hs):
    """Host stamps are time of day; any step back of more than an hour is
    midnight (a single capture never runs backwards otherwise)."""
    out, add, prev = [], 0.0, None
    for h in hs:
        if h is None:
            out.append(None)
            continue
        if prev is not None and h + add < prev - 3600:
            add += 86400
        v = h + add
        out.append(v)
        prev = v
    return out


def _fix_host(items, lineno_index, marks, marks_lineno_index):
    """Unwrap the host stamps of one file's events and marks together, in
    file order, so that both carry the same day counter."""
    allitems = sorted([(it[lineno_index], it) for it in items] + [(mk[marks_lineno_index], mk) for mk in marks],
                      key=lambda x: x[0])
    hs = unwrap_host([it[0] for _, it in allitems])
    for (_, it), h in zip(allitems, hs):
        it[0] = h


def window_min_index(vals, bounds):
    """[index of min(vals[lo:hi]) for lo, hi in bounds], in O(n) instead of
    O(n x window) (monotonic deque).

    `bounds` must slide: `lo` and `hi` non-decreasing and every window
    non-empty.  Ties keep the leftmost index, which is the one min() picks.
    """
    out, dq, added = [], deque(), 0   # dq: indices, vals[dq] non-decreasing
    for lo, hi in bounds:
        while added < hi:
            while dq and vals[dq[-1]] > vals[added]:
                dq.pop()
            dq.append(added)
            added += 1
        while dq[0] < lo:
            dq.popleft()
        out.append(dq[0])
    return out


def correct_host(items, seg_index, window_s=120.0):
    """Replace each host stamp by device time + the local minimum of
    (host - device) over +-window_s of device time, per boot segment.

    The host stamp is the *arrival* time of the line: a deferred log that is
    flushed late (the log thread starved, the COM reader stalled) can carry a
    stamp minutes after the event; the smallest offset seen nearby is the
    best estimate of the true one."""
    by_seg = defaultdict(list)
    for it in items:
        if it[0] is not None and it[1] is not None:
            by_seg[it[seg_index]].append(it)
    for lst in by_seg.values():
        lst.sort(key=lambda it: it[1])
        devs = [it[1] for it in lst]
        offs = [it[0] - it[1] for it in lst]
        bounds = [(bisect.bisect_left(devs, d - window_s), bisect.bisect_right(devs, d + window_s))
                  for d in devs]
        for it, i in zip(lst, window_min_index(offs, bounds)):
            it[0] = it[1] + offs[i]


def parse_periph(path):
    ev, marks = [], []  # [host, dev, pos, pressed, lineno, seg], [host, dev, kind, text, addr, lineno]
    seg = 0
    with open(path, "r", errors="replace") as fh:
        for no, line in enumerate(fh, 1):
            m = HOST.match(line)
            h = host_s(m) if m else None
            d = dev_s(line)
            pm = POS.search(line)
            if pm and d is not None:
                ev.append([h, d, int(pm.group(3)), pm.group(4) == "true", no, seg])
            elif QFULL.search(line):
                marks.append([h, d, "queue full", "", None, no])
            elif ERRN.search(line):
                marks.append([h, d, "error notifying", ERRN.search(line).group(1), None, no])
            elif DISC.search(line) and "zmk" in line:
                marks.append([h, d, "disconnected", DISC.search(line).group(1), None, no])
            elif BOOT.search(line):
                seg += 1
                marks.append([h, d, "boot", "", None, no])
    _fix_host(ev, 4, marks, 5)
    return ev, marks


def parse_central(path, on_line=None):
    """Notification bitmaps and link marks from the central's log.

    `on_line(lineno, line, host_s, seg, notif)` is called for every line, in
    file order, so that a caller needing other events from the same log does
    not have to read it a second time (scripts/log/crosshalf.py).  `notif` is
    (index into notifs, bitmap) on the line that completed a notification and
    None everywhere else.
    """
    notifs, marks = [], []  # [host, dev, bitmap, lineno, seg], [host, dev, kind, text, addr]
    pending = None
    peers = set()
    seg = 0
    with open(path, "r", errors="replace") as fh:
        for no, line in enumerate(fh, 1):
            m = HOST.match(line)
            h = host_s(m) if m else None
            rest = line[m.end():] if m else line
            notif = None
            hm = HEX.match(rest) if pending is not None else None
            if NOTIF.search(line):
                pending = (h, dev_s(line), no)
            elif hm:
                bits = tuple(int(x, 16) for x in hm.group(1).split())
                notifs.append([pending[0], pending[1], bits, pending[2], seg])
                pending = None
                notif = (len(notifs) - 1, bits)
            elif pending is not None and "split_central_notify_func: data" in line:
                pass
            else:
                pending = None
                if DISC.search(line) and "split_central_disconnected" in line:
                    marks.append([h, dev_s(line), "disconnected", DISC.search(line).group(1), None, no])
                elif BOOT.search(line):
                    seg += 1
                    marks.append([h, dev_s(line), "boot", "", None, no])
                else:
                    dm = DEVICE.search(line)
                    if dm:
                        marks.append([h, dev_s(line), "rssi", dm.group(2), dm.group(1), no])
                    else:
                        pm = SPLIT_PEER.search(line)
                        if pm:
                            peers.add(pm.group(1))
            if on_line is not None:
                on_line(no, line, h, seg, notif)
    # keep the scan RSSI of the split peer only (file order, then unwrap)
    marks = [mk for mk in marks if mk[2] != "rssi" or mk[4] in peers]
    _fix_host(notifs, 3, marks, 5)
    return notifs, marks


def bitmap_changes(bits, prev):
    """Positions that differ between two 16-byte split position bitmaps, in
    position order: [(position, pressed), ...].  The one place the bitmaps are
    unpacked (crosshalf.py and samehalf.py use it too)."""
    out = []
    for i in range(16):
        x = bits[i] ^ prev[i]
        if not x:
            continue
        for bit in range(8):
            if x >> bit & 1:
                out.append((i * 8 + bit, bool(bits[i] >> bit & 1)))
    return out


def transitions(notifs):
    out, prev = [], (0,) * 16
    for j, (_h, _d, bits, _no, _seg) in enumerate(notifs):
        out += [(j, pos, pressed) for pos, pressed in bitmap_changes(bits, prev)]
        prev = bits
    return out


def match(pev, notifs, trans=None):
    """Pair central transitions with peripheral events, per position, in order.

    Returns (pairs, dropped, unmatched): pairs are (peripheral index,
    notification index); dropped are peripheral indices that fell out of the
    window without a transition; unmatched are transitions with no event.
    `trans` is transitions(notifs) when the caller already has it."""
    by_pos = defaultdict(list)
    for k, e in enumerate(pev):
        by_pos[e[2]].append(k)
    ptr = defaultdict(int)
    matched = set()
    pairs, dropped, unmatched = [], [], []
    for j, pos, pressed in (transitions(notifs) if trans is None else trans):
        hc = notifs[j][0]
        if hc is None:
            unmatched.append((j, pos, pressed))
            continue
        lst = by_pos[pos]
        i = ptr[pos]
        while i < len(lst) and (pev[lst[i]][0] is None or pev[lst[i]][0] < hc - WIN_BEFORE):
            if pev[lst[i]][0] is not None and lst[i] not in matched:
                dropped.append(lst[i])
            i += 1
        ptr[pos] = i
        found = None
        m = i
        while m < len(lst):
            k = lst[m]
            if pev[k][0] > hc + WIN_AFTER:
                break
            if pev[k][3] == pressed and k not in matched:
                found = k
                break
            m += 1
        if found is None:
            unmatched.append((j, pos, pressed))
        else:
            pairs.append((found, j))
            matched.add(found)
            if found == lst[i]:
                ptr[pos] = i + 1
    dset = set(dropped)
    dropped += [k for k in range(len(pev)) if k not in matched and k not in dset and pev[k][0] is not None]
    pairs.sort()
    return pairs, sorted(set(dropped)), unmatched


def latencies(pev, notifs, pairs):
    """Delay above the local best case, in ms, per pair (drift-corrected).

    Each device clock restarts at a reboot, so the clock offset is fitted per
    (peripheral boot, central boot) segment pair."""
    groups = defaultdict(list)
    for pk, cj in pairs:
        groups[(pev[pk][5], notifs[cj][4])].append((pk, cj))
    out = []
    for key in sorted(groups):
        out += _latencies_segment(pev, notifs, groups[key])
    return out


def _latencies_segment(pev, notifs, pairs):
    d = sorted((pev[pk][1], notifs[cj][1] - pev[pk][1], pk, cj) for pk, cj in pairs)
    if len(d) < 5:
        return []
    xs = [x[0] for x in d]
    ys = [x[1] for x in d]
    xm, ym = statistics.fmean(xs), statistics.fmean(ys)
    sxx = sum((x - xm) ** 2 for x in xs)
    slope = sum((x - xm) * (y - ym) for x, y in zip(xs, ys)) / sxx if sxx else 0.0
    # The baseline of a point is the smallest y - slope * (x - t) in its
    # window.  Which point that is does not depend on t (adding slope * t to
    # every candidate keeps their order), so adj = y - slope * x picks it in
    # one sliding-window pass; the baseline itself is still computed from the
    # winner the way it always was.
    adj = [y - slope * x for x, y in zip(xs, ys)]
    n = len(d)
    wide, wide_bounds, narrow = [], [], []
    for idx, t in enumerate(xs):
        lo = bisect.bisect_left(xs, t - 30.0)
        hi = bisect.bisect_right(xs, t + 30.0)
        if hi - lo >= 5:
            wide.append(idx)
            wide_bounds.append((lo, hi))
        else:
            narrow.append(idx)   # too few neighbours in time: a fixed +-5 window
    best = [0] * n
    for idx, m in zip(wide, window_min_index(adj, wide_bounds)):
        best[idx] = m
    for idx in narrow:
        best[idx] = min(range(max(0, idx - 5), min(n, idx + 6)), key=adj.__getitem__)
    out = []
    for idx, (t, dd, pk, cj) in enumerate(d):
        m = best[idx]
        base = d[m][1] - slope * (d[m][0] - t)
        gap = (pev[pk][1] - pev[pk - 1][1]) * 1e3 if pk > 0 else float("inf")
        out.append({"ms": (dd - base) * 1e3, "pidx": pk, "cidx": cj, "gap_ms": gap})
    return out


def pct(vals, p):
    """The p-th percentile (p in 0..1) of an unsorted list; analyze_latency.py
    does the picking, with the percentage in 0..100."""
    if not vals:
        return float("nan")
    return al.percentile(sorted(vals), p * 100.0)


def summary(vals):
    v = sorted(vals)
    if not v:
        nan = float("nan")
        return {"count": 0, "p50": nan, "p90": nan, "p95": nan, "p99": nan, "max": nan}
    return {"count": len(v), "p50": al.percentile(v, 50), "p90": al.percentile(v, 90),
            "p95": al.percentile(v, 95), "p99": al.percentile(v, 99), "max": v[-1]}


def fmt(name, s):
    if not s["count"]:
        return f"  {name:<40} n=0"
    return (f"  {name:<40} n={s['count']:<5d} p50={s['p50']:6.1f} p90={s['p90']:6.1f} "
            f"p95={s['p95']:6.1f} p99={s['p99']:7.1f} max={s['max']:8.1f} ms")


def tod(h):
    return "--:--:--" if h is None else "%02d:%02d:%06.3f" % (int(h // 3600) % 24, int(h % 3600 // 60), h % 60)


def parse_session(text):
    a, b = text.split("-")
    def s(x):
        p = [int(v) for v in x.split(":")]
        return p[0] * 3600 + p[1] * 60 + (p[2] if len(p) > 2 else 0)
    return s(a), s(b)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--peripheral", required=True, metavar="LOG", help="right-half capture")
    ap.add_argument("--central", required=True, metavar="LOG", help="left-half capture, same session")
    ap.add_argument("--burst-ms", type=float, default=60.0,
                    help="an event is 'fast typing' when the previous key event is closer than this (default 60)")
    ap.add_argument("--session", metavar="HH:MM-HH:MM", help="restrict to a time-of-day window (host clock)")
    ap.add_argument("--exclude-s", type=float, default=30.0,
                    help="ignore events within this many seconds of a disconnect/boot for the 'clean' numbers (default 30)")
    ap.add_argument("--json", metavar="FILE", help="write the result as JSON")
    args = ap.parse_args(argv)

    pev, pmarks = parse_periph(args.peripheral)
    notifs, cmarks = parse_central(args.central)
    correct_host(pev, 5)
    correct_host(notifs, 4)
    if args.session:
        lo, hi = parse_session(args.session)
        pev = [e for e in pev if e[0] is not None and lo <= e[0] % 86400 < hi]
        notifs = [n for n in notifs if n[0] is not None and lo <= n[0] % 86400 < hi]
    if not pev or not notifs:
        print("no matching events found - check that the debug snippet is enabled and both logs "
              "carry [host ..] timestamps (scripts/log/capture.py)", file=sys.stderr)
        return 1
    trans = transitions(notifs)
    pairs, dropped, unmatched = match(pev, notifs, trans)
    lat = latencies(pev, notifs, pairs)

    disc = sorted(m[0] for m in pmarks + cmarks if m[2] in ("disconnected", "boot") and m[0] is not None)

    def near_disc(h):
        if h is None:
            return False
        k = bisect.bisect_left(disc, h - args.exclude_s)
        return k < len(disc) and disc[k] <= h + args.exclude_s

    open_press, lost_taps = {}, []
    dset = set(dropped)
    for k, e in enumerate(pev):
        if e[3]:
            open_press[e[2]] = k
        else:
            p0 = open_press.pop(e[2], None)
            if p0 is not None and p0 in dset and k in dset:
                lost_taps.append((p0, k))

    print(f"peripheral: {len(pev)} key events   central: {len(notifs)} notifications, "
          f"{len(trans)} transitions")
    print(f"delivered: {len(pairs)}   lost (no transition on the central): {len(dropped)}   "
          f"lost taps (press and release): {len(lost_taps)}   central transitions without a peripheral line: {len(unmatched)}")
    qf = [m for m in pmarks if m[2] == "queue full"]
    en = [m for m in pmarks if m[2] == "error notifying"]
    print(f"peripheral warnings: 'queue full' {len(qf)}, 'Error notifying' {len(en)}, disconnects "
          f"{sum(1 for m in pmarks if m[2] == 'disconnected')} (peripheral) / "
          f"{sum(1 for m in cmarks if m[2] == 'disconnected')} (central)")

    all_ms = [x["ms"] for x in lat]
    clean = [x for x in lat if not near_disc(pev[x['pidx']][0])]
    vals = [x["ms"] for x in clean]
    fast = [x["ms"] for x in clean if x["gap_ms"] < args.burst_ms]
    slow = [x["ms"] for x in clean if x["gap_ms"] >= args.burst_ms]
    s_all, s_clean, s_fast, s_slow = (summary(all_ms), summary(vals), summary(fast), summary(slow))
    print(f"\nsplit-link delay above the local best case (ms); 'clean' = not within {args.exclude_s:.0f} s of a disconnect or boot")
    print(fmt("all delivered events", s_all))
    print(fmt("clean", s_clean))
    print(fmt(f"clean, fast typing (gap < {args.burst_ms:.0f} ms)", s_fast))
    print(fmt(f"clean, slower (gap >= {args.burst_ms:.0f} ms)", s_slow))
    edges = [0, 2.5, 5, 7.5, 10, 15, 20, 30, 50, 100, 250, 1000, float("inf")]
    print("  clean histogram:")
    for a, b in zip(edges, edges[1:]):
        c = sum(1 for v in vals if a <= v < b)
        print(f"    {a:6.1f}-{b:<6.1f} {'#' * min(60, c * 60 // max(1, len(vals)))} {c}")

    worst = heapq.nlargest(15, lat, key=lambda x: x["ms"])
    if worst:
        print("\nslowest 15 delivered events:")
        for x in worst:
            e = pev[x["pidx"]]
            print(f"  {x['ms']:8.1f} ms  host {tod(e[0])}  pos {e[2]:2d} {'press' if e[3] else 'release'}  "
                  f"gap {x['gap_ms']:7.1f} ms  right line {e[4]}{'  (near disconnect)' if near_disc(e[0]) else ''}")
    if dropped:
        print("\nlost events (right half scanned them, the left never saw the transition):")
        grp = defaultdict(list)
        for k in dropped:
            grp[int(pev[k][0] // 60)].append(pev[k])
        for g in sorted(grp):
            lst = grp[g]
            print(f"  host {tod(lst[0][0])[:5]}  {len(lst):3d} events: "
                  + " ".join(f"{e[2]}{'P' if e[3] else 'R'}" for e in lst[:24]) + (" ..." if len(lst) > 24 else "")
                  + ("   (near disconnect)" if near_disc(lst[0][0]) else ""))
    if lost_taps:
        print("  lost taps: " + ", ".join(f"pos {pev[a][2]} at {tod(pev[a][0])}" for a, b in lost_taps[:20])
              + (" ..." if len(lost_taps) > 20 else ""))
    if qf or en:
        print("\nperipheral queue events:")
        for m in (qf + en)[:30]:
            print(f"  host {tod(m[0])}  dev {m[1]:.3f}  {m[2]} {m[3]}")
    rs = [m for m in cmarks if m[2] == "rssi"]
    if rs:
        print("\nsplit peer RSSI when the central scanned for it (each (re)connect):")
        print("  " + ", ".join(f"{tod(m[0])[:8]} {m[3]} dBm" for m in rs[:24]) + (" ..." if len(rs) > 24 else ""))

    if args.json:
        with open(args.json, "w") as fh:
            json.dump({
                "peripheral_events": len(pev), "central_notifications": len(notifs),
                "delivered": len(pairs), "lost": len(dropped), "lost_taps": len(lost_taps),
                "unmatched_central": len(unmatched), "queue_full": len(qf), "error_notifying": len(en),
                "all": s_all, "clean": s_clean, "clean_fast": s_fast, "clean_slow": s_slow,
                "lost_events": [{"host": tod(pev[k][0]), "position": pev[k][2], "pressed": pev[k][3]} for k in dropped],
            }, fh, indent=2)
        print(f"\nwrote {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
