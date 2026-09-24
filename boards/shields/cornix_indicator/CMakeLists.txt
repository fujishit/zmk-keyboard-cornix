# Code part of the cornix_indicator shield.
#
# Zephyr adds a shield's CMakeLists.txt to the build when that shield is
# selected, and only then: zephyr/boards/shields/CMakeLists.txt walks
# ${SHIELD_DIRS} (filled by cmake/modules/shields.cmake with the directory of
# each -DSHIELD=... entry) and add_subdirectory()s every directory that has
# one.  So nothing here is compiled for a build without the shield, and no
# change to the repository's own Zephyr module (root CMakeLists.txt /
# zephyr/module.yml), to config/west.yml or to build.yaml is needed to reach
# this file.
#
# Sources go into the `app` target, like every ZMK module does, because the
# <zmk/...> headers are added PRIVATE to `app` only
# (zmk/app/CMakeLists.txt: target_include_directories(app PRIVATE include)).
# `app` already exists at this point - Zephyr creates it in
# cmake/modules/kernel.cmake right before add_subdirectory(${ZEPHYR_BASE}).
# <zmk_rgbled_widget/widget.h> needs no extra include path: the widget module
# exports it globally with zephyr_include_directories(include).

if(CONFIG_CORNIX_INDICATOR_LEDS_ON_USB
   AND CONFIG_RGBLED_WIDGET_WS2812
   AND CONFIG_USB_DEVICE_STACK)
  target_sources(app PRIVATE src/leds_on_usb.c)
endif()

# src/split_rssi.c needs no LED at all - it is a logging feature first - but it
# does need zmk_usb_is_powered(), which only exists with CONFIG_ZMK_USB
# (zmk/app/src/usb.c).  Its Kconfig symbol already carries the
# ZMK_SPLIT_ROLE_CENTRAL && BT part of the condition.  src/split_rssi.h is
# found by both files through a plain quoted include, because it sits next to
# them.
if(CONFIG_CORNIX_INDICATOR_SPLIT_RSSI AND CONFIG_ZMK_USB)
  target_sources(app PRIVATE src/split_rssi.c)
endif()
