# SPDX-License-Identifier: MIT
#
# "host": a minimal simulated computer (a stand-in for the Mac) for the
# three-device Cornix BabbleSim simulation. Not a ZMK build -- a plain Zephyr
# Bluetooth central application for `nrf52_bsim`.
#
# Build (see scripts/bsim/run.sh, which does this for you):
#   west build -s tests/bsim/host -b nrf52_bsim \
#       -- -DEXTRA_ZEPHYR_MODULES=$BSIM_OUT_PATH/nrf_hw_models

cmake_minimum_required(VERSION 3.20.0)
find_package(Zephyr REQUIRED HINTS $ENV{ZEPHYR_BASE})
project(cornix_bsim_host)

target_sources(app PRIVATE src/main.c)
