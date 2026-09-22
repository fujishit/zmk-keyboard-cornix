/*
 * Keep the Cornix status LEDs lit while the half is USB-powered.
 *
 * zmk-rgbled-widget renders every status as a *timed* indication: it lights an
 * LED, marks it "shared" with an end time (CONN_*_DURATION_MS,
 * BATTERY_BLINK_MS), and when that time passes the LED is returned to its base
 * colour - black - after which RGBLED_WIDGET_EXT_POWER_TIMEOUT_MS cuts the
 * WS2812 rail.  That is the right trade-off on battery, and the wrong one
 * while a cable is supplying power: there the LEDs cost nothing and a
 * permanent "which output am I on / am I charged yet" readout is worth having.
 *
 * The widget has no Kconfig option for this - the only persistent path in
 * src/widget.c is the charging pulse (set_status_led(..., 0, true) in
 * indicate_battery_enhanced()), and it is hard-coded to the charging case.  So
 * this file re-asserts the same state through the widget's public API with a
 * zero duration, which is what makes a status persistent:
 *
 *   set_led_with_sharing(): share_timeout_ms == 0
 *     -> is_shared = false, share_end_time = 0
 *     -> check_shared_led_timeouts() never returns the LED to black
 *     -> ws2812_strip_can_power_off_locked() sees a non-black strip
 *     -> the ext_power off timer is never armed.
 *
 * It only ever *adds* a pin while zmk_usb_is_powered(); on battery every code
 * path here returns immediately and the widget behaves exactly as before.
 *
 * SPDX-License-Identifier: MIT
 */

#include <zephyr/kernel.h>
#include <zephyr/init.h>
#include <zephyr/logging/log.h>

#include <zmk/event_manager.h>
#include <zmk/endpoints.h>
#include <zmk/usb.h>
#include <zmk/events/usb_conn_state_changed.h>
#include <zmk/events/activity_state_changed.h>

#if IS_ENABLED(CONFIG_ZMK_BLE)
#include <zmk/ble.h>
#include <zmk/events/ble_active_profile_changed.h>
#include <zmk/events/endpoint_changed.h>
#endif

#if IS_ENABLED(CONFIG_ZMK_BATTERY_REPORTING)
#include <zmk/battery.h>
#include <zmk/events/battery_state_changed.h>
#endif

#if IS_ENABLED(CONFIG_ZMK_SPLIT_BLE) && !IS_ENABLED(CONFIG_ZMK_SPLIT_ROLE_CENTRAL)
#include <zmk/split/bluetooth/peripheral.h>
#include <zmk/events/split_peripheral_status_changed.h>
#endif

#include <zmk_rgbled_widget/widget.h>

LOG_MODULE_REGISTER(cornix_leds_on_usb, CONFIG_ZMK_LOG_LEVEL);

/* "Central" here means the same thing it means inside the widget: a build that
 * owns a BLE host profile, i.e. a non-split build or the split central. */
#define CORNIX_LED_CENTRAL                                                                         \
    (!IS_ENABLED(CONFIG_ZMK_SPLIT) || IS_ENABLED(CONFIG_ZMK_SPLIT_ROLE_CENTRAL))

/* The level at which indicate_battery_enhanced() stops pulsing and calls the
 * pack full.  Hard-coded there, mirrored here. */
#define CORNIX_LED_BATTERY_FULL_PCT 99

static struct k_work_delayable pin_work;

/* Set when the widget has just blanked every LED itself (its activity handler
 * clears the strip and cuts ext_power on ZMK_ACTIVITY_IDLE), so the next pin
 * has to restore the battery LED too and not only the connectivity one. */
static bool battery_needs_pin;

static uint8_t connectivity_color(void) {
#if CORNIX_LED_CENTRAL
    if (zmk_endpoint_get_selected().transport == ZMK_TRANSPORT_USB) {
        return CONFIG_RGBLED_WIDGET_CONN_COLOR_USB;
    }
#if IS_ENABLED(CONFIG_ZMK_BLE)
    switch (zmk_ble_active_profile_index()) {
    case 0:
        return CONFIG_RGBLED_WIDGET_CONN_COLOR_BT0;
    case 1:
        return CONFIG_RGBLED_WIDGET_CONN_COLOR_BT1;
    case 2:
        return CONFIG_RGBLED_WIDGET_CONN_COLOR_BT2;
    case 3:
        return CONFIG_RGBLED_WIDGET_CONN_COLOR_BT3;
    case 4:
        return CONFIG_RGBLED_WIDGET_CONN_COLOR_BT4;
    default:
        return CONFIG_RGBLED_WIDGET_CONN_COLOR_BT_FALLBACK;
    }
#else
    return 0;
#endif
#elif IS_ENABLED(CONFIG_ZMK_SPLIT_BLE)
    return zmk_split_bt_peripheral_is_connected() ? CONFIG_RGBLED_WIDGET_CONN_COLOR_CONNECTED
                                                  : CONFIG_RGBLED_WIDGET_CONN_COLOR_DISCONNECTED;
#else
    return 0;
#endif
}

/* Pin the connectivity LED to the colour of the *current* output.
 *
 * The animation that the widget last installed on that LED is deliberately
 * left alone: connected / USB is ANIM_STATIC (so this really is a steady
 * colour), while "advertising" stays a breathing pulse and "disconnected"
 * stays a blink - both of which are the states RMK kept blinking for anyway,
 * and both of which keep the strip powered because
 * ws2812_strip_can_power_off_locked() refuses to power off a non-static LED.
 */
static void pin_connectivity(void) {
    uint8_t color = connectivity_color();

    if (color == 0) {
        return;
    }

    int ret = ws2812_set_status_led(STATUS_CONNECTIVITY, color, 0, true);
    if (ret != 0) {
        /* -EBUSY: something with a higher priority (critical battery) owns the
         * LED.  Leave it alone; the next event re-runs this. */
        LOG_DBG("connectivity LED busy (%d)", ret);
        return;
    }
    LOG_DBG("USB powered: pinned connectivity LED to color %d", color);
}

static void pin_battery(void) {
#if IS_ENABLED(CONFIG_ZMK_BATTERY_REPORTING)
    uint8_t level = zmk_battery_state_of_charge();

    /* Below "full" the widget itself already holds the battery LED with a
     * persistent green pulse (indicate_battery_enhanced(), charging branch),
     * so re-asserting would only glitch one animation frame.  At or above
     * "full" it shows a *timed* static green and then lets the LED go dark -
     * that is the case that has to be pinned.  The exception is a blank the
     * widget did itself on going idle: then there is no pulse left to protect
     * and the LED has to be brought back at any level. */
    if (level < CORNIX_LED_BATTERY_FULL_PCT && !battery_needs_pin) {
        return;
    }

    int ret = ws2812_set_status_led(STATUS_BATTERY, CONFIG_RGBLED_WIDGET_BATTERY_COLOR_CHARGING, 0,
                                    true);
    if (ret != 0) {
        LOG_DBG("battery LED busy (%d)", ret);
        return;
    }
    battery_needs_pin = false;
    LOG_DBG("USB powered: pinned battery LED (%d%%)", level);
#endif
}

static void pin_work_cb(struct k_work *work) {
    ARG_UNUSED(work);

    if (!zmk_usb_is_powered()) {
        return;
    }

    pin_connectivity();
    pin_battery();
}

/* USB has gone away: hand the LEDs back to the widget's timed behaviour.
 *
 * The battery LED needs nothing - the widget's own usb_conn_state_changed
 * handler calls indicate_battery(), which re-arms a BATTERY_BLINK_MS timeout.
 * The connectivity LED does: it is only re-rendered on an endpoint / profile /
 * split event, so ask for one explicitly.  That call goes through
 * set_status_led(..., CONN_*_DURATION_MS, false), i.e. it becomes a shared,
 * expiring status again, after which EXT_POWER_TIMEOUT_MS cuts the rail. */
static void release(void) {
#if IS_ENABLED(CONFIG_ZMK_BLE)
    indicate_connectivity();
#endif
}

static int leds_on_usb_listener(const zmk_event_t *eh) {
    ARG_UNUSED(eh);

    const struct zmk_activity_state_changed *activity = as_zmk_activity_state_changed(eh);
    if (activity != NULL) {
        if (activity->state == ZMK_ACTIVITY_SLEEP) {
            /* Deep sleep powers the rail down on purpose; do not fight it. */
            k_work_cancel_delayable(&pin_work);
            battery_needs_pin = false;
            return 0;
        }
        if (activity->state == ZMK_ACTIVITY_IDLE) {
            /* The widget blanks the whole strip here and does not restore it
             * (only ZMK_ACTIVITY_ACTIVE re-runs its indications), so the
             * battery LED has to be brought back explicitly. */
            battery_needs_pin = true;
        }
    }

    if (zmk_usb_is_powered()) {
        /* Always *re*schedule: the widget renders the new state from its own
         * (16 ms debounced) work item, and on an activity change it first
         * clears every LED, so the pin has to land last. */
        k_work_reschedule(&pin_work, K_MSEC(CONFIG_CORNIX_INDICATOR_LEDS_ON_USB_DELAY_MS));
    } else {
        k_work_cancel_delayable(&pin_work);
        release();
    }

    return 0;
}

ZMK_LISTENER(cornix_leds_on_usb, leds_on_usb_listener);
ZMK_SUBSCRIPTION(cornix_leds_on_usb, zmk_usb_conn_state_changed);
ZMK_SUBSCRIPTION(cornix_leds_on_usb, zmk_activity_state_changed);
#if CORNIX_LED_CENTRAL
#if IS_ENABLED(CONFIG_ZMK_BLE)
ZMK_SUBSCRIPTION(cornix_leds_on_usb, zmk_endpoint_changed);
ZMK_SUBSCRIPTION(cornix_leds_on_usb, zmk_ble_active_profile_changed);
#endif
#elif IS_ENABLED(CONFIG_ZMK_SPLIT_BLE)
ZMK_SUBSCRIPTION(cornix_leds_on_usb, zmk_split_peripheral_status_changed);
#endif
#if IS_ENABLED(CONFIG_ZMK_BATTERY_REPORTING)
ZMK_SUBSCRIPTION(cornix_leds_on_usb, zmk_battery_state_changed);
#endif

/* Boot: the widget's own init thread starts 200 ms after boot and spends
 * BATTERY_BLINK_MS + INTERVAL_MS showing the battery before it shows
 * connectivity, so the first pin has to wait for all of that. */
static int leds_on_usb_init(void) {
    k_work_init_delayable(&pin_work, pin_work_cb);
    k_work_schedule(&pin_work, K_MSEC(CONFIG_CORNIX_INDICATOR_LEDS_ON_USB_BOOT_DELAY_MS));
    return 0;
}

SYS_INIT(leds_on_usb_init, APPLICATION, CONFIG_APPLICATION_INIT_PRIORITY);
