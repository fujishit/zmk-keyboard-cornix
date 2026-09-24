# Right-half link: which factors the logs support (2026-09-17 → 09-24)

Cross-log analysis of every capture in `logs/` (≈35 h of coverage over 7 days;
scripts and CSVs were produced in a scratchpad and are not committed). Numbers
are from `analyze_drops.py` per log pair plus the 5 s `split rssi:` samples
that exist since 09-24 16:10.

## Episodes

Every multi-minute trouble episode (supervision timeouts every few tens of
seconds, "failed to establish", lost keys via the 100 ms queue pop) sits at a
scan RSSI of −77…−86 dBm; every quiet stretch at −63…−75. The delay tail lives
entirely inside link-loss windows: with no split disconnect within ±5 min,
p50 4.5 ms / p99 34 ms over 23 763 events; with 3–10 disconnects in ±5 min,
p50 13 ms / p99 2.8 s.

| episode | length | timeouts | scan RSSI |
|---|---|---|---|
| 09-17 21:41→22:01 | 20 min | 117 | −79 |
| 09-18 19:58→21:07 | 69 min | 206 | −85 |
| 09-22 21:20→09-23 00:29 | 189 min | 422 | −85 |
| 09-23 11:38→12:43 | 65 min | 218 | −85 |
| 09-24 10:54→12:00 | 65 min | 21 | −86 |
| 09-24 15:00→15:15 | 15 min | 9 | −86 (right on battery) |

Same firmware, same desk, right on USB, no Mac link at all: 09-22 21:09→00:29
was a collapse (link up 36–45 %), 09-23 22:08→00:01 was perfect (16 452
events, p99 13–21 ms, 0 lost). Nothing the logs record differs.

## Verdict

1. **A multi-hour 15–20 dB change in the RF path** between the halves is the
   dominant factor. Its cause is not in the logs: geometry (where the MacBook
   and the halves were) was never recorded. Not a time-of-day rule.
2. **Right half on USB — not supported.** On USB continuously 09-21→09-24
   through both collapses and the best sessions; the 09-24 15:00 episode
   happened on battery.
3. **Second BLE link (Mac) — the opposite of the hypothesis.** A *steady* Mac
   link is the best observed condition (09-24 12:14→13:15: 0 timeouts). A
   *thrashing* Mac link (bond-failure loop 09-17/18, the gate's own
   disconnect loop 09-24 12:00) is devastating: split link up 0.9 % of the
   time. The worst collapses had no Mac link at all. The Mac grants
   interval 15 ms, latency 0, timeout 720 ms on every connection (36×); the
   latency-30 preference is never accepted.
4. **Right-half firmware variant — confounded.** The 09-19 "plain image is
   good" reading rests on 0.8 h that began 7 min after an episode ended;
   the current TX+8 + logging image contains the best session in the set.

## PHY

`split_central_process_connection` logs the *initial* params (Interval 6,
Latency 0 since 09-19, PHY 1) on every connection. Zephyr's host then
requests 2M on every link whose peer supports it (`CONFIG_BT_AUTO_PHY_UPDATE`,
host/conn.c), and both halves had `CONFIG_BT_CTLR_PHY_2M=y`; the update itself
is only logged with `CONFIG_BT_HCI_CORE_LOG_LEVEL_DBG`. The nRF52840 receiver
is 3–5 dB less sensitive on 2M, and −80 dBm is right at that cliff, so since
09-24 16:46 the right half runs with `CONFIG_BT_CTLR_PHY_2M=n`
(config/cornix_right.conf): the split link stays on 1M, the Mac link keeps 2M.

## Experiments that would actually discriminate

Run them *during* a bad hour, with the 5 s RSSI log running
(`python3 scripts/log/rssi_timeline.py logs/left-*.log --bucket 60`):

1. Right half on battery for 10 min, nothing else moved → RSSI median stays
   −85 ⇒ USB exonerated for good.
2. MacBook on / off the line between the halves, A-B-A-B, 10 min each →
   per-10-min median RSSI and `reason 8` count. The only direct test of the
   geometry hypothesis.
3. Same 200-character passage typed to the Mac (BLE) and to the PC (USB),
   5 min each → `analyze_drops.py` p50/p99 and lost per half. Prediction:
   indistinguishable.

Tooling caveats found on the way: `analyze_drops.py` assumes both logs start
on the same day (pairing a 09-24 right log with the 09-21 left log needs a
+3-day shift), and left builds before 09-18 09:47 did not log the
notification hexdump, so no delay numbers exist before then.
