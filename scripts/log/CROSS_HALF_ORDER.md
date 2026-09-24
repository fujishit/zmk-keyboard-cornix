# Cross-half transposition ("hare" -> "ahre"): is it real, why, what helps

Question (2026-09-23): a LEFT-half key sometimes overtakes an EARLIER right-half key.
Data: `logs/right-20260921-162158.log` + `logs/left-20260921-162216.log` (29 h, 5636 right
events, 7490 left events; images `cornix_right_tx8_log` / `cornix_left_full_p256m_debug`,
PREF_LATENCY=0), BabbleSim, ZMK main in `.sim/ws/zmk`. Tools here: `crosshalf.py` (field
logs), `bsim_order.py` + `bsim/` (simulation), `samehalf.py`, `holdoff-module/` (prototype fix).
The 2026-09-17 pair is unusable for ordering (Mac reconnect storm, 25 of 1236 events delivered).

## Verdict

**Real, and structural.** The central has no timestamp for remote keys: its own kscan events
are applied at once (`app/src/physical_layouts.c:266-303`, timestamp = `k_uptime_get()`), a
right-half key is applied when its GATT notification arrives
(`app/src/split/bluetooth/central.c:365-389` -> `peripheral_event_msgq` -> `:1284-1290` ->
`app/src/split/central.c:41-47`, timestamp = arrival time). Whenever the split delay of the
right key (right debounce 3 ms + 0-7.5 ms wait for the next connection event + retries)
exceeds the typing gap to the next left key, the left key is applied first. Nothing in ZMK
reorders by time; `keymap.c:835` applies events in raise order.

Field numbers (`crosshalf.py`, right event times mapped onto the left clock via the split
delay, +-1 ms):
* 849 consecutive right->left press pairs (< 200 ms). **13 reordered (1.5 %)**; 0 of 799
  left->right pairs (sanity check).
* 12 of the 13 sit in the RF-marginal evening (20:52, 21:01-21:18; right delay 36-600 ms,
  p50 81 ms) at ordinary gaps of 26-108 ms (p50 62, p95 95) -- i.e. at this user's normal
  roll speed (median right->left gap ~70 ms). Same session as yesterday's lost keys (RSSI
  -85 dBm, retransmissions). Per hour: clean hours p50 5 / p95 9 ms delay, 0-1 reorder;
  21h p50 11.4 / p95 56.5 ms, 11 reorders.
* 1 with a normal link: 10:42:43 `K` then `E`, gap 1.3 ms, delay 2.5 ms (practically a
  chord); 21:17:58 `ENTER` then `A`, gap 2.4 ms, delay 15.6 ms.
* Exposure with a normal link is low because the user rarely rolls across hands faster
  than 15 ms (6 of 849 pairs; 3 under 5 ms). But when it happens the odds are high: the
  probability that a right key is still in flight g ms after it was confirmed (n=2744,
  delay <= 30 ms) is 63 % at 5 ms, 29 % at 8 ms, 16 % at 10 ms, 4.6 % at 15 ms, 2.2 % at 20.
* BabbleSim (ideal radio, 7.5 ms interval, `bsim/central-local`: a left key scripted 4-19 ms
  after each of 20 right keys): **4/20 and 3/20 reordered** (seeds 23, 101; gaps 4.0-10.2 ms,
  delays 4.7-10.6 ms), exactly gap < delay. Deterministic, no RF needed.

So: "tenki -> tenik" style same-half swaps do not occur (below); "hare -> ahre" occurs
(a) routinely when the link is degraded (the evening symptom, any speed), and (b) rarely on a
good link, only for cross-hand rolls under ~8 ms.

Same-half (a)/(b)/(c), for completeness (`samehalf.py`): 10 right presses < 2 ms apart in 29 h,
all delivered as separate bitmaps in the right order (the central XORs bitmaps and emits
ascending positions, `central.c:373-389`, but the right sends one bitmap per event,
`service.c:230-249`); only 1 coalesced bitmap in the whole capture, at 21:20:39 inside the
queue-overflow collapse. Same-scan coalescing (two keys confirmed in the same 1 ms pass are
emitted row-major, `kscan_gpio_matrix.c:265-275`) needs presses within ~1 ms; 0 identical
timestamps seen. The kscan msgq (`physical_layouts.c:277`, size 4) drops, never reorders.

## Mitigations

**(i) Central-side local hold-off -- recommended.** Capture every LOCAL
`zmk_position_state_changed` before the keymap and re-raise it D ms later from the system
work queue (same thread that raises remote events, so no locking; FIFO keeps local order;
releases are held too, so tap lengths are unchanged; `ev->timestamp` stays the kscan time so
hold-tap/tapping-term logic is unaffected). Prototype: `holdoff-module/src/local_holdoff.c`
(~100 lines), pattern copied from hold-tap (`behavior_hold_tap.c:152-160,182-236,780`:
`copy_raised_zmk_position_state_changed()`, return `ZMK_EV_EVENT_CAPTURED`, later
`ZMK_EVENT_RELEASE()` which continues at `last_listener_index+1`, `event_manager.c:80-82`).
Hook point: nothing in ZMK is touched; only the listener order matters. Verified in
`.build/led/cornix_left_full_p256m_debug/zephyr/zmk.map` (`.event_subscription`): sources
compiled into `app` by a module/shield (`target_sources(app PRIVATE ...)`, as
`boards/shields/cornix_indicator/CMakeLists.txt` does) link BEFORE `keymap.c.obj`; this
repo's `zephyr_library()` objects (`ble_usb_gate.c`, `behavior_app_switch.c`) link AFTER it
and could not capture. Gate: hold only while a split peripheral is connected, polled through
`STRUCT_SECTION_FOREACH(zmk_split_transport_central, t)` -> `t->api->get_status()`
(`split/central.c:171`, `bluetooth/central.c:1236`); no central-side connect event exists
(`zmk_split_peripheral_status_changed` is raised only on the peripheral, `peripheral.c:90`).
Result in BabbleSim with D = 8: **0/20 reordered** in both seeds, local latency +8.06 ms.
On the field data D = 8 fixes 3 of 13 and creates 1 (a left->right roll where the right key
landed < 8 ms after the left press; 1 % of left->right pairs have a margin < 18.6 ms), D = 15
fixes 6 / creates 4, D = 20 fixes 7 / creates 10. The other reorders need 30-540 ms and are a
link problem, not a hold-off problem.
Cost: +D ms on every left key (today the left has ~0 ms beyond its 5 ms debounce, the right
2-15 ms; D = 8 makes the halves roughly equal). Choose D = 8 (one connection interval +
0.5 ms); 10 if the tail matters more than feel. Integration in this repo: `Kconfig` symbols
`CORNIX_LOCAL_HOLDOFF{,_MS,_QUEUE,_ONLY_WHEN_CONNECTED}` (module `Kconfig`), `target_sources(app
PRIVATE src/local_holdoff.c)` in the root `CMakeLists.txt` (not `zephyr_library_sources`),
`CONFIG_CORNIX_LOCAL_HOLDOFF=y` + `CONFIG_CORNIX_LOCAL_HOLDOFF_MS=8` in `config/cornix_left.conf`.

**(ii) Adaptive variants.** "Hold only while a right key is down": covers 5 of the 13 field
cases -- useless for the first letter of a word ("h" of "hare"), because the central does not
yet know the right key exists. "Hold only after recent remote activity": 12 of 13 had a
remote event within 300 ms, but the 13th (first right key after a pause) is exactly the
word-initial case, so the window would have to be seconds long, i.e. "always while typing".
"Hold only while the link is slow": the central cannot measure the split delay (no
timestamps, no retransmission counter exposed by the host stack). Keep (i) simple; the
connected gate is the only adaptive part worth having.

**(iii) Peripheral timestamps.** The BLE transport sends the whole 16-byte position bitmap per
event (`service.c:217-249`, `POS_STATE_LEN`) and the central XORs it; a per-event record with a
peripheral `k_uptime` delta would need a new characteristic/payload on both halves, a clock
offset estimate on the central (min over recent events, as `analyze_drops.py` does), and a
reorder buffer that holds local AND remote events until their true order is known -- which
is the hold-off again, plus 200-300 lines in ZMK core on both halves and a fork to maintain.
Not worth it: it can only ever reorder within the buffer window, and the evening cases
(100-600 ms late) would still come out wrong or unbearably late.

**(iv) Reduce the split delay.** Already at the floor: 7.5 ms interval
(`CONFIG_ZMK_SPLIT_BLE_PREF_INT=6`, BLE minimum), latency 0, 2M PHY, right press debounce
3 ms (`boards/jzf/cornix/Kconfig.defconfig:45`). The remaining 0.5-8 ms is the connection-event
phase and cannot be removed. RF margin is what makes the evening tail (see FAST_TYPING.md:
+8 dBm, queue sizes, move the USB-3 gear). Zero-code partial option: raise the left press
debounce, `CONFIG_ZMK_KSCAN_DEBOUNCE_PRESS_MS=10` in `config/cornix_left.conf` (left is -1 ->
DT default 5, `zmk,kscan-gpio-matrix.yaml:22`): a 5 ms press-only hold-off, no release
delay, no code; fixes the < 5 ms rolls only (2 of 13 in the replay). Not a substitute for (i).

**Recommendation:** (i) with D = 8 and the connected gate, plus the RF/queue items from
FAST_TYPING.md for the evening link. Verify on hardware with `crosshalf.py` (expect the
`REORDERED` count to drop to the > D-margin cases only and `left->right ... p1` margin to
stay > D).

## Files here
`crosshalf.py` (+ `analyze_drops.py` copy it imports; `--floor-ms`, `--hold`, `--host-shift-s`),
`crosshalf-0921.txt/.json`, `samehalf.py`, `bsim_order.py`, `bsim/central-local/` (keymap
+ conf), `bsim/build.sh` (builds under `bsim/build`, runs against `.build/bsim/peripheral`),
`bsim/logs/{plain,hold8}-{23,101}`, `holdoff-module/` (module.yml, CMakeLists, Kconfig, src).
