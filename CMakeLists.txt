# Cornix ZMK module - code part.
#
# Everything here is opt-in: with no CONFIG_CORNIX_* symbol enabled this file
# adds no library and no sources, so the board/shield/snippet parts of the
# module (zephyr/module.yml `settings:`) behave exactly as before.

if(CONFIG_CORNIX_BLE_PAUSE_ON_USB)
  zephyr_library()
  zephyr_library_sources(src/ble_usb_gate.c)

  # ZMK adds app/include to its own `app` target only, and PRIVATE at that
  # (zmk/app/CMakeLists.txt:25 `target_include_directories(app PRIVATE
  # include)`), so a separate zephyr_library does not inherit <zmk/...>.
  # Other ZMK modules dodge this by compiling into `app` with
  # target_sources(app ...); we keep our own library and add the include
  # directory explicitly instead.
  #
  # ZMK is the *application* here (west build -s zmk/app), so it has no
  # ZEPHYR_ZMK_MODULE_DIR; fall back to it only for setups where ZMK is
  # pulled in as a module instead.
  if(EXISTS ${APPLICATION_SOURCE_DIR}/include/zmk/ble.h)
    set(cornix_zmk_include ${APPLICATION_SOURCE_DIR}/include)
  elseif(DEFINED ZEPHYR_ZMK_MODULE_DIR)
    set(cornix_zmk_include ${ZEPHYR_ZMK_MODULE_DIR}/app/include)
  else()
    message(FATAL_ERROR
      "CONFIG_CORNIX_BLE_PAUSE_ON_USB needs ZMK's app/include directory, which was "
      "found neither at ${APPLICATION_SOURCE_DIR}/include nor through "
      "ZEPHYR_ZMK_MODULE_DIR.")
  endif()
  target_include_directories(${ZEPHYR_CURRENT_LIBRARY} PRIVATE ${cornix_zmk_include})
endif()

if(CONFIG_CORNIX_CRYPTO_SELFTEST)
  # Its own library (zephyr_library() above is already taken by the BLE/USB
  # gate and names its target after this directory, so a second call would
  # collide when both symbols are on).  The self-test only uses Zephyr and PSA
  # headers, so unlike the gate it needs no ZMK include path - and it therefore
  # also builds on a board without ZMK, e.g. native_sim.
  zephyr_library_named(cornix_crypto_selftest)
  zephyr_library_sources(src/crypto_selftest.c)

  # <psa/crypto.h> lives in the mbedTLS module, not on Zephyr's default include
  # path; the `mbedTLS` interface target carries it.  This is exactly what
  # Zephyr's own PSA users do, e.g.
  # zephyr/subsys/bluetooth/crypto/CMakeLists.txt:
  #   zephyr_library_link_libraries_ifdef(CONFIG_MBEDTLS mbedTLS)
  zephyr_library_link_libraries_ifdef(CONFIG_MBEDTLS mbedTLS)
endif()

if(CONFIG_CORNIX_REMOTE_BOOT)
  # Third library in this file (zephyr_library() is taken by the BLE/USB gate,
  # zephyr_library_named(cornix_crypto_selftest) by the PSA self-test), so this
  # one is named too.
  zephyr_library_named(cornix_remote_boot)
  zephyr_library_sources(src/remote_boot.c)

  # Same problem and same fix as the BLE/USB gate above: ZMK adds app/include
  # to its own `app` target PRIVATEly, so a separate zephyr_library does not
  # see <zmk/behavior.h> or <dt-bindings/zmk/reset.h> without this.
  if(EXISTS ${APPLICATION_SOURCE_DIR}/include/zmk/behavior.h)
    set(cornix_remote_boot_zmk_include ${APPLICATION_SOURCE_DIR}/include)
  elseif(DEFINED ZEPHYR_ZMK_MODULE_DIR)
    set(cornix_remote_boot_zmk_include ${ZEPHYR_ZMK_MODULE_DIR}/app/include)
  else()
    message(FATAL_ERROR
      "CONFIG_CORNIX_REMOTE_BOOT needs ZMK's app/include directory, which was found "
      "neither at ${APPLICATION_SOURCE_DIR}/include nor through ZEPHYR_ZMK_MODULE_DIR.")
  endif()
  target_include_directories(${ZEPHYR_CURRENT_LIBRARY} PRIVATE ${cornix_remote_boot_zmk_include})
endif()

if(CONFIG_CORNIX_APP_SWITCH)
  # Fourth library in this file (zephyr_library() is taken by the BLE/USB gate,
  # zephyr_library_named(cornix_crypto_selftest) by the PSA self-test and
  # zephyr_library_named(cornix_remote_boot) by the 1200 bps touch), so this one
  # is named too.
  zephyr_library_named(cornix_app_switch)
  zephyr_library_sources(src/behavior_app_switch.c)

  # Same problem and same fix as the two libraries above: ZMK adds app/include
  # to its own `app` target PRIVATEly (zmk/app/CMakeLists.txt:25), so a separate
  # zephyr_library sees neither <drivers/behavior.h> nor <zmk/hid.h>.
  if(EXISTS ${APPLICATION_SOURCE_DIR}/include/drivers/behavior.h)
    set(cornix_app_switch_zmk_include ${APPLICATION_SOURCE_DIR}/include)
  elseif(DEFINED ZEPHYR_ZMK_MODULE_DIR)
    set(cornix_app_switch_zmk_include ${ZEPHYR_ZMK_MODULE_DIR}/app/include)
  else()
    message(FATAL_ERROR
      "CONFIG_CORNIX_APP_SWITCH needs ZMK's app/include directory, which was found "
      "neither at ${APPLICATION_SOURCE_DIR}/include nor through ZEPHYR_ZMK_MODULE_DIR.")
  endif()
  target_include_directories(${ZEPHYR_CURRENT_LIBRARY} PRIVATE ${cornix_app_switch_zmk_include})
endif()
