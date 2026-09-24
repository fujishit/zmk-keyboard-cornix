# Testing and debugging without hardware

This repo has three layers for catching problems before (or without) flashing a
keyboard. All of them run in CI (`.github/workflows/test.yml`) and locally.

| Layer | What it catches | Needs | Command |
|---|---|---|---|
| Static validation | build matrix / defconfig / keymap / layout / metadata mistakes | Python 3 only | `just check`, `just check-test` |
| Keymap simulation | keymap behaviour (layers, hold-taps, combos, encoders) on the host | Python 3 + gcc (no SDK, no hardware) | `just sim-bootstrap` once, then `just sim-test` |
| Split BLE simulation | key latency over the split link vs `CONFIG_ZMK_SPLIT_BLE_PREF_LATENCY` | BabbleSim (built by `just bsim-bootstrap`) | `just bsim-test` |
| Latency model | what a given peripheral latency costs (delay vs radio wake-ups) | Python 3 only | `just latency-model --sweep 0,4,8,30` |
| On-device logging | BLE pairing, split latency, resets on real hardware | a debug firmware + USB cable | see [scripts/log/README.md](scripts/log/README.md) |

## 1. Static validation (`scripts/check_config.py`)

Runs in well under a second with the Python standard library only.

```sh
just check              # python3 scripts/check_config.py
just check-test         # scripts/unit_tests.sh: every suite under tests/
just check-test static  # one suite only
python3 scripts/check_config.py --json   # machine-readable output
```

`scripts/unit_tests.sh` is the single list of unit-test suites — `static`,
`log`, `model`, `vial`, `flash`, `remap`, `cheatsheet`, `bsim` — and both
`just check-test` and `.github/workflows/test.yml` call it, so neither can
grow a suite the other does not run. All of them are stdlib-only Python except
`tests/bsim`, which is in the list but skips itself when BabbleSim is not
installed (as on the CI runner); it is run with `BSIM_TEST_NO_BUILD=1` so that
it reuses the executables `just bsim-test` built rather than rebuilding three
nrf52_bsim images.

Checks: qualified board names (`cornix_left//zmk`, `nice_nano//zmk`) in
`build.yaml` and `build-debug.yaml`, shields/snippets exist, dongle shield
combinations, `config/west.yml` remotes and imports, the settings-backend
invariants from the README (`CONFIG_NVS=y`, `CONFIG_SETTINGS_NVS=y`, never
`CONFIG_SETTINGS_NONE=y`), split roles per board, matrix transform vs physical
layout key counts, every keymap layer has exactly as many bindings as the
layout has keys (50 for `layout_50`, 42 for `cornix42`), layer numbers and
key positions in range, JSON metadata, shield and board metadata.

Errors fail the run; warnings are printed but do not. Add a check by writing
a `check_*` function in `scripts/check_config.py` that returns a list of error
strings, register it in `run_all_checks()`, and add a synthetic-input test in
`tests/static/test_check_config.py`.

## 2. Keymap simulation on `native_sim` (`scripts/sim/`)

ZMK is built for Zephyr's host board (`native_sim//zmk_test_mock`) with a mock
matrix driver that replays scripted key presses, exactly like ZMK's own
`app/tests`. Each case under `tests/sim/` is a directory with:

- `native_sim.keymap` — includes a shared mock (`tests/sim/_common/*.dtsi`)
  that wires a 4x14 `zmk,kscan-mock` to the real Cornix `default_transform` /
  `layout_50`, plus either the board keymap or `config/cornix.keymap`, and an
  `events = < ZMK_MOCK_PRESS(row,col,ms) ... >` script.
- `events.patterns` — sed filter selecting the log lines to compare.
- `keycode_events.snapshot` — expected output.
- optional `native_sim.conf`.

```sh
just sim-bootstrap                  # once: .sim/venv + .sim/ws (west workspace), ~4 min
just sim-test                       # build + run all cases, ~70 s on 16 cores
just sim-test --no-build            # re-run existing binaries, seconds
just sim-test tests/sim/user-keymap/homerow-mods --verbose
just sim-test tests/sim/<group>/<case> --auto-accept   # (re)generate a snapshot
```

The workspace lives in `.sim/` (git-ignored) and is separate from the repo
root on purpose: the repo root already has a `zephyr/` module directory that
would collide with a west workspace. BLE and split (central/peripheral)
behaviour cannot be simulated this way; use the on-device logs for those.

See `tests/sim/README.md` for how to add a case and a position → `RC(row,col)`
table for the Cornix matrix.

## 3. Split BLE link simulation with BabbleSim (`scripts/bsim/`)

Two ZMK instances (split central and peripheral) run on Zephyr's `nrf52_bsim`
board with the real Bluetooth host and nRF52 link-layer controller, connected
through BabbleSim's simulated 2.4 GHz phy. The peripheral replays mock key
events; the central's log shows when each reached the keymap. Both processes
share the simulated clock, so the measured delay is radio-link latency only.

```sh
just bsim-bootstrap        # once: downloads 32-bit libc/libgcc packages (no root) and builds BabbleSim into .sim/bsim
just bsim-test             # build central (latency 30 and 0), peripheral and host; run; print the tables
just bsim-test --no-build --no-host          # two-device run without the host computer
just bsim-test --no-build --seed 57 --per 0.15    # re-run with a packet-error rate
BSIM_TEST_NO_BUILD=1 python3 -m unittest discover -s tests/bsim -v
```

Results on 2026-09-17 (simulated time, 40 key events per run): the default
split latency 30 adds about 2 ms to the median compared with latency 0, and
adding a third device that plays the host computer (`tests/bsim/host/`, a
Zephyr app that connects to the ZMK central over HID with macOS-like
parameters, `--host`, on by default) does not change that: the worst
peripheral key → host HID report delay stays under 25 ms. The simulation
therefore does **not** reproduce the hundreds of milliseconds seen on
hardware. The Zephyr controller cancels peripheral latency as soon as a PDU is
queued (`ull_conn.c`, `ll_tx_mem_enqueue` → `ull_periph_latency_cancel`) and
keeps it cancelled while anything is unacknowledged, on both links.

One thing the three-device run did expose: ZMK also asks the host for
peripheral latency 30 on the computer link (`CONFIG_BT_PERIPHERAL_PREF_LATENCY`
in ZMK's Kconfig, applied by `CONFIG_BT_GAP_AUTO_UPDATE_CONN_PARAMS` a few
seconds after connecting), which is a second latency window on the keystroke
path that `CONFIG_ZMK_SPLIT_BLE_PREF_LATENCY=0` does not touch. The remaining
differences from hardware (real macOS host, radio environment, +8/0 dBm
asymmetry, crystal drift, stored bonds, typing load) and the full result matrix
are in `tests/bsim/README.md`. `tests/bsim/test_split_latency.py` asserts only
what the simulation reproduces, and its sanity thresholds are documented so
that a future failure is treated as a finding rather than a bug.

## 4. Peripheral-latency model (`scripts/model/`)

`split_latency_model.py` is a stdlib discrete-event model of connection events
with a peripheral latency `L`, in two modes: the peripheral sleeps through its
allowed latency (`--wake-on-data none`, the pessimistic hardware behaviour) or
cancels it when data is queued (`immediate`, what the Zephyr controller source
does). `--sweep` prints, per latency value, the worst-case wait, median and
p95 delay, and the radio wake rate as a battery-cost proxy, to help choose a
value between 0 and the ZMK default of 30. `--compare` validates the model
against `analyze_latency.py --json` output from real logs.

## 5. Keymap source of truth (`config/cornix.keymap` + `scripts/remap.py`)

`config/cornix.keymap` is the source of truth. It was imported once from the
stock RMK/Vial export `config/vial/cornix-default-keymap.vil` with
`scripts/vial2zmk.py` (kept as a one-shot importer; the previous homerow-mods
keymap is parked unbuilt in `config/keymaps/`). Day-to-day edits go through
`scripts/remap.py`, which addresses keys by ZMK position number (0-49, the
numbered grid in `config/keymap-notes.md`) and layer name, rewrites only the
touched layer's bindings block plus its grid comment, and refuses any edit
that fails the static keymap checks:

```sh
just remap show                                  # numbered grid of every layer
just remap set --layer Base 22 '&kp MINUS'
just remap layer add Gaming --after Win           # &trans-filled, shifts &mo indices
just remap --dry-run swap --layer Base 47 49
```

Every request and decision is logged in `config/keymap-notes.md`. `tests/remap/`
covers the CLI, `tests/vial/` the importer, and `tests/sim/user-keymap/` runs
the resulting keymap on `native_sim` (basic keys, Num layer, mouse layer,
encoders, OS-detection overlay, the Conn connection layer that comes up while
both right thumb Fn keys 45 + 46 are held).

Layer 1 ("Win") is a `&trans` overlay with only the OS-specific differences
(Cmd→Ctrl on the thumb rest key 41 and on 39). It is switched on and off by
the `zmk-feature-os-detection` module (USB enumeration and BLE GATT
fingerprints, central only, configured in `config/cornix_left.conf`), with a
manual `&tog 1` on the Fn3 layer as a fallback. The `os-detect-*` simulation
cases prove the layer arithmetic; the detection event itself cannot fire on
`native_sim` (no USB host), so the auto-switch needs a real host to verify.

## 5b. BLE pause while on USB (`src/ble_usb_gate.c`)

The repo is also a small code module (`CMakeLists.txt` + `Kconfig` at the
root, wired through `zephyr/module.yml`). `CONFIG_CORNIX_BLE_PAUSE_ON_USB`
(enabled in `config/cornix_left.conf`, central only) disconnects the BLE host
profile and stops advertising while USB is both the preferred and the selected
output, and resumes advertising when the output goes back to BLE or USB is
unplugged. It exists because a Mac that cannot complete encryption keeps
reconnecting about once per second and starves the split link. It is compiled
out on the right half and on `native_sim`; the ARM builds and `nm` are the
verification, and the hardware check is described in
`scripts/log/README.md` section 4b.

## 5c. LED indicators (`boards/shields/cornix_indicator`)

The `cornix_indicator` shield drives the two WS2812 LEDs per half through the
`hitsmaxft/zmk-rgbled-widget` fork (pinned in `config/west.yml`; the upstream
module cannot drive a strip or two LEDs). `build.yaml` and `build-debug.yaml`
have `*_indicator` targets; locally add `-DSHIELD=cornix_indicator`. The
mapping from the stock RMK LED behaviour to what ZMK shows, and what cannot be
matched (the central cannot show the split-link state), is in
`boards/shields/cornix_indicator/README.md`. Two real bugs were fixed there: a
space inside `shields_list_contains` made the shield never activate, and the
shield's board `.conf` files must be named `<board>_nrf52840_zmk.conf` to be
picked up on Zephyr 4.1.

## 5c-2. Custom behaviors in this module

The repo module now also ships behaviors (`dts/bindings/behaviors/`,
`zephyr/module.yml` declares `dts_root: .`). `zmk,behavior-cornix-app-switch`
(`&app_tab` on the Win layer's Tab) turns "hold 41 (Ctrl on Windows) + Tab"
into Alt+Tab with Alt held until 41 is released, so the Windows switcher stays
open like on a normal keyboard; `tests/sim/user-keymap/app-switch` proves the
HID report sequence. Passkey digits reaching the USB host during BLE pairing
cannot be fixed from a module (listener link order inside ZMK); see
`scripts/log/README.md` 5c.

## 5d. On-device crypto self-test and SMP debug (`src/crypto_selftest.c`)

`CONFIG_CORNIX_CRYPTO_SELFTEST=y` (opt-in, `-DCONFIG_CORNIX_CRYPTO_SELFTEST=y`)
runs RFC 5903 / RFC 4493 test vectors through the same PSA calls the Zephyr
Bluetooth host uses (P-256 public-key derivation, ECDH, AES-CMAC, RNG) and
logs `crypto selftest: ... PASS|FAIL` plus `ALL PASS (12 checks)`. It runs on
`native_sim` (`tests/sim/crypto/selftest`) and on the real left half; on
2026-09-19 both P-256 drivers passed on hardware, so a Secure Connections
pairing failure is not the crypto primitives. The `cornix-smp-debug` snippet
(Kconfig only, list it last in `-S`) raises the SMP/keys/crypto log levels so
a failing pairing shows which check failed; it logs key material, so keep it
to local debugging. Reading guide: `scripts/log/README.md` section 5b.

## 6. Flashing (`scripts/flash.py`)

`just flash <file.uf2> --label left|right` waits for a UF2 bootloader volume
(double-tap reset), shows its `INFO_UF2.TXT`, copies the file and waits for
the reboot. With a debug image on the left half no button is needed:
`python3 scripts/flash.py --enter left <file.uf2>` opens the left's log CDC
port at 1200 bps, which the `CONFIG_CORNIX_REMOTE_BOOT` module turns into a
`&bootloader` invocation; `--enter right` uses 2400 bps and the left forwards
the `bootload` behavior to the right half over the split link (the right needs
no special firmware, only a USB cable so its drive appears); 4800 bps is a
plain reset. A running `capture.py` is paused through its pidfile for the
duration. Verified on hardware 2026-09-19: left 7 s, right 9 s end to end. It works from WSL2 (through PowerShell), Linux and
macOS. Details and the settings_reset two-step are in
[scripts/FLASHING.md](scripts/FLASHING.md).

## 7. On-device logging and analysis (`scripts/log/`)

Debug firmware variants are defined in `build-debug.yaml` (artifact names end
in `_debug`) and built by the "Build debug firmware" workflow on demand or on
`debug/**` branches. They add the local snippet `snippets/cornix-debug-log`
together with ZMK's `zmk-usb-logging`, so both halves expose a USB serial log
port even though the right half keeps `CONFIG_ZMK_USB=n` for HID.

```sh
python3 scripts/log/capture.py --list                 # find the ZMK CDC ports
python3 scripts/log/capture.py --label right          # host-timestamped log in logs/
python3 scripts/log/capture.py --label left           # in a second terminal
python3 scripts/log/analyze_latency.py --peripheral logs/right-*.log --central logs/left-*.log
python3 scripts/log/analyze_ble.py logs/left-*.log    # pairing / bond / reboot diagnosis
```

`analyze_latency.py` reports per-stage key latency (peripheral scan → BLE
notify → central receive → keymap → HID report, plus cross-device timing
aligned on the host clock) and the BLE connection parameters actually in use.
`analyze_ble.py` lists connections and disconnections with decoded reason
codes, security/pairing failures, bond and settings loading, reboot counts and
fatal errors. Both exit non-zero when no matching log lines are found, which
usually means the debug snippet is not in the firmware.

Building a debug image locally reuses the simulation workspace plus an ARM
toolchain in `.sim/sdk` (installed once by hand, see `scripts/log/README.md`):

```sh
cd .sim/ws && source ../venv/bin/activate
west build -s zmk/app -d ../../.build/debug/cornix_right -b cornix_right//zmk \
  -S "zmk-usb-logging cornix-debug-log" -- -DZMK_CONFIG=$PWD/../../config -DZMK_EXTRA_MODULES=$PWD/../..
grep -E "CONFIG_(NVS|SETTINGS_NVS|SETTINGS_NONE|ZMK_USB_LOGGING)=" ../../.build/debug/cornix_right/zephyr/.config
```

Full instructions, the WSL2 `usbipd` note, and a split-latency / pairing-failure
checklist are in [scripts/log/README.md](scripts/log/README.md).

## Cheat sheet in CI

`config/cheatsheet.html` is a committed, generated file. The
`Keymap cheat sheet` workflow (`.github/workflows/cheatsheet.yml`) regenerates
it on every push to `main` that touches the keymap, the layout or the
generator, and commits the result back as `github-actions[bot]`; on a pull
request it fails when the committed file is stale (`just cheatsheet` fixes
that). The footer stamp is the keymap's last commit, not the wall clock, so
two runs on the same keymap give byte-identical HTML.
