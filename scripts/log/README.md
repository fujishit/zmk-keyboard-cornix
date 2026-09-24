# On-device logging and latency analysis

Tools to collect Zephyr/ZMK logs from the real Cornix halves over USB CDC-ACM
and to analyze them, so that problems like "the right half feels laggy" or
"the keyboard does not pair with my Mac" can be diagnosed from evidence instead
of guesses.

| File | Purpose |
| --- | --- |
| `snippets/cornix-debug-log/` | Kconfig snippet that turns on verbose, deferred USB logging on every role (also the right half, which has `CONFIG_ZMK_USB=n`). |
| `build-debug.yaml` + `.github/workflows/build-debug.yml` | GitHub Actions matrix producing `*_debug.uf2` artifacts. |
| `scripts/log/capture.py` | Serial capture with host timestamps (stdlib only; no pyserial). |
| `scripts/log/analyze_latency.py` | Per-stage and cross-device key latency, BLE connection parameters, warnings. |
| `scripts/log/analyze_ble.py` | Connection / pairing / bond / reboot / crash diagnostics. |
| `tests/log/` | `python3 -m unittest discover -s tests/log -v` (synthetic fixtures). |

Everything in `scripts/log` is Python 3 standard library only.

## 1. Build the debug firmware

The debug firmware is the normal firmware plus two snippets:

* `zmk-usb-logging` (from ZMK): adds a `zephyr,cdc-acm-uart` node under
  `zephyr_udc0` and makes it the `zephyr,console`, sets `CONFIG_ZMK_USB_LOGGING=y`.
  All three Cornix boards carry `zephyr_udc0: &usbd` in `boards/jzf/cornix/nrf_e73.dtsi`.
* `cornix-debug-log` (this repo, `snippets/cornix-debug-log/`): Kconfig only,
  see the comments in `cornix-debug-log.conf` for the source of every symbol.

The right half keeps `CONFIG_ZMK_USB=n` (no USB HID; it is a BLE split
peripheral) but still enumerates the CDC-ACM log port, because
`CONFIG_ZMK_USB_LOGGING` selects the USB device stack directly
(`zmk/app/Kconfig`, `config ZMK_USB_LOGGING`) and `app/src/usb.c` -- the
`usb_enable()` call -- is compiled under `CONFIG_USB_DEVICE_STACK`, not
`CONFIG_ZMK_USB` (`zmk/app/CMakeLists.txt`).

### GitHub Actions

Run the "Build debug firmware (USB logging)" workflow manually (Actions >
workflow_dispatch) or push a branch named `debug/<anything>`. It uses
`build-debug.yaml`:

| artifact | board | snippets |
| --- | --- | --- |
| `cornix_left_default_nosd_debug` | `cornix_left//zmk` | `zmk-usb-logging cornix-debug-log` |
| `cornix_left_for_dongle_nosd_debug` | `cornix_ph_left//zmk` | `zmk-usb-logging cornix-debug-log` |
| `cornix_right_nosd_debug` | `cornix_right//zmk` | `zmk-usb-logging cornix-debug-log` |
| `cornix_dongle_nosd_debug` | `nice_nano//zmk` + dongle shields | `studio-rpc-usb-uart nrf52840-nosd zmk-usb-logging cornix-debug-log` |

`studio-rpc-usb-uart` and `zmk-usb-logging` coexist: each snippet adds its own
`zephyr,cdc-acm-uart` node (`snippet_studio_rpc_usb_uart` chosen as
`zmk,studio-rpc-uart`, `snippet_zmk_usb_logging_uart` chosen as
`zephyr,console`), so the dongle simply shows two CDC-ACM ports. The Studio
snippet is not used on the halves, exactly like `build.yaml`, because its
`.conf` forces `CONFIG_ZMK_USB=y`.

### Locally with west

From a west workspace whose manifest is `config/west.yml` (see `Justfile`
`init`, or the `.sim/ws` workspace created by `scripts/sim/bootstrap.sh` plus a
Zephyr SDK with the `arm-zephyr-eabi` toolchain):

```sh
west build -s zmk/app -d .build/debug/cornix_right -b cornix_right//zmk \
    -S "zmk-usb-logging cornix-debug-log" -- \
    -DZMK_CONFIG=/path/to/zmk-keyboard-cornix/config \
    -DZMK_EXTRA_MODULES=/path/to/zmk-keyboard-cornix
```

The left half and the dongle enable ZMK Studio, whose build needs `protoc`
and the Python `protobuf`/`grpcio-tools` packages; the `.sim/venv` workspace
has them (`protoc` is symlinked into `.sim/tools/bin`). Add `.sim/tools/bin`
to `PATH` and set `ZEPHYR_SDK_INSTALL_DIR=.sim/sdk/zephyr-sdk-0.17.0`
`ZEPHYR_TOOLCHAIN_VARIANT=zephyr` before running `west build`.

The local snippet is found because `zephyr/module.yml` declares
`snippet_root: .`. After building, check `.build/debug/cornix_right/zephyr/.config`:

```text
CONFIG_ZMK_USB_LOGGING=y
CONFIG_USB_CDC_ACM=y
CONFIG_ZMK_LOG_LEVEL=4
CONFIG_ZMK_USB=n            (right half only; the left/dongle have =y)
CONFIG_NVS=y
CONFIG_SETTINGS_NVS=y
                             and NO CONFIG_SETTINGS_NONE=y
```

### Flashing

Same as the release firmware (README "Flashing and recovery"): double-tap
reset to enter the UF2 bootloader, copy the `*_debug.uf2` for that role, and
reset both halves together so the split link re-forms. Logging costs battery;
go back to the normal firmware when done.

## 2. Capture

Plug **both halves into USB at the same time**. The right half stays a BLE
split peripheral of the left (its USB port only carries the log), so the key
path being measured is unchanged. Run one capture per device, in two terminals:

```sh
python3 scripts/log/capture.py --label right --port /dev/serial/by-id/usb-ZMK_Project_Cornix_XXXX-if00
python3 scripts/log/capture.py --label left  --port /dev/serial/by-id/usb-ZMK_Project_Cornix_YYYY-if00
```

`python3 scripts/log/capture.py --list` shows the candidates; with a single
port `--port` can be omitted. Files land in `logs/<label>-<YYYYmmdd-HHMMSS>.log`
(`logs/` is git-ignored). Each line gets a `[host HH:MM:SS.mmm]` prefix taken
from the same host clock, which is what allows the two devices to be
correlated. The capture survives device resets (retry loop) and stops with
Ctrl-C. Then press a few keys on the right half, slowly, and let the capture
run for a minute so the clock alignment has enough samples.

### WSL2 (no usbipd required)

WSL2 does not see USB devices at all, and `/dev/ttyS*` are **not** the Windows
COM ports. Instead of attaching the device to Linux, `capture.py` reads it on
the Windows side through `powershell.exe` (always on `PATH` inside WSL via
interop). Nothing has to be installed on Windows and no administrator shell is
involved.

```sh
python3 scripts/log/capture.py --list          # enumerate the Windows COM ports
python3 scripts/log/capture.py --com COM5 --label dongle
```

```text
Windows COM:
  COM5   USB Serial Device (COM5) [VID_1D50&PID_615E]  <- ZMK
  COM6   USB Serial Device (COM6) [VID_1D50&PID_615E]  <- ZMK
would use: pass --com (COM5, COM6)
```

* Ports are enumerated with `Get-CimInstance Win32_PnPEntity` filtered on
  `(COMn)` in the name; the VID/PID come from the `PNPDeviceID`. A ZMK port is
  one with **VID 1D50** (or `ZMK` in its name) and is preferred over anything
  else (Bluetooth virtual ports, FTDI adapters, ...).
* With exactly one ZMK port it is selected automatically, so `--com` can be
  omitted. A **dongle exposes two CDC ports** (Studio RPC + the log, and on a
  localized Windows both are just called "USB Serial Device"), so `--com` must
  say which one; if one stays silent, try the other.
* Auto-detection kicks in only under WSL (`/proc/version` contains `microsoft`,
  or `WSL_DISTRO_NAME` is set) **and** when no `/dev/ttyACM*` exists -- so an
  already-attached usbipd device keeps using the normal Linux path. `--port`
  always forces the Linux path, `--com` always forces the Windows one.
* Reading is done by `scripts/log/read_com.ps1`, started as
  `powershell.exe -NoProfile -ExecutionPolicy Bypass -File <wslpath -w ...>
  -Port COMn -Baud 115200`. It opens
  `[System.IO.Ports.SerialPort]::new('COMn', 115200, 'None', 8, 'One')` with
  `DtrEnable = $true` (a CDC-ACM console only transmits once the host asserted
  DTR) and pumps `ReadExisting()` to stdout, UTF-8, flushed on every chunk.
  `capture.py` reassembles the lines, strips CR and ANSI colour codes, and
  applies the same `[host HH:MM:SS.mmm]` prefix, file naming, tee and Ctrl-C
  handling as the Linux path -- the resulting log file is identical.
* The script exits with a distinct code when the port fails (3 = could not be
  opened, 4 = it went away), which feeds the same reconnect loop, so a ZMK
  reset or a re-plug is recovered automatically.
* `--com COM99` for a port Windows does not have fails immediately with the
  list of ports that do exist (exit 2). `access denied` /
  `アクセスが拒否されました` means another program (PuTTY, TeraTerm, ZMK Studio,
  a second `capture.py`) already holds the port -- only one reader at a time.
* Caveats: `powershell.exe` startup costs roughly 0.3-1 s, so the first line
  and each reconnect are that late (the host timestamps themselves are taken
  when the bytes reach `capture.py`, not when PowerShell started); the port is
  polled every 20 ms, which adds up to 20 ms of arrival jitter on top of the
  deferred-logging flush delay. For sub-millisecond host stamps use the usbipd
  route below or capture from Linux/macOS.
* `--verbose-child` echoes the PowerShell reader's own status lines.

### WSL2 (alternative: usbipd-win)

If usbipd-win is installed the device can be handed to Linux instead, from an
administrator PowerShell on the Windows side: `usbipd list`,
`usbipd bind --busid <id>` once, `usbipd attach --wsl --busid <id>` after every
plug/reset. `/dev/ttyACM*` then appears in WSL and the normal termios path is
used (`/dev/serial/by-id` usually does not exist on WSL2 -- no udev -- so pass
`--port /dev/ttyACMn`). This avoids the PowerShell startup/polling overhead.

### Windows / other terminal programs

A PuTTY session log ("Printable output"), `tio --log`, or `minicom -C` file can
be fed to the analyzers as is; `capture.py --tio` wraps `tio --timestamp` and
its `[HH:MM:SS.mmm]` prefix is understood too. Without host timestamps only
per-device numbers are produced.

## 3. Analyze

```sh
python3 scripts/log/analyze_latency.py --peripheral logs/right-*.log --central logs/left-*.log [--hist] [--verbose] [--json out.json]
python3 scripts/log/analyze_ble.py logs/left-*.log [--json out.json]
python3 scripts/log/analyze_latency.py logs/right-*.log     # single file
python3 scripts/log/rssi_timeline.py logs/left-*.log [--bucket 300] [--json out.json]
```

Output: one table `count / min / median / p95 / max` (ms) per stage, the BLE
connection parameters seen in the log (with the interval converted to ms and
the worst-case peripheral-latency window), and grouped warnings/errors. Exit
code 1 and the message `no matching events found - check that the debug
snippet is enabled` when nothing matched.

### Split-link signal strength

`rssi_timeline.py` prints a per-minute median / min / max of the split link's
RSSI from a **left-half** log, plus the reconnect-time RSSI of the peripheral
in the same buckets, so a latency or drop-out episode can be lined up against
the radio. It reads two kinds of line: `split rssi: -85 dBm (peer <addr>)`,
logged at `INF` every `CONFIG_CORNIX_INDICATOR_SPLIT_RSSI_PERIOD_MS` (5 s) by
`boards/shields/cornix_indicator/src/split_rssi.c` while the left half is
USB-powered - the RSSI of the *live connection*, read with HCI Read RSSI - and
`split_central_device_found: [DEVICE]: <addr>, ... RSSI -85`, one advertising
packet seen while the link is *down*, filtered to the peripheral address taken
from the `split_central_connected: Connected: <addr>` lines of the same log.
Measured on this keyboard: -63..-70 dBm in the good state, -85..-89 dBm in the
bad one (supervision timeouts every ~40 s); the connectivity LED of the left
half shows the same -70 / -80 bands in green / yellow / red while on a cable.
For a quick look without the script, the prefix is a plain grep handle:

```sh
# every sample, as "<timestamp> <dBm>"
grep -h 'split rssi' logs/left-*.log \
  | awk -F'split rssi: ' '{ split($2, a, " ")
                            match($1, /[0-9]+:[0-9][0-9]:[0-9][0-9]\.[0-9]+/)
                            print substr($1, RSTART, RLENGTH), a[1] }'

# worst sample per minute (host clock if capture.py wrote one, device clock
# otherwise - the first timestamp on the line either way)
grep -h 'split rssi' logs/left-*.log \
  | awk -F'split rssi: ' '{ split($2, a, " "); r = a[1] + 0
                            match($1, /[0-9]+:[0-9][0-9]:[0-9][0-9]\./)
                            m = substr($1, RSTART, 5)
                            if (!(m in w) || r < w[m]) w[m] = r }
                          END { for (m in w) print m, w[m] }' | sort
```

Needs a left-half build with `-DSHIELD=cornix_indicator` (the symbol is
`default y` there on the central) captured at `INF` or lower; without it the
script only has the reconnect-time scan samples to work with, and says so.

### What the latency analyzer keys on

Every stage is derived from `<dbg> zmk:` lines of ZMK `main` (function name
prefix from `CONFIG_LOG_FUNC_NAME_PREFIX_DBG`). Message strings were read from
the sources on the date this tool was written; **they can drift with ZMK
main**. The regexes are applied with `re.search` on the whole message, so a
renamed function does not matter, a reworded message does. If a stage is
missing, grep the log for the message and adjust `EVENT_PATTERNS`.

| stage event | message (regex) | source (ZMK main) |
| --- | --- | --- |
| kscan | `Sending event at (\d+),(\d+) state (on\|off)` | `app/module/drivers/kscan/kscan_gpio_matrix.c` `kscan_matrix_read()` (logged after debouncing) |
| position | `Row: (\d+), col: (\d+), position: (\d+), pressed: (true\|false)` | `app/src/physical_layouts.c` `zmk_physical_layouts_kscan_process_msgq()` |
| split_listener | `split_peripheral_listener:` with empty message (`LOG_DBG("")`) | `app/src/split/peripheral.c` `split_peripheral_listener()` -- the event is handed to the BLE transport here; `app/src/split/bluetooth/service.c` `send_position_state()` only logs on failure (`Position state message queue full`, `Failed to queue position state`, `Error notifying %d`) |
| notify | `\[NOTIFICATION\] data \S+ length (\d+)` | `app/src/split/bluetooth/central.c` `split_central_notify_func()` (GATT notify callback, BT RX thread) |
| trigger | `Trigger key position state change of type (\d+)` | `app/src/split/bluetooth/central.c` `peripheral_event_work_callback()` (system work queue) |
| keymap | `layer_id: (\d+) position: (\d+), binding name: (\S+)` | `app/src/keymap.c` `zmk_keymap_apply_position_state()` |
| hid | `usage_page 0x.. keycode 0x.. implicit_mods` | `app/src/hid_listener.c` `hid_listener_keycode_pressed()/released()`; the HID report is sent synchronously right after (`app/src/endpoints.c` logs only failures: `FAILED TO SEND OVER USB/HOG`) |
| BLE params | `interval (\d+) latency (\d+) timeout (\d+)` | `app/src/ble.c` and `app/src/split/bluetooth/peripheral.c` `le_param_updated()` (units 1.25 ms / intervals / 10 ms) |
| BLE params | `New connection params: Interval: (\d+), Latency: (\d+), PHY: (\d+)` | `app/src/split/bluetooth/central.c` `split_central_process_connection()` |
| PHY | `PHY updated: status: 0x.. , tx: N, rx: N` | `zephyr/subsys/bluetooth/host/hci_core.c` `le_phy_update_complete()` -- only with the opt-in `CONFIG_BT_HCI_CORE_LOG_LEVEL_DBG` |
| param failure | `Send (auto )?LE param update failed (err N)` | `zephyr/subsys/bluetooth/host/conn.c` |

Pairing rules: `kscan -> position` by (row, col, pressed); `position ->
split_listener` immediately following; `notification -> trigger` most recent
notification; `trigger | local position -> keymap` closest preceding
candidate (a central also has its own keys, reported as `local ...` stages);
`keymap -> hid` only when nothing else intervenes (layer/hold-tap bindings
produce no immediate HID report and are counted as `keymap (no immediate hid)`).

Cross-device (`--peripheral` + `--central`): each file's device clock is
aligned to the host clock with the 5th percentile of `host - device` over all
lines (the host stamp is arrival time = device time + deferred-log flush delay,
so the low percentile is the best estimate of the constant part). Then the
peripheral `position` event is matched to the central `keymap` event with the
same key position. The report also prints the raw host-stamp difference for
comparison and, per file, the flush-delay spread, which tells how trustworthy
the host stamps are. ~1 ms of error is expected; a `LOG_PROCESS_THREAD_SLEEP_MS`
worth of jitter shows up in the p95 column.

Reference numbers with the stock 7.5 ms connection interval: peripheral
`kscan -> split_listener` well under 1 ms, `notification -> hid` on the central
under 1 ms, and `peripheral position -> central keymap (aligned)` typically
in the 2..15 ms range (one connection interval plus scheduling). Values of
tens to hundreds of milliseconds, or a large p95/max gap, point at the radio
link or the connection parameters, not at the firmware path.

## 3b. Reproduction protocol: right-half latency vs PREF_LATENCY

Goal: capture the mechanism behind the right-half (peripheral) latency that
`CONFIG_ZMK_SPLIT_BLE_PREF_LATENCY=0` on the central fixes, so it can be
compared with the BabbleSim simulation (`tests/bsim/`) and the model
(`scripts/model/`). Pre-built debug images live in `firmware/debug/` after
running the local build (see section 1):

| Image | Central pref latency | Purpose |
|---|---|---|
| `cornix_left_lat30_debug.uf2` | 30 (ZMK default) | reproduces the bad behaviour |
| `cornix_left_lat0_debug.uf2` | 0 | the known fix, for A/B |
| `cornix_right_debug.uf2` | n/a (peripheral) | same image for every run |
| `cornix_left_split30_host0_debug.uf2` | split 30, host link 0 | isolates the host-link latency window |
| `cornix_left_split0_host0_debug.uf2` | split 0, host link 0 | both windows closed |

The "host link" column is `CONFIG_BT_PERIPHERAL_PREF_LATENCY`: ZMK asks the
computer for peripheral latency 30 on that link too (see `tests/bsim/README.md`),
so the keystroke path has two latency windows, and the split one is only the
first.

Capture both halves at once. On macOS the ports show up as
`/dev/cu.usbmodem*` and `capture.py` finds them directly (no udev, no
usbipd). On WSL2 use `--com COMn` (see section 2; `--list` shows which is
which), or attach both devices with `usbipd` first.

```sh
python3 scripts/log/capture.py --list
python3 scripts/log/capture.py --label right   --port /dev/cu.usbmodemXXXX1
python3 scripts/log/capture.py --label left    --port /dev/cu.usbmodemXXXX2   # second terminal
```

Runs (each about 60 s, same key script every time, right hand only):

1. **A: left = lat30, Mac connected over BLE** (the failing setup). Wait for
   `[SUBSCRIBED]` on the left log and for the Mac to be connected, then press
   single keys on the right half at irregular intervals: ~20 presses with
   0.3–3 s gaps, then a burst of 10 quick presses, then 5 s idle, then 5 more
   presses. Note the wall-clock time of anything you feel is late.
2. **B: same firmware, Mac Bluetooth turned off** (only the split link
   exists). If latency disappears here, the host link is the trigger.
3. **C: left = lat0, Mac connected.** Expected fast; this is the control.
4. **E: left = split30_host0, Mac connected.** If A is slow and E is fast,
   the window that matters is the Mac link, not the split link.
5. Optional **D: left = lat30, Mac connected over USB instead** (select the
   USB endpoint on the left with `&out OUT_USB`; the BLE link to the Mac stays
   up unless you disconnect the profile, so this separates HID traffic from the
   link itself).

Analyze each run:

```sh
python3 scripts/log/analyze_latency.py --peripheral logs/right-<A>.log --central logs/left-<A>.log --hist
python3 scripts/log/analyze_latency.py ... --json > logs/A.json
python3 scripts/model/split_latency_model.py --compare logs/A.json          # model vs measured
python3 scripts/bsim/measure.py ...   # compare with the simulated numbers in tests/bsim/README.md
```

What to look at:

- `cross: peripheral position -> central keymap (aligned)`: p95/max near
  232 ms in A and small in C confirms the latency window is the culprit;
  a bimodal histogram (a fast cluster plus a ~200 ms cluster) points at
  missed connection events rather than a constant queueing delay.
- `BLE connection parameters`: the split link on the right (`le_param_updated
  ... interval 6 latency 30 timeout 400`) and, on the left, the Mac link
  parameters (`interval`/`latency` lines for the host address). Mac links are
  typically 15 ms interval; note its latency value.
- `peripheral: position -> split_listener` staying small in A means the right
  half handed the packet to its controller quickly and the wait happened on
  air, which matches the "sleeps through the latency window" mechanism.
- Warnings: `Send LE param update failed`, `Disconnected ... reason 0x08`,
  or `Position state message queue full` change the diagnosis (see section 4).
- `analyze_ble.py logs/left-<A>.log` for resets or security events during the
  run.

Share `logs/A.json`, `logs/B.json`, `logs/C.json` (or the raw logs) to feed the
simulation: the host-link parameters seen in A are what the three-device
BabbleSim setup should be configured with.

### Observed on hardware (2026-09-17)

After resetting the Mac's Bluetooth settings the keyboard paired again. With
the left half connected to the Mac over BLE, right-half latency was small even
with the default `CONFIG_ZMK_SPLIT_BLE_PREF_LATENCY=30`; the latency was
noticeable while the left half had no BLE host link (and switching the output
to BLE improved it). `PREF_LATENCY=0` also removed it. The mechanism is still
unexplained: the BabbleSim runs without a host (`--no-host`, where the ZMK
central keeps advertising for the open profile) did not reproduce it. If it
comes back, capture runs A and B from the protocol above; the pair "no host
link + latency 30" is the condition to reproduce.

### Observed on hardware (2026-09-17, evening): right half on USB

Second episode, captured with the debug firmware (`firmware/debug/keymap_*_debug.uf2`,
logs in `logs/left-usbvsble.log`, `logs/right-usbvsble.log`, `logs/left-boot.log`):

- Trigger, as reported by the user: plugging the **right half** into the
  Windows PC's USB port. Unplugging it fixed both the Mac link and the right
  half immediately; later the same plug-in did not reproduce. Intermittent.
- What the logs showed during the episode (both halves on the PC's USB):
  the Mac link looped ~1/s through connect → `Security failed ... level 1
  err 9` (BT_SECURITY_ERR_UNSPECIFIED) → `Disconnected (reason 0x08)`; the
  split link did the same, plus six `conn ... failed to establish. RF noise?`
  (HCI 0x3E) and the right half advertising at **RSSI -81 to -92 dBm** on
  the same desk. Both links failing at the encryption step with supervision
  timeouts is consistent with heavy packet loss, not with missing keys (the
  right half had `bt/keys/...` for the left, and the Mac link recovered
  without re-pairing once USB was unplugged).
- Leading hypothesis: 2.4 GHz noise from a USB 3.x port/cable next to the
  right half's antenna (0 dBm TX on the right vs +8 dBm on the left makes it
  the weaker direction). Not proven.

If it comes back, in this order:

1. Right half on a USB charger / power bank instead of the PC (power only).
   Latency there → power/cable side; no latency → PC USB noise.
2. Right half on a USB 2.0 port, or a USB 2.0 hub/extension away from the PC.
3. Capture while it is happening: `python3 scripts/log/capture.py --com COM5
   --label left` and `--com COM6 --label right` (see section 2), then
   `analyze_ble.py` (look for `Security failed`, reason 0x08/0x3e, RSSI in
   `split_central_device_found` lines) and `analyze_latency.py`.

Firmware-side mitigations if USB noise is confirmed: `CONFIG_BT_CTLR_TX_PWR_PLUS_8=y`
on the right half (battery cost), and a lower `CONFIG_ZMK_SPLIT_BLE_PREF_LATENCY`.

## 4. Split-latency checklist (symbol-verified)

Where each option is set in this repo and where the symbol was verified
(`zmk` = `zmkfirmware/zmk` main, `zephyr` = `zmkfirmware/zephyr`
`v4.1.0+zmk-fixes`, the revision pinned in `zmk/app/west.yml`).

| Option | Effect on latency | In this repo | Verified in |
| --- | --- | --- | --- |
| `CONFIG_ZMK_KSCAN_DEBOUNCE_PRESS_MS` / `_RELEASE_MS` | A key is reported only after the input is stable this long: a fixed floor on every key. `-1` = devicetree `debounce-press-ms` (default 5). | `boards/jzf/cornix/Kconfig.defconfig`: 3 / 3 ms on `cornix_right` and `cornix_ph_left`; the left keeps the 5 ms default. | `zmk/app/module/drivers/kscan/Kconfig` (`config ZMK_KSCAN_DEBOUNCE_PRESS_MS`, `..._RELEASE_MS`) |
| `CONFIG_ZMK_KSCAN_MATRIX_POLLING` | Poll instead of GPIO interrupts; adds up to one poll period (`poll-period-ms`, default 10) before the first sample. | not set (interrupt driven; `cornix.dtsi` `kscan0` has no poll property) | `zmk/app/module/drivers/kscan/Kconfig` |
| `CONFIG_ZMK_KSCAN_MATRIX_WAIT_BEFORE_INPUTS` / `_WAIT_BETWEEN_OUTPUTS` | Extra ticks per scanned column. | not set (0) | `zmk/app/module/drivers/kscan/Kconfig` |
| `CONFIG_ZMK_SPLIT_BLE_PREF_INT` (default 6 = 7.5 ms), `_PREF_LATENCY` (30), `_PREF_TIMEOUT` (400 = 4 s) | The connection parameters the **central** requests when it creates the split connection (`BT_LE_CONN_PARAM(...)` in `central.c`). The interval bounds how soon a notification can be sent; peripheral latency lets the peripheral skip up to 30 intervals when it has nothing to send. | not overridden anywhere in `boards/` or `config/` -> ZMK defaults | `zmk/app/src/split/bluetooth/Kconfig`, used in `zmk/app/src/split/bluetooth/central.c` |
| `CONFIG_BT_GAP_AUTO_UPDATE_CONN_PARAMS` | ZMK sets it to `n` on the split peripheral so the peripheral never requests its own parameters; the central's values above win. | ZMK default for `!ZMK_SPLIT_ROLE_CENTRAL` | `zmk/app/src/split/bluetooth/Kconfig`; `zephyr/subsys/bluetooth/host/Kconfig.gatt` |
| `CONFIG_BT_PERIPHERAL_PREF_MIN_INT` / `_MAX_INT` / `_LATENCY` / `_TIMEOUT` | Peripheral Preferred Connection Parameters advertised to a *host* (the left half's link to the computer). ZMK defaults 6 / 12 / 30 / 400. | ZMK defaults | `zephyr/subsys/bluetooth/host/Kconfig.gatt` (`if BT_GAP_PERIPHERAL_PREF_PARAMS`), defaults in `zmk/app/Kconfig` |
| `CONFIG_BT_CTLR_TX_PWR_PLUS_8` | +8 dBm radio output; link margin and retransmissions. | `cornix_left_*defconfig` and `cornix_dongle_adapter.conf` only -- **the right half does not set it** and transmits at the default 0 dBm. | `zephyr/subsys/bluetooth/controller/Kconfig` (`choice BT_CTLR_TX_PWR`) |
| `CONFIG_ZMK_SPLIT_BLE_CENTRAL_BATTERY_LEVEL_FETCHING` / `_PROXY` | Central subscribes to the peripheral's BAS notifications and reads the level at connect; adds GATT traffic on the same link. | `y` on the left (`Kconfig.defconfig`, `cornix_left_*defconfig`) and on the dongle shield | `zmk/app/src/split/bluetooth/Kconfig` |
| `CONFIG_ZMK_BATTERY_REPORT_INTERVAL` | How often the peripheral samples/reports its battery (default 60 s). | 60 on the left and dongle; right uses the default | `zmk/app/Kconfig` |
| `CONFIG_ZMK_SPLIT_BLE_PERIPHERAL_POSITION_QUEUE_SIZE` (10), `CONFIG_ZMK_SPLIT_BLE_CENTRAL_POSITION_QUEUE_SIZE` (5), `CONFIG_ZMK_SPLIT_BLE_PERIPHERAL_PRIORITY` (5) | Queue depth and priority of the notify work queue. `Position state message queue full` in the log means events pile up faster than the link drains them. | ZMK defaults | `zmk/app/src/split/bluetooth/Kconfig` |
| `CONFIG_BT_ATT_TX_COUNT` | Outstanding ATT packets; ZMK raises it to 10 on the central. | ZMK default | `zmk/app/src/split/bluetooth/Kconfig`; `zephyr/subsys/bluetooth/host/Kconfig.gatt` |
| Modules from `config/west.yml` on the peripheral | `zmk-module-settings-rpc` declares `CONFIG_ZMK_SPLIT_RELAY_EVENT` (`default y` if `ZMK_SPLIT`), `zmk-module-battery-history` `ZMK_BATTERY_HISTORY` (periodic flash writes), `zmk-module-ble-management` / `zmk-module-runtime-input-processor` (RPC parts depend on `ZMK_STUDIO`). A local `cornix_right//zmk` debug build showed **none** of these symbols in `.config` (not even as "is not set"), i.e. the modules contribute nothing to the right half; check the left/dongle `.config` before blaming them there. | all four pulled for every build; none active on the right (verified) | each module's `Kconfig` on github.com/cormoran; `.build/debug/cornix_right/zephyr/.config` |
| `CONFIG_ZMK_USB_LOGGING` + `CONFIG_LOG_MODE_DEFERRED` | The debug build itself. Deferred mode keeps `LOG_*` calls to a buffer copy; the flush thread is low priority. | `snippets/cornix-debug-log` | `zmk/app/Kconfig`; `zephyr/subsys/logging/Kconfig.mode` |

## 4b. BLE pause while on USB (`CONFIG_CORNIX_BLE_PAUSE_ON_USB`)

Enabled on the left half in `config/cornix_left.conf`; implemented by this
repository itself (`Kconfig`, `CMakeLists.txt`, `src/ble_usb_gate.c`, wired in
through `zephyr/module.yml`). It exists because of the episode in section 3:
while the left half was used over USB on the PC, the bonded Mac kept
reconnecting about once a second and every attempt died at the encryption step
(12 h, 45,910 cycles, 0 successes - `analyze_ble.py` counts them), and that
reconnect storm is radio time the central cannot give the already weak split
link. Turning Bluetooth off on the Mac fixed it; the feature does the same
from the keyboard side, automatically.

What it does, on the central only: while the USB endpoint is the one actually
carrying keystrokes - the **selected** transport is `ZMK_TRANSPORT_USB` - it
disconnects the active BLE profile (`zmk_ble_prof_disconnect()`) and keeps
advertising stopped (`bt_le_adv_stop()`, re-run after every
`zmk_ble_active_profile_changed` because ZMK restarts advertising from its own
`connected()`/`disconnected()` callbacks). When USB stops being the selected
transport it hands advertising back to ZMK. The split link is untouched: the
central reaches the right half as a BLE *central*, which needs no advertising,
and `zmk_ble_prof_disconnect()` only looks at the host profile's address. Every
action is delayed by `CONFIG_CORNIX_BLE_PAUSE_ON_USB_DELAY_MS` (500 ms) to
debounce the event burst of a replug.

### The BLE grace window (why a selected-only rule is safe)

With BLE paused there is no BLE host, so ZMK's endpoint selection falls back
from a preferred BLE to USB (`endpoints.c`, `get_selected_transport()`) and the
selected transport is USB *whatever the user asked for*. A bare "gate when
selected is USB" rule would therefore make `&out OUT_BLE` a no-op and lock BLE
off for good. The way out is the **BLE grace window**: whenever the
**preferred** transport becomes BLE while USB is plugged in - `&out OUT_BLE`,
the output toggle, or the value restored from settings at boot - advertising is
allowed for `CONFIG_CORNIX_BLE_PAUSE_ON_USB_BLE_GRACE_MS` (20 s). Then either

* a host connects, becomes the selected endpoint, and the gate stays open for
  as long as `selected == BLE` (typing on the Mac with the cable only supplying
  power); or
* the window expires with nothing connected, so the preference was stale: the
  gate sets it back with
  `zmk_endpoint_set_preferred_transport(ZMK_TRANSPORT_USB)` - ZMK persists that
  - and closes.

The earlier rule was "**preferred** USB **and** selected USB", and the left-half
capture of 2026-09-18 20:02 is why it had to go. The user had once pressed
`&out OUT_BLE` at 19:53 just to test it; ZMK persisted `preferred = BLE` and
restored it on every boot afterwards. With the Mac not connected the selection
fell back to USB, so the keyboard typed over USB with `preferred = BLE`, the
first half of the condition was false forever, and the log repeated

```
zmk: ble gate: preferred 2 selected 1 -> gated 0 (paused 0)
```

(2 = `ZMK_TRANSPORT_BLE`, 1 = `ZMK_TRANSPORT_USB`) while the Mac's reconnect
storm was free to resume. A stale preference can no longer disable the gate:
it only ever buys one grace window before being rewritten to USB.

### Events vs. the watchdog

`zmk_endpoint_set_preferred_transport()` only raises `zmk_endpoint_changed`
when the *selected* instance really changes, so some transitions arrive as
events and some do not:

| Transition | Event? | Who acts |
| --- | --- | --- |
| USB becomes ready (`preferred = USB`) | `zmk_usb_conn_state_changed` + `zmk_endpoint_changed` | event, gate closes within `DELAY_MS` |
| `&out OUT_BLE` while gated | no (BLE not ready -> selection stays USB) | watchdog, re-armed every `DELAY_MS` while gated -> opens the window |
| host connects during the window | `zmk_ble_active_profile_changed` + `zmk_endpoint_changed` | event, window closed early, gate stays open |
| `&out OUT_USB` while the host is selected | yes (BLE:0 -> USB really changes) | event, gate closes within `DELAY_MS` |
| window expires / `&out OUT_USB` during the window | no | watchdog |
| `&out OUT_USB` while the gate is open with USB already selected | **no** (selection unchanged) | watchdog |
| USB unplugged | `zmk_usb_conn_state_changed` + `zmk_endpoint_changed` | event, gate opens, advertising resumes |

The watchdog re-arms every `DELAY_MS` for as long as USB can carry keystrokes
(`zmk_usb_is_hid_ready()`), plus while a grace window is open - not only while
the gate is closed. The 2026-09-18 20:09 capture is why: it shows
`zmk_endpoint_set_preferred_transport: Selected endpoint transport 1` from
`&out OUT_USB` with no `ble gate:` line for the next 3 s, because with the gate
open on a USB-selected keyboard nothing was re-evaluating it. A tick that finds
nothing to do touches neither the radio nor the log, and the watchdog stops
once USB is unplugged, which is the only state where the battery would pay for
it.

Log lines (debug firmware, `capture.py`):

```
zmk: ble gate: init (preferred 1 selected 0)
zmk: ble gate: BLE grace window (45000 ms)
zmk: ble gate: BLE grace window closed (preferred 2 selected 2)
zmk: ble gate: preferred 2 selected 1 -> gated 0 (paused 1)
zmk: ble gate: resumed
zmk: ble gate: BLE did not connect, preferring USB again
zmk: ble gate: preferred 1 selected 1 -> gated 1 (paused 0)
zmk: ble gate: disconnecting host profile 0 (err 0)
zmk: ble gate: paused (USB active)
```

The `init` line is logged from the **first work run**, `DELAY_MS` after boot,
not from `SYS_INIT`: `SYS_INIT` runs before `main()` calls `settings_load()`
(`zmk/app/src/main.c`), so a value printed there would be the compile-time
default, not the restored preference - which is exactly what made the
2026-09-18 log read `ble gate: init (preferred 1 selected 0)` on a keyboard
whose stored preference was BLE.

(An abnormal resume also logs `ble gate: failed to restart advertising (err
...), retrying`; it keeps retrying every
`CONFIG_CORNIX_BLE_PAUSE_ON_USB_DELAY_MS` until it succeeds.)

`grep "ble gate:" logs/left-*.log` is enough; `analyze_ble.py` does not know
about these lines, but it is what shows the effect (no more
connect -> `Security failed` -> `Disconnected (reason 0x08)` cycles while
paused).

### Verifying on hardware

1. Flash `firmware/debug/keymap_left_debug.uf2` and capture:
   `python3 scripts/log/capture.py --label left`.
2. Plug the left half into the PC. Within ~0.5 s of USB enumerating, expect
   `ble gate: init (...)` and then - unless the stored preference is BLE, see
   step 5 - `ble gate: paused (USB active)`, the Mac showing the keyboard as
   disconnected, and the reconnect cycles in
   `python3 scripts/log/analyze_ble.py logs/left-*.log` stopping for the rest
   of the capture.
3. Type for a minute and compare the split-link disconnects with the same
   capture taken before the feature (section 3 protocol, runs A/B).
4. Ask for BLE: `&out OUT_BLE` = hold both right thumb Fn keys (positions 45
   and 46, which switches the Conn layer on) and tap position 12, the left Ctrl (ex-Caps)
   position. Expect `ble gate: BLE grace window (45000 ms)` and
   `ble gate: resumed` within ~0.5 s, and the Mac to connect within the 20 s.
   Once it does, expect `ble gate: BLE grace window closed (preferred 2
   selected 2)` and type: the keystrokes must go to the Mac and the gate must
   stay open. Then `&out OUT_USB` (hold 45 + 46, tap 0, the TAB position): `ble gate: paused (USB
   active)` again within ~0.5 s - note that this press raises no endpoint
   event if USB was already the selected transport, so it is the watchdog that
   closes the gate, still within `DELAY_MS`.
5. Let a window expire on purpose: press `&out OUT_BLE` with the Mac's
   Bluetooth switched off. After 20 s expect `ble gate: BLE did not connect,
   preferring USB again` followed by `ble gate: paused (USB active)`. Reboot
   the keyboard: the stored preference is USB again, so the `init` line reads
   `preferred 1` and the gate closes as soon as USB is ready - this is the
   regression the 2026-09-18 capture was about.
6. Unplugging USB instead of using `&out` must also produce `ble gate:
   resumed` - the selected transport stops being USB by itself.

Caveats: while USB is the selected endpoint the BLE host cannot connect at all,
so BLE OS detection and ZMK Studio over BLE are unavailable until you switch
back (USB Studio and USB OS detection are unaffected). Using BLE with the cable
plugged in only for power works, but the host has to connect inside the grace
window; if it is asleep for longer, the preference falls back to USB and has to
be re-asserted with `&out OUT_BLE`. The right half never compiles the feature
in - the Kconfig symbol `depends on ZMK_SPLIT_ROLE_CENTRAL || !ZMK_SPLIT`, and
`cornix_right`'s `.config` has no `CONFIG_CORNIX_BLE_PAUSE_ON_USB` line at all.

## 5. Bluetooth pairing failure

Symptoms on the host (macOS example): "encryption failed" right after connect,
then "LE Link disconnected ... Connection timed out" and endless connection
retries. `analyze_ble.py` shows the keyboard's view of it:

* **boot**: number of reboots (banners + `Welcome to ZMK!` + device clock
  resets). More than one boot in a short capture = the firmware resets/crashes.
* **fatal**: `>>> ZEPHYR FATAL ERROR n: <reason>` / `Halting system` /
  `ASSERTION FAIL` / Cortex-M fault dumps (`zephyr/kernel/fatal.c`,
  `zephyr/arch/arm/core/cortex_m/fault.c`). With deferred logging the fatal
  line itself is often lost when the board resets, hence the boot counter.
* **connection timeline**: `Connected <addr>` / `Disconnected from <addr>
  (reason 0x..)` (`zmk/app/src/ble.c`, host link), `Connected: <addr>` /
  `Disconnected: <addr> (reason n)` (`zmk/app/src/split/bluetooth/central.c`,
  split link), `Failed to connect to <addr> (n)`, `Security changed: <addr>
  level n` / `Security failed: <addr> level n err n` (`ble.c`,
  `split/bluetooth/peripheral.c`). HCI reasons are decoded: 0x08 supervision
  timeout, 0x13 remote user terminated, 0x16 local host terminated, 0x3d MIC
  failure (encryption key mismatch), 0x3e failed to establish;
  `bt_security_err` 2 = PIN or key missing.
* **pairing**: `pairing failed (peer reason 0x..)`, `Refusing new pairing. The
  old bond must be unpaired first.`, `SMP Timeout`, `New auth requirements ...,
  repairing` (`zephyr/subsys/bluetooth/host/smp.c`); `Rejecting pairing request
  to taken profile n`, `Pairing completed but current profile is not open`,
  `Pairing cancelled` (`zmk/app/src/ble.c`).
* **bonds / settings**: `Loaded <addr> address for profile n` and `Setting BLE
  value <key>` (`ble.c`), `set-value OK. key: bt/keys/...` (one per stored bond,
  `zephyr/subsys/settings/src/settings.c`, needs the snippet's
  `CONFIG_SETTINGS_LOG_LEVEL_DBG`), `set-value failure`, `Failed to save keys`
  / `Keys for ... have no aging counter` (`zephyr/subsys/bluetooth/host/keys.c`),
  `Failed to read ID address from storage` / `Unable to setup an identity
  address` (`zephyr/subsys/bluetooth/host/settings.c`), `Identity: <addr>`
  (`hci_core.c`), `Clearing all existing BLE bond information` (`ble.c`).
* **hints**: rule-based reading of the above.

Recovery, in order:

1. **Check the build first**: the final `.config` of every role must contain
   `CONFIG_NVS=y` and `CONFIG_SETTINGS_NVS=y` and must **not** contain
   `CONFIG_SETTINGS_NONE=y` (README "Zephyr 4.1 requirements"). A build with
   `SETTINGS_NONE` forgets its identity and bonds on every reboot, which
   produces exactly the "encryption failed / timed out" loop on the host.
   In GitHub Actions the "Kconfig file" step of the build prints it.
2. On the host, forget/remove the keyboard (macOS: System Settings >
   Bluetooth > (i) > Forget This Device).
3. On the keyboard, clear the profile that was paired to that host: `&bt BT_CLR`
   on the profile (`BT_CLR_ALL` wipes every profile). The Cornix keymap has no
   BT_CLR key any more (removed 2026-09-24 as too easy to hit by accident):
   put it back temporarily with `just remap set --layer Conn 24 '&bt BT_CLR'`,
   flash, clear, and revert. If the halves also lost each other, flash the
   `settings_reset` UF2 (`cornix_reset` for the right half,
   `reset_nicenano_nosd` for a nice!nano dongle) to **each** affected role, let
   it boot once, then flash the real firmware.
4. Flash both halves (and the dongle, if used) with firmware from the same
   build, reset them together so the split link re-forms, then pair the host
   again on a free profile.
5. Do not mix `nrf52840-nosd` and SoftDevice flash layouts between versions
   without a settings reset: the storage partition moves and the bonds become
   unreadable (README "Flashing and recovery", `bootloader/README.md`).

### 5b. LE Secure Connections: `Security failed ... err 1`

`err 1` is `BT_SECURITY_ERR_AUTH_FAIL`, and it is ambiguous on purpose:
`security_err_get()` (`zephyr/subsys/bluetooth/host/smp.c`) maps **both**
`BT_SMP_ERR_CONFIRM_FAILED` and `BT_SMP_ERR_DHKEY_CHECK_FAILED` to it. The
peripheral-side code that produces the second one -
`compute_and_check_and_send_periph_dhcheck()`, "compare received E with
calculated remote" - is

```c
if (memcmp(smp->e, re, 16)) {
        return BT_SMP_ERR_DHKEY_CHECK_FAILED;
}
```

with no log line at all, so at the default verbosity the capture shows the
failure and nothing about its cause. Two build options fill that in.

#### The `cornix-smp-debug` snippet

`snippets/cornix-smp-debug/` is Kconfig only and stacks on the other two
(list it **last**, it raises levels that `cornix-debug-log` sets to INF):

```sh
west build -s zmk/app -d .build/led/cornix_left_smp_debug -b cornix_left//zmk -p \
    -S "zmk-usb-logging cornix-debug-log cornix-smp-debug" -- \
    -DZMK_CONFIG=$REPO/config -DZMK_EXTRA_MODULES=$REPO -DSHIELD=cornix_indicator
```

It sets `CONFIG_BT_SMP_LOG_LEVEL_DBG`, `CONFIG_BT_CRYPTO_LOG_LEVEL_DBG` and
`CONFIG_BT_KEYS_LOG_LEVEL_DBG` (the `.conf` documents why `BT_ECC` and
`BT_CONN` are deliberately left alone - `bt_ecc` has no log symbol of its own,
it borrows `CONFIG_BT_HCI_CORE_LOG_LEVEL`). **The log then contains the LTK and
the DHKey in clear**; Zephyr prints a CMake warning to that effect. Treat the
capture as a secret and go back to the release firmware afterwards.

Reading a pairing attempt, in the order the lines appear:

| line (module) | what it tells you |
| --- | --- |
| `bt_smp: req: io_capability 0x.., oob_flag 0x.., auth_req 0x.., max_key_size .., init_key_dist .., resp_key_dist ..` | the host's Pairing Request. `auth_req` bit 3 = SC (LE Secure Connections), bit 2 = MITM, bit 0 = bonding. |
| `bt_smp: rsp: io_capability 0x.. ...` | our Pairing Response. The **method** is decided by these two `io_capability` values through the SMP table (`get_pair_method()`): `0x03` NoInputNoOutput on our side means Just Works, `0x02` KeyboardOnly with MITM means Passkey Entry. The chosen method is stored in `smp->method` and is never logged, so read it off here (and off ZMK's `auth_passkey_entry` / `auth_pairing_accept` callbacks). |
| `bt_smp: Remote is using Debug Public key` (INF) / `Received invalid public key` (WRN) | the peer's public key. The second one is answered with `BT_SMP_ERR_INVALID_PARAMS`, **not** AUTH_FAIL - so if you see `err 1`, this was not it. |
| `bt_crypto: u %s` / `v %s` / `x %s z 0x%x` then `res %s` (f4) | one confirm value per round: 1 round for Just Works, 20 for Passkey Entry. All 20 passing and a failure afterwards rules out `CONFIRM_FAILED`. |
| `bt_crypto: w %s` / `n1` / `n2`, `t`, `mackey`, `ltk` (f5) | `w` **is the ECDH shared secret (DHKey)**. If it is wrong, MacKey and LTK are wrong and everything below fails. |
| `bt_crypto: w`/`n1`/`n2`/`r`/`io_cap`/`a1`/`a2` then `res %s` (f6) | the DHKey check values. f6 is computed twice on the peripheral: once for the value we send, once for the value we expect from the host. |
| `bt_smp: ` (entry trace of `smp_dhkey_check`) as the **last** SMP line before the failure | the DHKey check was the step that failed: compare the second f6 `res` with the `e` the host sent. That is `BT_SMP_ERR_DHKEY_CHECK_FAILED`. |
| `bt_smp: got status 0x%x` (`smp_pairing_complete`) | the SMP error code as a number, unmapped - the unambiguous answer when it appears. |
| `bt_keys: Stored keys for <addr>` | the pairing completed and the LTK reached flash. Its absence after a "successful" pairing is a settings-backend problem, not a crypto one (see step 1 above). |

Reading order in practice: find `Security failed`, walk **backwards** to the
last `bt_smp` line, and look at the `bt_crypto` f5 `w` value. Three outcomes:

* confirm rounds fail early -> `CONFIRM_FAILED`: the passkey or the Na/Nb
  nonces disagree, i.e. a user/UX or RNG problem;
* all rounds pass, then failure right after the f6 pair -> `DHKEY_CHECK_FAILED`:
  the two sides computed different DHKeys or different MacKeys;
* `bt_ecc: Failed to generate ECC key %d` / `Raw key agreement failed %d`
  (LOG_ERR, visible at any level) -> PSA itself refused, and the number is a
  `psa_status_t`.

#### The `CONFIG_CORNIX_CRYPTO_SELFTEST` build option

The table above tells you *that* the DHKeys disagreed, not *whose* is wrong.
`CONFIG_CORNIX_CRYPTO_SELFTEST=y` (root `Kconfig`, `src/crypto_selftest.c`)
answers that part on the device, without a peer: a couple of seconds after boot
(`CONFIG_CORNIX_CRYPTO_SELFTEST_DELAY_MS`, default 2000, which is enough for
the USB CDC-ACM console to enumerate) it runs the same PSA Crypto calls the
Bluetooth host uses against RFC 5903 section 8.1 (P-256 / ECDH) and RFC 4493
section 4 (AES-CMAC) and logs:

```text
crypto selftest: psa-init PASS (psa_crypto_init=0)
crypto selftest: driver=p256-m
crypto selftest: pubkey-i PASS (X=dad0b65394221cf9..)
crypto selftest: pubkey-r PASS (X=d12dfb5289c8d4f8..)
crypto selftest: pubkey-import PASS (gi accepted as ECC_PUBLIC_KEY)
crypto selftest: ecdh-i PASS (DHKey=d6840f6b42f6edaf..)
crypto selftest: ecdh-r PASS (DHKey=d6840f6b42f6edaf..)
crypto selftest: ecdh-roundtrip PASS (shared=..)
crypto selftest: cmac-len0 PASS (bb1d6929e9593728..)
crypto selftest: cmac-len16 PASS (070a16b46b4d4144..)
crypto selftest: cmac-len40 PASS (dfa66747de9ae630..)
crypto selftest: cmac-len64 PASS (51f0bebf7e3b9d92..)
crypto selftest: random PASS (3 distinct draws, first ..)
crypto selftest: ALL PASS (12 checks)
```

* `driver=` says which P-256 implementation the build uses: `p256-m` when
  `CONFIG_MBEDTLS_PSA_P256M_DRIVER_ENABLED=y` (`BT_ECC` implies it), otherwise
  the generic mbedTLS ECP code. Building both and comparing isolates a driver
  bug from everything else: add `-DCONFIG_MBEDTLS_PSA_P256M_DRIVER_ENABLED=n`
  for the second image.
* The `ecdh-*` lines print the first 8 bytes of the shared secret; on a correct
  build they are `d6840f6b42f6edaf..`, the x-coordinate `gir^x` from RFC 5903.
* A `FAIL` line ends in the failing `psa_status_t` or the wrong value, and the
  run ends with `crypto selftest: FAILED n` instead of `ALL PASS`.

`ALL PASS` means the crypto is not the problem and the search moves to what
feeds it (nonces, addresses, the `preq`/`prsp` bytes fed to f6, or the peer).
Anything else is the answer.

The same self-test runs on the host, without Bluetooth, as
`tests/sim/crypto/selftest` (`scripts/sim/run.sh`), which is what keeps the
embedded vectors honest.

### 5c. Known limitation: passkey digits also reach the host

With `CONFIG_ZMK_BLE_PASSKEY_ENTRY=y` (this repository's passkey debug images),
the six digits typed to answer a host's passkey prompt are **also sent to the
currently selected endpoint** - on hardware they were typed into whatever window
the USB host had focused. This is a ZMK ordering bug, and it cannot be fixed
from this module.

Why. ZMK's passkey collector is a plain keycode listener that *does* try to
swallow the digits - `zmk_ble_handle_key_user()` returns
`ZMK_EV_EVENT_HANDLED` for every press and for every release while
`auth_passkey_entry_conn` is set (`zmk/app/src/ble.c`, "Key press, ignoring" and
the digit/RETURN/ESCAPE branches). But `ZMK_EV_EVENT_HANDLED` only stops
listeners that have not run **yet**: `zmk_event_manager_handle_from()` walks the
`.event_subscription` section in index order and returns as soon as a callback
returns it (`zmk/app/src/event_manager.c:20-46`). That section is filled in
plain **link order** - `zmk/app/include/linker/zmk-events.ld` has a bare
`KEEP(*(".event_subscription"))` with no `SORT`, and the entries follow the
order of `target_sources(app ...)` in `zmk/app/CMakeLists.txt`, where
`src/hid_listener.c` (line 76) comes before `src/ble.c` (line 86). The map file
of a debug build shows the result:

```sh
grep -n 'zmk_event_sub_hid_listener\|zmk_event_sub_zmk_ble' \
    .build/led/cornix_left_full_p256m_debug/zephyr/zmk.map
#   0x...a13c  zmk_event_sub_hid_listenerzmk_keycode_state_changed
#   0x...a154  zmk_event_sub_zmk_blezmk_keycode_state_changed
```

`hid_listener` runs first, presses the key and calls
`zmk_endpoint_send_report()` (`zmk/app/src/hid_listener.c:39-55`); ZMK's BLE
listener only gets to say "handled" afterwards, when the report is already on
the wire.

A listener in this module cannot get in front of it. Zephyr links module
libraries after the application's objects - this module's own subscriptions sit
at the very end of the same section (`zmk_event_sub_cornix_*` in the map above)
- so link order cannot be won, and there is no way to inject an entry ahead of
`hid_listener`'s without editing `zmk-events.ld` or ZMK's `CMakeLists.txt`.
Detecting the passkey window from outside ZMK is not possible either: it is the
static `auth_passkey_entry_conn` in `ble.c`, ZMK raises no event when a passkey
is requested, and the Zephyr callback that would tell us
(`bt_conn_auth_cb.passkey_entry`) is a **single** global registration that ZMK
already owns - `bt_conn_auth_cb_register()` would return `-EALREADY`, and
stealing it would break passkey entry outright. (The multi-subscriber
`bt_conn_auth_info_cb` only reports `pairing_complete`/`pairing_failed`, i.e.
the end of the window, never its start.)

The fix belongs upstream: move `target_sources(app PRIVATE src/ble.c)` above
`src/hid_listener.c` in `zmk/app/CMakeLists.txt`, or give the passkey listener
an explicitly ordered section entry. Both are ZMK changes, and this repository
deliberately carries no ZMK patches.

**Workaround** until then: type the passkey with the Num layer (hold 42) while
the USB host has a harmless window focused - a scratch text field or the
desktop - so the six digits that leak land somewhere they cannot do damage.
Pressing ESCAPE cancels the pairing and RETURN submits, and those leak too.
