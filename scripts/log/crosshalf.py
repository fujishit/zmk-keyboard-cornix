#!/usr/bin/env python3
"""Cross-half key ordering on the split central, from a right/left log pair.

Question: when a key on the right half (BLE split peripheral) is pressed
*before* a key on the left half (the central), does the central ever apply
the left key first?  The central has no timestamp for remote events: it
applies its own kscan events as soon as they are scanned
(zmk/app/src/physical_layouts.c) and remote events when the GATT
notification arrives (zmk/app/src/split/bluetooth/central.c), so a right
key that is still in flight (debounce + connection interval + retries) is
overtaken by a left key pressed a few milliseconds later.

Inputs (same capture session, scripts/log/capture.py):
  right  ``Row: R, col: C, position: P, pressed: ..`` -- the right half's
         own events on its own clock
  left   ``Row: ..`` lines = the left half's own keys (local),
         ``[NOTIFICATION] data .. length 16`` + hex dump = remote bitmaps,
         ``Trigger key position state change`` = one remote event applied,
         followed by ``layer_id: L position: P, binding name: X``.

Method: analyze_drops.match() pairs every right event with the notification
that carried it, and analyze_drops.latencies() gives the split delay above
the local best case (drift-corrected per boot segment).  The right event's
press time on the *left* clock is then
    t_true = t_apply(remote) - (delay_above_best + floor)
with ``floor`` = the best-case split delay that the local-minimum method
cannot see (about 1 ms; --floor-ms, see the sensitivity table).  Local
events have t_true = t_apply = the kscan time.  All presses are sorted by
t_true; two consecutive presses on different halves whose apply order on
the central is the opposite of their true order are a *reorder*.

A local hold-off of D ms (t_apply(local) += D) is replayed on the same data
to count how many reorders it fixes and how many it creates (left pressed
first, right lands within D).

Usage:
  python3 crosshalf.py --peripheral logs/right-X.log --central logs/left-Y.log [--floor-ms 1] [--json out.json]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))   # scripts/, for remap.py
import analyze_drops as ad  # noqa: E402

TRIGGER = re.compile(r"Trigger key position state change of type 0")


def base_labels():
    """Key names of the Base layer, read from the keymap so they cannot go
    stale; an empty list (positions are then printed as numbers) if the keymap
    cannot be parsed."""
    try:
        import remap
        keymap = remap.parse(remap.DEFAULT_KEYMAP)
        base = keymap.layers[0]
        return [remap.show_label(b, keymap.display_names) for b in base.bindings]
    except Exception:
        return []


BASE = base_labels()


def key(pos):
    return BASE[pos] if 0 <= pos < len(BASE) else str(pos)


def parse_central_events(path):
    """Ordered list of every position event the central applied.

    Returns (events, notifs): events = [host, dev, pos, pressed, lineno, seg,
    kind, notif_index]; kind 'L' = local kscan, 'R' = remote.  notifs is the
    same structure analyze_drops.parse_central() returns (for match()).

    Collected from analyze_drops.parse_central()'s own pass over the file, so
    the (large) central log is read once."""
    events = []
    # [previous bitmap, transitions of the current notification not yet
    #  applied, index of that notification]
    state = [(0,) * 16, [], None]

    def collect(no, line, h, seg, notif):
        if ad.BOOT.search(line):
            state[0], state[1] = (0,) * 16, []
            return
        if notif is not None:
            state[2], bits = notif[0], notif[1]
            state[1] = ad.bitmap_changes(bits, state[0])
            state[0] = bits
            return
        d = ad.dev_s(line)
        if d is None:
            return
        pm = ad.POS.search(line)
        if pm:
            events.append([h, d, int(pm.group(3)), pm.group(4) == "true", no, seg, "L", None])
        elif TRIGGER.search(line) and state[1]:
            pos, pressed = state[1].pop(0)
            events.append([h, d, pos, pressed, no, seg, "R", state[2]])

    notifs, _marks = ad.parse_central(path, collect)
    ad._fix_host(events, 4, [], 5)
    return events, notifs


def align(pev, cev, notifs, floor_ms):
    """Attach to every remote press/release on the central its true time on
    the central's clock (dev seconds) and the total split delay in ms."""
    pairs, dropped, unmatched = ad.match(pev, notifs)
    lat = ad.latencies(pev, notifs, pairs)
    # remote events on the central by (notif index, position, pressed)
    rem = defaultdict(list)
    for k, e in enumerate(cev):
        if e[6] == "R":
            rem[(e[7], e[2], e[3])].append(k)
    n_aligned = 0
    for x in lat:
        e = pev[x["pidx"]]
        lst = rem.get((x["cidx"], e[2], e[3]))
        if not lst:
            continue
        k = lst.pop(0)
        delay = x["ms"] + floor_ms
        cev[k].append(cev[k][1] - delay / 1e3)  # [8] t_true (central clock)
        cev[k].append(delay)                     # [9] delay ms
        n_aligned += 1
    for e in cev:
        if len(e) == 8:
            e.append(e[1] if e[6] == "L" else None)
            e.append(0.0 if e[6] == "L" else None)
    return n_aligned, len(pairs), len(dropped)


def pct(v, p):
    return ad.pct(v, p)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--peripheral", required=True)
    ap.add_argument("--central", required=True)
    ap.add_argument("--floor-ms", type=float, default=1.0,
                    help="best-case split delay hidden by the local-minimum method (default 1)")
    ap.add_argument("--session", metavar="HH:MM-HH:MM")
    ap.add_argument("--hold", default="0,3,5,8,10,12,15,20", help="local hold-off values to replay, ms")
    ap.add_argument("--host-shift-s", type=float, default=0.0,
                    help="add this to the right half's host stamps (86400 when its capture started the day before the left's)")
    ap.add_argument("--list-fast-ms", type=float, default=20.0, help="list every right->left pair closer than this")
    ap.add_argument("--json")
    args = ap.parse_args(argv)

    pev, pmarks = ad.parse_periph(args.peripheral)
    for e in pev:
        if e[0] is not None:
            e[0] += args.host_shift_s
    ad.correct_host(pev, 5)
    cev, notifs = parse_central_events(args.central)
    ad.correct_host(notifs, 4)
    ad.correct_host(cev, 5)
    if args.session:
        lo, hi = ad.parse_session(args.session)
        pev = [e for e in pev if e[0] is not None and lo <= e[0] % 86400 < hi]
        notifs = [n for n in notifs if n[0] is not None and lo <= n[0] % 86400 < hi]
        cev = [e for e in cev if e[0] is not None and lo <= e[0] % 86400 < hi]
    n_aligned, n_pairs, n_dropped = align(pev, cev, notifs, args.floor_ms)
    nl = sum(1 for e in cev if e[6] == "L")
    nr = sum(1 for e in cev if e[6] == "R")
    print(f"central applied {len(cev)} position events: {nl} local, {nr} remote; "
          f"{n_aligned} remote events aligned to the right half's clock ({n_pairs} matched, {n_dropped} lost)")

    # presses only, with a true time; per boot segment of the central
    presses = []
    for k, e in enumerate(cev):
        if e[3] and e[8] is not None:
            presses.append((e[8], e[1], e[6], e[2], e[9], k, e[5], e[0]))
    presses.sort(key=lambda p: (p[6], p[0]))
    # consecutive cross-half pairs within the same segment
    rl, lr = [], []  # right-then-left / left-then-right true-order pairs
    for a, b in zip(presses, presses[1:]):
        if a[6] != b[6] or a[2] == b[2]:
            continue
        gap = (b[0] - a[0]) * 1e3
        if gap > 200:
            continue
        (rl if a[2] == "R" else lr).append((a, b, gap))
    print(f"\nconsecutive cross-half press pairs within 200 ms: right->left {len(rl)}, left->right {len(lr)}")
    edges = [0, 5, 10, 15, 20, 30, 50, 100, 200]
    print("  right->left gap histogram (ms) and how many of them the central applied in reverse:")
    rl_rev = [(a, b, g) for a, b, g in rl if a[1] > b[1]]
    for lo_, hi_ in zip(edges, edges[1:]):
        n = sum(1 for _, _, g in rl if lo_ <= g < hi_)
        r = sum(1 for _, _, g in rl_rev if lo_ <= g < hi_)
        print(f"    {lo_:4d}-{hi_:<4d} n={n:5d}  reversed={r:4d}")
    lr_rev = [(a, b, g) for a, b, g in lr if a[1] > b[1]]
    print(f"  left->right pairs applied in reverse (should be ~0): {len(lr_rev)}")

    gaps = [g for _, _, g in rl_rev]
    delays = [a[4] for a, _, _ in rl_rev]
    print(f"\nREORDERED right->left pairs: {len(rl_rev)} of {len(rl)} "
          f"({100.0 * len(rl_rev) / max(1, len(rl)):.2f} %)")
    if rl_rev:
        print(f"  typing gap of the reordered pairs: p50 {pct(gaps, .5):.1f}  p95 {pct(gaps, .95):.1f}  max {max(gaps):.1f} ms")
        print(f"  split delay of the overtaken right key: p50 {pct(delays, .5):.1f}  p95 {pct(delays, .95):.1f}  max {max(delays):.1f} ms")
        print(f"  reorders where the right key's delay was <= 15 ms (normal link): "
              f"{sum(1 for d in delays if d <= 15)}; > 15 ms: {sum(1 for d in delays if d > 15)}")
        print("  examples (host time, right key, left key, gap, right delay, how late the right key landed after the left):")
        for a, b, g in sorted(rl_rev, key=lambda x: -x[2])[:12]:
            print(f"    {ad.tod(b[7])}  {key(a[3]):>5}({a[3]:2d}) then {key(b[3]):<5}({b[3]:2d})  gap {g:5.1f} ms  "
                  f"delay {a[4]:5.1f} ms  landed {(a[1] - b[1]) * 1e3:5.1f} ms after")
    fast = sorted((a, b, g) for a, b, g in rl if g < args.list_fast_ms)
    if fast:
        print(f"\nevery right->left pair with gap < {args.list_fast_ms:.0f} ms (REV = applied in reverse):")
        for a, b, g in sorted(fast, key=lambda x: x[2]):
            print(f"    {ad.tod(b[7])}  {key(a[3]):>5}({a[3]:2d}) then {key(b[3]):<5}({b[3]:2d})  gap {g:5.1f} ms  "
                  f"delay {a[4]:5.1f} ms  margin {(b[1] - a[1]) * 1e3:6.1f} ms {'REV' if a[1] > b[1] else ''}")
    # adaptive-variant check: at the left press of each reordered pair, was any
    # right key already down on the central (applied press without release), or
    # had any remote event been applied within the previous 300 ms?
    if rl_rev:
        held_cnt = recent_cnt = 0
        remote = defaultdict(list)          # segment -> its remote events, in order
        for e in cev:
            if e[6] == "R":
                remote[e[5]].append(e)
        queries = defaultdict(list)         # segment -> apply times to answer
        for a, b, g in rl_rev:
            queries[b[6]].append(b[1])
        for seg, times in queries.items():
            evs = remote.get(seg, ())
            down, last, k = set(), None, 0
            for tl in sorted(times):
                while k < len(evs) and evs[k][1] <= tl:
                    e = evs[k]
                    down.add(e[2]) if e[3] else down.discard(e[2])
                    last = e[1] if last is None else max(last, e[1])
                    k += 1
                held_cnt += bool(down)
                recent_cnt += last is not None and tl - last < 0.3
        print(f"  at the overtaking left press: a right key was already down on the central in {held_cnt} of {len(rl_rev)} cases; "
              f"a remote event had been applied within 300 ms in {recent_cnt} of {len(rl_rev)}")
    # exposure: how often is a left key pressed within X ms after a right key
    print("\nexposure: right->left pairs with gap < 15 ms: "
          f"{sum(1 for _, _, g in rl if g < 15)}  (< 10 ms: {sum(1 for _, _, g in rl if g < 10)}, "
          f"< 5 ms: {sum(1 for _, _, g in rl if g < 5)})")
    all_delays = [p[4] for p in presses if p[2] == "R"]
    print(f"split delay of all right presses (delay above best + floor {args.floor_ms:.1f}): "
          f"p50 {pct(all_delays, .5):.1f}  p95 {pct(all_delays, .95):.1f}  p99 {pct(all_delays, .99):.1f} ms")

    # P(reorder | gap) under a normal link = P(split delay > gap)
    normal = [p[4] for p in presses if p[2] == "R" and p[4] <= 30]
    print("\nprobability that a right key is still in flight g ms after it was confirmed (normal link, delay <= 30 ms; "
          f"n={len(normal)}):")
    print("   g ms:  " + "  ".join(f"{g:>5d}" for g in (2, 3, 4, 5, 6, 8, 10, 12, 15, 20)))
    print("   P   :  " + "  ".join(f"{100.0 * sum(1 for d in normal if d > g) / max(1, len(normal)):4.1f}%" for g in (2, 3, 4, 5, 6, 8, 10, 12, 15, 20)))
    # per-hour table
    print("\nper hour (host clock): right->left pairs / reordered / right delay p50,p95")
    byh = defaultdict(lambda: [0, 0, []])
    for a, b, g in rl:
        hh = int(b[7] // 3600) % 24 if b[7] is not None else -1
        byh[hh][0] += 1
        byh[hh][1] += 1 if a[1] > b[1] else 0
        byh[hh][2].append(a[4])
    for hh in sorted(byh):
        n, r, ds = byh[hh]
        print(f"  {hh:02d}h  pairs {n:5d}  reordered {r:4d}  delay p50 {pct(ds, .5):5.1f} p95 {pct(ds, .95):5.1f} ms")

    # hold-off replay
    print("\nlocal hold-off replay (all cross-half consecutive pairs; 'created' = left-first pairs that a held left key now loses):")
    print("   D ms   fixed/reordered R->L   created L->R   net reorders")
    holds = [float(x) for x in args.hold.split(",")]
    res = {}
    for D in holds:
        rl_r = sum(1 for a, b, _ in rl if a[1] > b[1] + D / 1e3)
        lr_r = sum(1 for a, b, _ in lr if a[1] + D / 1e3 > b[1])
        res[D] = (rl_r, lr_r)
        print(f"  {D:5.1f}   {len(rl_rev) - rl_r:5d}/{len(rl_rev):<5d}          {lr_r:5d}          {rl_r + lr_r:5d}")
    # how close are left->right pairs: margin = right arrival - left press
    margins = sorted((b[1] - a[1]) * 1e3 for a, b, _ in lr)
    print("  left->right pairs: right key landed this long after the left key was applied (ms): "
          f"p1 {pct(margins, .01):.1f}  p5 {pct(margins, .05):.1f}  p50 {pct(margins, .5):.1f}")

    if args.json:
        with open(args.json, "w") as fh:
            json.dump({"rl_pairs": len(rl), "rl_reordered": len(rl_rev), "lr_pairs": len(lr),
                       "reordered_gap_ms": gaps, "reordered_delay_ms": delays,
                       "hold": {str(D): {"rl_left": v[0], "lr_created": v[1]} for D, v in res.items()}}, fh, indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
