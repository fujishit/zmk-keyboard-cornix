# Split BLE latency simulation (BabbleSim)

A three-device simulation of the Cornix in use:

| bsim `-d=` | device | what it runs |
|---|---|---|
| 0 | right half | full ZMK, split BLE **peripheral**, scripted key events |
| 1 | left half | full ZMK, split BLE **central** *and* HID peripheral |
| 2 | the computer | `tests/bsim/host`, a plain Zephyr BLE central |

Each process runs the real Zephyr BLE host *and* the real nRF52 link-layer
controller on Zephyr's `nrf52_bsim` board, and they talk over
[BabbleSim](https://BabbleSim.github.io)'s simulated 2.4 GHz physical layer.
No hardware, no root.

Device 2 exists so that the left half has to schedule **two** connections at
once, exactly as it does on hardware: it is the split central of the right
half *and* a HID peripheral of the computer. `scripts/bsim/run.sh --no-host`
drops it and gives back the original two-device simulation.

All processes are driven by the same simulated clock, so their log timestamps
are on one timeline: the delay from "the peripheral scanned a key" to "the
central's keymap acted on it" and on to "the computer received the HID report"
can be measured directly, without the host-clock alignment that
`scripts/log/analyze_latency.py` has to do for real captures.

This is layer 4 of the testing story in [TESTING.md](../../TESTING.md): the
one that can see BLE and split behaviour, which `tests/sim` (native_sim)
cannot.

## Quick start

```sh
scripts/sim/bootstrap.sh          # once: .sim/venv + .sim/ws (shared with tests/sim)
scripts/bsim/bootstrap.sh         # once: 32-bit toolchain bits + BabbleSim, ~1 min
scripts/bsim/run.sh               # build all three roles, run both variants, print the table
scripts/bsim/run.sh --no-build    # re-run the existing executables, ~8 s
scripts/bsim/run.sh --no-build --no-host   # the two-device simulation, ~7 s
python3 -m unittest discover -s tests/bsim -v         # the regression test
BSIM_TEST_NO_BUILD=1 python3 -m unittest discover -s tests/bsim -v   # fast
```

`scripts/bsim/run.sh --help` lists every option (`--latency`, `--sim-length`,
`--seed`, `--per`, `--host` / `--no-host`, `--host-interval`,
`--host-latency`, `--host-timeout`, `--host-accept-updates`,
`--central-pref-latency`, `--scenario`, `--log-dir`, `--json`, `--no-build`,
`--no-measure`).

## Layout

```
tests/bsim/
  split-latency/
    _common/cornix_bsim.dtsi         mock kscan (4x14) + the board's layout_50 / default_transform
    _common/cornix_bsim_keymap.dtsi  the above + boards/jzf/cornix/cornix.keymap
    _common/bsim.conf                what all three cases share (split + BLE, no USB/battery/
                                     LEDs/display/sleep, immediate logging); run.sh passes it
                                     to every ZMK image as -DEXTRA_CONF_FILE
    peripheral/nrf52_bsim.keymap     scripted key events (20 press/release pairs, irregular gaps)
    peripheral/nrf52_bsim.conf       ROLE_CENTRAL=n
    peripheral-fast/                 the same, with the fast-typing burst script (run.sh --scenario fast)
    central/nrf52_bsim.keymap        same matrix and keymap, no local events
    central/nrf52_bsim.conf          ROLE_CENTRAL=y + the advertised keyboard name
  test_split_latency.py              unittest wrapper around scripts/bsim/run.sh
scripts/bsim/
  bootstrap.sh                       fetches and builds BabbleSim (and the 32-bit bits it needs)
  run.sh                             builds the two roles, runs the phy + both devices
  measure.py                         parses the logs, prints the latency table (stdlib only)
```

Build output and logs land in `.build/bsim/` (`logs/peripheral-<lat>.log`,
`logs/central-<lat>.log`, `logs/phy-<lat>.log`), which is git-ignored.

The peripheral's script starts with a warm-up press on key position 47 and
only then begins the 20 measured presses at about t = 8.1 s, so the split link
has plenty of time to advertise, connect, discover, pair and subscribe first
(it normally takes 0.5–4 s of simulated time). `measure.py` excludes that
warm-up position and anything before the central's `[SUBSCRIBED]` line.

## The simulated host computer (device 2)

`tests/bsim/host` is a small plain-Zephyr BLE central, derived from
`zephyr/samples/bluetooth/central_hr` but pointed at the HID service. It does
what a Mac does with a ZMK keyboard:

1. **active scan**, and connect only to an advertiser that carries *both* the
   HID service UUID `0x1812` *and* a name containing the filter (default
   `"Cornix"`). The ZMK central advertises name + `0x1812` + `0x180F`
   (`zmk/app/src/ble.c`, `zmk_ble_ad[]`); the split peripheral advertises the
   128-bit split service UUID and no name
   (`zmk/app/src/split/bluetooth/peripheral.c`), so it is never picked up.
2. **pair, just works.** ZMK's HID characteristics are
   `BT_GATT_PERM_READ_ENCRYPT` / `BT_GATT_PERM_WRITE_ENCRYPT`
   (`zmk/app/src/hog.c`), so the CCC cannot even be written on a plain link;
   the host calls `bt_conn_set_security(conn, BT_SECURITY_L2)` and discovers
   from `security_changed()`. Both sides have no IO capabilities, so the MITM
   bit is cleared and `auth_pairing_accept()` on the ZMK side lets it through
   because the profile is open (no settings backend on `ARCH_POSIX`).
3. **discover and subscribe**: primary `0x1812`, then every `0x2A4D` report
   characteristic that has the notify property, then each one's `0x2902` CCC.
   With the Cornix keymap that is 2 characteristics (keyboard + consumer).
4. **log every notification** with the simulated timestamp
   (`[HOST HID REPORT] n=.. handle=.. length=.. data=..`), which is what
   `measure.py` uses for the "peripheral key -> host HID report" column.

Connection parameters are the interesting knob, because they decide how the
two links compete for radio time. They default to a macOS-like 15 ms / latency
0 / 4 s timeout and are overridable per run, from `run.sh` or directly:

```sh
scripts/bsim/run.sh --host-interval 9 --host-latency 0 --host-timeout 400
.build/bsim/host/zephyr/zephyr.exe -s=sim -d=2 -host_interval=24 -host_latency=4
```

It is built with plain `west build -s tests/bsim/host -b nrf52_bsim`, with one
wrinkle: it is *not* a ZMK application, so the ZMK modules of the shared
`.sim/ws` workspace have to be kept out of the build (several of them, e.g.
`zmk-dongle-display`, have `Kconfig.defconfig` files that only parse when
ZMK's own Kconfig tree is present, and Zephyr turns Kconfig warnings into
errors). `run.sh`'s `build_host()` therefore passes an explicit
`-DZEPHYR_MODULES=` list: everything `west list` reports minus `zmk*`, plus
`$BSIM_OUT_PATH/nrf_hw_models`.

## Results

Peripheral key event -> central keymap, **simulated** time, 40 key events per
run, ideal radio, `CONFIG_ZMK_SPLIT_BLE_PREF_INT=6` (7.5 ms interval).

### Two devices (`--no-host`)

| PREF_LATENCY | min | median | p95 | max |
|---|---|---|---|---|
| 30 (ZMK default) | 0.7 - 2.8 ms | 5.74 - 6.29 ms | 9.02 - 10.24 ms | 9.7 - 17.0 ms |
| 0 | 0.3 - 1.1 ms | 3.86 - 4.42 ms | 7.02 - 8.26 ms | 7.7 - 15.3 ms |
| difference | | **+1.56 - +2.24 ms** | **+1.29 - +2.64 ms** | noisy, both signs |

(ranges over BabbleSim seeds 7, 11, 23, 57, 101, 199, 333, 5000; one sample
run in full:)

```
peripheral key -> central keymap, simulated time (ms)
  PREF_LATENCY    n      min   median      p95      max
            30   40     2.34     5.74     9.02    15.18
             0   40     0.44     3.86     7.68     8.17
         delta         +1.90    +1.88    +1.34    +7.02   (30 minus 0)
```

The on-device stages (`peripheral scan -> BLE transport`, `central notify ->
keymap`) are always 0.00 ms: on the native simulator, computation costs no
simulated time, only radio events and timers advance the clock. All of the
delay is the radio link.

### Three devices: the matrix

Ranges over BabbleSim seeds (11, 23, and 101 where shown); 40 key events per
run for the split column, 36 for the host column (only `&kp` bindings produce
a HID report; the script also hits one `&mo` and one `&trans` position).
"host" is `key on the right half -> HID report received by the computer`.

| host link | PREF_LATENCY | split median | split p95 | split max | host median | host p95 | host max |
|---|---|---|---|---|---|---|---|
| absent | 30 | 5.7 - 6.3 | 9.0 - 9.7 | 15.2 - 17.0 | - | - | - |
| absent | 0 | 3.9 - 4.1 | 7.0 - 8.3 | 8.2 - 11.9 | - | - | - |
| 15 ms / lat 0 -> 15 ms / lat 30 (macOS-like, default) | 30 | 5.5 - 6.2 | 9.2 - 14.7 | 9.6 - 15.3 | 11.9 - 15.9 | 17.8 - 23.7 | **18.0 - 24.7** |
| 15 ms / lat 0 -> 15 ms / lat 30 | 0 | 4.6 - 4.9 | 10.8 - 14.7 | 11.8 - 15.3 | 12.4 - 15.5 | 19.3 - 21.5 | 19.6 - 21.8 |
| 11.25 ms / lat 0 -> 15 ms / lat 30 | 30 | 6.2 - 7.5 | 9.4 - 14.3 | 9.7 - 14.7 | 16.1 - 16.2 | 22.0 - 22.1 | 22.2 - 22.4 |
| 11.25 ms / lat 0 -> 15 ms / lat 30 | 0 | 4.0 - 4.6 | 7.0 - 11.4 | 7.3 - 11.9 | 11.7 - 15.5 | 17.0 - 20.6 | 17.4 - 20.9 |
| 30 ms / lat 0 -> 15 ms / lat 30 | 30 | 6.2 - 7.5 | 9.4 - 14.3 | 9.6 - 14.7 | 16.4 | 22.2 - 22.3 | 22.4 - 22.5 |
| 30 ms / lat 0 -> 15 ms / lat 30 | 0 | 3.8 - 4.2 | 6.8 - 11.2 | 7.2 - 11.9 | 11.7 - 15.1 | 17.0 - 20.6 | 17.4 - 20.9 |
| 15 ms / lat 30 from the start | 30 | 5.7 - 6.2 | 9.2 - 14.7 | 9.6 - 15.3 | 11.9 - 15.9 | 17.8 - 21.8 | 18.0 - 22.4 |
| 15 ms / lat 30 from the start | 0 | 4.4 - 4.9 | 10.7 - 14.7 | 11.6 - 15.3 | 12.4 - 15.1 | 20.6 - 21.5 | 20.9 - 21.8 |
| 15 ms / lat 0, update rejected (`--host-accept-updates 0`) | 30 | 6.2 - 9.3 | 9.2 - 15.3 | 9.6 - 15.9 | 11.9 - 15.9 | 17.8 - 21.8 | 18.0 - 22.4 |
| 15 ms / lat 0, update rejected | 0 | 6.6 - 7.5 | 11.9 - 15.0 | 12.2 - 15.3 | 12.4 - 15.5 | 20.6 - 21.5 | 20.9 - 21.8 |
| 15 ms / lat 0, `--central-pref-latency 0` | 30 | 6.2 - 9.3 | 9.2 - 15.3 | 9.6 - 15.9 | 11.9 - 15.9 | 17.8 - 21.8 | 18.0 - 22.4 |
| 15 ms / lat 0, `--central-pref-latency 0` | 0 | 6.6 - 7.5 | 11.9 - 15.0 | 12.2 - 15.3 | 12.4 - 15.5 | 20.6 - 21.5 | 20.9 - 21.8 |

Deliberately unrealistic stress cases, to try to force a scheduling collision:

| host link | PREF_LATENCY | split median | split max | host median | host max |
|---|---|---|---|---|---|
| 7.5 ms / lat 0 (same interval as the split link) | 30 | 5.6 - 6.2 | 9.6 | 9.9 - 11.8 | 13.2 - 15.8 |
| 7.5 ms / lat 0 | 0 | 3.7 - 7.0 | 7.7 - 14.7 | 9.8 - 14.8 | 13.9 - 23.3 |
| 8.75 ms / lat 0 (slow beat against 7.5 ms) | 30 | 5.9 - 6.4 | 15.4 - 16.4 | 11.5 - 13.5 | 25.2 - 26.3 |
| 8.75 ms / lat 0 | 0 | 3.7 - 4.0 | 13.0 - 14.3 | 9.9 - 10.6 | 22.9 - 24.2 |
| 30 ms / lat 30 pinned (930 ms latency window) | 30 | 6.2 - 6.5 | 9.6 - 14.3 | 22.4 - 26.2 | 37.1 - 37.4 |
| 30 ms / lat 30 pinned | 0 | 3.8 - 3.9 | 7.2 - 11.2 | 16.5 - 20.1 | 32.2 - 35.9 |
| 100 ms / lat 0 pinned | 30 | 6.2 - 6.4 | 9.6 - 13.3 | 52.8 - 61.9 | 105.2 - 106.5 |
| 100 ms / lat 0 pinned | 0 | 3.8 | 7.2 | 59.2 - 60.5 | 101.0 - 105.8 |
| 15 ms / lat 0, 5 % packet error rate | 30 | 6.4 - 7.4 | 10.7 - 24.8 | 12.5 - 16.7 | 32.3 - 42.2 |
| 15 ms / lat 0, 5 % packet error rate | 0 | 5.3 - 6.0 | 22.6 - 33.1 | 16.0 - 17.7 | 42.2 - 49.3 |

Reproduce any row with, e.g.:

```sh
scripts/bsim/run.sh --no-build --seed 23 --host-interval 24 --host-latency 30 \
                    --host-accept-updates 0
```

### Fast typing (`--scenario fast`)

`tests/bsim/split-latency/peripheral-fast/nrf52_bsim.keymap` replays what a
fast typist does on the right half, all on `&kp` positions: 36 presses with
2-key rollover (the next key goes down 15 ms before the previous one comes
up, presses 35-60 ms apart, i.e. 17-28 keys/s), a 4-key chord pressed and
released within 3 ms (all four changes inside one 7.5 ms connection interval),
and six taps 35 ms apart -- 92 measured events in 2.5 s. It exists because on
hardware a key is occasionally lost on the split link
(`scripts/log/FAST_TYPING.md`), and the question was whether a burst alone
can overflow the peripheral's 10-deep notify queue
(`CONFIG_ZMK_SPLIT_BLE_PERIPHERAL_POSITION_QUEUE_SIZE`; overflow logs
`Position state message queue full` and discards the oldest state) or its 3
ATT TX buffers (`CONFIG_BT_ATT_TX_COUNT`).

```sh
scripts/bsim/run.sh --scenario fast                      # builds peripheral-fast, runs with the host
scripts/bsim/run.sh --no-build --scenario fast --per 0.1 --seed 23 --host-interval 9 --host-latency 30
BSIM_TEST_NO_BUILD=1 python3 -m unittest tests.bsim.test_split_latency.FastTypingTest -v
```

Results on 2026-09-22, seeds 7/23/101/199, `PREF_LATENCY` 0 and 30 pooled
(the slow script's rows are the same seeds, for comparison). "lost" counts
peripheral key events that never reached the central keymap; `measure.py`
now prints it together with the peripheral's `queue full` count.

| scenario | host link | PER | split p50 | split p95 | split p99 | split max | lost | queue full | host p50 | host max |
|---|---|---|---|---|---|---|---|---|---|---|
| slow | none | 0 | 4.0-6.3 | 7.0-10.2 | 10.6-17.0 | 10.6-17.0 | 0 / 320 | 0 | - | - |
| fast | none | 0 | 3.9-5.6 | 7.2-10.0 | 10.8-14.2 | 12.1-15.1 | 0 / 736 | 0 | - | - |
| slow | 15 ms / lat 0 -> 30 | 0 | 3.7-6.3 | 7.4-11.4 | 7.7-11.9 | 7.7-11.9 | 0 / 320 | 0 | 11.9-16.6 | 18.0-26.8 |
| fast | 15 ms / lat 0 -> 30 | 0 | 3.8-7.0 | 7.1-14.4 | 7.6-15.2 | 7.7-15.3 | 0 / 736 | 0 | 9.3-17.9 | 18.3-26.7 |
| slow | 11.25 ms / lat 30 | 0 | 3.7-6.8 | 6.7-14.3 | 7.7-14.7 | 7.7-14.7 | 0 / 320 | 0 | 11.0-18.3 | 17.2-24.4 |
| fast | 11.25 ms / lat 30 | 0 | 3.7-6.8 | 7.1-14.4 | 7.5-15.2 | 7.7-15.2 | 0 / 736 | 0 | 11.6-17.7 | 17.5-33.6 |
| slow | none | 0.1 | 4.1-7.0 | 13.6-21.6 | 21.3-35.9 | 21.3-35.9 | 0 / 320 | 0 | - | - |
| fast | none | 0.1 | 4.9-7.0 | 13.3-17.0 | 21.7-23.6 | 21.1-27.9 | 0 / 736 | 0 | - | - |
| slow | 15 ms / lat 0 -> 30 | 0.1 | 4.0-8.3 | 10.6-32.1 | 14.1-49.4 | 14.1-49.4 | 0 / 320 | 0 | 12.1-20.7 | 26.1-65.6 |
| fast | 15 ms / lat 0 -> 30 | 0.1 | 5.0-11.0 | 12.1-29.5 | 16.9-39.1 | 21.2-44.9 | 0 / 736 | 0 | 14.6-23.8 | 47.0-69.0 |
| fast | 15 ms / lat 0 -> 30 | 0.3 (seed 11, lat 0) | 10.2 | 39.0 | 46.4 | 48.6 | 0 / 92 | 0 | 64.4 | 520.1 |
| fast | 15 ms / lat 0 -> 30 | 0.5 (seed 11, lat 0) | 936 | 1193 | 1241 | 1241 | **33 / 83** | 0 | - | - |

Reading it:

* A burst costs nothing extra on an ideal or mildly lossy link: the fast
  rows sit on top of the slow rows at every percentile, the 4-key chord's
  four notifications go out in the same connection event (3.0-4.7 ms each),
  and no event is ever lost. With 3 TX buffers and a 7.5 ms interval the
  link carries far more than any typist produces (well over 100 position
  states per second), so throughput is not the mechanism.
* The second host link (15 ms or 11.25 ms, latency 30) adds at most one
  skipped split connection event (+7.5 ms at the p95); `SCHED_ADVANCED=y`
  on the central places the two connections apart.
* Loss appears only when the link itself fails: at PER 0.3 nothing is lost
  but the tail reaches 49 ms (split) / 520 ms (host); at PER 0.5 the link
  drops (`Disconnected ... reason 0x08` at 4.6 s) and every notification
  queued while it is down fails with `Error notifying -107` (ENOTCONN) --
  33 of 83 key events never arrive. That is one of the two loss paths seen
  on hardware; the other, `Position state message queue full` (the notify
  thread blocked in `bt_gatt_notify()` for lack of an ATT buffer while the
  peer stops acknowledging), needs a link that stalls *without*
  disconnecting for a second or more, which the uniform-loss modem does not
  produce.

`FastTypingTest` asserts the ideal-link behaviour (every event delivered,
no `queue full`, p95 < 40 ms, p99 < 45 ms); a failure there means the
simulation has started losing keys in a burst, which is a finding.

## The simulation still does not reproduce the hardware symptom

On real hardware the right half was badly laggy with the ZMK default
`CONFIG_ZMK_SPLIT_BLE_PREF_LATENCY=30` (with a 7.5 ms interval that permits
skipping up to 31 x 7.5 = **232.5 ms** of connection events) and setting it to
0 on the central fixed it.

Adding the third device **did not** close that gap. The worst
peripheral-key -> computer delay anywhere in the matrix above with plausible
host parameters is **24.7 ms**, and the worst split-link delay is **17.0 ms**.
The only way to get past 100 ms was to pin the host link to a 100 ms
connection interval, and then the delay is simply the interval, not a latency
window: it is the same for `PREF_LATENCY` 30 and 0.

The reason is in Zephyr's link layer, and it is worth knowing before blaming
the simulation. Line numbers are for the Zephyr 4.1 tree in `.sim/ws/zephyr`:

* `subsys/bluetooth/controller/ll_sw/ull_conn.c:265-267`,
  at the end of `ll_tx_mem_enqueue()`:

  ```c
  if (IS_ENABLED(CONFIG_BT_PERIPHERAL) && conn->lll.role) {
          ull_periph_latency_cancel(conn, handle);
  }
  ```

  i.e. the moment a peripheral's *host* queues a PDU, the controller breaks
  peripheral latency. This fires on the right half when it queues the split
  position notification, **and on the left half when it queues the HID
  report** for the computer, because on that link the left half is the
  peripheral. It is called from seven places in `ull_conn.c` (lines 267, 320,
  428, 517, 541, 592, 681), i.e. from essentially every path that has
  something to send or acknowledge.
* `subsys/bluetooth/controller/ll_sw/ull_peripheral.c:502-520`,
  `ull_periph_latency_cancel()` does it by `ticker_update()`-ing the
  connection ticker so the next connection event happens at the next anchor
  point instead of after the latency window.
* `subsys/bluetooth/controller/ll_sw/ull_conn.c:1090-1100`, in
  `ull_conn_done()`: `lll->latency_event` is set back to `0` for as long as
  anything is still queued or unacknowledged, and only re-armed to
  `lll->latency` once both the tx queue and the LLL memq are empty. So even a
  lost packet does not cost a full latency window.

The residual cost of `PREF_LATENCY=30` is the cost of that re-arming: about
+1.6 to +2.2 ms at the median, roughly a quarter of a connection interval on
average plus the odd event where the cancel lands too late for the next
anchor. That is visible in every row of the matrix.

The pre-emption hypothesis (`ull_conn_done()` not re-evaluating latency for an
event with `done->extra.trx_cnt == 0`, `ull_conn.c:1074` and `1106-1112`) was
the reason for building the third device. It does not bite here: even when the
host link is pinned to exactly the split link's 7.5 ms interval, or to 8.75 ms
so that the two anchor points beat slowly against each other, the split-link
p95 stays at 9 - 16 ms. `CONFIG_BT_CTLR_SCHED_ADVANCED=y`
(`ull_sched.c`'s `ull_sched_after_cen_slot_get()`) places the central-role
split connection in the gaps, and `ull_conn_done()`'s `trx_cnt == 0` branch
only skips the drift/latency re-evaluation for that one event -- the queued
PDU still holds `latency_event` at 0 through the next one.

One thing the third device *did* reveal, which the two-device run could not:

* **ZMK asks the computer for peripheral latency 30 as well.**
  `zmk/app/Kconfig:239-240` sets `CONFIG_BT_PERIPHERAL_PREF_LATENCY=30`
  (with `BT_PERIPHERAL_PREF_MIN_INT=6` / `MAX_INT=12`), independently of
  `CONFIG_ZMK_SPLIT_BLE_PREF_LATENCY`. With `CONFIG_BT_GAP_AUTO_UPDATE_CONN_PARAMS=y`
  the left half sends an update request about 5 s after connecting, and a
  macOS-like host accepts it, so the keyboard -> computer link ends up at
  15 ms / latency 30, i.e. a **465 ms** worst case on paper:

  ```
  [   5.430 s] host link   interval 6-12 latency 30 timeout 400 (4000 ms)   [host: keyboard's update request]
  [   5.533 s] host link   interval 12 (15.00 ms) latency 30 timeout 400 (4000 ms)  -> may skip up to 465.0 ms
  ```

  In the simulation it costs nothing, for the same
  `ull_periph_latency_cancel()` reason, and `--central-pref-latency 0` (which
  rebuilds the central with `CONFIG_BT_PERIPHERAL_PREF_LATENCY=0`) measures
  the same to within noise. But it is a second, independent 30-event latency
  window on the path a keystroke actually takes, and it is *not* covered by
  the `CONFIG_ZMK_SPLIT_BLE_PREF_LATENCY=0` fix.

Experiments that did **not** close the gap, in summary:

* the third device with macOS-like parameters (15 ms / latency 0), and with
  11.25 ms and 30 ms intervals, latency 30 variants, and with the keyboard's
  own update request rejected;
* `CONFIG_BT_PERIPHERAL_PREF_LATENCY=0` on the central;
* host link pinned to 7.5 ms, 8.75 ms, 30 ms and 100 ms;
* 5 % and 15 % packet error rate on the simulated radio
  (`scripts/bsim/run.sh --per 0.05`, which switches the phy to the
  `BLE_simple` modem): both variants degrade about equally;
* longer idle gaps between presses (the script already uses gaps up to
  1150 ms, far more than the 232 ms latency window);
* seeds 11, 23, 101 (and 7, 57, 199, 333, 5000 for the two-device case).

### What still differs from hardware

The controller configuration does not: diffing
`.build/debug/cornix_left_lat30/zephyr/.config` against
`.build/bsim/central-30/zephyr/.config` over `CONFIG_BT_CTLR_*` shows exactly
one difference, `TX_PWR_PLUS_8` vs `TX_PWR_0`, and the right half's two
configs are identical. `SCHED_ADVANCED`, `XTAL_ADVANCED`, `CENTRAL_SPACING=0`,
`LLL_PRIO`/`ULL_*_PRIO` and the absence of `BT_CTLR_LOW_LAT` all match. So the
remaining candidates are outside the link-layer configuration:

* **the real host is macOS, not a Zephyr central.** A Mac re-negotiates
  connection parameters on its own schedule, suspends and resumes links, and
  interleaves classic Bluetooth and Wi-Fi on the same radio.
* **the RF environment.** BabbleSim's default "magic" modem is ideal; `--per`
  models uniform random packet loss, not the bursty, direction-asymmetric loss
  of a 2.4 GHz band shared with Wi-Fi, nor a body between the two halves.
* **transmit power asymmetry.** The right half transmits at 0 dBm while the
  left half sets `CONFIG_BT_CTLR_TX_PWR_PLUS_8` (see scripts/log/README.md
  section 4), so on hardware the right-to-left direction is the weak one.
  BabbleSim's ideal modem ignores power entirely.
* **clock accuracy.** The simulation gives both devices a perfect clock;
  real nRF52 crystals drift, and drift is what makes a peripheral widen its
  receive window after skipping events.
* **no persistent bonds.** On `ARCH_POSIX` ZMK's `imply BT_SETTINGS` is off,
  so every run pairs fresh. On hardware the halves reconnect to a stored bond,
  which takes a different code path in `ull_peripheral.c` / `ull_central.c`.
* **no real key-repeat load.** The scripted 20 presses are one key at a time;
  real typing overlaps presses and releases and fills the tx queue
  differently, which is exactly what `ull_conn_done()`'s re-arming depends on.

What the simulation *does* establish, reproducibly and without hardware, is
that `PREF_LATENCY=30` is not free even on a perfect link, that
`PREF_LATENCY=0` is faster at almost every percentile, and that adding the
computer's connection does not by itself turn that into hundreds of
milliseconds. That is what `test_split_latency.py` asserts; it deliberately
does not assert a several-hundred-millisecond delay that the simulation cannot
produce. If any of the "does not stall" assertions ever starts failing, the
simulation has begun reproducing the hardware symptom -- that is a finding,
not a bug.

## How it is built, and the workarounds

`scripts/bsim/bootstrap.sh` records all of this; the short version:

1. **BabbleSim sources.** Zephyr's manifest has them in the (inactive)
   `babblesim` group; the bootstrap clones the same repositories at the same
   revisions into `.sim/bsim/components/` rather than switching the shared
   `.sim/ws` west workspace's group filter, plus
   `zephyrproject-rtos/nrf_hw_models` into `.sim/bsim/nrf_hw_models`, which is
   passed to the build as an extra Zephyr module. `BSIM_OUT_PATH` and
   `BSIM_COMPONENTS_PATH` point at `.sim/bsim` and `.sim/bsim/components`.
2. **GNU make.** There is no system `make`; the one built by
   `scripts/sim/bootstrap.sh` into `.sim/tools/bin` is used.
3. **No `libfftw3` needed.** The BabbleSim docs mention it, but of the
   components used here none actually link it (only a `clean` rule in
   `components/common/Makefile` mentions the name).
4. **32-bit x86 without root.** `zephyr/boards/native/nrf_bsim/CMakeLists.txt`
   links `libUtilv1.32.a`, `libPhyComv1.32.a`, `lib2G4PhyComv1.32.a` and
   `libRandv2.32.a`, so `nrf52_bsim` is 32-bit only, and this host has no gcc
   multilib (`gcc -m32` fails with "cannot find Scrt1.o"). The bootstrap
   downloads `libc6-i386`, `libc6-dev-i386`, `lib32gcc-s1` and
   `lib32gcc-<gcc major>-dev` with `apt-get download` (which needs no root) and
   unpacks them with `dpkg-deb -x` into `.sim/tools/i386-multilib`, then
   generates `.sim/tools/bsim-bin/gcc`, a wrapper that adds the right `-B`,
   `-L`, `-idirafter`, `-Wl,--sysroot`, `-Wl,--dynamic-linker` and `-rpath`
   options whenever `-m32` is on the command line and otherwise execs the real
   gcc untouched. `scripts/bsim/run.sh` puts that directory first on `PATH`;
   `scripts/sim/run.sh` is unaffected because it only adds `.sim/tools/bin`.
   Two details worth remembering:
   * `gcc -m32` searches `/usr/include/i386-linux-gnu`, which does not exist on
     a Debian amd64 host; the bi-arch headers are in
     `/usr/include/x86_64-linux-gnu` and only `gnu/stubs-32.h` comes from
     `libc6-dev-i386`. The wrapper adds both directories with `-idirafter`.
   * glibc `dlopen()`s `libgcc_s.so.1` for `pthread_exit()`, and a
     `DT_RUNPATH` on the executable does not cover `dlopen`, so `run.sh` also
     exports `LD_LIBRARY_PATH`. (The wrapper additionally passes
     `-Wl,--disable-new-dtags` so freshly built executables carry a real
     `DT_RPATH`.)
5. **No settings backend is needed.** ZMK's `app/Kconfig` has
   `imply BT_SETTINGS if !ARCH_POSIX` / `imply SETTINGS if !ARCH_POSIX`, so on
   `nrf52_bsim` (POSIX arch) settings, bonding persistence and battery
   reporting are off by themselves. The two halves pair fresh at every boot,
   which is exactly what this test wants. The board's `storage_partition`
   would work (the NVMC is modelled) if persistence were ever needed.
6. **No USB, ext-power, RGB or display.** `nrf52_bsim.dts` deletes `usbd`;
   the role `.conf` files turn all of it off explicitly so no nRF-only driver
   is pulled in.

7. **The host application is not a ZMK build.** `tests/bsim/host` is plain
   Zephyr, so `run.sh`'s `build_host()` cannot reuse the ZMK `build()` helper:
   it passes an explicit `-DZEPHYR_MODULES=` list (everything `west list`
   reports except `zmk*`, plus `$BSIM_OUT_PATH/nrf_hw_models`) so that the ZMK
   modules' Kconfig fragments are not parsed without ZMK's Kconfig tree. Its
   run-time options are registered with `bs_add_extra_dynargs()` from a
   `NATIVE_TASK(..., PRE_BOOT_1, 100)`, because the BabbleSim command line is
   parsed before the Zephyr kernel starts. BabbleSim resets every registered
   option to a "not given" marker before parsing, so the Kconfig defaults are
   applied afterwards in `apply_arg_defaults()` rather than as C initialisers.

Build and run cost on a 16-core host: BabbleSim `make everything -j16` about
50 s, each ZMK image 12-20 s, the host image about 10 s, and a
25-simulated-second run of both variants about 7 s of wall time with two
devices and about 8 s with three.
