# Cornix ZMK module - code part.
#
# Everything here is opt-in: with no CONFIG_CORNIX_* symbol enabled this file
# adds no library and no sources, so the board/shield/snippet parts of the
# module (zephyr/module.yml `settings:`) behave exactly as before.

# ZMK adds app/include to its own `app` target only, and PRIVATE at that
# (zmk/app/CMakeLists.txt:25 `target_include_directories(app PRIVATE
# include)`), so a separate zephyr_library does not inherit <zmk/...>.  Other
# ZMK modules dodge this by compiling into `app` with target_sources(app ...);
# we keep our own libraries - each source below is its own zephyr_library, so
# their ZMK_LISTENERs link after ZMK's own - and add the include directory
# explicitly instead.
#
# ZMK is the *application* here (west build -s zmk/app), so it has no
# ZEPHYR_ZMK_MODULE_DIR; fall back to it only for setups where ZMK is pulled in
# as a module instead.  Resolved once, lazily, by the first library that needs
# it; the crypto self-test does not (Zephyr and PSA headers only, which is also
# why it builds on a board without ZMK, e.g. native_sim).
function(cornix_zmk_library name source)
  zephyr_library_named(${name})
  zephyr_library_sources(${source})

  if(NOT DEFINED cornix_zmk_include)
    if(EXISTS ${APPLICATION_SOURCE_DIR}/include/zmk/behavior.h)
      set(cornix_zmk_include ${APPLICATION_SOURCE_DIR}/include)
    elseif(DEFINED ZEPHYR_ZMK_MODULE_DIR)
      set(cornix_zmk_include ${ZEPHYR_ZMK_MODULE_DIR}/app/include)
    else()
      message(FATAL_ERROR
        "${name} (${source}) needs ZMK's app/include directory, which was found "
        "neither at ${APPLICATION_SOURCE_DIR}/include nor through ZEPHYR_ZMK_MODULE_DIR.")
    endif()
    set(cornix_zmk_include ${cornix_zmk_include} PARENT_SCOPE)
  endif()
  target_include_directories(${name} PRIVATE ${cornix_zmk_include})
endfunction()

if(CONFIG_CORNIX_BLE_PAUSE_ON_USB)
  cornix_zmk_library(cornix_ble_usb_gate src/ble_usb_gate.c)
endif()

if(CONFIG_CORNIX_CRYPTO_SELFTEST)
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
  cornix_zmk_library(cornix_remote_boot src/remote_boot.c)
endif()

if(CONFIG_CORNIX_APP_SWITCH)
  cornix_zmk_library(cornix_app_switch src/behavior_app_switch.c)
endif()
