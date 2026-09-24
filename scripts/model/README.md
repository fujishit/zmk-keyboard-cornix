# Split-link latency model

`split_latency_model.py` is a small discrete-event model of the path a key
press takes from the **right half** (BLE split *peripheral*) to the keymap on
the **left half** (split *central*). Python standard library only, like
everything else in `scripts/`.

It exists for three reasons:

1. **Teaching** — to show, with numbers instead of hand-waving, why
   `CONFIG_ZMK_SPLIT_BLE_PREF_LATENCY=30` can make the right half feel laggy
   and what lowering it costs in battery.
2. **Picking a value** — `--sweep` prints delay *and* radio wake rate for a
   list of latencies, so a value between 0 and 30 can be chosen on evidence.
3. **Regression / validation** — `--compare` puts the model next to a real
   capture analysed by [`scripts/log/analyze_latency.py`](../log/analyze_latency.py),
   in the same table format, so the two can be read side by side.

```sh
python3 scripts/model/split_latency_model.py                       # ZMK defaults
python3 scripts/model/split_latency_model.py --sweep               # pick a latency
python3 scripts/model/split_latency_model.py --latency 0           # the fix the user applied
python3 scripts/model/split_latency_model.py --wake-on-data immediate
python3 scripts/model/split_latency_model.py --compare out.json    # vs. real logs
python3 -m unittest discover -s tests/model -v                     # 41 tests
```

The default is `--wake-on-data none`, the pessimistic behaviour that matches
what this keyboard actually did. Zephyr's controller source says it should be
`immediate` — see "What the Zephyr controller actually does" below, which is
the most important section of this file.

## The stages it models

The defaults follow this repo and ZMK main:

```
key press --scan--> position --queue--> queued in the controller
          --wait--> connection event --air--> notify --central--> keymap
```

| Stage | Option | Default | Where the default comes from |
| --- | --- | --- | --- |
| debounce + matrix scan | `--scan-ms` | `uniform:3,4` | `CONFIG_ZMK_KSCAN_DEBOUNCE_PRESS_MS=3` on `cornix_right` (`boards/jzf/cornix/Kconfig.defconfig`) |
| ZMK msgq + work queue + `bt_gatt_notify` + HCI | `--queue-ms` | `uniform:0.2,0.6` | `zmk/app/src/split/bluetooth/service.c` (see below) |
| waiting for a connection event | `--interval-units`, `--latency`, `--wake-on-data` | 6, 30, `none` | `CONFIG_ZMK_SPLIT_BLE_PREF_INT` / `_PREF_LATENCY` (`zmk/app/src/split/bluetooth/Kconfig:65-75`) |
| on-air time inside the event | `--air-ms` | `0.3` | a 2-byte notification at 1M PHY, rounded up |
| central notify callback -> work queue -> keymap | `--central-ms` | `uniform:0.2,0.6` | `split_central_notify_func()` -> `peripheral_event_work_callback()` |
| guard before an anchor | `--prepare-ms` | `0.5` | data queued later than this misses that event (controller prepare) |

The reported stage **`model: peripheral position -> central keymap (aligned)`**
is deliberately named after
`cross: peripheral position -> central keymap (aligned)` in
`analyze_latency.py`, covers exactly the same span (queue + wait + air +
central) and is printed with the identical table header and `%.2f` formatting,
so the two outputs line up column for column. `--compare` automates that
comparison.

### The real ZMK send path (so the stages are not invented)

`zmk/app/src/split/peripheral.c:148` `split_peripheral_listener()` hands the
position event to the BLE transport, which calls
`zmk/app/src/split/bluetooth/service.c:230` `send_position_state()`:

* `k_msgq_put(&position_state_msgq, ..., K_MSEC(100))` — queue depth 10
  (`CONFIG_ZMK_SPLIT_BLE_PERIPHERAL_POSITION_QUEUE_SIZE`); on overflow the
  oldest entry is dropped and the log says `Position state message queue full`.
* `k_work_submit_to_queue(&service_work_q, &service_position_notify_work)`
  (`service.c:246`) — a dedicated work queue thread, priority
  `CONFIG_ZMK_SPLIT_BLE_PERIPHERAL_PRIORITY=5`.
* `send_position_state_callback()` (`service.c:217`) **drains the whole
  message queue in a `while` loop**, calling `bt_gatt_notify()`
  (`service.c:221`) once per queued position.

That last point is why the model lets a key queued while an earlier packet is
still waiting **ride the same connection event** instead of waiting for the
next one: several PDUs can go out in one connection event.

## What the Zephyr controller actually does with peripheral latency

This is the question the whole model turns on, so it was read in the pinned
Zephyr (`zmkfirmware/zephyr` `v4.1.0+zmk-fixes`, checked out in
`.sim/ws/zephyr`). **The source says the controller wakes early — i.e.
`--wake-on-data immediate`.** Three independent places:

| What | Where |
| --- | --- |
| Every ACL TX buffer handed to the controller cancels peripheral latency. `ll_tx_mem_enqueue()` ends with `if (IS_ENABLED(CONFIG_BT_PERIPHERAL) && conn->lll.role) ull_periph_latency_cancel(conn, handle);` | `zephyr/subsys/bluetooth/controller/ll_sw/ull_conn.c:217` (function), `:267` (the call) |
| `ull_periph_latency_cancel()` re-arms the connection ticker when latency is currently in effect: `if (conn->lll.latency_event && !conn->periph.latency_cancel)` then `ticker_update(..., 0, 0, 0, 0, 1, 0, ...)`. That `1` is the `lazy` argument; `ticker.c` does `user_op->params.update.lazy--` and stores it as `lazy_periodic`, so passing 1 sets the skip count to **0** — the next connection event is attended. | `zephyr/subsys/bluetooth/controller/ll_sw/ull_peripheral.c:502-520`; `zephyr/subsys/bluetooth/controller/ticker/ticker.c:1706-1730` |
| At the end of every connection event, `ull_conn_done()` reloads the skip counter **only if there is nothing to send**: `if (ull_tx_q_peek(&conn->tx_q) \|\| memq_peek(...)) { lll->latency_event = 0U; } else if (lll->periph.latency_enabled) { lll->latency_event = lll->latency; }` | `zephyr/subsys/bluetooth/controller/ll_sw/ull_conn.c:1094-1099` |

Related: latency is not even armed until the first acknowledgement
(`lll->periph.latency_enabled = 1` in
`zephyr/subsys/bluetooth/controller/ll_sw/nordic/lll/lll_conn.c:1075`, cleared
at connection setup in `ull_adv.c:1082`), and latency is also broken while a
supervision-timeout countdown is running — i.e. right after an event was
missed (`lll->latency_event = 0U;` under `if (conn->supervision_expire)`,
`zephyr/subsys/bluetooth/controller/ll_sw/ull_conn.c:1157-1162`).

**So why did `CONFIG_ZMK_SPLIT_BLE_PREF_LATENCY=0` fix the real keyboard?**
The model does not claim to know, and that is exactly why both behaviours are
selectable. Honest candidates, none of them verified here:

* `ull_periph_latency_cancel()` runs in **thread context** (`TICKER_USER_ID_THREAD`)
  and is a no-op when `conn->periph.latency_cancel` is already set or when
  `latency_event` happens to be 0 at that instant. A ticker update that lands
  after the next event has been prepared only takes effect one event later —
  and if it keeps losing that race, behaviour degrades toward `none`.
* With latency 30 the peripheral's receive window widening grows over the whole
  232.5 ms window (sleep-clock accuracy), so missed anchors and
  retransmissions become more likely — and each retransmission that has to
  wait for the *next* attended event costs a full window. `--loss-prob`
  reproduces that asymmetry.
* The central -> peripheral direction (`ZMK_SPLIT_RELAY_EVENT`, battery reads,
  LED/indicator updates) is *not* modelled at all and is genuinely bounded by
  when the peripheral listens.

Practically: run the model both ways, then use `--compare` against a real
capture to decide which one the hardware behaves like. If the measured
distribution looks like the `none` row, the early wake is not working on that
link; if it looks like `immediate`, the latency is not the cause and something
else is.

## Sweep with the default parameters

`python3 scripts/model/split_latency_model.py --events 5000 --seed 1 --sweep`
(interval 6 = 7.50 ms, key gaps `uniform:50,2000` ms — i.e. a human typing
slowly, which is the worst case for latency because the link is idle between
keys). All values in ms except the two rightmost columns.

```
sweep over peripheral latency (wake-on-data=none; same key presses for every row)
  latency worst(L+1)I   median      p95      max  idle wake/s   sim wake/s
        0        7.50     5.37     8.73     9.38       133.33       133.38
        1       15.00     9.09    15.85    16.88        66.67        66.69
        2       22.50    12.88    22.97    24.38        44.44        44.46
        4       37.50    20.36    36.99    39.33        26.67        26.68
        8       67.50    35.31    65.82    69.38        14.81        14.82
       16      127.50    64.52   122.97   129.32         7.84         7.85
       30      232.50   117.93   222.50   234.10         4.30         4.30
```

The same sweep with `--wake-on-data immediate` — delay is flat, only the
battery column moves, which is the whole point of peripheral latency:

```
sweep over peripheral latency (wake-on-data=immediate; same key presses for every row)
  latency worst(L+1)I   median      p95      max  idle wake/s   sim wake/s
        0        7.50     5.37     8.73     9.38       133.33       133.38
        1       15.00     5.37     8.73     9.38        66.67        66.93
        2       22.50     5.37     8.73     9.38        44.44        44.79
        4       37.50     5.37     8.73     9.38        26.67        27.06
        8       67.50     5.37     8.73     9.38        14.81        15.25
       16      127.50     5.37     8.73     9.38         7.84         8.31
       30      232.50     5.37     8.73     9.38         4.30         4.77
```

Reading the trade-off, if the link really behaves like `none`: latency **4**
already cuts the median from ~118 ms to ~20 ms while still waking the radio
only ~27 times a second instead of ~133 (a factor of 5 saved against latency
0). Latency **8** (median ~35 ms, p95 ~66 ms) is about the last value that
still feels like a keyboard. `wake/s` is a *proxy* for battery cost, not a
current figure: it counts connection events attended, and says nothing about
the energy per event, idle current between events, or the fact that a longer
window also means a longer receive window.

## Assumptions, and what is deliberately not modelled

Assumed:

* Connection events on a perfect grid `t0 + k*I`; `t0` is randomised per seed
  so the key presses are not phase-locked to the grid.
* `wake-on-data none`: the peripheral attends only events `0, L+1, 2(L+1), ...`
  — a hypothetical controller that sleeps through its whole allowed window.
* `wake-on-data immediate`: the peripheral attends the first event at least
  `--prepare-ms` after the data was queued, and the latency window restarts
  from each attended event.
* A key queued while an earlier packet is still waiting rides the same event.
* `--loss-prob` retransmits at the next *attended* event (one interval when
  awake, a whole window when asleep).

Not modelled, on purpose:

* **No radio**: no channel map, no frequency hopping, no window widening, no
  clock drift, no RSSI, no interference. `--loss-prob` is a crude stand-in.
  (The right half also transmits at 0 dBm — it does not set
  `CONFIG_BT_CTLR_TX_PWR_PLUS_8`, unlike the left; see the checklist in
  `scripts/log/README.md`.)
* **No controller scheduling**: no ticker, no mayfly priorities, no collision
  with the central's own host link, no LLCP control procedures (which also
  suspend latency), no connection parameter update procedure.
* **No queue back-pressure**: the 10-deep msgq, `CONFIG_BT_ATT_TX_COUNT` and
  `Position state message queue full` are not simulated. Fast typing with a
  large latency really does hit those; the model would under-report it.
* **No central -> peripheral traffic**, no battery notifications, no HID
  report or USB/BLE endpoint time on the central (the analyzer's
  `keymap -> hid` stage).
* Key presses only, releases are not modelled separately (they behave the
  same, with `_RELEASE_MS` debounce instead).

## How it relates to the other layers

| Layer | What it is | Trust |
| --- | --- | --- |
| `scripts/model/` (this) | arithmetic on a connection-event grid, no code from ZMK or Zephyr runs | lowest — it is only as good as its assumptions, but it is instant and explains *why* |
| `tests/bsim/`, `scripts/bsim/` (being added separately) | Babblesim: the real Zephyr controller and the real ZMK split code, two simulated devices over a simulated radio | high — it can actually settle the `none` vs `immediate` question |
| `scripts/log/` + real hardware | `capture.py` then `analyze_latency.py --peripheral ... --central ...` | ground truth, but noisy and needs a debug build |

The intended workflow:

```sh
# 1. capture on hardware (see scripts/log/README.md)
python3 scripts/log/analyze_latency.py --peripheral logs/right-*.log \
        --central logs/left-*.log --json measured.json

# 2. confront the model with it
python3 scripts/model/split_latency_model.py --events 2000 --compare measured.json
python3 scripts/model/split_latency_model.py --events 2000 --wake-on-data immediate \
        --compare measured.json
```

`--compare` reads the analyzer's JSON (`stages` ->
`cross: peripheral position -> central keymap (aligned)`, plus the `ble` block
so the interval/latency actually negotiated on the link are printed next to
the modelled ones) and prints measured, model and `model - measured` rows. If
the JSON has no cross stage — a single-file capture, or one without host
timestamps — it says so and lists the stages it did find.

## JSON output

`--json -` (or `--json FILE`) writes the whole result:

* `params` — every input, echoed back, distributions as `kind:values` strings.
* `derived` — `interval_ms`, `worst_case_wait_ms` = `(L+1)*I`,
  `mean_wait_no_wake_ms`, `idle_wakes_per_s`, `supervision_timeout_ms`,
  `grid_t0_ms`.
* `stages` — one entry per stage, each with `count / min / median / p95 / max /
  unit` (they are produced by `analyze_latency.py`'s own `summarize()`) and
  `samples` (suppressed by `--no-samples`), each sample carrying `ms`, `key`,
  `t_press_ms`, `event` (connection-event index) and `retries`.
* `wake` — `attended_events`, `idle_wakes`, `span_s`, `events_per_s`.
* `retransmissions`, `warnings`, and `sweep` / `compare` when those were asked
  for.

A warning is emitted when `2 * (L+1) * I` exceeds the supervision timeout,
because the BLE spec forbids those parameters and a real controller would
reject them.

## Tests

`tests/model/test_split_latency_model.py`, 41 cases, all with fixed seeds:

```sh
python3 -m unittest discover -s tests/model -v
```

They pin the invariants that matter: latency 30 + `none` reaches ~232.5 ms with
a median near half of it and **no** sample above the window plus processing
(the check that catches a broken connection-event assignment); latency 0 stays
under one interval plus processing; `immediate` produces bit-identical
distributions for every latency while the wake rate still falls; the sweep is
monotone in both directions; the JSON schema; and `--compare` against the
fixtures in `tests/model/fixtures/` — `measured_latency30.json`,
`measured_latency0.json` (a synthetic capture in the analyzer's exact schema
for each case) and `measured_no_cross.json` (the graceful-failure path).
