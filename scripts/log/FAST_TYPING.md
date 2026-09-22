# Fast typing on the right half: does a key ever get lost?

Question (2026-09-22): "when I type fast, very rarely the right half seems to
lag and a key does not go in -- or maybe I imagine it." Right half = split BLE
peripheral (`firmware/keymap/cornix_right_tx8_log.uf2`, +8 dBm, USB logging),
left half = central (`cornix_left_full_p256m_debug.uf2`, `PREF_LATENCY=0`).
Evidence: `logs/right-20260921-162158.log` + `logs/left-20260921-162216.log`
(29 h, 5636 right-half key events), the 2026-09-17 pair, BabbleSim
(`tests/bsim/README.md`, "Fast typing"), and the ZMK/Zephyr sources in `.sim/ws`.
Tool: `scripts/log/analyze_drops.py` (new; pairs the right half's key events
with the position bitmaps the left half received).

## (a) Is it real?

Yes -- but it is a **link-loss** effect, not a typing-speed effect.

* Normal operation loses nothing and shows no burst penalty. 10:30-10:50
  (874 events, 105 of them within 60 ms of the previous one): 874 delivered,
  delay above best case p50 3.9 / p95 7.4 / p99 13.9 / max 27.9 ms; fast-typed
  events p50 4.1 / p95 7.9 / max 25.4 ms. A 4-key chord in one 7.5 ms interval
  and 2-key rollover at 28 keys/s are also lossless in BabbleSim (0 lost in
  736 burst events over 4 seeds, with and without the Mac-like host link).
* Whole capture: 5619 of 5636 delivered. The 17 lost events (8 complete taps)
  all sit in link-loss windows: the first reconnect (16:22), the left half's
  reboot (20:57:12, 8 events pressed while it was down), and 21:20:37.
* 21:20:37 is the reported symptom, caught in the act: RSSI of the right half
  had fallen from -63 dBm (afternoon) to -83..-88 dBm, supervision timeouts
  every few seconds (59 on the right, 141 on the left in 4 minutes). During a
  burst of 18 events in 0.4 s the right half logged `Position state message
  queue full, popping first message` 5 times, exactly 100 ms apart, and the
  left received the bitmap sequence with 5 states missing: the second tap of
  position 34 (`,`) and the tap of position 44 never existed for the left half,
  while the surviving events arrived 0.7-5.9 s late.
* The run-up is visible: 20:30-20:56 (2618 events) is as good as the morning
  (p50 4.1 / p95 7.9 / p99 16.4 / max 52 ms, none lost), but 21:05-21:19 (1417
  events, still none lost) has p95 43 / p99 194 / max 750 ms -- a quarter of an
  hour of heavy retransmission before the link collapsed.
* 2026-09-17 (old images, `PREF_LATENCY=30`, Mac reconnect storm): 92 `queue
  full` and 263 `Error notifying` on the right in four episodes (20:58, 20:59,
  21:05, 00:23); hundreds of events never reached the left -- same mechanism.

## (b) Mechanism, ranked

1. **Link stall -> peripheral queue overflow** (`zmk/app/src/split/bluetooth/service.c`).
   The notify work queue calls `bt_gatt_notify()`, which for a non-system-workqueue
   thread allocates from `att_pool` with `K_FOREVER`
   (`zephyr/subsys/bluetooth/host/att.c`, `bt_att_chan_create_pdu()`); the pool is
   `CONFIG_BT_ATT_TX_COUNT=3`. When the peer stops acknowledging, the thread
   blocks with 3 notifications in flight, the 10-deep `position_state_msgq`
   fills, and `send_position_state()` -- running on the **system work queue**
   from the key-scan path -- waits `K_MSEC(100)` per event and then discards the
   **oldest** state. Consequences: intermediate press/release states vanish (a
   tap is gone), every further key blocks the system work queue for 100 ms, and
   that queue is also where the matrix scan (`kscan_gpio_matrix` work) and
   `physical_layouts_kscan_msgq` (`CONFIG_ZMK_KSCAN_EVENT_QUEUE_SIZE=4`,
   `k_msgq_put(.., K_NO_WAIT)` with the result ignored) live, so short presses
   can be missed by the scanner as well. If the link then drops, everything
   still queued fails with `Error notifying -128/-107`. Fast typing only
   shortens the fuse: 10 queued events are 5 taps, i.e. a 0.6 s stall at 8
   taps/s but 2.5 s at 2 taps/s.
2. **Retransmissions on a marginal link**: +7.5 ms per retry; the 10-30 ms
   tail of normal sessions and the 21:05-21:19 run-up. No loss by itself.
3. **Host-link scheduling collision** (Mac 15 or 11.25 ms + latency 30 vs split
   7.5 ms, one nRF52840 controller): BabbleSim shows at most one skipped split
   event (+7.5 ms at p95), no loss; `CONFIG_BT_CTLR_SCHED_ADVANCED=y` on the
   left. Not involved in these captures at all: the left was on USB with the BLE
   gate paused, so no Mac link existed.
4. **USB logging on the right**: deferred mode, `LOG_MODE_OVERFLOW`, log thread
   at the lowest priority, `cdc_acm_poll_out()` sleeps 1 ms at a time only in
   that thread -- it cannot stall the BLE or key threads, and the central's own
   notify -> keymap stage never exceeded 0.52 ms in 6389 samples. It does hurt
   the *capture*: the right half's lines arrived up to 2 min late at 21:12.
5. **TX +8 dBm on the right** and the DC/DC change: no negative effect seen;
   the RF margin is the real problem (-85 dBm at desk distance means ~25 dB of
   extra loss versus the afternoon, like the 2026-09-17 USB-3 suspicion).

## (c) What would help (proposals, not applied)

`config/cornix_right.conf` (new file; the right half currently has none):
```
CONFIG_ZMK_SPLIT_BLE_PERIPHERAL_POSITION_QUEUE_SIZE=32   # 10 -> 32: a stall of 16 taps before anything is discarded (+352 B RAM)
CONFIG_ZMK_KSCAN_EVENT_QUEUE_SIZE=16                     # 4 -> 16: the scan queue survives the 100 ms-per-key blocking
CONFIG_BT_CTLR_TX_PWR_PLUS_8=y                           # make the tx8 experiment permanent: margin against the evening losses (a few % more average current)
```
Trade-offs: a bigger queue turns a lost key into a late burst (seconds late
after a long stall) -- still better; it does not remove the 100 ms blocking
once full, which is ZMK code (`K_MSEC(100)` in `send_position_state()` and
the ignored `k_msgq_put` result in `physical_layouts.c` are upstream issues
worth filing). `CONFIG_BT_ATT_TX_COUNT`/`BT_CONN_TX_MAX`/`BT_BUF_ACL_TX_COUNT`
= 6 on the right would let 6 notifications be in flight before the notify
thread blocks; harmless but not needed by the evidence (BabbleSim carries a
4-key chord in one event with 3). Do **not** shorten
`CONFIG_ZMK_SPLIT_BLE_PREF_TIMEOUT` (4 s): a faster disconnect discards the
queue just the same. 2M PHY and data length are already negotiated
(`BT_AUTO_PHY_UPDATE=y`); `SCHED_ADVANCED` is already on. Dropping the logging
snippet on the right buys battery, not latency. The lasting fix is
environmental: find what sits near the right half in the evening (USB 3
cable/hub/SSD, phone, the PC's Wi-Fi adapter) -- RSSI in the log tells.

## (d) Catching it next time

1. Keep both captures running (they are: `logs/capture-{left,right}.pid`):
   `python3 scripts/log/capture.py --list`, then `--com COM5 --label left` and
   `--com COM6 --label right` in two terminals. `kill -USR1 $(cat
   logs/capture-right.pid)` frees a port for 4 s when flashing.
2. When a key is missed, note the wall-clock time, or tap one right-hand key 5
   times quickly as a marker.
3. `python3 scripts/log/analyze_drops.py --peripheral logs/right-<ts>.log
   --central logs/left-<ts>.log --session HH:MM-HH:MM` (10 min around it).
   Read: `lost events` / `lost taps` (which keys, when), `queue full` lines
   (mechanism 1), `slowest 15` (delays), the RSSI list (link margin), and the
   `fast typing` vs `slower` rows (a real burst penalty would show there).
4. `python3 scripts/log/analyze_ble.py logs/left-<ts>.log` for `reason 0x08`
   / `0x3e` and `Security failed` around that time; `grep -n "queue
   full\|Error notifying" logs/right-<ts>.log`.
5. A lost tap **without** `queue full`, without a disconnect within 30 s and
   with RSSI above -75 dBm would be a new mechanism: send both logs.
