/*
 * Cornix: pause the BLE host link while the USB endpoint is in use.
 *
 * Copyright (c) 2026 Cornix contributors
 * SPDX-License-Identifier: MIT
 *
 * Why
 * ---
 * The left half is the split central: it owns the USB connection to the
 * computer, the BLE connection to the *host* (profile 0, a Mac here) and, as a
 * BLE central, the connection to the right half.  Measured on hardware on
 * 2026-09-18: while the keyboard was used over USB on a different computer,
 * the bonded Mac kept trying to reconnect roughly once a second and every
 * attempt died at the encryption step (12 h, 45,910 cycles, 0 successes).
 * That reconnect storm is radio time the central cannot spend on the already
 * weak split link (RSSI -80..-89 dBm), which dropped several times per minute
 * while typing; switching Bluetooth off on the Mac fixed it.
 *
 * What this does
 * --------------
 * While the USB endpoint is the one actually carrying keystrokes - the
 * *selected* transport is USB - disconnect the active BLE profile and keep
 * advertising stopped, so no host can connect or retry.  As soon as USB stops
 * being the selected transport (the cable is unplugged, or a BLE host connects
 * and becomes the selected endpoint), hand advertising back to ZMK.
 *
 * The one exception is the *BLE grace window*, and it is what keeps
 * `&out OUT_BLE` usable at all.  ZMK's endpoint selection falls back from a
 * preferred-but-not-ready BLE to USB (get_selected_transport(),
 * endpoints.c:424-453), so while we hold BLE down the selected transport is
 * USB no matter what the user asked for: a bare "gate when selected == USB"
 * rule would lock BLE off for good.  So whenever the *preferred* transport
 * becomes BLE while USB is plugged in - `&out OUT_BLE`, the output toggle, or
 * the value restored from settings at boot - the gate opens for
 * CONFIG_CORNIX_BLE_PAUSE_ON_USB_BLE_GRACE_MS (default 20 s) and advertising
 * is allowed, so a host can actually connect.  Then either:
 *
 *   - the host connects  -> the selected transport becomes BLE, the window is
 *                           closed early and the gate simply stays open for as
 *                           long as selected == BLE (this is the "typing on
 *                           the Mac while the cable only supplies power"
 *                           case); or
 *   - the window expires -> nothing connected, so the preference is stale.  We
 *                           put it back with
 *                           zmk_endpoint_set_preferred_transport(ZMK_TRANSPORT_USB)
 *                           - which ZMK persists, endpoints.c:127-141 ->
 *                           endpoints_save_preferred() - and gate.
 *
 * The rule used to be "preferred == USB && selected == USB", and hardware on
 * 2026-09-18 20:02 showed why that is not enough: the user had once pressed
 * `&out OUT_BLE` to test, ZMK persisted preferred = BLE, and every boot
 * restored it.  With the Mac not connected the selection fell back to USB, so
 * the user typed over USB with preferred = BLE and the first half of the
 * condition was false forever - "ble gate: preferred 2 selected 1 -> gated 0
 * (paused 0)", no pause, and the reconnect storm free to resume.  A stale
 * preference can no longer disable the gate: it only ever buys one grace
 * window, after which it is rewritten to USB.
 *
 * Which transitions raise an event, and which need the watchdog
 * -------------------------------------------------------------
 * zmk_endpoint_set_preferred_transport() (endpoints.c:127-141) only calls
 * update_current_endpoint(), which raises zmk_endpoint_changed *only* when the
 * selected instance really changes (endpoints.c:482-497).  Walking the
 * sequence this feature has to support, USB plugged in throughout:
 *
 *  1. USB becomes ready, preferred USB -> selected USB.  zmk_usb_conn_state_changed
 *     and, from endpoints.c's own listener, zmk_endpoint_changed: EVENT.
 *     -> the gate closes within DELAY_MS.
 *  2. `&out OUT_BLE` while gated: preferred USB -> BLE, but BLE is not ready
 *     (we are holding it down), so the selection falls back to USB and nothing
 *     changes: NO EVENT.  -> the watchdog, which re-arms every DELAY_MS while
 *     the gate is closed, sees preferred == BLE and opens the grace window;
 *     the gate opens and advertising is handed back to ZMK.
 *  3. The Mac connects during the window: ble.c raises
 *     zmk_ble_active_profile_changed, endpoints.c re-selects BLE:0 and raises
 *     zmk_endpoint_changed: EVENT.  -> the window is closed early and the gate
 *     stays open because selected == BLE; typing goes to the Mac.
 *  4. `&out OUT_USB` while typing on the Mac: preferred BLE -> USB and USB
 *     *is* ready, so the selection really changes BLE:0 -> USB: EVENT.
 *     -> the gate closes within DELAY_MS.
 *  5. The window expires with nothing connected: NO EVENT (nothing changed).
 *     -> the watchdog is what notices, rewrites the preference to USB and
 *     closes the gate.  The same tick covers `&out OUT_USB` pressed *during*
 *     the window, which raises no event either (preferred BLE -> USB with USB
 *     already selected changes nothing).  Hardware, 2026-09-18 20:09: the log
 *     shows "Selected endpoint transport 1" from `&out OUT_USB` with no
 *     "ble gate:" line for the next 3 s, because back then the watchdog only
 *     re-armed while the gate was already closed - so with the gate open on a
 *     USB-selected keyboard, nothing re-evaluated it and `&out OUT_USB` could
 *     not close it.  Hence the rule below.
 *  6. USB unplugged: zmk_usb_conn_state_changed, and the selection changes to
 *     BLE (if a host is connected) or to NONE: EVENT.  -> selected != USB, so
 *     the gate opens and advertising resumes; a pending or open grace window
 *     is dropped, because with no USB there is nothing to gate.
 *  7. Boot: SYS_INIT runs before main() calls settings_load()
 *     (zmk/app/src/main.c:22-28), so the restored preference is not visible
 *     there - which is why nothing is logged from SYS_INIT.  The first work
 *     run, DELAY_MS later, reads the settled values and logs the "init" line;
 *     if the restored preference is BLE the grace window is armed and opens as
 *     soon as USB is ready (case 1 above turns into case 2's window).
 *
 * The watchdog therefore re-arms every DELAY_MS for as long as USB can carry
 * keystrokes at all (zmk_usb_is_hid_ready()), plus while a grace window is
 * open, rather than only while the gate is closed: every "no event" row above
 * happens with the cable plugged in, and a tick that finds nothing to do costs
 * one comparison and touches neither the radio nor the log.  It stops once USB
 * is unplugged, which is the only state where the battery would pay for it.
 *
 * How it interacts with ZMK's own advertising state machine
 * ---------------------------------------------------------
 * ble.c keeps a static `advertising_status` and reconciles it in
 * update_advertising() (zmk/app/src/ble.c:177-221).  It restarts advertising
 * from several places we have to expect while gated:
 *
 *   - connected():      advertising_status = ZMK_ADV_NONE (ble.c:513) then
 *                       update_advertising() (ble.c:517 / 523), and only then
 *                       k_work_submit(&raise_profile_changed_event_work)
 *                       (ble.c:527).
 *   - disconnected():   k_work_submit(&update_advertising_work) (ble.c:548)
 *                       *before* k_work_submit(&raise_profile_changed_event_work)
 *                       (ble.c:552) - both on the system work queue, in that
 *                       order, so advertising is already back on by the time
 *                       our listener sees zmk_ble_active_profile_changed.
 *   - zmk_ble_prof_select(): update_advertising() (ble.c:300) then
 *                       raise_profile_changed_event() (ble.c:302), same
 *                       context.
 *   - set_profile_address() / auth_pairing_complete(): ble.c:118 and 665.
 *
 * In every case the event we subscribe to is raised *after* ZMK has already
 * (re)started advertising, so re-running bt_le_adv_stop() from the event is
 * enough; we still do it from a delayable work item, both to debounce bursts
 * and to cover the one case where the work queue order is not on our side
 * (our own disconnect: it is us who triggers disconnected(), so we schedule a
 * re-check afterwards).
 *
 * Stopping advertising behind ble.c's back leaves its `advertising_status` at
 * ZMK_ADV_CONN while nothing is actually advertising.  That is deliberate and
 * is what keeps the gate closed: every later update_advertising() call sees
 * "desired CONN, current CONN", which is not a case in its switch
 * (ble.c:197-220), and does nothing.  The flip side is that ZMK can no longer
 * start advertising by itself, so resuming has to go through the one public
 * API that *resets* that state: zmk_ble_set_device_name() stops advertising
 * when it thinks it is advertising, sets advertising_status = ZMK_ADV_NONE and
 * then calls update_advertising() (ble.c:358-376).  Passing the name that is
 * already set makes bt_set_name() a no-op that returns 0 without touching
 * settings (zephyr/subsys/bluetooth/host/hci_core.c:4456-4458), so the whole
 * call is just "re-evaluate advertising".  zmk_ble_prof_select() cannot be
 * used for this: it returns early when the index does not change (ble.c:293).
 *
 * It takes *two* of those calls, though, and that is not a workaround for a
 * race but for ble.c:366-374: the first call finds advertising_status ==
 * ZMK_ADV_CONN, calls bt_le_adv_stop() - which returns -EALREADY, because we
 * already stopped it - and returns that error early, after having set
 * advertising_status = ZMK_ADV_NONE.  The second call then walks straight into
 * update_advertising(), which sees "desired CONN, current NONE" and really
 * starts advertising.  If even the second one fails we keep the paused state
 * and retry from the work item instead of leaving BLE off for good.
 *
 * The split link is never touched.  zmk_ble_prof_disconnect() looks up only
 * the conn of that host profile's address and disconnects it (ble.c:318-336),
 * peripheral addresses live in a separate table (ble.c:82), and
 * bt_le_adv_stop() only affects this device's own peripheral-role
 * advertising - the central reaches the right half by scanning and connecting
 * as a central (split/bluetooth/central.c), which needs no advertising.
 */

#include <zephyr/kernel.h>
#include <zephyr/init.h>
#include <zephyr/logging/log.h>
#include <zephyr/bluetooth/bluetooth.h>

#include <zmk/ble.h>
#include <zmk/endpoints.h>
#include <zmk/endpoints_types.h>
#include <zmk/usb.h>
#include <zmk/event_manager.h>
#include <zmk/events/ble_active_profile_changed.h>
#include <zmk/events/endpoint_changed.h>
#include <zmk/events/usb_conn_state_changed.h>

#if !IS_ENABLED(CONFIG_CORNIX_BLE_PAUSE_ON_USB)
#error "src/ble_usb_gate.c is only built with CONFIG_CORNIX_BLE_PAUSE_ON_USB=y (see CMakeLists.txt)"
#endif

/* Log lines go to ZMK's own log module so that they carry the "zmk:" prefix
 * the log analyzers in scripts/log/ filter on. */
LOG_MODULE_DECLARE(zmk, CONFIG_ZMK_LOG_LEVEL);

#define GATE_DELAY K_MSEC(CONFIG_CORNIX_BLE_PAUSE_ON_USB_DELAY_MS)
#define GRACE_MS CONFIG_CORNIX_BLE_PAUSE_ON_USB_BLE_GRACE_MS

/* True while the USB endpoint is the one in use, i.e. while BLE should stay
 * down. */
static bool gated;
/* True while we hold BLE down; also guards the INF log lines so that the
 * watchdog does not spam the log the analyzers read. */
static bool paused;
/* Set by the listener: an event arrived, so advertising may have been
 * restarted by ble.c and has to be stopped again. */
static bool enforce_pending;
/* Rate-limits the error log while a resume keeps failing. */
static bool resume_failed;

/* Grace window (see the file header).  `pending` is armed the moment the
 * preferred transport becomes BLE and is consumed - turned into an open
 * window - as soon as USB is the selected transport, which is the only state
 * the window has to protect against; that ordering is what makes a preference
 * restored from settings at boot open a window when USB enumerates, several
 * seconds later. */
static bool grace_pending;
static bool grace_active;
static int64_t grace_deadline;

/* Last preferred transport we saw, to detect "became BLE".  Zero-initialised,
 * i.e. ZMK_TRANSPORT_NONE, so the very first observation - the one that reads
 * the value restored from settings - can never be mistaken for "no change". */
static enum zmk_transport last_preferred;
/* The init line is logged from the first work run, not from SYS_INIT, because
 * settings are not loaded yet at SYS_INIT time. */
static bool init_logged;

static struct k_work_delayable gate_work;

/* Runs on the system work queue. */
static void gate_work_handler(struct k_work *work) {
    ARG_UNUSED(work);

    enum zmk_transport preferred = zmk_endpoint_get_preferred_transport();
    enum zmk_transport selected = zmk_endpoint_get_selected().transport;

    if (!init_logged) {
        init_logged = true;
        /* Unlike SYS_INIT, this runs after main() has called settings_load(),
         * so these are the values the gate actually works with. */
        LOG_INF("ble gate: init (preferred %d selected %d)", preferred, selected);
    }

    /* --- grace window bookkeeping ------------------------------------- */

    if (preferred == ZMK_TRANSPORT_BLE && last_preferred != ZMK_TRANSPORT_BLE) {
        /* `&out OUT_BLE` / the output toggle, or the value restored from
         * settings on the first run: the user asked for BLE, so give a host a
         * chance to connect before deciding the preference is stale. */
        grace_pending = true;
    }
    last_preferred = preferred;

    if (preferred != ZMK_TRANSPORT_BLE || selected == ZMK_TRANSPORT_BLE) {
        /* Back on USB by the user's own choice, or a host actually connected
         * and is now the selected endpoint: no window needed either way. */
        if (grace_active) {
            LOG_INF("ble gate: BLE grace window closed (preferred %d selected %d)", preferred,
                    selected);
        }
        grace_pending = false;
        grace_active = false;
    }

    if (grace_pending && selected == ZMK_TRANSPORT_USB) {
        grace_pending = false;
        grace_active = true;
        grace_deadline = k_uptime_get() + GRACE_MS;
        LOG_INF("ble gate: BLE grace window (%d ms)", GRACE_MS);
    }

    if (grace_active) {
        if (selected != ZMK_TRANSPORT_USB) {
            /* Cable gone (or NONE): nothing to gate, so the window has no
             * purpose and the preference is left alone. */
            grace_active = false;
        } else if (k_uptime_get() >= grace_deadline) {
            grace_active = false;
            LOG_INF("ble gate: BLE did not connect, preferring USB again");
            /* ZMK persists this (endpoints.c:135 -> endpoints_save_preferred()),
             * so a stale preferred = BLE cannot survive into the next boot and
             * disable the gate for good. */
            zmk_endpoint_set_preferred_transport(ZMK_TRANSPORT_USB);
            preferred = zmk_endpoint_get_preferred_transport();
            last_preferred = preferred;
            selected = zmk_endpoint_get_selected().transport;
        }
    }

    /* --- the gate itself ---------------------------------------------- */

    bool was_gated = gated;
    gated = (selected == ZMK_TRANSPORT_USB) && !grace_active;
    if (gated != was_gated || enforce_pending) {
        /* One line per transition or event, so the on-device log shows what
         * the gate decided and why (preferred/selected transport). */
        LOG_INF("ble gate: preferred %d selected %d -> gated %d (paused %d)", preferred, selected,
                gated, paused);
    }

    if (gated) {
        /* Only touch the radio when something can have changed: on an event,
         * when closing the gate for the first time, or when a host managed to
         * connect anyway.  The plain watchdog tick does nothing, so it neither
         * costs radio time nor fills the log with Zephyr's "Advertiser already
         * stopped" warnings. */
        bool apply = enforce_pending || !paused;
        enforce_pending = false;

        if (zmk_ble_active_profile_is_connected()) {
            int index = zmk_ble_active_profile_index();
            int err = zmk_ble_prof_disconnect((uint8_t)index);
            LOG_INF("ble gate: disconnecting host profile %d (err %d)", index, err);
            /* disconnected() submits update_advertising_work (ble.c:548), so
             * advertising comes back right after; stop it on the next tick. */
            apply = true;
            enforce_pending = true;
        }

        if (apply) {
            int err = bt_le_adv_stop();
            if (err && err != -EALREADY) {
                LOG_WRN("ble gate: failed to stop advertising (err %d)", err);
            }
        }

        if (!paused) {
            paused = true;
            LOG_INF("ble gate: paused (USB active)");
        }

        /* Watchdog: `&out OUT_BLE` while USB is plugged in raises no event. */
        k_work_reschedule(&gate_work, GATE_DELAY);
        return;
    }

    enforce_pending = false;

    if (paused) {
        /* See the file header: this is "re-evaluate advertising" spelled with
         * a public ZMK function, and it takes two calls (ble.c:366-374).
         * CONFIG_BT_DEVICE_NAME_DYNAMIC is select'ed by our Kconfig, so
         * bt_set_name() itself cannot fail for the name that is already set. */
        int err = zmk_ble_set_device_name((char *)bt_get_name());
        if (err) {
            err = zmk_ble_set_device_name((char *)bt_get_name());
        }

        if (err) {
            if (!resume_failed) {
                resume_failed = true;
                LOG_ERR("ble gate: failed to restart advertising (err %d), retrying", err);
            }
            /* Stay paused and try again; an event may also retrigger us
             * earlier.  The grace window keeps ticking meanwhile, which is
             * fine: it is re-evaluated on every run. */
            k_work_reschedule(&gate_work, GATE_DELAY);
            return;
        }

        resume_failed = false;
        paused = false;
        LOG_INF("ble gate: resumed");
    }

    /* Watchdog.  It re-arms for as long as USB can carry keystrokes, not just
     * while the gate is closed: `&out OUT_USB` pressed while USB is already
     * the selected (fallback) transport changes nothing selected and raises no
     * event at all, so nothing else would ever re-evaluate the gate.  It also
     * covers the grace window expiring.  Stopping once USB is unplugged is
     * what keeps this off the battery. */
    if (grace_active || zmk_usb_is_hid_ready()) {
        k_work_reschedule(&gate_work, GATE_DELAY);
    }
}

static int ble_usb_gate_listener(const zmk_event_t *eh) {
    ARG_UNUSED(eh);

    /* Any of the three events can mean BLE came back up (ble.c restarts
     * advertising from connected()/disconnected() and from profile changes),
     * so always re-apply rather than only on a state change. */
    enforce_pending = true;
    k_work_reschedule(&gate_work, GATE_DELAY);

    return ZMK_EV_EVENT_BUBBLE;
}

ZMK_LISTENER(cornix_ble_usb_gate, ble_usb_gate_listener);
ZMK_SUBSCRIPTION(cornix_ble_usb_gate, zmk_endpoint_changed);
ZMK_SUBSCRIPTION(cornix_ble_usb_gate, zmk_usb_conn_state_changed);
ZMK_SUBSCRIPTION(cornix_ble_usb_gate, zmk_ble_active_profile_changed);

static int ble_usb_gate_init(void) {
    k_work_init_delayable(&gate_work, gate_work_handler);

    /* Nothing is logged here on purpose: SYS_INIT runs before main() calls
     * settings_load() (zmk/app/src/main.c:22-28), so the preferred transport
     * is still the compile-time default and printing it would be misleading -
     * exactly the "ble gate: init (preferred 1 selected 0)" line that made the
     * 2026-09-18 log hard to read.  The first work run logs it instead.
     *
     * The first run is scheduled unconditionally, because it is also what
     * picks up a preferred transport restored from settings and arms the
     * grace window for it. */
    k_work_reschedule(&gate_work, GATE_DELAY);

    return 0;
}

/* After ZMK's BLE init (zmk/app/src/ble.c uses SYS_INIT at APPLICATION), and
 * late enough that the endpoints module has its state. */
SYS_INIT(ble_usb_gate_init, APPLICATION, CONFIG_APPLICATION_INIT_PRIORITY);
