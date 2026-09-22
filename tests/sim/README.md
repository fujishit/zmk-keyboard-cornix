# Cornix host-simulation tests

These tests build ZMK for Zephyr's host board (`native_sim//zmk_test_mock`,
the same target ZMK's own `app/run-test.sh` uses) with ZMK's mock kscan
driver, replay a scripted sequence of key presses, and diff the resulting HID
events against a stored snapshot. No hardware, no Zephyr SDK and no root
access are required.

## Quick start

```sh
scripts/sim/bootstrap.sh   # once: .sim/venv (west, cmake, ninja) + .sim/ws (zmk, zephyr, modules)
scripts/sim/run.sh         # build + run every case under tests/sim
scripts/sim/run.sh --no-build --verbose tests/sim/user-keymap   # fast re-run of a subset
scripts/sim/run.sh --auto-accept tests/sim/user-keymap/new-case # (re)generate a snapshot
```

`run.sh --help` lists all options and the `SIM_WS` / `ZMK_APP_DIR` /
`SIM_VENV` environment overrides used in CI (where the workspace is prepared by
`west init -l config && west update` instead of `bootstrap.sh`).

Build output lands in `.build/sim/<case>/`; `keycode_events_full.log` there is
the complete debug log of a run, which is the thing to read when a snapshot
does not match.

## Layout

```
tests/sim/
  _common/
    cornix_mock.dtsi     mock kscan (4x14) wired to the board's layout_50 / default_transform
    board_keymap.dtsi    cornix_mock + boards/jzf/cornix/cornix.keymap (board default keymap)
    user_keymap.dtsi     cornix_mock + config/cornix.keymap             (user keymap)
  board-keymap/<case>/   cases exercising the board's default keymap
  user-keymap/<case>/    cases exercising the user's keymap
  crypto/<case>/         cases exercising this module's own code, not a keymap
```

Every case directory follows the upstream ZMK convention:

| file                      | purpose                                                          |
| ------------------------- | ---------------------------------------------------------------- |
| `native_sim.keymap`       | required; marks the directory as a case. Includes a `_common` fixture and sets `&kscan { events = <...>; }` |
| `events.patterns`         | required; `sed -n` script selecting/rewriting log lines to compare |
| `keycode_events.snapshot` | required; expected output (generate with `--auto-accept`)        |
| `native_sim.conf`         | optional Kconfig fragment (e.g. `CONFIG_ZMK_POINTING=y`)         |
| `native_sim.overlay`      | optional extra devicetree overlay                                |
| `extra-cmake-args`        | optional; extra `-D...` arguments, one per line                  |
| `pending`                 | optional marker; a mismatch is reported as PENDING, not FAILED   |

## The cases

| case | what it pins down |
| --- | --- |
| `crypto/selftest` | `CONFIG_CORNIX_CRYPTO_SELFTEST` (`src/crypto_selftest.c`): P-256 / ECDH against RFC 5903 8.1, AES-CMAC against RFC 4493 4, a random key-pair round trip and a non-constant RNG - all through the PSA calls Zephyr's Bluetooth host uses |
| `board-keymap/basic-press` | the board's own keymap produces the expected keycode |
| `board-keymap/encoder` | the board keymap's encoder bindings |
| `board-keymap/layer-mo` | `&mo` on the board keymap |
| `user-keymap/basic-press` | `config/cornix.keymap` Base layer key |
| `user-keymap/encoder` | `&inc_dec_kp` / `&inc_dec_msc`. The left encoder turns one slow step each way (`C_VOL_UP` / `C_VOL_DN`); the right one is *flicked* three detents each way, 20 ms apart - faster than the 24 ms `tap-ms`, so the serial behaviour queue is the bottleneck. Pins down the 2026-09-19 scroll tuning: `ZMK_POINTING_DEFAULT_SCRL_VAL = 188` + `tap-ms = <24>` gives **3 wheel units per detent** (`188 * 16 / 1000 = 3.008` on the first `&msc` tick) instead of the old 1 unit per 150 ms |
| `user-keymap/layer-num` | `&mo 2` on key position **42** reaches the Num layer (layer **2**, because the Win overlay at index 1 shifted every Vial `MO()` up by one) |
| `user-keymap/mouse-click` | `&mkp MCLK` on the inner key |
| `user-keymap/mouse-layer` | the mouse on Fn3 (hold position 45): `&mmv MOVE_UP` on 8, the three clicks on the left thumb keys 41/42/43 (`&mkp LCLK`/`RCLK`/`MCLK` -> Button 0/1/2) and the two speed keys 2/3 (W/E, `&mo 6`/`&mo 7`), whose `&mmv_input_listener` overrides scale the same 200 ms hold of position 8 to 2x (70 units) and 1/2 (17 units) of the plain 35 units. Position 49 is a plain `&kp RIGHT` (`0x4F`) again |
| `user-keymap/symbols` | the programming symbols on Num (hold position 42): `` ` `` (`0x35`) on 13, `[`/`]` (`0x2F`/`0x30`) on 20/21 and `{`/`}` on 34/35 - the last two are `LS(LBKT)`/`LS(RBKT)`, so they show the same `0x2F`/`0x30` usage with `implicit_mods 0x02` |
| `user-keymap/fn2-keys` | the function row and navigation cluster on Fn2 (hold position 46 = `RC(3,10)`): `F1` (`0x3A`) on 1 and `F12` (`0x45`) on 0, plus `HOME`/`PG_DN`/`PG_UP`/`END` (`0x4A`/`0x4E`/`0x4B`/`0x4D`) on H/J/K/L (18/19/20/21) and `PSCRN`/`INS` (`0x46`/`0x49`) on 22/23 |
| `user-keymap/conn-layer` | the Conn layer (layer 5, 2026-09-22): a `conditional_layers` node (`if-layers = <3 4>; then-layer = <5>;`) switches it on while Fn3 (45 = `RC(3,11)`) and Fn2 (46 = `RC(3,10)`) are both held. The snapshot shows `conditional layer on: 5`, the two `&out` presses on 0 / 12 resolving on `layer_id: 5` (binding `outputs`) and reaching `zmk_endpoint_set_preferred_transport` with 1 (USB) then 2 (BLE) - native_sim has no transport, so nothing actually switches - Backspace (11) and Space (44) falling through 5 → 4 → 3 → 0, and `conditional layer off: 5` when 46 is released. It also pins down that 46 on Fn3 and 45 on Fn2 are `&trans` (they were `&none`, which made the second thumb key dead). `&bt BT_SEL` is not pressed: without `CONFIG_ZMK_BLE` the `bt` behavior has no driver on native_sim |
| `user-keymap/app-switch` | `&app_tab` on position 0 of the Win layer, this module's `zmk,behavior-cornix-app-switch` (`src/behavior_app_switch.c`): holding 41 (LCTRL here) and tapping 0 masks that Ctrl out of the report, presses LALT (`0xE2`) as a *real* modifier and taps TAB (`0x2B`); a second tap sends TAB only, so Alt stays held and the Windows switcher stays open; releasing 41 releases `0xE2`. Pressed with nothing held it is a plain `&kp TAB`. The Win layer is switched on with the keymap's own `&tog 1`, so the case needs no OS detection |
| `user-keymap/os-detect-windows` | the USB fingerprint classifier plus the Win overlay: with layer 1 active the physical LGUI key (39) and the thumb rest key (41) send LCTRL (`0xE0`), while the physical Ctrl key (38) is `&trans` and still sends LCTRL from Base; it ends with the same `&app_tab` chord as the `app-switch` case |
| `user-keymap/os-detect-macos` | the same fingerprints, Base layer: positions 39 and 41 send LGUI (`0xE3`), position 38 sends LCTRL (`0xE0`), and 41 + 0 is plain `LGUI` + `TAB` - the macOS half of the app-switch chord, which needs no behavior at all |

## The OS-detection cases

`user-keymap/os-detect-*` build
[zmk-feature-os-detection](https://github.com/cormoran/zmk-feature-os-detection)
with `CONFIG_ZMK_OS_DETECTION_TEST_INJECT=y`. native_sim has no USB device
controller, so the module replays four recorded real-hardware enumeration
sequences through `zmk_os_detection_observe_setup()` at boot
(`src/os_detection_usb.c`, `os_detection_test_inject_init()`), in a fixed
order, all in one process: Windows, macOS, Linux, Apple-with-remote-wakeup.
Both snapshots therefore start with the same four lines

```
zmk: os detection: USB fingerprint settled on 1   # ZMK_OS_WINDOWS
zmk: os detection: USB fingerprint settled on 2   # ZMK_OS_MACOS
zmk: os detection: USB fingerprint settled on 3   # ZMK_OS_LINUX
zmk: os detection: USB fingerprint settled on 2   # ZMK_OS_MACOS (Mac/iPhone)
```

There is no way to pick one scenario, and the injection finishes before the
mock kscan sends its first event, so the two cases differ only in which layer
is active when the keys are pressed.

**The automatic layer switch itself cannot run here.**
`zmk_os_detection_current()` (`src/os_detection_core.c`) only reports the USB
result while the USB endpoint is *selected*, and on native_sim no transport is
ever ready - the full log shows ZMK's own
`Preferred endpoint transport is 1 but no transports are ready` - so the
endpoint stays `ZMK_TRANSPORT_NONE`, `zmk_os_changed` is never raised and
`os_detection_layer_listener()` never fires. The Windows case therefore
activates layer 1 through the manual fallback on the keymap, `&tog 1` on Fn3
(hold position 45 = `RC(3,11)`, then tap position 0), which ends in the same
`zmk_keymap_layer_activate(1, false)` call the module makes. Whether detection really drives the layer has to be checked on
hardware.

The `events.patterns` of these cases keep
`zmk_keymap_apply_position_state:` lines, so the snapshot records the layer
each key press resolved on (`layer_id: 1` vs `layer_id: 0`) as well as the
resulting HID usage.

## Adding a case

1. Create `tests/sim/<group>/<name>/`.
2. Write `native_sim.keymap`:

   ```dts
   #include "../../_common/user_keymap.dtsi"   /* or board_keymap.dtsi */

   &kscan {
       events = <
           ZMK_MOCK_PRESS(1,1,50)       /* row, column, delay-after in ms */
           ZMK_MOCK_RELEASE(1,1,10)
       >;
   };
   ```

   Rows/columns are matrix coordinates of the Cornix `default_transform`,
   not key positions. `_common/cornix_mock.dtsi` has a cheat sheet; in short
   the left hand is `RC(row, col)` and the right hand is `RC(row, 12 - j)`
   with `j = 0` the innermost column. `python3 scripts/remap.py show`
   prints the key positions of the user keymap with their bindings.
3. Write `events.patterns`. The most common one is

   ```
   s/.*hid_listener_keycode_//p
   ```

   which yields lines like
   `pressed: usage_page 0x07 keycode 0x14 implicit_mods 0x00 explicit_mods 0x00`.
   Other useful patterns: `s/.*mo_keymap_binding_/mo_/p` (momentary layers)
   and `s/.*decide_hold_tap/ht_decide/p` (hold-tap decisions). Look at the
   `LOG_DBG` calls in `zmk/app/src` for more.

   The modifier *report* state is not in those lines - `implicit_mods` /
   `explicit_mods` are what the event carried, not what the host will see.
   For a case that turns on masking (`user-keymap/app-switch`,
   `user-keymap/os-detect-windows`) add

   ```
   s/.*hid_masked_modifiers_set.*Modifiers set to /mask mods: Modifiers set to /p
   s/.*hid_masked_modifiers_clear.*Modifiers set to /unmask mods: Modifiers set to /p
   ```

   which prints `zmk/app/src/hid.c`'s `SET_MODIFIERS()` result, i.e. the
   modifier byte of the report that is about to go out.
4. Run `scripts/sim/run.sh --verbose tests/sim/<group>/<name>` and read the
   output. Only when it matches what the keymap is supposed to do, store it
   with `--auto-accept`. Do not accept a snapshot you have not read.

Keycodes in the snapshot are raw HID usages (`0x04` = A ... `0x1D` = Z,
`0x1E` = 1, `0xE0` = Left Control, usage page `0x0C` = consumer keys such as
volume).

## Timing notes

- `ZMK_MOCK_PRESS(r,c,d)` waits `d` ms *after* the event - and the delay of
  the **first** event is also the delay *before* it, because ZMK's mock driver
  reads `events[0]`'s millisecond field twice
  (`zmk/app/module/drivers/kscan/kscan_mock.c`,
  `kscan_mock_schedule_next_event_*` runs before `event_index++`).
- The mock kscan exits the process after the last event; use a long delay on
  the last event when waiting for something that happens later (e.g. encoder
  or debounce work).
- To wait without generating a key event, use a matrix cell that
  `default_transform` does not map - `RC(0,6)`, `RC(0,13)`, `RC(1,6)`,
  `RC(2,13)`, `RC(3,6)` and `RC(3,13)` are free on the Cornix 50-key layout.
- `config/cornix.keymap` has **no hold-tap at all** since 2026-09-19, when the
  mouse moved onto Fn3 and position 49 went back to `&kp RIGHT`: the
  `&lt 6 RIGHT` layer-tap and the keymap's `&lt` override
  (`flavor = "balanced"`, `tapping-term-ms = <200>`, `quick-tap-ms = <200>`)
  are both gone, so no case has to wait out a tapping term any more.
  `config/keymaps/cornix-homerow-mods.keymap` (kept but not built) still has
  home-row mods, where a case holding one has to wait out the 200 ms term
  before pressing the second key and leave >200 ms between a release and a
  following press of the same key.
- A case that compares pointer speeds must hold the `&mmv` key for the same
  time in every run, because `&mmv` accelerates (`time-to-max-speed-ms = 300`,
  `acceleration-exponent = 1`). `user-keymap/mouse-layer` uses 200 ms
  everywhere and its `events.patterns` drops the `Mouse scroll set to` lines
  and the `Mouse movement set to 0/0` line that follows every report, so the
  snapshot is one line per real movement and the three runs can be summed by
  eye.
- `&msc` does **not** accelerate (`acceleration-exponent = <0>`), so a scroll
  case does not have to hold anything for a fixed time: the full amount lands
  on the first tick, 16 ms after the press. What a scroll case does have to
  mind is that `zmk,behavior-sensor-rotate-var` pushes every detent through
  the *serial* behaviour queue, one `tap-ms` each - so with `tap-ms = <24>`
  the reports come out 25 ms apart however fast the mock encoder turns.
- `zmk,sensor-encoder-mock` (`zmk/app/module/drivers/sensor/encoder_mock`)
  fires its first event `event-startup-delay` ms after boot and the rest
  `event-period` ms apart, one per entry of `events` (degrees; with
  `triggers-per-rotation = <20>` one detent is 18). Give the two encoders
  different startup delays so their output does not interleave.
- The timestamps are stripped from `keycode_events_full.log` (`run.sh` runs
  `sed -e "s/.*> //"`). To measure *when* something happened, run the
  executable directly: `.build/sim/<case>/zephyr/zmk.exe | grep ...`.
