/*
 * Periodic RSSI of the split link, on the left half (split central).
 *
 * Why: the only signal-strength number this firmware ever prints is the one
 * the scanner reports at reconnect time (`split_central_device_found:
 * [DEVICE]: ... RSSI -NN`, zmk/app/src/split/bluetooth/central.c), which is a
 * single advertising packet at a moment when the link is by definition down.
 * Once connected there is nothing at all, so "is the right half struggling
 * because of the radio, or because of something in the firmware?" cannot be
 * answered from a log.  This file answers it: every
 * CONFIG_CORNIX_INDICATOR_SPLIT_RSSI_PERIOD_MS it reads the *connection* RSSI
 * out of the controller and logs one line, and - while the LEDs are pinned by
 * src/leds_on_usb.c - paints the connectivity LED by it, so the halves and the
 * laptop can be moved around with immediate feedback.
 *
 * How: HCI Read RSSI (Core spec Vol 4 Part E 7.5.4, opcode 0x1405) on the
 * connection handle.  The command is only *answered* when the controller is
 * built with CONFIG_BT_CTLR_CONN_RSSI (zephyr/subsys/bluetooth/controller/
 * hci/hci.c status_cmd_handle()), which is `default y` only for BT_HCI_RAW -
 * hence the explicit `CONFIG_BT_CTLR_CONN_RSSI=y` in
 * boards/cornix_left_nrf52840_zmk.conf.  Without it every read returns
 * "unknown HCI command" and this file logs that once at DBG and nothing else.
 *
 * Which connection: the left half has two kinds of LE link - the host (Mac)
 * link, where it is the *peripheral* (ZMK advertises, the host connects), and
 * the split link to the right half, where it is the *central* (ZMK scans and
 * connects).  So role == BT_CONN_ROLE_CENTRAL selects the split link, with no
 * need to reach into ZMK's private peripheral table.
 *
 * Cost: nothing on battery.  The sampler only runs while zmk_usb_is_powered()
 * and stops itself - it does not even keep a timer armed - as soon as the
 * cable is out, which is also the only time the LED is pinned and the log is
 * being captured over USB CDC anyway.
 *
 * SPDX-License-Identifier: MIT
 */

#include <zephyr/init.h>
#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>
#include <zephyr/net_buf.h>
#include <zephyr/sys/atomic.h>
#include <zephyr/sys/byteorder.h>
#include <zephyr/sys/util.h>

#include <zephyr/bluetooth/addr.h>
#include <zephyr/bluetooth/bluetooth.h>
#include <zephyr/bluetooth/conn.h>
#include <zephyr/bluetooth/hci.h>
#include <zephyr/bluetooth/hci_types.h>

#include <zmk/event_manager.h>
#include <zmk/events/usb_conn_state_changed.h>
#include <zmk/usb.h>

#include "split_rssi.h"

LOG_MODULE_REGISTER(cornix_split_rssi, CONFIG_ZMK_LOG_LEVEL);

/* leds_on_usb.c is compiled under three conditions in CMakeLists.txt, not one;
 * mirror all three or the refresh call below would not link in a build that
 * has the symbol but neither the WS2812 backend nor a USB device stack. */
#define CORNIX_RSSI_HAS_LEDS                                                                       \
    (IS_ENABLED(CONFIG_CORNIX_INDICATOR_LEDS_ON_USB) &&                                            \
     IS_ENABLED(CONFIG_RGBLED_WIDGET_WS2812) && IS_ENABLED(CONFIG_USB_DEVICE_STACK))

/* Upper bound on central-role links to sample in one sweep.  Cornix has one
 * (CONFIG_ZMK_SPLIT_BLE_CENTRAL_PERIPHERALS=1), but the controller's table is
 * what bt_conn_foreach() walks, so size the scratch array by that. */
#define CORNIX_RSSI_MAX_LINKS CONFIG_BT_MAX_CONN

static struct k_work_delayable sample_work;

/* Written from the system work queue, read from anywhere (leds_on_usb.c's pin
 * path runs on the same queue, but cornix_split_rssi_latest() is public). */
static atomic_t latest_rssi = ATOMIC_INIT(0);
static atomic_t link_up = ATOMIC_INIT(1);

/* "log the failure once, not every tick": cleared on the next success. */
static bool failure_logged;

/* Band of the last sample, so the LED is only re-pinned when the colour would
 * actually change. */
static enum cornix_split_rssi_band last_band = CORNIX_SPLIT_RSSI_UNKNOWN;

int cornix_split_rssi_latest(void) { return (int)atomic_get(&latest_rssi); }

bool cornix_split_rssi_link_up(void) { return atomic_get(&link_up) != 0; }

struct link_sweep {
    struct bt_conn *conns[CORNIX_RSSI_MAX_LINKS];
    uint8_t count;
};

/* bt_conn_foreach() only holds its own reference for the duration of the
 * callback, so take one of our own and do the (blocking) HCI round trip
 * outside the walk. */
static void collect_central_links(struct bt_conn *conn, void *data) {
    struct link_sweep *sweep = data;
    struct bt_conn_info info;

    if (sweep->count >= ARRAY_SIZE(sweep->conns)) {
        return;
    }
    if (bt_conn_get_info(conn, &info) != 0) {
        return;
    }
    if (info.type != BT_CONN_TYPE_LE || info.state != BT_CONN_STATE_CONNECTED) {
        return;
    }
    /* Central role == the link we opened == the right half.  The host link is
     * peripheral role and is deliberately skipped. */
    if (info.role != BT_CONN_ROLE_CENTRAL) {
        return;
    }

    sweep->conns[sweep->count++] = bt_conn_ref(conn);
}

static int read_conn_rssi(struct bt_conn *conn, int8_t *rssi) {
    struct bt_hci_cp_read_rssi *cp;
    struct bt_hci_rp_read_rssi *rp;
    struct net_buf *buf;
    struct net_buf *rsp = NULL;
    uint16_t handle;
    int err;

    err = bt_hci_get_conn_handle(conn, &handle);
    if (err != 0) {
        return err;
    }

    buf = bt_hci_cmd_create(BT_HCI_OP_READ_RSSI, sizeof(*cp));
    if (buf == NULL) {
        return -ENOBUFS;
    }
    cp = net_buf_add(buf, sizeof(*cp));
    cp->handle = sys_cpu_to_le16(handle);

    /* bt_hci_cmd_send_sync() consumes `buf` on every path, including errors.
     * It blocks this work item (not the whole system) for one HCI round trip
     * to the on-chip controller: tens of microseconds, once per period. */
    err = bt_hci_cmd_send_sync(BT_HCI_OP_READ_RSSI, buf, &rsp);
    if (err != 0) {
        return err;
    }
    if (rsp == NULL) {
        return -EIO;
    }

    rp = (void *)rsp->data;
    if (rsp->len < sizeof(*rp)) {
        err = -EMSGSIZE;
    } else if (rp->status != 0) {
        err = -EIO;
    } else {
        *rssi = rp->rssi;
    }

    net_buf_unref(rsp);
    return err;
}

static void log_failure_once(const char *what, int err) {
    if (failure_logged) {
        return;
    }
    failure_logged = true;
    LOG_DBG("split rssi unavailable: %s (%d); "
            "needs CONFIG_BT_CTLR_CONN_RSSI=y and a live split link",
            what, err);
}

static void publish(int rssi, bool up) {
    enum cornix_split_rssi_band band = cornix_split_rssi_classify(rssi, up);

    atomic_set(&latest_rssi, rssi);
    atomic_set(&link_up, up ? 1 : 0);

    if (band == last_band) {
        return;
    }
    last_band = band;

#if CORNIX_RSSI_HAS_LEDS
    cornix_leds_on_usb_refresh();
#endif
}

static void sample_once(void) {
    struct link_sweep sweep = {0};
    bool have_sample = false;
    int worst = 0;

    if (!bt_is_ready()) {
        log_failure_once("bluetooth not ready", -EAGAIN);
        return;
    }

    bt_conn_foreach(BT_CONN_TYPE_LE, collect_central_links, &sweep);

    if (sweep.count == 0) {
        /* No split link at all: distinct from "connected but unsampled". */
        log_failure_once("no central-role connection", -ENOTCONN);
        publish(0, false);
        return;
    }

    for (uint8_t i = 0; i < sweep.count; i++) {
        char addr[BT_ADDR_LE_STR_LEN];
        int8_t rssi = 0;
        int err = read_conn_rssi(sweep.conns[i], &rssi);

        if (err == 0) {
            bt_addr_le_to_str(bt_conn_get_dst(sweep.conns[i]), addr, sizeof(addr));
            /* The `split rssi:` prefix is the grep handle the host tools use
             * (scripts/log/rssi_timeline.py); do not reword it. */
            LOG_INF("split rssi: %d dBm (peer %s)", rssi, addr);
            if (!have_sample || rssi < worst) {
                worst = rssi;
            }
            have_sample = true;
        } else {
            log_failure_once("HCI Read RSSI failed", err);
        }

        bt_conn_unref(sweep.conns[i]);
    }

    if (have_sample) {
        failure_logged = false;
        /* With more than one peripheral the worst link is the one that will
         * drop first, so that is the one worth showing. */
        publish(worst, true);
    } else {
        /* Link is up, the number is not available: leave the last known value
         * alone rather than flapping the LED. */
        publish(cornix_split_rssi_latest(), true);
    }
}

static void sample_work_cb(struct k_work *work) {
    ARG_UNUSED(work);

    if (!zmk_usb_is_powered()) {
        /* Battery must not pay for this.  No re-arm either; the USB event
         * below restarts the cycle when a cable comes back. */
        return;
    }

    sample_once();
    k_work_reschedule(&sample_work, K_MSEC(CONFIG_CORNIX_INDICATOR_SPLIT_RSSI_PERIOD_MS));
}

static int split_rssi_listener(const zmk_event_t *eh) {
    ARG_UNUSED(eh);

    if (zmk_usb_is_powered()) {
        /* One period of grace: on a fresh plug-in (or boot) the split link is
         * usually still coming up, and a first sweep at t=0 would only paint
         * the LED "link down" for a moment. */
        k_work_reschedule(&sample_work, K_MSEC(CONFIG_CORNIX_INDICATOR_SPLIT_RSSI_PERIOD_MS));
    } else {
        k_work_cancel_delayable(&sample_work);
        failure_logged = false;
        last_band = CORNIX_SPLIT_RSSI_UNKNOWN;
        /* Forget the sample so the LED does not come back showing a stale
         * band when the cable returns; leds_on_usb.c has already handed the
         * LEDs back to the widget by then. */
        atomic_set(&latest_rssi, 0);
        atomic_set(&link_up, 1);
    }

    return 0;
}

ZMK_LISTENER(cornix_split_rssi, split_rssi_listener);
ZMK_SUBSCRIPTION(cornix_split_rssi, zmk_usb_conn_state_changed);

static int split_rssi_init(void) {
    k_work_init_delayable(&sample_work, sample_work_cb);
    /* Covers "booted with the cable already in", where no USB event follows. */
    k_work_schedule(&sample_work, K_MSEC(CONFIG_CORNIX_INDICATOR_SPLIT_RSSI_PERIOD_MS));
    return 0;
}

SYS_INIT(split_rssi_init, APPLICATION, CONFIG_APPLICATION_INIT_PRIORITY);
