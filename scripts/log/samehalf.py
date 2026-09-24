#!/usr/bin/env python3
"""Same-half coalescing check: consecutive right-half presses within 2 ms
(scan order vs position order) and central bitmaps that changed >= 2 bits."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import analyze_drops as ad
pev, _ = ad.parse_periph(sys.argv[1]); notifs, _ = ad.parse_central(sys.argv[2])
close = [(a, b) for a, b in zip(pev, pev[1:]) if a[3] and b[3] and a[5] == b[5] and 0 <= (b[1] - a[1]) * 1e3 < 2.0]
desc = [(a, b) for a, b in close if b[2] < a[2]]
print(f"right half: {sum(1 for e in pev if e[3])} presses; consecutive presses < 2 ms apart: {len(close)} "
      f"(descending position: {len(desc)}, ascending: {len(close) - len(desc)}); identical timestamps: "
      f"{sum(1 for a, b in close if a[1] == b[1])}")
for a, b in close[:8]:
    print(f"   {ad.tod(a[0])} pos {a[2]} then {b[2]}  {(b[1] - a[1]) * 1e3:.3f} ms")
multi = []
prev = (0,) * 16
for j, n in enumerate(notifs):
    bits = n[2]
    ch = [pos for pos, _pressed in ad.bitmap_changes(bits, prev)]
    prev = bits
    if len(ch) >= 2:
        multi.append((n, ch))
print(f"left half: {len(notifs)} bitmaps; with >= 2 changed bits (coalesced): {len(multi)}")
for n, ch in multi[:12]:
    print(f"   {ad.tod(n[0])} changed {ch}")
