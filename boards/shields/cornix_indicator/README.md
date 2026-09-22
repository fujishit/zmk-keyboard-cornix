# `cornix_indicator` - RGB status LEDs

Optional shield that lights the two WS2812 LEDs each Cornix half carries
(`ws2812@0`, `chain-length = <2>`, SPI3 - see
`boards/jzf/cornix/cornix.dtsi`) with battery, Bluetooth and split-link
status, as the stock RMK firmware did.

It is opt-in: the default build artifacts leave `spi3` and `ws2812@0`
disabled. Add the shield to a build target (see `build.yaml`) or pass
`-DSHIELD=cornix_indicator` locally.

## Which module this needs

`hitsmaxft/zmk-rgbled-widget`, pinned by commit in `config/west.yml`
(`a2b29334`, 2026-08-19) - **not** upstream `caksoylar/zmk-rgbled-widget`.

Upstream drives a *single* RGB LED through three GPIOs and knows nothing
about WS2812 strips, LED indices or per-profile colours. Everything this
shield configures exists only in the fork:

| Symbol | Purpose | Upstream? |
| --- | --- | --- |
| `CONFIG_RGBLED_WIDGET_WS2812` | WS2812 backend | no |
| `CONFIG_RGBLED_WIDGET_LED_COUNT` | 2 LEDs per half | no |
| `CONFIG_RGBLED_WIDGET_BATTERY_LED_INDEX` / `_CONN_LED_INDEX` | which LED shows what | no |
| `CONFIG_RGBLED_WIDGET_CONN_COLOR_BT0..BT4` | per-profile colours | no |
| `CONFIG_RGBLED_WIDGET_BATTERY_COLOR_CHARGING` | charging colour | no |
| `CONFIG_RGBLED_WIDGET_BRIGHTNESS` | strip brightness | no |
| `CONFIG_RGBLED_WIDGET_EXT_POWER_TIMEOUT_MS` | idle power-off (commit 52d51a6) | no |

One thing the fork does **not** have is a "stay lit" mode: every status is a
*timed* indication, and the only persistent path in `src/widget.c` is the
charging pulse. "Lit while on a cable" is therefore implemented here, in
`src/leds_on_usb.c` - see below.

The fork also happens to be Cornix-oriented (its `5f02956` is literally
"RGB LED Widget adapted for cornix original indication and add-ons"), and its
default profile colours are already RMK's green / red / blue.

Builds clean against ZMK `main` + Zephyr 4.1 for `cornix_left//zmk`,
`cornix_right//zmk` and `cornix_ph_left//zmk`.

## Files

| File | Applies to |
| --- | --- |
| `cornix_indicator.overlay` | enables `spi3`, `ws2812@0` and `EXT_POWER`, adds the `status-ws2812` alias |
| `cornix_indicator.conf` | settings shared by both halves |
| `Kconfig.defconfig` | this shield's own `CONFIG_CORNIX_INDICATOR_*` symbols |
| `CMakeLists.txt` | compiles `src/` into `app` when the shield is selected |
| `src/leds_on_usb.c` | keeps the LEDs lit while the half is USB-powered |
| `boards/cornix_left_nrf52840_zmk.conf` | left half (central) LED assignment |
| `boards/cornix_right_nrf52840_zmk.conf` | right half (peripheral) LED assignment |
| `boards/cornix_ph_left_nrf52840_zmk.conf` | left half in dongle mode (peripheral) |

A shield may carry a `CMakeLists.txt`: Zephyr walks `${SHIELD_DIRS}` in
`zephyr/boards/shields/CMakeLists.txt` and `add_subdirectory()`s every selected
shield that has one. That is how `src/leds_on_usb.c` reaches the build without
touching the repository's own Zephyr module (root `CMakeLists.txt` /
`zephyr/module.yml`), `config/west.yml` or `build.yaml`, and without a fork of
zmk-rgbled-widget: nothing here is compiled unless `-DSHIELD=cornix_indicator`
is passed. Sources go into the `app` target, like every ZMK module, because
`<zmk/...>` is `PRIVATE` to `app`.

Zephyr looks for `<board>_<soc>_<variant>.conf` in a shield's `boards/`
directory - a bare `cornix_left.conf` there is silently ignored. See
`zephyr_build_string()` in `zephyr/cmake/modules/extensions.cmake`.

## LED assignment

The widget addresses LEDs by their position in the WS2812 chain, so index 0
is whichever LED is wired first.

| Half | LED 0 | LED 1 |
| --- | --- | --- |
| left (`cornix_left`, `cornix_ph_left`) | connectivity | battery |
| right (`cornix_right`) | battery | connectivity |

If the two LEDs come out mirrored on your unit, swap
`CONFIG_RGBLED_WIDGET_BATTERY_LED_INDEX` and
`CONFIG_RGBLED_WIDGET_CONN_LED_INDEX` in that half's `boards/*.conf`. That is
the only change needed; nothing else depends on the order.

## RMK behaviour -> what ZMK does

### Left half

| # | RMK behaviour | ZMK / `cornix_indicator` | Match |
| --- | --- | --- | --- |
| 1 | left LED green / red / blue = BT profile 0 / 1 / 2 | same colours, `CONN_COLOR_BT0..BT2` | exact |
| 2 | left LED slow blink = searching | profile open -> **breathing pulse** in the profile colour for 5 s (`CONN_ADV_DURATION_MS`), then dark | close; pulse instead of blink, and it stops after 5 s instead of running until a host connects |
| 3 | left LED one flash then off = connected | profile colour static for 1.5 s (`CONN_CONNECTED_DURATION_MS`), then dark - **steady, indefinitely, on USB power** | exact |
| - | (not in RMK) profile lost while not advertising | profile colour blinks 500 ms on/off for 3 s | extra |
| 4 | right LED red blink = battery low | red breathing pulse at or below 20 %, repeated on every battery report (60 s) | close; pulse rather than square blink |
| 5 | right LED slow green blink = charging | green breathing pulse while USB power is present and the level is below 99 % | close |
| 6 | right LED green then off = charged | **green static, indefinitely**, at or above 99 % on USB power | better |
| 7 | right LED slow blue blink = right half link lost | **not available** - see below | missing |
| 8 | right LED blue flash then off = right half connected | **not available** - see below | missing |
| - | (not in RMK) boot | battery colour on the battery LED (green > 80 %, yellow 20-80 %, red <= 20 %), then the BT state on the other LED | extra |

### Right half

| # | RMK behaviour | ZMK / `cornix_indicator` | Match |
| --- | --- | --- | --- |
| 9 | left LED = battery, same scheme as above | rows 4-6 apply unchanged | close |
| 10 | right LED blue slow blink = link to left half lost | blue, 500 ms on/off, for 3 s after the link drops (`CONN_COLOR_DISCONNECTED`, `CONN_DISCONNECTED_DURATION_MS`) | close; blinks for 3 s rather than until reconnect |
| 11 | right LED blue flash = left half connected | blue static for 1.5 s, then dark | exact |

`cornix_ph_left` (left half driven by a dongle) is a peripheral too, so its
connectivity LED behaves like rows 10-11, with the dongle as the far end.

### What could not be matched

* **Split-link status on the left half (rows 7 and 8).** The module only
  indicates split connection state on the *peripheral* side: its
  `zmk_split_peripheral_status_changed` subscription is compiled under
  `#elif IS_ENABLED(CONFIG_ZMK_SPLIT_BLE)`, i.e. only when the build is not a
  central. On a central it shows the BLE *host* profile and nothing else.
  The right-hand LED of the left half therefore shows battery only. The one
  workaround the module offers - `CONFIG_RGBLED_WIDGET_BATTERY_SHOW_PERIPHERALS`,
  which paints the battery LED magenta when a peripheral's battery cannot be
  read - is inert in WS2812 mode, because `indicate_battery_enhanced()` reads
  the local battery only. Fixing this properly means a patch to the module.
* **Blink vs. pulse.** Where RMK blinked (hard on/off), the WS2812 backend
  mostly uses a breathing pulse. The animation type per status is hard-coded
  in `src/widget.c`; only colours and durations are configurable.
* **"Until it changes" vs. timed.** RMK kept blinking while searching or
  while the link was down. On battery every indication here has a duration,
  after which the LED goes dark and the WS2812 rail is cut. Raise
  `CONFIG_RGBLED_WIDGET_CONN_ADV_DURATION_MS` /
  `CONFIG_RGBLED_WIDGET_CONN_DISCONNECTED_DURATION_MS` to trade battery for a
  longer hint. On USB power this no longer applies - see
  "USB-powered vs. battery" below.
* **Battery indication is event-driven.** Off charge, the level is only
  re-shown when ZMK reports a change (`CONFIG_ZMK_BATTERY_REPORT_INTERVAL`,
  60 s on the left half) and only while at or below the critical threshold.
  There is no continuous fuel gauge, in either firmware.
* **Battery thresholds.** `CONFIG_RGBLED_WIDGET_BATTERY_LEVEL_CRITICAL` is
  deliberately set equal to `_LEVEL_LOW` (20 %), because the module only
  animates at or below *critical* and shows a steady colour between critical
  and low. RMK had a single "low" state, so this collapses the two.

## USB-powered vs. battery

The two behaviours differ on purpose: on a cable the LEDs cost nothing and a
permanent "which output am I on / am I charged yet" readout is worth having; on
battery every lit millisecond is current.

| | on USB power (cable plugged in) | on battery |
| --- | --- | --- |
| connectivity LED, USB is the selected output | **cyan, steady, indefinitely** | n/a |
| connectivity LED, BLE profile connected | **profile colour, steady, indefinitely** | profile colour for 1.5 s, then dark |
| connectivity LED, profile advertising | profile colour, breathing, **until the state changes** | profile colour, breathing, 5 s, then dark |
| connectivity LED, profile lost | profile colour, 500 ms blink, **until the state changes** | profile colour, 500 ms blink, 3 s, then dark |
| connectivity LED, peripheral half | link colour (blue), **steady / blinking indefinitely** | blue for 1.5 s (linked) or 3 s of blink (lost), then dark |
| battery LED, charging below 99 % | green, breathing, indefinitely | n/a |
| battery LED, at or above 99 % | **green, steady, indefinitely** | n/a |
| battery LED, level report | n/a (a cable means charging) | level colour for 2 s (green / yellow / red), then dark; red breathes at or below 20 % |
| WS2812 rail (`EXT_POWER`) | **stays on** | cut 1 s after the last indication ends |
| idle timeout (no typing) | LEDs come back ~250 ms after the widget blanks them | LEDs stay off |
| deep sleep | rail off (not fought) | rail off |

`CONFIG_CORNIX_INDICATOR_LEDS_ON_USB=y` (default) is what makes the left
column happen; set it to `n` to get the right column in both cases, i.e. the
behaviour this shield had before.

### How it works

`src/leds_on_usb.c` adds no LED logic of its own. It subscribes to the same
events the widget does (`zmk_usb_conn_state_changed`, `zmk_endpoint_changed`,
`zmk_ble_active_profile_changed`, `zmk_split_peripheral_status_changed`,
`zmk_battery_state_changed`, `zmk_activity_state_changed`) and, **only while
`zmk_usb_is_powered()`**, re-asserts the state the widget just rendered through
its public API with a *zero* duration:

```c
ws2812_set_status_led(STATUS_CONNECTIVITY, color, /* duration_ms */ 0, true);
```

A zero duration is what the widget treats as persistent: `set_led_with_sharing()`
then leaves `is_shared = false` and `share_end_time = 0`, so
`check_shared_led_timeouts()` never returns the LED to black, and
`ws2812_strip_can_power_off_locked()` sees a non-black strip and never arms the
`ext_power` off timer. The animation the widget installed is deliberately left
alone, which is why "advertising" keeps breathing and "lost" keeps blinking
instead of freezing.

The re-assert is delayed by `CONFIG_CORNIX_INDICATOR_LEDS_ON_USB_DELAY_MS`
(250 ms) so that it lands after the widget's own 16 ms-debounced render - and,
on an activity change, after the widget has blanked every LED and cut the rail,
which is what brings them back on idle. The first one after boot waits
`CONFIG_CORNIX_INDICATOR_LEDS_ON_USB_BOOT_DELAY_MS` (4 s), because the widget's
init thread spends `BATTERY_BLINK_MS + INTERVAL_MS` on the battery before it
shows connectivity.

The battery LED is only pinned at or above 99 %, plus once after an idle blank:
below "full" the widget already holds it with a persistent green pulse of its
own, and re-asserting would only glitch one animation frame - but after the
widget's own idle blank there is no pulse left to protect and the LED has to be
brought back at any level.

When the cable is pulled, the pin is cancelled and `indicate_connectivity()` is
called once, which puts the connectivity LED back on a timed
`CONN_*_DURATION_MS` indication; the widget's own USB handler does the same for
the battery LED. Everything then expires and the rail is cut as before.

`CONFIG_RGBLED_WIDGET_CONN_SHOW_USB` is `y` for this to be useful: it is what
paints the USB endpoint cyan instead of black, and - less obviously - it is
also what compiles the widget's `zmk_endpoint_changed` subscription at all.

## Power

`CONFIG_RGBLED_WIDGET_EXT_POWER_TIMEOUT_MS=1000` (from commit 52d51a6) is
kept: 1 s after the last animation ends and no LED is left lit, the widget
turns off the WS2812 rail through the board's `EXT_POWER` node. LEDs that are
intentionally kept lit are not turned off - which is exactly the lever
`src/leds_on_usb.c` pulls while a cable is plugged in, and why the table above
has two columns. `CONFIG_RGBLED_WIDGET_BRIGHTNESS` is 64/255 for the same
reason. RGB still costs more than no RGB, on battery.

## Building

```sh
# regular
west build -s zmk/app -d build/led_left -b 'cornix_left//zmk' -p -- \
  -DZMK_CONFIG=<repo>/config -DZMK_EXTRA_MODULES=<repo> -DSHIELD=cornix_indicator

# with USB logging (the widget logs every state change at INF level)
west build -s zmk/app -d build/led_left_debug -b 'cornix_left//zmk' -p \
  -S "zmk-usb-logging cornix-debug-log" -- \
  -DZMK_CONFIG=<repo>/config -DZMK_EXTRA_MODULES=<repo> -DSHIELD=cornix_indicator
```

CI targets are in `build.yaml` (`cornix_left_indicator`,
`cornix_right_indicator`, `cornix_left_for_dongle_indicator`) and
`build-debug.yaml` (same names with `_debug`).

Flash indicator firmware to **both** halves, or neither: the two halves do not
have to agree, but a half without it simply keeps its LEDs dark.
