/*
 * Cornix: enter the UF2 bootloader from the host, over the USB CDC-ACM console.
 *
 * Copyright (c) 2026 Cornix contributors
 * SPDX-License-Identifier: MIT
 *
 * Why
 * ---
 * Flashing a Cornix half means getting it into the Adafruit nRF52 UF2
 * bootloader, and the two documented ways both need a finger on the keyboard:
 * double-tapping the reset button, or pressing the `&bootloader` key.  That is
 * fine at the desk and impossible for anything remote (a CI-ish "build, flash,
 * capture the log" loop, or simply a keyboard that is plugged into a machine
 * you are ssh'd into).
 *
 * This implements the classic "1200 bps touch" instead, the same trick the
 * Arduino / Adafruit tooling uses: the host opens the CDC-ACM port, sets the
 * line coding to a magic baud rate and closes it again.  No data is sent, so it
 * works from any serial library - `mode COM5 baud=12` on Windows, `stty 1200`
 * on Linux, a three-line PowerShell snippet (see scripts/flash.py --enter).
 *
 * Mechanism, host -> device
 * -------------------------
 * A SetLineCoding control request lands in Zephyr's legacy USB device stack at
 * zephyr/subsys/usb/device/class/cdc_acm.c:158 (`case SET_LINE_CODING`).  It
 * remembers the old dwDTERate, copies the new line coding in, and - under
 * CONFIG_CDC_ACM_DTE_RATE_CALLBACK_SUPPORT - calls a user callback *only when
 * the rate actually changed* (cdc_acm.c:182-186):
 *
 *     if (rate != new_rate && dev_data->rate_cb != NULL) {
 *             dev_data->rate_cb(common->dev, new_rate);
 *     }
 *
 * The callback is registered with cdc_acm_dte_rate_callback_set()
 * (cdc_acm.c:724-737, declared in zephyr/include/zephyr/drivers/uart/cdc_acm.h:47),
 * which only checks that the device really is a CDC ACM device and then stores
 * the pointer in the *class driver's* own data.  That is worth spelling out,
 * because on a ZMK debug build the CDC device is owned by the console / logging
 * backend: CONFIG_ZMK_USB_LOGGING wires `chosen { zephyr,console = &cdc_acm_uart; }`
 * (zmk/app/snippets/zmk-usb-logging) and the UART log backend drives it through
 * the `uart` API.  The rate callback is not part of that API and does not
 * conflict with it - the log backend keeps writing bytes while we get told
 * about SetLineCoding - so registering it on the console device is safe.  What
 * we must NOT do is call uart_* configuration functions on it.
 *
 * The magic rates (all of them "impossible" for a real console):
 *
 *   1200 -> this half (the left/central, the one with the console) reboots into
 *           its UF2 bootloader;
 *   2400 -> the *right* half, which has no USB stack at all, is told to do the
 *           same over the split link (see below);
 *   4800 -> a plain cold reboot of this half, which is the "restart the
 *           firmware without unplugging" button.
 *
 * Any other rate is ignored, so a terminal program that opens the port at
 * 115200 (or at whatever it defaults to) cannot reboot the keyboard by
 * accident.  The action is deferred by CONFIG_CORNIX_REMOTE_BOOT_DELAY_MS on a
 * work item rather than run from the callback, for three reasons: the callback
 * runs in the USB stack's context and must return quickly so the control
 * transfer can be ACKed; the host still has to close the port cleanly; and a
 * host that sets the line coding twice in a row (some drivers do) would
 * otherwise fire twice.
 *
 * Mechanism, left -> right
 * ------------------------
 * The right half is a plain BLE peripheral build: no USB stack, no console,
 * nothing to touch.  It does however run every behavior the central asks it to
 * run, which is exactly how pressing `&bootloader` on a peripheral key already
 * works today:
 *
 *   - behavior_reset declares `.locality = BEHAVIOR_LOCALITY_EVENT_SOURCE`
 *     (zmk/app/src/behaviors/behavior_reset.c:61);
 *   - zmk_behavior_invoke_binding() therefore forwards it to the half the event
 *     came from: `zmk_split_central_invoke_behavior(event.source, ...)` when
 *     the source is not ZMK_POSITION_STATE_CHANGE_SOURCE_LOCAL
 *     (zmk/app/src/behavior.c:96-103);
 *   - that packs the *name* of the behavior into the split command
 *     (zmk/app/src/split/central.c:81-111, `command.data.invoke_behavior.behavior_dev`)
 *     and the BLE transport writes it to the peripheral's "run behavior"
 *     characteristic (zmk/app/src/split/bluetooth/central.c:1038-1062);
 *   - the peripheral looks the name up and calls behavior_keymap_binding_pressed()
 *     on it (zmk/app/src/split/peripheral.c:38-53).
 *
 * The name is "bootload" - the node name of the `bootloader:` label in
 * zmk/app/dts/behaviors/reset.dtsi, kept at 8 characters precisely because
 * "Behavior can be invoked on peripherals, so name must be <= 8 characters"
 * (the split payload's behavior_dev field is a fixed-size string).  reset.dtsi
 * is included by every ZMK build through app/dts/behaviors.dtsi, so
 * `zmk_behavior_get_binding("bootload")` resolves on the *stock* peripheral
 * firmware: the right half needs no change at all for this to work.
 *
 * We call zmk_split_central_invoke_behavior(0, ...) directly instead of going
 * through zmk_behavior_invoke_binding() with a faked event source, because the
 * direct call returns an error we can log when no peripheral is connected
 * (-ENODEV from split/central.c:83 when no transport is up).  Note that a
 * connected-but-idle peripheral is only detected one layer further down, where
 * the work item logs "Source not connected" and drops the command
 * (bluetooth/central.c:1031-1034) - so the warning here is a lower bound: see
 * the caveat in scripts/FLASHING.md.
 *
 * Only a *pressed* event is sent.  behavior_reset has no release handler and
 * the peripheral reboots on the press, so a matching release would have nothing
 * to arrive at.
 *
 * Why the local half also goes through the behavior
 * -------------------------------------------------
 * The local reboot could be a bare sys_reboot(RST_UF2) - 0x57, the magic the
 * Adafruit bootloader looks for, zmk/app/include/dt-bindings/zmk/reset.h:13 -
 * and on the nRF52 that value is what Zephyr's Nordic SoC code stores in
 * GPREGRET before the reset (ZMK forces CONFIG_NRF_STORE_REBOOT_TYPE_GPREGRET=y
 * for SOC_SERIES_NRF52X, zmk/app/Kconfig:54-60).  But behavior_reset has a
 * second path for boards that use the retention subsystem
 * (CONFIG_RETENTION_BOOT_MODE: bootmode_set() + sys_reboot(SYS_REBOOT_WARM),
 * behavior_reset.c:40-50), and hard-coding one of the two here would silently
 * break the other.  Invoking the behavior device itself is the same code that
 * the `&bootloader` key runs, whichever path the board is built with.
 */

#include <zephyr/kernel.h>
#include <zephyr/init.h>
#include <zephyr/sys/atomic.h>
#include <zephyr/device.h>
#include <zephyr/devicetree.h>
#include <zephyr/logging/log.h>
#include <zephyr/sys/reboot.h>
#include <zephyr/drivers/uart/cdc_acm.h>

#include <zmk/behavior.h>
#include <zmk/events/position_state_changed.h>

#if IS_ENABLED(CONFIG_ZMK_SPLIT) && IS_ENABLED(CONFIG_ZMK_SPLIT_ROLE_CENTRAL)
#include <zmk/split/central.h>
#endif

#if !IS_ENABLED(CONFIG_CORNIX_REMOTE_BOOT)
#error "src/remote_boot.c is only built with CONFIG_CORNIX_REMOTE_BOOT=y (see CMakeLists.txt)"
#endif

/* Same log module as the rest of this repository's code, so the "zmk:" prefix
 * the analyzers in scripts/log/ filter on is present. */
LOG_MODULE_DECLARE(zmk, CONFIG_ZMK_LOG_LEVEL);

/* The magic dwDTERate values.  Chosen from the low legacy rates no console
 * uses, with 1200 keeping the meaning it has everywhere else. */
#define RATE_LOCAL_BOOTLOADER 1200U
#define RATE_PERIPHERAL_BOOTLOADER 2400U
#define RATE_RESET 4800U

#define ACTION_NONE 0
#define ACTION_LOCAL_BOOTLOADER 1
#define ACTION_PERIPHERAL_BOOTLOADER 2
#define ACTION_RESET 3

/* reset.dtsi always defines it; assert rather than silently degrade. */
#if !DT_NODE_EXISTS(DT_NODELABEL(bootloader))
#error "CONFIG_CORNIX_REMOTE_BOOT needs ZMK's `bootloader:` reset behavior (app/dts/behaviors/reset.dtsi)"
#endif

/* "bootload" - the DT node name, which is what the peripheral looks up. */
#define BOOTLOADER_BEHAVIOR_NAME DEVICE_DT_NAME(DT_NODELABEL(bootloader))

static struct k_work_delayable action_work;
static atomic_t pending_action = ATOMIC_INIT(ACTION_NONE);

static struct zmk_behavior_binding_event make_event(uint8_t source) {
    struct zmk_behavior_binding_event event = {
        .layer = -1,
        .position = 0,
        .timestamp = k_uptime_get(),
    };
#if IS_ENABLED(CONFIG_ZMK_SPLIT)
    event.source = source;
#else
    ARG_UNUSED(source);
#endif
    return event;
}

static void enter_local_bootloader(void) {
    struct zmk_behavior_binding binding = {
        .behavior_dev = BOOTLOADER_BEHAVIOR_NAME,
        .param1 = 0,
        .param2 = 0,
    };

    /* Source LOCAL keeps zmk_behavior_invoke_binding() on this half even though
     * behavior_reset's locality is EVENT_SOURCE (behavior.c:96-103). */
    struct zmk_behavior_binding_event event = make_event(ZMK_POSITION_STATE_CHANGE_SOURCE_LOCAL);

    if (!zmk_behavior_get_binding(BOOTLOADER_BEHAVIOR_NAME)) {
        /* Should be unreachable - see the #error above - but never leave the
         * keyboard running with the user believing it is being flashed. */
        LOG_ERR("remote boot: no `%s` behavior, falling back to sys_reboot(RST_UF2)",
                BOOTLOADER_BEHAVIOR_NAME);
        sys_reboot(0x57 /* RST_UF2 */);
        return;
    }

    zmk_behavior_invoke_binding(&binding, event, true);
    /* Not reached: the behavior reboots. */
    LOG_ERR("remote boot: the bootloader behavior returned");
}

static void enter_peripheral_bootloader(void) {
#if IS_ENABLED(CONFIG_ZMK_SPLIT) && IS_ENABLED(CONFIG_ZMK_SPLIT_ROLE_CENTRAL)
    struct zmk_behavior_binding binding = {
        .behavior_dev = BOOTLOADER_BEHAVIOR_NAME,
        .param1 = 0,
        .param2 = 0,
    };
    struct zmk_behavior_binding_event event =
        make_event(CONFIG_CORNIX_REMOTE_BOOT_PERIPHERAL_INDEX);

    int err = zmk_split_central_invoke_behavior(CONFIG_CORNIX_REMOTE_BOOT_PERIPHERAL_INDEX,
                                                &binding, event, true);
    if (err) {
        LOG_WRN("remote boot: peripheral %d did not accept `%s` (err %d); is the other half "
                "connected?",
                CONFIG_CORNIX_REMOTE_BOOT_PERIPHERAL_INDEX, BOOTLOADER_BEHAVIOR_NAME, err);
    }
#else
    LOG_WRN("remote boot: this build is not a split central, ignoring the peripheral request");
#endif
}

static void action_work_handler(struct k_work *work) {
    ARG_UNUSED(work);

    switch (atomic_set(&pending_action, ACTION_NONE)) {
    case ACTION_LOCAL_BOOTLOADER:
        enter_local_bootloader();
        break;
    case ACTION_PERIPHERAL_BOOTLOADER:
        enter_peripheral_bootloader();
        break;
    case ACTION_RESET:
        sys_reboot(SYS_REBOOT_COLD);
        break;
    default:
        break;
    }
}

/* Runs in the USB device stack's context (cdc_acm.c:182-186): log, record, and
 * get out of the way so the control transfer is ACKed and the host's close()
 * completes before anything reboots. */
static void dte_rate_changed(const struct device *dev, uint32_t rate) {
    ARG_UNUSED(dev);

    int action = ACTION_NONE;

    switch (rate) {
    case RATE_LOCAL_BOOTLOADER:
        LOG_INF("remote boot: 1200 bps -> entering UF2 bootloader");
        action = ACTION_LOCAL_BOOTLOADER;
        break;
    case RATE_PERIPHERAL_BOOTLOADER:
        LOG_INF("remote boot: 2400 bps -> peripheral %d bootloader",
                CONFIG_CORNIX_REMOTE_BOOT_PERIPHERAL_INDEX);
        action = ACTION_PERIPHERAL_BOOTLOADER;
        break;
    case RATE_RESET:
        LOG_INF("remote boot: 4800 bps -> reset");
        action = ACTION_RESET;
        break;
    default:
        LOG_DBG("remote boot: ignoring DTE rate %u", rate);
        return;
    }

    atomic_set(&pending_action, action);
    k_work_reschedule(&action_work, K_MSEC(CONFIG_CORNIX_REMOTE_BOOT_DELAY_MS));
}

static int remote_boot_init(void) {
    k_work_init_delayable(&action_work, action_work_handler);

    const struct device *console = DEVICE_DT_GET(DT_CHOSEN(zephyr_console));

    if (!device_is_ready(console)) {
        LOG_WRN("remote boot: console device %s is not ready", console->name);
        return 0;
    }

    /* Returns -EINVAL when the chosen console is not a CDC ACM device, which is
     * the case for a UART console build; that is a configuration mistake worth a
     * log line, not a boot failure. */
    int err = cdc_acm_dte_rate_callback_set(console, dte_rate_changed);
    if (err) {
        LOG_WRN("remote boot: %s is not a CDC ACM device (err %d), disabled", console->name, err);
        return 0;
    }

    LOG_INF("remote boot: armed on %s (1200 = bootloader, 2400 = peripheral bootloader, "
            "4800 = reset)",
            console->name);
    return 0;
}

/* APPLICATION level, after the USB device stack (POST_KERNEL) and after ZMK's
 * behaviors are initialised, so zmk_behavior_get_binding() can resolve.  The
 * callback itself can only fire once the host has enumerated the port anyway. */
SYS_INIT(remote_boot_init, APPLICATION, CONFIG_APPLICATION_INIT_PRIORITY);
