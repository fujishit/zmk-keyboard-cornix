/*
 * Cornix: a macOS-style application switcher for Windows ("hold 41, tap Tab").
 *
 * Copyright (c) 2026 Cornix contributors
 * SPDX-License-Identifier: MIT
 *
 * The problem
 * -----------
 * On macOS the left thumb rest key (ZMK position 41) is Cmd, so "hold 41, tap
 * Tab" is Cmd+Tab: the switcher stays open for as long as Cmd is held and each
 * further Tab advances the selection.  On the Win layer the same key is Ctrl,
 * so the chord has to come out as *Alt*+Tab instead, and the previous
 * implementation did that with a mod-morph:
 *
 *     win_tab: win_tab {
 *         compatible = "zmk,behavior-mod-morph";
 *         bindings = <&kp TAB>, <&kp LA(TAB)>;
 *         mods = <(MOD_LCTL|MOD_RCTL)>;
 *     };
 *
 * `&kp LA(TAB)` carries LALT as an *implicit* modifier of the Tab keypress
 * (zmk/app/include/zmk/events/keycode_state_changed.h:35-39 puts SELECT_MODS()
 * into `implicit_modifiers` for a non-modifier usage), and
 * hid_listener_keycode_released() releases every implicit modifier together
 * with the key (zmk/app/src/hid_listener.c:86,
 * `zmk_hid_implicit_modifiers_release()`).  So Alt goes *down and up with each
 * Tab*, which is exactly what the Windows switcher takes as "commit and
 * close": the switcher flashes and the window changes on every tap instead of
 * staying open while the user picks a window.
 *
 * What this behavior does instead
 * -------------------------------
 * It makes Alt a real, *held* modifier that outlives the Tab taps and is
 * released only when the thumb key is released - i.e. the same shape as
 * Cmd+Tab on macOS:
 *
 *   press, hold-mod (LCTRL) is in the HID modifier state
 *       -> mask hold-mod out of the report   (zmk_hid_masked_modifiers_set)
 *       -> press switch-mod (LALT)           (a real modifier keypress)
 *       -> tap tap-key (TAB)
 *   press again, still in the switch state
 *       -> tap tap-key only; LALT stays down, the switcher stays open
 *   release of `mod-position` (41)
 *       -> release switch-mod, unmask hold-mod, leave the switch state
 *   press, hold-mod is NOT in the HID modifier state
 *       -> behave exactly like `&kp TAB` (press on press, release on release,
 *          so holding it still auto-repeats for indentation)
 *
 * The hold-mod is *masked*, not unregistered: zmk_hid_masked_modifiers_set()
 * only changes what the report shows
 * (zmk/app/src/hid.c:45, `(mods & ~masked_modifiers) | implicit_modifiers`),
 * it leaves ZMK's own explicit modifier reference counts alone
 * (zmk/app/src/hid.c:38-39, 54-74).  That matters twice:
 *
 *   - the `&kp LCTRL` on position 41 still gets a balanced
 *     register/unregister pair, so it never trips the "Tried to unregister
 *     modifier %d too often" error path (zmk/app/src/hid.c:63-66);
 *   - masking and unmasking do not send a report by themselves (SET_MODIFIERS
 *     only updates the report struct), so the order in which the position
 *     release reaches this behavior and ZMK's keymap listener cannot produce a
 *     stray Ctrl on the wire.  Concretely, with this module's subscriptions
 *     linking *after* ZMK's own (see "Listener order" below) the sequence on
 *     releasing 41 is: keymap -> `&kp LCTRL` release -> report {LALT}; then
 *     this listener -> LALT release -> report {}; then the unmask, which sends
 *     nothing.  Had it been the other way round the unmask would still be a
 *     no-op on the wire because the following LCTRL release sends {} anyway.
 *
 * The switch state ends on the release of `mod-position` and nothing else, so
 * any other key pressed while 41 is still held after an Alt+Tab also sees the
 * masked Ctrl (it comes out with Alt instead).  That is the documented,
 * accepted simplification: the chord is a switcher, and the user's hand leaves
 * 41 to end it.  As a safety net, a press that finds the switch state active
 * but the hold-mod gone from the HID state (a `mod-position` release this
 * behavior never saw, e.g. a layer change that swallowed it) cleans up first
 * and then starts over.
 *
 * Listener order
 * --------------
 * ZMK's event manager walks `.event_subscription` in *link* order
 * (zmk/app/src/event_manager.c:20-46, `__event_subscriptions_start + i`), and
 * a module's library links after ZMK's `app` objects, so this listener always
 * runs after ZMK's `keymap` listener for the same position event.  Nothing
 * here depends on that ordering (see above), it is only noted because it is
 * what makes the reports come out in the order the snapshot in
 * tests/sim/user-keymap/app-switch records.
 *
 * Devicetree
 * ----------
 *     app_tab: app_tab {
 *         compatible = "zmk,behavior-cornix-app-switch";
 *         #binding-cells = <0>;
 *         hold-mod = <LCTRL>;      // the modifier the finger is physically on
 *         switch-mod = <LALT>;     // the modifier the host should see instead
 *         tap = <TAB>;
 *         mod-position = <41>;     // ZMK key position of the hold-mod key
 *     };
 *
 * `hold-mod` and `switch-mod` are ZMK *keycodes* (LCTRL, LALT), not MOD_*
 * flags, so the keymap reads like the rest of the file; the flag needed for
 * masking is derived from the usage id at compile time.
 */

#define DT_DRV_COMPAT zmk_behavior_cornix_app_switch

#include <zephyr/device.h>
#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>
#include <zephyr/sys/util.h>

#include <drivers/behavior.h>

#include <zmk/behavior.h>
#include <zmk/event_manager.h>
#include <zmk/events/keycode_state_changed.h>
#include <zmk/events/position_state_changed.h>
#include <zmk/hid.h>
#include <zmk/keys.h>

#include <dt-bindings/zmk/hid_usage.h>
#include <dt-bindings/zmk/hid_usage_pages.h>

LOG_MODULE_DECLARE(zmk, CONFIG_ZMK_LOG_LEVEL);

#if DT_HAS_COMPAT_STATUS_OKAY(DT_DRV_COMPAT)

struct behavior_app_switch_config {
    /* Encoded ZMK keycodes, as `&kp <x>` would take them.  `hold-mod` itself is
     * only ever needed as a HID modifier bit, so it is kept as `hold_mod_flag`
     * below rather than as a keycode. */
    uint32_t switch_mod;
    uint32_t tap;
    /* The HID modifier bit of `hold_mod`, for zmk_hid_masked_modifiers_set(). */
    zmk_mod_flags_t hold_mod_flag;
    uint32_t mod_position;
};

struct behavior_app_switch_data {
    /* switch-mod is held and hold-mod is masked out of the report. */
    bool switching;
    /* The plain `&kp <tap>` path is down and owes a release. */
    bool tap_held;
};

/* raise_zmk_keycode_state_changed_from_encoded() is "press the key ZMK's `&kp`
 * would press": the same helper behavior_key_press.c:43 uses, so a modifier
 * keycode ends up in `explicit_modifiers` and a normal key as an ordinary
 * usage. */
static void app_switch_tap(const struct behavior_app_switch_config *cfg, int64_t timestamp) {
    raise_zmk_keycode_state_changed_from_encoded(cfg->tap, true, timestamp);
    raise_zmk_keycode_state_changed_from_encoded(cfg->tap, false, timestamp);
}

static void app_switch_begin(const struct device *dev, int64_t timestamp) {
    const struct behavior_app_switch_config *cfg = dev->config;
    struct behavior_app_switch_data *data = dev->data;

    /* Order matters: mask first, so the very first report that carries the
     * switch modifier does not also carry the held hold-mod. */
    zmk_hid_masked_modifiers_set(cfg->hold_mod_flag);
    raise_zmk_keycode_state_changed_from_encoded(cfg->switch_mod, true, timestamp);
    data->switching = true;

    LOG_DBG("app switch: entered, masking mods 0x%02X, holding 0x%02X", cfg->hold_mod_flag,
            (uint32_t)ZMK_HID_USAGE_ID(cfg->switch_mod));
}

static void app_switch_end(const struct device *dev, int64_t timestamp) {
    const struct behavior_app_switch_config *cfg = dev->config;
    struct behavior_app_switch_data *data = dev->data;

    data->switching = false;
    /* The hold-mod is NOT pressed again: the user has let go of it, and the
     * `&kp` on `mod-position` has already sent (or is about to send) its own
     * release. */
    raise_zmk_keycode_state_changed_from_encoded(cfg->switch_mod, false, timestamp);
    zmk_hid_masked_modifiers_clear();

    LOG_DBG("app switch: left, released 0x%02X", (uint32_t)ZMK_HID_USAGE_ID(cfg->switch_mod));
}

static int on_app_switch_binding_pressed(struct zmk_behavior_binding *binding,
                                         struct zmk_behavior_binding_event event) {
    const struct device *dev = zmk_behavior_get_binding(binding->behavior_dev);
    if (dev == NULL) {
        return -ENODEV;
    }
    const struct behavior_app_switch_config *cfg = dev->config;
    struct behavior_app_switch_data *data = dev->data;

    /* Read once: the only thing this handler raises before the second test is
     * the *switch* modifier's release, which cannot touch the hold-mod bit. */
    const bool hold_mod_down = (zmk_hid_get_explicit_mods() & cfg->hold_mod_flag) != 0;

    if (data->switching) {
        if (hold_mod_down) {
            /* Still holding 41: one more step through the switcher. */
            app_switch_tap(cfg, event.timestamp);
            return ZMK_BEHAVIOR_OPAQUE;
        }
        /* The release of `mod-position` never reached the listener; recover. */
        LOG_WRN("app switch: hold mod 0x%02X gone without a release of position %d",
                cfg->hold_mod_flag, cfg->mod_position);
        app_switch_end(dev, event.timestamp);
    }

    if (hold_mod_down) {
        app_switch_begin(dev, event.timestamp);
        app_switch_tap(cfg, event.timestamp);
        return ZMK_BEHAVIOR_OPAQUE;
    }

    /* No hold-mod: a plain `&kp <tap>`, release mirrored on the key release so
     * that holding it still auto-repeats. */
    data->tap_held = true;
    raise_zmk_keycode_state_changed_from_encoded(cfg->tap, true, event.timestamp);
    return ZMK_BEHAVIOR_OPAQUE;
}

static int on_app_switch_binding_released(struct zmk_behavior_binding *binding,
                                          struct zmk_behavior_binding_event event) {
    const struct device *dev = zmk_behavior_get_binding(binding->behavior_dev);
    if (dev == NULL) {
        return -ENODEV;
    }
    const struct behavior_app_switch_config *cfg = dev->config;
    struct behavior_app_switch_data *data = dev->data;

    if (data->tap_held) {
        data->tap_held = false;
        raise_zmk_keycode_state_changed_from_encoded(cfg->tap, false, event.timestamp);
    }
    /* In the switch state the tap was already completed on the press: the
     * switcher is driven by taps, and the switch modifier is let go of by the
     * release of `mod-position`, not of this key. */
    return ZMK_BEHAVIOR_OPAQUE;
}

static const struct behavior_driver_api behavior_app_switch_driver_api = {
    .binding_pressed = on_app_switch_binding_pressed,
    .binding_released = on_app_switch_binding_released,
#if IS_ENABLED(CONFIG_ZMK_BEHAVIOR_METADATA)
    .get_parameter_metadata = zmk_behavior_get_empty_param_metadata,
#endif // IS_ENABLED(CONFIG_ZMK_BEHAVIOR_METADATA)
};

#define APP_SWITCH_INST_PTR(n) DEVICE_DT_INST_GET(n),

static const struct device *const app_switch_devs[] = {
    DT_INST_FOREACH_STATUS_OKAY(APP_SWITCH_INST_PTR)};

/* The release of the key that physically holds the hold-mod is what ends the
 * switch state.  A position event is used rather than the hold-mod's keycode
 * release because it is unambiguous: a keycode release only says "some LCTRL
 * went up", while the position says it was *this* thumb key. */
static int app_switch_position_listener(const zmk_event_t *eh) {
    const struct zmk_position_state_changed *ev = as_zmk_position_state_changed(eh);
    if (ev == NULL || ev->state) {
        return ZMK_EV_EVENT_BUBBLE;
    }

    for (size_t i = 0; i < ARRAY_SIZE(app_switch_devs); i++) {
        const struct device *dev = app_switch_devs[i];
        const struct behavior_app_switch_config *cfg = dev->config;
        struct behavior_app_switch_data *data = dev->data;

        if (data->switching && ev->position == cfg->mod_position) {
            app_switch_end(dev, ev->timestamp);
        }
    }

    return ZMK_EV_EVENT_BUBBLE;
}

ZMK_LISTENER(cornix_app_switch, app_switch_position_listener);
ZMK_SUBSCRIPTION(cornix_app_switch, zmk_position_state_changed);

/* MOD_LCTL .. MOD_RGUI are BIT(usage - 0xE0); zmk_hid_register_mod() uses the
 * same arithmetic (zmk/app/src/hid.c, zmk_hid_keyboard_press()). */
#define APP_SWITCH_MOD_FLAG(keycode)                                                               \
    ((zmk_mod_flags_t)BIT(ZMK_HID_USAGE_ID(keycode) - HID_USAGE_KEY_KEYBOARD_LEFTCONTROL))

#define APP_SWITCH_ASSERT_IS_MOD(n, prop)                                                          \
    BUILD_ASSERT(ZMK_HID_USAGE_ID(DT_INST_PROP(n, prop)) >=                                        \
                         HID_USAGE_KEY_KEYBOARD_LEFTCONTROL &&                                     \
                     ZMK_HID_USAGE_ID(DT_INST_PROP(n, prop)) <= HID_USAGE_KEY_KEYBOARD_RIGHT_GUI,  \
                 "cornix-app-switch: " #prop " must be a modifier keycode (LCTRL .. RGUI)")

#define APP_SWITCH_INST(n)                                                                         \
    APP_SWITCH_ASSERT_IS_MOD(n, hold_mod);                                                         \
    APP_SWITCH_ASSERT_IS_MOD(n, switch_mod);                                                       \
    static const struct behavior_app_switch_config behavior_app_switch_config_##n = {              \
        .switch_mod = DT_INST_PROP(n, switch_mod),                                                 \
        .tap = DT_INST_PROP(n, tap),                                                               \
        .hold_mod_flag = APP_SWITCH_MOD_FLAG(DT_INST_PROP(n, hold_mod)),                           \
        .mod_position = DT_INST_PROP(n, mod_position),                                             \
    };                                                                                             \
    static struct behavior_app_switch_data behavior_app_switch_data_##n = {};                      \
    BEHAVIOR_DT_INST_DEFINE(n, NULL, NULL, &behavior_app_switch_data_##n,                          \
                            &behavior_app_switch_config_##n, POST_KERNEL,                          \
                            CONFIG_KERNEL_INIT_PRIORITY_DEFAULT,                                   \
                            &behavior_app_switch_driver_api);

DT_INST_FOREACH_STATUS_OKAY(APP_SWITCH_INST)

#endif /* DT_HAS_COMPAT_STATUS_OKAY(DT_DRV_COMPAT) */
