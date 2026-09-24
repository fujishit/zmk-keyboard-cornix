/*
 * Shared surface between src/split_rssi.c (the sampler) and
 * src/leds_on_usb.c (the LED that shows the result).
 *
 * Both files live in this directory and are compiled into `app`, so a plain
 * quoted include finds this header with no extra include path.  Either file
 * can be absent from a build: split_rssi.c is gated on
 * CONFIG_CORNIX_INDICATOR_SPLIT_RSSI and leds_on_usb.c on
 * CONFIG_CORNIX_INDICATOR_LEDS_ON_USB (plus the WS2812 / USB prerequisites) in
 * CMakeLists.txt, and every call across the pair is wrapped in the matching
 * `#if IS_ENABLED(...)`.
 *
 * SPDX-License-Identifier: MIT
 */

#pragma once

#include <stdbool.h>

#include <zephyr/kernel.h>

/* RSSI band edges, in dBm.  Measured on this keyboard: a healthy split link
 * sits at -63..-70 dBm, the state where the right half drops out with
 * supervision timeouts sits at -85..-89 dBm. */
#define CORNIX_SPLIT_RSSI_GOOD_DBM (-70)
#define CORNIX_SPLIT_RSSI_FAIR_DBM (-80)

enum cornix_split_rssi_band {
    /* Link is up but nothing has been sampled yet (or the read failed). */
    CORNIX_SPLIT_RSSI_UNKNOWN = 0,
    /* The last sweep found no central-role connection at all. */
    CORNIX_SPLIT_RSSI_DOWN,
    CORNIX_SPLIT_RSSI_GOOD,
    CORNIX_SPLIT_RSSI_FAIR,
    CORNIX_SPLIT_RSSI_POOR,
};

/* Pure classification, so the sampler and the LED code cannot drift apart.
 * `rssi` is a dBm value or 0 for "no sample". */
static inline enum cornix_split_rssi_band cornix_split_rssi_classify(int rssi, bool link_up) {
    if (!link_up) {
        return CORNIX_SPLIT_RSSI_DOWN;
    }
    if (rssi == 0) {
        return CORNIX_SPLIT_RSSI_UNKNOWN;
    }
    if (rssi >= CORNIX_SPLIT_RSSI_GOOD_DBM) {
        return CORNIX_SPLIT_RSSI_GOOD;
    }
    if (rssi >= CORNIX_SPLIT_RSSI_FAIR_DBM) {
        return CORNIX_SPLIT_RSSI_FAIR;
    }
    return CORNIX_SPLIT_RSSI_POOR;
}

#if IS_ENABLED(CONFIG_CORNIX_INDICATOR_SPLIT_RSSI)

/* Latest RSSI of the split link, in dBm; 0 when nothing has been sampled
 * (never sampled, on battery, or the HCI read failed). */
int cornix_split_rssi_latest(void);

/* False once a sweep has found no central-role LE connection, i.e. the right
 * half is not connected.  True before the first sweep, so "unknown" and "down"
 * stay distinguishable. */
bool cornix_split_rssi_link_up(void);

#endif /* CONFIG_CORNIX_INDICATOR_SPLIT_RSSI */

/* Ask leds_on_usb.c to re-run its pin path (a no-op unless USB-powered).
 * Defined in src/leds_on_usb.c; called from src/split_rssi.c when a new sample
 * moves the RSSI into a different band. */
void cornix_leds_on_usb_refresh(void);
