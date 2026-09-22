#!/usr/bin/env python3
"""Discrete-event model of ZMK split-keyboard key latency over the BLE link.

Standard library only. This is a *teaching and regression* model, not a radio
or controller simulator: it answers "given a connection interval I, a
peripheral latency L and a wake policy, how long does a key pressed on the
peripheral (right half) wait until the central (left half) applies it to the
keymap?"  See scripts/model/README.md for the assumptions and for what the
Zephyr controller source says about the wake policy.

Stages (all in ms, matching the log stages of scripts/log/analyze_latency.py):

  key press --scan--> position --queue--> queued --wait--> air --central--> keymap
              (1)                 (2)              (3)          (4)

  (1) --scan-ms     debounce + matrix scan on the peripheral
                    (CONFIG_ZMK_KSCAN_DEBOUNCE_PRESS_MS=3 on cornix_right)
  (2) --queue-ms    zmk_split_bt_position_pressed() -> k_msgq_put ->
                    k_work_submit_to_queue(service_work_q) -> bt_gatt_notify ->
                    host TX -> HCI -> controller ll_tx_mem_enqueue()
  (3) wait          the interesting part: the data sits in the controller until
                    the next *attended* connection event (see --wake-on-data)
  (4) --air-ms + --central-ms
                    on-air time inside the connection event, then the central's
                    notify callback -> work queue -> keymap

The reported stage "peripheral position -> central keymap" is (2)+(3)+(4) and
is directly comparable with the analyzer's
"cross: peripheral position -> central keymap (aligned)" stage.

Connection events are anchored at t0 + k*I (k = 0, 1, 2, ...).  Which events
the peripheral attends depends on --wake-on-data:

  none       the peripheral sleeps through its whole allowed latency: after an
             attended event k it next attends k + (L+1), whatever happens in
             between.  A key event waits on average (L+1)*I/2 and at worst
             (L+1)*I.  (Hypothetical "no early wake" controller.)
  immediate  the controller cancels the latency as soon as the host queues
             data (what Zephyr's ull_periph_latency_cancel() does): the data
             goes out at the first event whose anchor is at least --prepare-ms
             after it was queued, so latency L has no effect on key delay.

Optional packet loss (--loss-prob) makes the notification miss its event
and be retransmitted at the next attended event.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import statistics
import sys

CONN_INTERVAL_UNIT_MS = 1.25   # BLE connection interval unit
SUPERVISION_UNIT_MS = 10.0     # BLE supervision timeout unit

CROSS_STAGE = "peripheral position -> central keymap (aligned)"
MEASURED_STAGE = "cross: " + CROSS_STAGE
MODEL_STAGE = "model: " + CROSS_STAGE

WAKE_MODES = ("none", "immediate")
DEFAULT_SWEEP = "0,1,2,4,8,16,30"


# --------------------------------------------------------------------------
# parameter parsing
# --------------------------------------------------------------------------
class Dist:
    """A tiny distribution spec: fixed value, uniform range, or a cycled list."""

    def __init__(self, kind: str, values: list[float]) -> None:
        self.kind = kind
        self.values = values
        self._i = 0

    @classmethod
    def parse(cls, text: str) -> "Dist":
        text = str(text).strip()
        kind = None
        if ":" in text:
            kind, text = text.split(":", 1)
            kind = kind.strip().lower()
        try:
            values = [float(x) for x in text.replace(";", ",").split(",") if x.strip()]
        except ValueError:
            raise argparse.ArgumentTypeError("cannot parse numbers in %r" % text)
        if not values:
            raise argparse.ArgumentTypeError("empty distribution %r" % text)
        if kind is None:
            kind = "fixed" if len(values) == 1 else ("uniform" if len(values) == 2 else "list")
        if kind == "fixed" and len(values) != 1:
            raise argparse.ArgumentTypeError("fixed: takes one value")
        if kind == "uniform":
            if len(values) != 2 or values[0] > values[1]:
                raise argparse.ArgumentTypeError("uniform: takes LO,HI with LO <= HI")
        elif kind not in ("fixed", "list"):
            raise argparse.ArgumentTypeError("unknown distribution kind %r (fixed|uniform|list)" % kind)
        if any(v < 0 for v in values):
            raise argparse.ArgumentTypeError("values must be >= 0")
        return cls(kind, values)

    def sample(self, rng: random.Random) -> float:
        if self.kind == "fixed":
            return self.values[0]
        if self.kind == "uniform":
            return rng.uniform(self.values[0], self.values[1])
        v = self.values[self._i % len(self.values)]
        self._i += 1
        return v

    def reset(self) -> None:
        self._i = 0

    @property
    def max(self) -> float:
        return max(self.values)

    def describe(self) -> str:
        return "%s:%s" % (self.kind, ",".join("%g" % v for v in self.values))


def parse_int_list(text: str) -> list[int]:
    try:
        out = [int(x) for x in text.replace(";", ",").split(",") if x.strip()]
    except ValueError:
        raise argparse.ArgumentTypeError("cannot parse integer list %r" % text)
    if not out or any(x < 0 for x in out):
        raise argparse.ArgumentTypeError("latency list must be non-negative integers")
    return out


# --------------------------------------------------------------------------
# summary helpers (kept identical in style to scripts/log/analyze_latency.py)
# --------------------------------------------------------------------------
def percentile(sorted_vals: list[float], p: float) -> float:
    if not sorted_vals:
        raise ValueError("empty")
    idx = int(round(p / 100.0 * (len(sorted_vals) - 1)))
    return sorted_vals[max(0, min(idx, len(sorted_vals) - 1))]


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


def fmt_ms(x: float) -> str:
    return "%.2f" % x


TABLE_HEADER = "  %-58s %6s %8s %8s %8s %8s" % ("stage", "count", "min", "median", "p95", "max")


def table_row(name: str, s: dict) -> str:
    return "  %-58s %6d %8s %8s %8s %8s" % (name, s["count"], fmt_ms(s["min"]), fmt_ms(s["median"]),
                                            fmt_ms(s["p95"]), fmt_ms(s["max"]))


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


# --------------------------------------------------------------------------
# the model
# --------------------------------------------------------------------------
class Params:
    def __init__(self, interval_units: int = 6, latency: int = 30, timeout_units: int = 400,
                 events: int = 200, seed: int = 1, key_gap: Dist | str = "uniform:50,2000",
                 wake_on_data: str = "none", scan: Dist | str = "uniform:3,4",
                 queue: Dist | str = "uniform:0.2,0.6", air_ms: float = 0.3,
                 central: Dist | str = "uniform:0.2,0.6", prepare_ms: float = 0.5,
                 loss_prob: float = 0.0) -> None:
        if interval_units < 6 or interval_units > 3200:
            raise ValueError("interval units must be 6..3200 (7.5 ms .. 4 s)")
        if latency < 0 or latency > 499:
            raise ValueError("latency must be 0..499")
        if wake_on_data not in WAKE_MODES:
            raise ValueError("wake_on_data must be one of %s" % (WAKE_MODES,))
        if not 0.0 <= loss_prob < 1.0:
            raise ValueError("loss probability must be in [0, 1)")
        if events < 1:
            raise ValueError("events must be >= 1")
        self.interval_units = interval_units
        self.latency = latency
        self.timeout_units = timeout_units
        self.events = events
        self.seed = seed
        self.key_gap = key_gap if isinstance(key_gap, Dist) else Dist.parse(key_gap)
        self.wake_on_data = wake_on_data
        self.scan = scan if isinstance(scan, Dist) else Dist.parse(scan)
        self.queue = queue if isinstance(queue, Dist) else Dist.parse(queue)
        self.air_ms = float(air_ms)
        self.central = central if isinstance(central, Dist) else Dist.parse(central)
        self.prepare_ms = float(prepare_ms)
        self.loss_prob = float(loss_prob)

    @property
    def interval_ms(self) -> float:
        return self.interval_units * CONN_INTERVAL_UNIT_MS

    @property
    def worst_case_wait_ms(self) -> float:
        """(L+1) x I: the longest a queued packet can wait with no early wake."""
        return (self.latency + 1) * self.interval_ms

    @property
    def supervision_timeout_ms(self) -> float:
        return self.timeout_units * SUPERVISION_UNIT_MS

    def with_latency(self, latency: int) -> "Params":
        p = Params.__new__(Params)
        p.__dict__.update(self.__dict__)
        p.latency = latency
        return p

    def to_dict(self) -> dict:
        return {
            "interval_units": self.interval_units,
            "interval_ms": self.interval_ms,
            "latency": self.latency,
            "timeout_units": self.timeout_units,
            "supervision_timeout_ms": self.supervision_timeout_ms,
            "events": self.events,
            "seed": self.seed,
            "key_gap_ms": self.key_gap.describe(),
            "wake_on_data": self.wake_on_data,
            "scan_ms": self.scan.describe(),
            "queue_ms": self.queue.describe(),
            "air_ms": self.air_ms,
            "central_ms": self.central.describe(),
            "prepare_ms": self.prepare_ms,
            "loss_prob": self.loss_prob,
        }


def generate_keys(p: Params) -> tuple[float, list[float]]:
    """Return (t0 of the connection-event grid, key press times) for the seed.

    Uses its own RNG stream so that every latency in a sweep sees exactly the
    same key presses and the same event grid phase.
    """
    rng = random.Random(p.seed)
    p.key_gap.reset()
    t0 = rng.uniform(0.0, p.interval_ms)   # phase of the grid relative to the keys
    keys = []
    t = 0.0
    for _ in range(p.events):
        t += p.key_gap.sample(rng)
        keys.append(t)
    return t0, keys


def simulate(p: Params) -> dict:
    """Run the model. Returns the result dict (see README for the schema)."""
    t0, keys = generate_keys(p)
    rng = random.Random(p.seed + 1_000_003)   # per-key processing noise and losses
    p.scan.reset()
    p.queue.reset()
    p.central.reset()
    I = p.interval_ms
    L1 = p.latency + 1

    def anchor(k: int) -> float:
        return t0 + k * I

    def first_event_at_or_after(t: float) -> int:
        return max(0, int(math.ceil((t - t0) / I - 1e-9)))

    stages: dict[str, list[dict]] = {
        "model: kscan -> position": [],
        "model: position -> queued": [],
        "model: queued -> air (connection-event wait)": [],
        MODEL_STAGE: [],
        "model: key press -> central keymap": [],
    }
    k_last = 0            # last attended connection event (index 0 is always attended)
    attended = 1
    retransmissions = 0
    idle_wakes = 0
    last_delivery = 0.0

    for n, t_press in enumerate(keys):
        scan = p.scan.sample(rng)
        queue = p.queue.sample(rng)
        central = p.central.sample(rng)
        t_position = t_press + scan
        t_queued = t_position + queue
        ready = t_queued + p.prepare_ms
        if anchor(k_last) >= ready:
            # The peripheral is already scheduled to listen at k_last, which is
            # still in the future because an earlier key is waiting for it.
            # This key rides the same connection event: send_position_state_callback()
            # drains the whole message queue (zmk app/src/split/bluetooth/service.c)
            # and the controller can put several PDUs into one connection event.
            k = k_last
        else:
            # Events the peripheral attended with nothing to send since the last one.
            while anchor(k_last + L1) < ready:
                k_last += L1
                attended += 1
                idle_wakes += 1
            k_idle = k_last + L1      # the event the latency window forces it to attend
            if p.wake_on_data == "none":
                k = k_idle
            else:
                k = min(k_idle, first_event_at_or_after(ready))
            attended += 1
        retries = 0
        while p.loss_prob > 0.0 and rng.random() < p.loss_prob:
            # not acknowledged: the PDU stays in the TX queue and is resent at the
            # next attended event (immediate: next interval; none: next grid event)
            k += 1 if p.wake_on_data == "immediate" else L1
            attended += 1
            retries += 1
        retransmissions += retries
        k_last = k
        t_air = anchor(k) + p.air_ms
        t_keymap = t_air + central
        last_delivery = t_keymap
        rec = {"key": n, "t_press_ms": t_press, "event": k, "retries": retries}
        stages["model: kscan -> position"].append(dict(rec, ms=scan))
        stages["model: position -> queued"].append(dict(rec, ms=queue))
        stages["model: queued -> air (connection-event wait)"].append(dict(rec, ms=t_air - t_queued))
        stages[MODEL_STAGE].append(dict(rec, ms=t_keymap - t_position))
        stages["model: key press -> central keymap"].append(dict(rec, ms=t_keymap - t_press))

    span_ms = max(last_delivery - keys[0], I)
    summary = {}
    for name, samples in stages.items():
        s = summarize(samples)
        s["samples"] = samples
        summary[name] = s
    warnings = []
    if 2 * p.worst_case_wait_ms > p.supervision_timeout_ms:
        warnings.append("supervision timeout %.0f ms < 2 x (L+1) x I = %.1f ms: the BLE spec requires the "
                        "timeout to exceed twice the latency window; the controller would reject these "
                        "parameters" % (p.supervision_timeout_ms, 2 * p.worst_case_wait_ms))
    return {
        "params": p.to_dict(),
        "derived": {
            "interval_ms": I,
            "worst_case_wait_ms": p.worst_case_wait_ms,
            "mean_wait_no_wake_ms": p.worst_case_wait_ms / 2.0,
            "idle_wakes_per_s": 1000.0 / p.worst_case_wait_ms,
            "supervision_timeout_ms": p.supervision_timeout_ms,
            "grid_t0_ms": t0,
        },
        "stages": summary,
        "wake": {
            "attended_events": attended,
            "idle_wakes": idle_wakes,
            "span_s": span_ms / 1000.0,
            "events_per_s": attended / (span_ms / 1000.0),
        },
        "retransmissions": retransmissions,
        "warnings": warnings,
    }


def run_sweep(p: Params, latencies: list[int]) -> list[dict]:
    rows = []
    for L in latencies:
        r = simulate(p.with_latency(L))
        s = r["stages"][MODEL_STAGE]
        rows.append({
            "latency": L,
            "worst_case_ms": (L + 1) * p.interval_ms,
            "median": s["median"],
            "p95": s["p95"],
            "max": s["max"],
            "idle_wakes_per_s": r["derived"]["idle_wakes_per_s"],
            "sim_wakes_per_s": r["wake"]["events_per_s"],
        })
    return rows


# --------------------------------------------------------------------------
# comparison with scripts/log/analyze_latency.py --json output
# --------------------------------------------------------------------------
def load_measured(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict) or "stages" not in data:
        raise ValueError("%s does not look like analyze_latency.py --json output (no 'stages')" % path)
    return data


def compare(model_result: dict, measured: dict) -> dict:
    """Pick the cross stage out of the analyzer's JSON and put it next to the model."""
    m = measured["stages"].get(MEASURED_STAGE)
    out: dict = {"measured_stage": MEASURED_STAGE, "model_stage": MODEL_STAGE, "ble": []}
    for b in measured.get("ble", []):
        if "interval" in b and "latency" in b:
            out["ble"].append({"label": b.get("label"), "interval": b["interval"], "latency": b["latency"],
                               "timeout": b.get("timeout"), "kind": b.get("kind")})
    if m is None:
        out["measured"] = None
        out["message"] = ("stage %r not in the JSON (needs --peripheral and --central logs with host "
                          "timestamps); available: %s" % (MEASURED_STAGE, ", ".join(measured["stages"])))
        return out
    model = model_result["stages"][MODEL_STAGE]
    out["measured"] = {k: m[k] for k in ("count", "min", "median", "p95", "max")}
    out["model"] = {k: model[k] for k in ("count", "min", "median", "p95", "max")}
    out["delta_model_minus_measured"] = {k: model[k] - m[k] for k in ("min", "median", "p95", "max")}
    if "samples" in m:
        out["measured_samples_ms"] = [s["ms"] for s in m["samples"]]
    return out


# --------------------------------------------------------------------------
# reporting
# --------------------------------------------------------------------------
def report_text(result: dict, show_hist: bool = False, verbose: bool = False) -> str:
    p = result["params"]
    d = result["derived"]
    out = [
        "split-latency model: interval %d x 1.25 = %.2f ms, latency %d, supervision timeout %.0f ms, "
        "wake-on-data=%s, %d key events, seed %d" % (
            p["interval_units"], d["interval_ms"], p["latency"], d["supervision_timeout_ms"],
            p["wake_on_data"], p["events"], p["seed"]),
        "  key gap %s ms, scan %s ms, queue %s ms, air %.2f ms, central %s ms, prepare guard %.2f ms, "
        "loss %.3f" % (p["key_gap_ms"], p["scan_ms"], p["queue_ms"], p["air_ms"], p["central_ms"],
                       p["prepare_ms"], p["loss_prob"]),
        "  worst-case wait (L+1) x I = %.1f ms, mean wait without early wake = %.1f ms, "
        "idle radio wakes %.2f /s" % (d["worst_case_wait_ms"], d["mean_wait_no_wake_ms"],
                                       d["idle_wakes_per_s"]),
        "",
        "latency per stage (ms)",
        TABLE_HEADER,
    ]
    for name, s in result["stages"].items():
        out.append(table_row(name, s))
        if show_hist:
            out.extend(histogram([x["ms"] for x in s["samples"]]))
        if verbose:
            for x in s["samples"]:
                out.append("      %8s ms  %s" % (fmt_ms(x["ms"]), " ".join(
                    "%s=%s" % (k, ("%.2f" % v) if isinstance(v, float) else v)
                    for k, v in x.items() if k != "ms")))
    w = result["wake"]
    out.append("  radio wakes: %d connection events attended over %.1f s = %.2f /s (%d idle), "
               "%d retransmissions" % (w["attended_events"], w["span_s"], w["events_per_s"],
                                        w["idle_wakes"], result["retransmissions"]))
    for msg in result["warnings"]:
        out.append("  warning: " + msg)
    if "sweep" in result:
        out.append("")
        out.extend(sweep_text(result["sweep"], p["wake_on_data"]))
    if "compare" in result:
        out.append("")
        out.extend(compare_text(result["compare"], show_hist))
    return "\n".join(out)


def sweep_text(rows: list[dict], wake_mode: str) -> list[str]:
    out = ["sweep over peripheral latency (wake-on-data=%s; same key presses for every row)" % wake_mode,
           "  %7s %11s %8s %8s %8s %12s %12s" % ("latency", "worst(L+1)I", "median", "p95", "max",
                                                  "idle wake/s", "sim wake/s")]
    for r in rows:
        out.append("  %7d %11s %8s %8s %8s %12.2f %12.2f" % (
            r["latency"], fmt_ms(r["worst_case_ms"]), fmt_ms(r["median"]), fmt_ms(r["p95"]),
            fmt_ms(r["max"]), r["idle_wakes_per_s"], r["sim_wakes_per_s"]))
    return out


def compare_text(c: dict, show_hist: bool = False) -> list[str]:
    out = ["model vs measured: %s" % CROSS_STAGE]
    if c.get("measured") is None:
        out.append("  " + c["message"])
        return out
    out.append(TABLE_HEADER)
    out.append(table_row("measured (analyze_latency.py)", c["measured"]))
    out.append(table_row("model", c["model"]))
    dl = c["delta_model_minus_measured"]
    out.append("  %-58s %6s %8s %8s %8s %8s" % ("model - measured", "", fmt_ms(dl["min"]),
                                                 fmt_ms(dl["median"]), fmt_ms(dl["p95"]), fmt_ms(dl["max"])))
    if show_hist and c.get("measured_samples_ms"):
        out.append("  measured histogram:")
        out.extend(histogram(c["measured_samples_ms"]))
    for b in c["ble"]:
        out.append("  BLE parameters in the log [%s] %s: interval %d (%.2f ms) latency %d%s" % (
            b["label"], b["kind"], b["interval"], b["interval"] * CONN_INTERVAL_UNIT_MS, b["latency"],
            (" timeout %d" % b["timeout"]) if b.get("timeout") is not None else ""))
    if c["ble"]:
        out.append("  (re-run with --interval-units/--latency matching the log if they differ from the model)")
    return out


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                formatter_class=argparse.RawDescriptionHelpFormatter,
                                epilog=__doc__.split("\n\n", 1)[1])
    p.add_argument("--interval-units", type=int, default=6,
                   help="connection interval in 1.25 ms units (CONFIG_ZMK_SPLIT_BLE_PREF_INT, default 6 = 7.5 ms)")
    p.add_argument("--latency", type=int, default=30,
                   help="peripheral latency in connection events (CONFIG_ZMK_SPLIT_BLE_PREF_LATENCY, default 30)")
    p.add_argument("--timeout-units", type=int, default=400,
                   help="supervision timeout in 10 ms units (CONFIG_ZMK_SPLIT_BLE_PREF_TIMEOUT, default 400 = 4 s)")
    p.add_argument("--events", type=int, default=200, help="number of key events to simulate (default 200)")
    p.add_argument("--seed", type=int, default=1, help="random seed (default 1)")
    p.add_argument("--key-gap-ms", type=Dist.parse, default="uniform:50,2000", metavar="DIST",
                   help="gap between key presses: 'uniform:LO,HI' (default uniform:50,2000), 'fixed:X', "
                        "'list:a,b,c' (cycled); a bare 'X' means fixed, 'LO,HI' means uniform")
    p.add_argument("--wake-on-data", choices=WAKE_MODES, default="none",
                   help="none: the peripheral sleeps through its latency; immediate: the controller cancels "
                        "latency when data is queued (default none)")
    p.add_argument("--scan-ms", type=Dist.parse, default="uniform:3,4", metavar="DIST",
                   help="peripheral debounce + scan, key press -> position event (default uniform:3,4)")
    p.add_argument("--queue-ms", type=Dist.parse, default="uniform:0.2,0.6", metavar="DIST",
                   help="ZMK msgq + work queue + bt_gatt_notify + HCI, position -> data queued in the "
                        "controller (default uniform:0.2,0.6)")
    p.add_argument("--air-ms", type=float, default=0.3,
                   help="connection-event anchor -> notification received by the central (default 0.3)")
    p.add_argument("--central-ms", type=Dist.parse, default="uniform:0.2,0.6", metavar="DIST",
                   help="central notify callback -> work queue -> keymap (default uniform:0.2,0.6)")
    p.add_argument("--prepare-ms", type=float, default=0.5,
                   help="data queued less than this before an anchor misses that event (default 0.5)")
    p.add_argument("--loss-prob", type=float, default=0.0,
                   help="probability that a notification is not acknowledged in its event and is "
                        "retransmitted at the next attended event (default 0)")
    p.add_argument("--sweep", nargs="?", const=DEFAULT_SWEEP, metavar="L1,L2,...",
                   help="also run every listed latency (default list %s) and print worst case, median, "
                        "p95, max and radio wake rate for each" % DEFAULT_SWEEP)
    p.add_argument("--compare", metavar="JSON",
                   help="JSON written by scripts/log/analyze_latency.py --json; prints the measured "
                        "'%s' stage next to the model" % MEASURED_STAGE)
    p.add_argument("--json", metavar="FILE", help="write the full result as JSON ('-' for stdout)")
    p.add_argument("--no-samples", action="store_true", help="omit per-event samples from the JSON")
    p.add_argument("--hist", action="store_true", help="print a small histogram per stage")
    p.add_argument("--verbose", action="store_true", help="print every simulated key event")
    return p


def params_from_args(args: argparse.Namespace) -> Params:
    return Params(interval_units=args.interval_units, latency=args.latency, timeout_units=args.timeout_units,
                  events=args.events, seed=args.seed, key_gap=args.key_gap_ms, wake_on_data=args.wake_on_data,
                  scan=args.scan_ms, queue=args.queue_ms, air_ms=args.air_ms, central=args.central_ms,
                  prepare_ms=args.prepare_ms, loss_prob=args.loss_prob)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        p = params_from_args(args)
        result = simulate(p)
        if args.sweep is not None:
            result["sweep"] = run_sweep(p, parse_int_list(args.sweep))
        if args.compare:
            result["compare"] = compare(result, load_measured(args.compare))
    except (ValueError, OSError, argparse.ArgumentTypeError) as e:
        print("error: %s" % e, file=sys.stderr)
        return 2
    text = report_text(result, args.hist, args.verbose)
    if args.no_samples:
        for s in result["stages"].values():
            s.pop("samples", None)
        if "compare" in result:
            result["compare"].pop("measured_samples_ms", None)
    if args.json == "-":
        print(json.dumps(result, indent=2))
    else:
        print(text)
        if args.json:
            with open(args.json, "w", encoding="utf-8") as fh:
                json.dump(result, fh, indent=2)
            print("json written to %s" % args.json)
    return 0


if __name__ == "__main__":
    sys.exit(main())
