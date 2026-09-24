#!/usr/bin/env python3
"""Split-latency analyzer for ZMK USB CDC-ACM debug logs (stdlib only).

Input: one log file, or a peripheral log plus a central log captured at the
same time with scripts/log/capture.py (host timestamps let the two device
clocks be aligned). Lines are Zephyr log lines, optionally prefixed by the
capture.py "[host HH:MM:SS.mmm]" (or tio "[HH:MM:SS.mmm]") host timestamp:

    [host 12:34:56.789] [00:00:05.000,123] <dbg> zmk: func_name: message

The event stages below are keyed on ZMK `main` DBG messages (see the table in
scripts/log/README.md and snippets/cornix-debug-log/cornix-debug-log.conf for
the source references). Message strings can drift with ZMK main; every regex
is applied with re.search on the whole message, so a changed function-name
prefix does not break matching, but a reworded message does. If nothing
matches, the tool says so instead of printing zeros.

Stages (all times are the device's own log timestamp unless noted):
  peripheral  kscan -> position         debounce done -> matrix transform
              position -> split_listener  event handed to the split transport
              kscan -> split_listener
  central     notification -> trigger   GATT notify callback -> work queue
              trigger -> keymap         work queue -> keymap binding lookup
              keymap -> hid             binding -> HID report update/send
              notification -> hid
  central     local kscan -> hid        the central's own keys, for comparison
  cross       peripheral position -> central keymap / hid
              (device clocks aligned through the host timestamps)
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from collections import defaultdict, deque

# --------------------------------------------------------------------------
# line parsing
# --------------------------------------------------------------------------
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
# capture.py "[host HH:MM:SS.mmm]" or tio "[HH:MM:SS.mmm]" (also accepts "," and 1-6 fraction digits)
HOST_RE = re.compile(r"^\[(?:host\s+)?(\d{1,2}):(\d{2}):(\d{2})[.,](\d{1,6})\]\s*")
# Zephyr LOG_OUTPUT_FORMAT_TIME_TIMESTAMP: "[hh:mm:ss.mmm,uuu] <lvl> module: message"
ZEPHYR_RE = re.compile(
    r"\[(\d+):(\d{2}):(\d{2})\.(\d{3}),(\d{3})\]\s*<(dbg|inf|wrn|err)>\s*([A-Za-z0-9_.\-]+):\s?(.*)$"
)
# printk()/banner lines routed through the logger: timestamp but no level/module
TS_ONLY_RE = re.compile(r"\[(\d+):(\d{2}):(\d{2})\.(\d{3}),(\d{3})\]\s*(.*)$")
# CONFIG_LOG_FUNC_NAME_PREFIX_DBG: "<c identifier>: message"
FUNC_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*):\s?(.*)$")


class Line:
    __slots__ = ("no", "raw", "t_host", "t_dev", "level", "module", "func", "msg", "text", "seg")

    def __init__(self, no: int, raw: str) -> None:
        self.no = no
        self.raw = raw
        self.t_host: float | None = None
        self.t_dev: float | None = None
        self.level: str | None = None
        self.module: str | None = None
        self.func: str | None = None
        self.msg: str = ""
        self.text: str = ""  # message including the function prefix
        self.seg = 0

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "Line(%d, %r)" % (self.no, self.raw[:60])


def host_seconds(h: str, m: str, s: str, frac: str) -> float:
    return int(h) * 3600 + int(m) * 60 + int(s) + int(frac.ljust(6, "0")[:6]) / 1e6


def parse_line(raw: str, no: int = 0) -> Line | None:
    """Parse one log line. Returns None for blank lines."""
    raw = raw.rstrip("\r\n")
    if not raw.strip():
        return None
    line = Line(no, raw)
    s = ANSI_RE.sub("", raw)
    m = HOST_RE.match(s)
    if m:
        line.t_host = host_seconds(*m.groups())
        s = s[m.end():]
    z = ZEPHYR_RE.search(s)
    if z:
        h, mi, se, ms, us, lvl, mod, msg = z.groups()
        line.t_dev = int(h) * 3600 + int(mi) * 60 + int(se) + int(ms) / 1e3 + int(us) / 1e6
        line.level = lvl
        line.module = mod
        line.text = msg.strip()
        f = FUNC_RE.match(line.text) if lvl == "dbg" else None
        if f:
            line.func, line.msg = f.group(1), f.group(2)
        else:
            line.msg = line.text
        return line
    t = TS_ONLY_RE.search(s)
    if t:
        h, mi, se, ms, us, msg = t.groups()
        line.t_dev = int(h) * 3600 + int(mi) * 60 + int(se) + int(ms) / 1e3 + int(us) / 1e6
        line.text = line.msg = msg.strip()
        return line
    line.text = line.msg = s.strip()
    return line


class Lines(list):
    """The parsed lines of one file plus `segments`, the number of device-clock
    runs assign_segments() found (so nothing has to recompute max(seg))."""

    __slots__ = ("segments",)

    def __init__(self, items=(), segments: int = 1) -> None:
        super().__init__(items)
        self.segments = segments


def parse_file(path: str) -> Lines:
    lines: list[Line] = []
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for no, raw in enumerate(fh, 1):
            ln = parse_line(raw, no)
            if ln is not None:
                lines.append(ln)
    return Lines(lines, assign_segments(lines))


def assign_segments(lines: list[Line], reset_threshold: float = 5.0) -> int:
    """Number the monotonic runs of the device clock (a jump backwards = reboot)."""
    seg = 0
    last: float | None = None
    for ln in lines:
        if ln.t_dev is None:
            ln.seg = seg
            continue
        if last is not None and ln.t_dev < last - reset_threshold:
            seg += 1
        last = ln.t_dev
        ln.seg = seg
    return seg + 1


# --------------------------------------------------------------------------
# event extraction
# --------------------------------------------------------------------------
def _kscan(m: re.Match) -> dict:
    return {"row": int(m.group(1)), "col": int(m.group(2)), "pressed": m.group(3) == "on"}


def _position(m: re.Match) -> dict:
    return {"row": int(m.group(1)), "col": int(m.group(2)), "position": int(m.group(3)),
            "pressed": m.group(4) == "true"}


EVENT_PATTERNS = [
    # zmk/app/module/drivers/kscan/kscan_gpio_matrix.c kscan_matrix_read()
    ("kscan", re.compile(r"Sending event at (\d+),(\d+) state (on|off)"), _kscan),
    # zmk/app/src/physical_layouts.c zmk_physical_layouts_kscan_process_msgq()
    ("position", re.compile(r"Row: (\d+), col: (\d+), position: (\d+), pressed: (true|false)"), _position),
    # zmk/app/src/split/peripheral.c split_peripheral_listener(): LOG_DBG("")
    ("split_listener", re.compile(r"^split_peripheral_listener:?\s*$"), lambda m: {}),
    # zmk/app/src/split/bluetooth/central.c split_central_notify_func()
    ("notify", re.compile(r"\[NOTIFICATION\] data \S+ length (\d+)"), lambda m: {"length": int(m.group(1))}),
    # zmk/app/src/split/bluetooth/central.c peripheral_event_work_callback()
    ("trigger", re.compile(r"Trigger key position state change of type (\d+)"), lambda m: {"type": int(m.group(1))}),
    # zmk/app/src/keymap.c zmk_keymap_apply_position_state()
    ("keymap", re.compile(r"layer_id: (\d+) position: (\d+), binding name: (\S+)"),
     lambda m: {"layer": int(m.group(1)), "position": int(m.group(2)), "binding": m.group(3)}),
    # zmk/app/src/hid_listener.c hid_listener_keycode_pressed()/released()
    ("hid", re.compile(r"usage_page 0x([0-9A-Fa-f]+) keycode 0x([0-9A-Fa-f]+) implicit_mods"),
     lambda m: {"usage_page": int(m.group(1), 16), "keycode": int(m.group(2), 16)}),
]

# BLE connection-parameter lines (units: interval 1.25 ms, timeout 10 ms)
BLE_PATTERNS = [
    # zmk/app/src/ble.c le_param_updated() and zmk/app/src/split/bluetooth/peripheral.c le_param_updated()
    ("param_update", re.compile(r"interval (\d+) latency (\d+) timeout (\d+)"),
     lambda m: {"interval": int(m.group(1)), "latency": int(m.group(2)), "timeout": int(m.group(3))}),
    # zmk/app/src/split/bluetooth/central.c split_central_process_connection()
    ("conn_params", re.compile(r"New connection params: Interval: (\d+), Latency: (\d+), PHY: (\d+)"),
     lambda m: {"interval": int(m.group(1)), "latency": int(m.group(2)), "phy": int(m.group(3))}),
    # zephyr/subsys/bluetooth/host/hci_core.c le_phy_update_complete() (BT_HCI_CORE at DBG only)
    ("phy_update", re.compile(r"PHY updated: status: 0x([0-9A-Fa-f]+) ?(.*?),? tx: (\d+), rx: (\d+)"),
     lambda m: {"status": int(m.group(1), 16), "tx_phy": int(m.group(3)), "rx_phy": int(m.group(4))}),
    # zephyr/subsys/bluetooth/host/conn.c send_conn_le_param_update()/auto update (WRN)
    ("param_update_failed", re.compile(r"Send (?:auto )?LE param update failed \(err (-?\d+)\)"),
     lambda m: {"err": int(m.group(1))}),
]

# Other tools key on single stages/parameters (scripts/log/analyze_drops.py,
# scripts/bsim/measure.py); they take the pattern from here instead of copying it.
EVENT_RE = {kind: rx for kind, rx, _conv in EVENT_PATTERNS}
BLE_RE = {kind: rx for kind, rx, _conv in BLE_PATTERNS}

# dbg/inf lines that are worth listing next to the wrn/err ones
WARN_RE = re.compile(
    r"queue full|Error notifying|FAILED TO SEND|Failed to|failed|ENOMEM|\(-\d+\)|[Tt]imeout|"
    r"[Dd]isconnect|not connected|Not sending|Unable to|Ignoring|RF noise|Fatal error"
)
NUM_RE = re.compile(r"0x[0-9A-Fa-f]+|-?\d+")


class Event:
    __slots__ = ("kind", "line", "fields", "prev", "next")

    def __init__(self, kind: str, line: Line, fields: dict) -> None:
        self.kind = kind
        self.line = line
        self.fields = fields
        self.prev: Event | None = None
        self.next: Event | None = None

    @property
    def t(self) -> float:
        assert self.line.t_dev is not None
        return self.line.t_dev

    def __repr__(self) -> str:  # pragma: no cover
        return "Event(%s@%d %r)" % (self.kind, self.line.no, self.fields)


def extract_events(lines: list[Line]) -> list[Event]:
    events: list[Event] = []
    for ln in lines:
        if ln.t_dev is None or ln.level != "dbg":
            continue
        for kind, rx, conv in EVENT_PATTERNS:
            m = rx.search(ln.text)
            if m:
                ev = Event(kind, ln, conv(m))
                if kind == "hid" and ln.func:
                    ev.fields["pressed"] = not ln.func.endswith("released")
                events.append(ev)
                break
    return events


def extract_ble(lines: list[Line]) -> list[dict]:
    out = []
    for ln in lines:
        for kind, rx, conv in BLE_PATTERNS:
            m = rx.search(ln.text)
            if m:
                d = {"kind": kind, "line": ln.no, "t_dev": ln.t_dev, "module": ln.module, "text": ln.text}
                d.update(conv(m))
                if "interval" in d:
                    d["interval_ms"] = d["interval"] * 1.25
                    d["max_peripheral_latency_ms"] = (d["latency"] + 1) * d["interval"] * 1.25
                if "timeout" in d:
                    d["timeout_ms"] = d["timeout"] * 10
                out.append(d)
                break
    return out


def extract_warnings(lines: list[Line]) -> list[dict]:
    """wrn/err lines plus dbg/inf lines matching WARN_RE, de-duplicated by message shape."""
    groups: dict[tuple, dict] = {}
    for ln in lines:
        if ln.t_dev is None or ln.level is None:
            continue
        if ln.level not in ("wrn", "err") and not WARN_RE.search(ln.text):
            continue
        key = (ln.level, ln.module, NUM_RE.sub("#", ln.text))
        g = groups.get(key)
        if g is None:
            groups[key] = {"level": ln.level, "module": ln.module, "example": ln.text,
                           "count": 1, "first_line": ln.no, "first_t_dev": ln.t_dev}
        else:
            g["count"] += 1
    return sorted(groups.values(), key=lambda g: (-g["count"], g["first_line"]))


# --------------------------------------------------------------------------
# host/device clock alignment
# --------------------------------------------------------------------------
def percentile(sorted_vals: list[float], p: float) -> float:
    if not sorted_vals:
        raise ValueError("empty")
    idx = int(round(p / 100.0 * (len(sorted_vals) - 1)))
    return sorted_vals[max(0, min(idx, len(sorted_vals) - 1))]


def clock_offsets(lines: list[Line]) -> dict[int, dict]:
    """Per device-clock segment: offset so that t_dev + offset ~= t_host.

    The host stamp of a line is when it *arrived*, i.e. device time plus the
    deferred-log flush delay (0..LOG_PROCESS_THREAD_SLEEP_MS) plus USB. The
    5th percentile of (host - device) over many lines approximates the
    constant part of that delay, and the spread tells how noisy host stamps are.
    """
    per_seg: dict[int, list[float]] = defaultdict(list)
    for ln in lines:
        if ln.t_dev is not None and ln.t_host is not None:
            per_seg[ln.seg].append(ln.t_host - ln.t_dev)
    out = {}
    for seg, deltas in per_seg.items():
        deltas.sort()
        base = percentile(deltas, 5) if len(deltas) >= 20 else deltas[0]
        out[seg] = {
            "samples": len(deltas),
            "offset_s": base,
            "flush_delay_ms": {
                "min": (deltas[0] - base) * 1e3,
                "median": (statistics.median(deltas) - base) * 1e3,
                "p95": (percentile(deltas, 95) - base) * 1e3,
                "max": (deltas[-1] - base) * 1e3,
            },
        }
    return out


def aligned_time(ev: Event, offsets: dict[int, dict]) -> float | None:
    o = offsets.get(ev.line.seg)
    if o is None:
        return None
    return ev.t + o["offset_s"]


# --------------------------------------------------------------------------
# per-device pairing
# --------------------------------------------------------------------------
class DeviceStats:
    def __init__(self) -> None:
        self.stages: dict[str, list[dict]] = defaultdict(list)
        self.unmatched: dict[str, int] = defaultdict(int)
        self.events: list[Event] = []
        self.role_hint = "unknown"


def _add(stats: DeviceStats, stage: str, a: Event, b: Event) -> None:
    stats.stages[stage].append({"ms": (b.t - a.t) * 1e3, "from_line": a.line.no, "to_line": b.line.no})


def pair_device(events: list[Event], window_s: float) -> DeviceStats:
    """Link the events of one device into chains and collect stage latencies.

    kscan(row,col,pressed) -> position(row,col,pressed)    keyed match
    position -> split_listener                              immediately following
    notify -> trigger                                       FIFO
    trigger | position(position) -> keymap(position)        closest preceding candidate
    keymap -> hid                                           only if no other event intervenes
    """
    st = DeviceStats()
    st.events = events
    kinds = {e.kind for e in events}
    if "split_listener" in kinds and "notify" not in kinds:
        st.role_hint = "peripheral"
    elif "notify" in kinds or "trigger" in kinds:
        st.role_hint = "central"
    elif "hid" in kinds:
        st.role_hint = "central"

    kscan_q: dict[tuple, deque] = defaultdict(deque)
    pos_q: dict[int, deque] = defaultdict(deque)
    last_position: Event | None = None
    notify_q: deque = deque()
    trigger_q: deque = deque()
    pending_keymap: Event | None = None

    def stale(q: deque, now: float) -> None:
        while q and now - q[0].t > window_s:
            st.unmatched[q[0].kind] += 1
            q.popleft()

    for ev in events:
        if ev.kind == "kscan":
            stale(kscan_q[(ev.fields["row"], ev.fields["col"], ev.fields["pressed"])], ev.t)
            kscan_q[(ev.fields["row"], ev.fields["col"], ev.fields["pressed"])].append(ev)
        elif ev.kind == "position":
            key = (ev.fields["row"], ev.fields["col"], ev.fields["pressed"])
            stale(kscan_q[key], ev.t)
            if kscan_q[key]:
                k = kscan_q[key].popleft()
                ev.prev, k.next = k, ev
                _add(st, "kscan -> position", k, ev)
            stale(pos_q[ev.fields["position"]], ev.t)
            pos_q[ev.fields["position"]].append(ev)
            last_position = ev
        elif ev.kind == "split_listener":
            if last_position is not None and last_position.next is None and ev.t - last_position.t <= window_s:
                ev.prev, last_position.next = last_position, ev
                _add(st, "position -> split_listener", last_position, ev)
                if last_position.prev is not None:
                    _add(st, "kscan -> split_listener", last_position.prev, ev)
            else:
                st.unmatched["split_listener"] += 1
            last_position = None
        elif ev.kind == "notify":
            # A new notification supersedes the previous one: its triggers are
            # all logged before the next notify callback runs.
            for n in notify_q:
                if n.next is None:
                    st.unmatched["notification"] += 1
            notify_q.clear()
            notify_q.append(ev)
        elif ev.kind == "trigger":
            stale(notify_q, ev.t)
            if notify_q:
                n = notify_q[-1]  # one notification can carry several changed positions
                ev.prev = n
                if n.next is None:
                    n.next = ev
                _add(st, "notification -> trigger", n, ev)
            else:
                st.unmatched["trigger"] += 1
            stale(trigger_q, ev.t)
            trigger_q.append(ev)
        elif ev.kind == "keymap":
            if pending_keymap is not None:
                st.unmatched["keymap (no immediate hid)"] += 1
            pending_keymap = ev
            stale(trigger_q, ev.t)
            pq = pos_q[ev.fields["position"]]
            stale(pq, ev.t)
            cands = []
            if trigger_q:
                cands.append(trigger_q[0])
            if pq:
                cands.append(pq[0])
            if cands:
                src = max(cands, key=lambda c: c.t)
                if src.kind == "trigger":
                    trigger_q.popleft()
                    _add(st, "trigger -> keymap", src, ev)
                    if src.prev is not None:
                        _add(st, "notification -> keymap", src.prev, ev)
                else:
                    pq.popleft()
                    _add(st, "local position -> keymap", src, ev)
                    if src.prev is not None:
                        _add(st, "local kscan -> keymap", src.prev, ev)
                ev.prev, src.next = src, ev
            else:
                st.unmatched["keymap"] += 1
        elif ev.kind == "hid":
            if pending_keymap is not None and ev.t - pending_keymap.t <= window_s:
                km = pending_keymap
                ev.prev, km.next = km, ev
                _add(st, "keymap -> hid", km, ev)
                src = km.prev
                if src is not None and src.kind == "trigger":
                    _add(st, "trigger -> hid", src, ev)
                    if src.prev is not None:
                        _add(st, "notification -> hid", src.prev, ev)
                elif src is not None and src.kind == "position":
                    _add(st, "local position -> hid", src, ev)
                    if src.prev is not None:
                        _add(st, "local kscan -> hid", src.prev, ev)
                pending_keymap = None
            else:
                st.unmatched["hid"] += 1

    # leftovers
    for q in kscan_q.values():
        st.unmatched["kscan"] += len(q)
    st.unmatched["notification"] += len(notify_q)
    st.unmatched["trigger"] += len(trigger_q)
    if pending_keymap is not None:
        st.unmatched["keymap (no immediate hid)"] += 1
    return st


# --------------------------------------------------------------------------
# cross-device pairing
# --------------------------------------------------------------------------
def pair_cross(periph: DeviceStats, central: DeviceStats, off_p: dict, off_c: dict,
               window_s: float, tol_s: float = 0.003) -> dict[str, list[dict]]:
    """Match peripheral position events to central keymap events by key position."""
    stages: dict[str, list[dict]] = defaultdict(list)
    by_pos: dict[int, deque] = defaultdict(deque)
    for ev in central.events:
        if ev.kind == "keymap" and aligned_time(ev, off_c) is not None:
            by_pos[ev.fields["position"]].append(ev)
    for pev in periph.events:
        if pev.kind != "position":
            continue
        ta = aligned_time(pev, off_p)
        if ta is None:
            continue
        q = by_pos[pev.fields["position"]]
        while q and aligned_time(q[0], off_c) < ta - tol_s:
            q.popleft()
        if not q:
            continue
        cev = q[0]
        tc = aligned_time(cev, off_c)
        if tc - ta > window_s:
            continue
        q.popleft()
        rec = {"ms": (tc - ta) * 1e3, "position": pev.fields["position"],
               "peripheral_line": pev.line.no, "central_line": cev.line.no}
        stages["peripheral position -> central keymap (aligned)"].append(rec)
        if pev.line.t_host is not None and cev.line.t_host is not None:
            stages["peripheral position -> central keymap (raw host stamps)"].append(
                dict(rec, ms=(cev.line.t_host - pev.line.t_host) * 1e3))
        if pev.prev is not None:
            stages["peripheral kscan -> central keymap (aligned)"].append(
                dict(rec, ms=(tc - aligned_time(pev.prev, off_p)) * 1e3))
        if cev.next is not None and cev.next.kind == "hid":
            th = aligned_time(cev.next, off_c)
            stages["peripheral position -> central hid (aligned)"].append(dict(rec, ms=(th - ta) * 1e3))
            if pev.prev is not None:
                stages["peripheral kscan -> central hid (aligned)"].append(
                    dict(rec, ms=(th - aligned_time(pev.prev, off_p)) * 1e3))
    return stages


# --------------------------------------------------------------------------
# reporting
# --------------------------------------------------------------------------
def summarize(samples: list[dict]) -> dict:
    vals = sorted(s["ms"] for s in samples)
    return {
        "count": len(vals),
        "min": vals[0],
        "median": statistics.median(vals),
        "p95": percentile(vals, 95),
        "max": vals[-1],
        "unit": "ms",
    }


def histogram(vals: list[float], bins: int = 8, width: int = 30) -> list[str]:
    if not vals:
        return []
    lo, hi = min(vals), max(vals)
    if hi - lo < 1e-9:
        return ["  %8.2f ms  %s %d" % (lo, "#" * min(width, len(vals)), len(vals))]
    step = (hi - lo) / bins
    counts = [0] * bins
    for v in vals:
        i = min(int((v - lo) / step), bins - 1)
        counts[i] += 1
    peak = max(counts)
    rows = []
    for i, c in enumerate(counts):
        rows.append("  %8.2f-%-8.2f %s %d" % (lo + i * step, lo + (i + 1) * step,
                                              "#" * int(round(width * c / peak)), c))
    return rows


def fmt_ms(x: float) -> str:
    return "%.2f" % x


# The stage table is printed by scripts/model/split_latency_model.py too, so
# that a model run and an analyzer run can be read side by side.
TABLE_HEADER = "  %-58s %6s %8s %8s %8s %8s" % ("stage", "count", "min", "median", "p95", "max")


def table_row(name: str, s: dict) -> str:
    return "  %-58s %6d %8s %8s %8s %8s" % (name, s["count"], fmt_ms(s["min"]), fmt_ms(s["median"]),
                                            fmt_ms(s["p95"]), fmt_ms(s["max"]))


def report_text(result: dict, show_hist: bool, verbose: bool) -> str:
    out: list[str] = []
    for f in result["files"]:
        out.append("file: %s  label=%s  role=%s  lines=%d  events=%d  device-clock segments=%d" % (
            f["path"], f["label"], f["role_hint"], f["lines"], f["events"], f["segments"]))
        for seg, o in sorted(f["clock_offsets"].items()):
            fd = o["flush_delay_ms"]
            out.append("  host/device clock (segment %s): %d samples, log flush delay min/median/p95/max = "
                       "%s/%s/%s/%s ms" % (seg, o["samples"], fmt_ms(fd["min"]), fmt_ms(fd["median"]),
                                            fmt_ms(fd["p95"]), fmt_ms(fd["max"])))
    out.append("")
    if not result["stages"]:
        out.append(result["message"])
        return "\n".join(out)

    out.append("latency per stage (ms)")
    out.append(TABLE_HEADER)
    for name, s in result["stages"].items():
        out.append(table_row(name, s))
        if show_hist:
            out.extend(histogram([x["ms"] for x in s["samples"]]))
        if verbose:
            for x in s["samples"]:
                out.append("      %8s ms  %s" % (fmt_ms(x["ms"]), " ".join(
                    "%s=%s" % (k, v) for k, v in x.items() if k != "ms")))
    unmatched = {k: v for k, v in result["unmatched"].items() if v}
    if unmatched:
        out.append("  unmatched events: " + ", ".join("%s=%d" % kv for kv in sorted(unmatched.items())))
    out.append("")

    out.append("BLE connection parameters seen in the logs")
    if not result["ble"]:
        out.append("  (none - le_param_updated / New connection params lines need CONFIG_ZMK_LOG_LEVEL_DBG)")
    for b in result["ble"]:
        extra = ""
        if "interval" in b:
            extra = "  => interval %.2f ms, latency %d (worst-case %.1f ms)" % (
                b["interval_ms"], b["latency"], b["max_peripheral_latency_ms"])
            if "timeout_ms" in b:
                extra += ", supervision timeout %d ms" % b["timeout_ms"]
        out.append("  [%s] line %d t=%.3f %s: %s%s" % (b["label"], b["line"], b["t_dev"] or 0.0,
                                                       b["kind"], b["text"], extra))
    out.append("")

    out.append("warnings / errors (grouped)")
    if not result["warnings"]:
        out.append("  (none)")
    for w in result["warnings"]:
        out.append("  [%s] %5dx <%s> %s: %s  (first at line %d, t=%.3f)" % (
            w["label"], w["count"], w["level"], w["module"], w["example"], w["first_line"], w["first_t_dev"]))
    return "\n".join(out)


NO_EVENTS_MSG = ("no matching events found - check that the debug snippet is enabled "
                 "(build with -S \"zmk-usb-logging cornix-debug-log\", CONFIG_ZMK_LOG_LEVEL_DBG=y) "
                 "and that the log contains '<dbg> zmk:' lines such as 'Sending event at', "
                 "'Row: ..., position:', '[NOTIFICATION]' or 'usage_page 0x..'.")


def analyze(paths: list[tuple[str, str]], window_ms: float = 2000.0, max_warnings: int = 30,
            keep_samples: bool = True) -> dict:
    """paths: list of (label, path); label in {'peripheral','central','device'}."""
    window_s = window_ms / 1e3
    files = []
    devices: dict[str, DeviceStats] = {}
    offsets: dict[str, dict] = {}
    ble: list[dict] = []
    warnings: list[dict] = []
    for label, path in paths:
        lines = parse_file(path)
        events = extract_events(lines)
        st = pair_device(events, window_s)
        devices[label] = st
        offsets[label] = clock_offsets(lines)
        for b in extract_ble(lines):
            b["label"] = label
            ble.append(b)
        for w in extract_warnings(lines):
            w["label"] = label
            warnings.append(w)
        files.append({"path": path, "label": label, "role_hint": st.role_hint, "lines": len(lines),
                      "events": len(events), "segments": lines.segments,
                      "clock_offsets": offsets[label]})

    stages: dict[str, list[dict]] = {}
    unmatched: dict[str, int] = {}
    for label, st in devices.items():
        for name, samples in st.stages.items():
            stages["%s: %s" % (label, name)] = samples
        for k, v in st.unmatched.items():
            unmatched["%s: %s" % (label, k)] = v
    if "peripheral" in devices and "central" in devices:
        cross = pair_cross(devices["peripheral"], devices["central"], offsets["peripheral"],
                           offsets["central"], window_s)
        for name, samples in cross.items():
            stages["cross: %s" % name] = samples

    summary = {}
    for name, samples in stages.items():
        s = summarize(samples)
        if keep_samples:
            s["samples"] = samples
        summary[name] = s
    warnings.sort(key=lambda w: (-w["count"], w["label"], w["first_line"]))
    return {
        "files": files,
        "window_ms": window_ms,
        "stages": summary,
        "unmatched": unmatched,
        "ble": ble,
        "warnings": warnings[:max_warnings],
        "message": "" if summary else NO_EVENTS_MSG,
    }


def doc_parser(doc: str) -> argparse.ArgumentParser:
    """Parser whose --help is the module docstring: first paragraph, then the rest."""
    return argparse.ArgumentParser(description=doc.split("\n\n")[0],
                                   formatter_class=argparse.RawDescriptionHelpFormatter,
                                   epilog=doc.split("\n\n", 1)[1])


def add_log_arguments(p: argparse.ArgumentParser,
                      files_help: str = "log file(s)") -> None:
    """The log-selection arguments every tool here takes."""
    p.add_argument("files", nargs="*", help=files_help)
    p.add_argument("--peripheral", metavar="LOG", help="log captured from the peripheral (right) half")
    p.add_argument("--central", metavar="LOG", help="log captured from the central (left half or dongle)")


def paths_from_args(args: argparse.Namespace, offset_files: bool = True) -> list[tuple[str, str]]:
    """(label, path) pairs from --peripheral/--central and the positional files.

    offset_files=True (this tool): the positional files continue the numbering
    after --peripheral/--central, so `--peripheral P extra.log` labels extra.log
    "device2".  False (analyze_ble.py): the files are numbered on their own and
    a lone file is always "device".  Both spellings are kept deliberately; the
    label only names the file in the report and in the JSON.
    """
    paths: list[tuple[str, str]] = []
    if getattr(args, "peripheral", None):
        paths.append(("peripheral", args.peripheral))
    if getattr(args, "central", None):
        paths.append(("central", args.central))
    base = len(paths) if offset_files else 0
    for i, f in enumerate(args.files, 1):
        alone = len(args.files) == 1 and (base == 0 or not offset_files)
        paths.append(("device" if alone else "device%d" % (base + i), f))
    return paths


def emit_json(json_arg: str | None, result: dict, text: str, note: bool = True) -> None:
    """Print the report, or the whole result as JSON when --json is '-'."""
    if json_arg == "-":
        print(json.dumps(result, indent=2))
        return
    print(text)
    if json_arg:
        with open(json_arg, "w", encoding="utf-8") as fh:
            json.dump(result, fh, indent=2)
        if note:
            print("json written to %s" % json_arg)


def build_parser() -> argparse.ArgumentParser:
    p = doc_parser(__doc__)
    add_log_arguments(p, "log file(s); a single positional file is analyzed alone")
    p.add_argument("--window", type=float, default=2000.0, help="max ms between linked events (default 2000)")
    p.add_argument("--json", metavar="FILE", help="also write the full result as JSON ('-' for stdout)")
    p.add_argument("--no-samples", action="store_true", help="omit per-event samples from the JSON")
    p.add_argument("--hist", action="store_true", help="print a small histogram per stage")
    p.add_argument("--verbose", action="store_true", help="print every matched pair with line numbers")
    p.add_argument("--max-warnings", type=int, default=30)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    paths = paths_from_args(args)
    if not paths:
        build_parser().print_usage(sys.stderr)
        return 2
    result = analyze(paths, args.window, args.max_warnings, keep_samples=not args.no_samples)
    emit_json(args.json, result, report_text(result, args.hist, args.verbose))
    return 0 if result["stages"] else 1


if __name__ == "__main__":
    sys.exit(main())
