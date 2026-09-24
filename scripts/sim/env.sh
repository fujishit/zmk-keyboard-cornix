#!/usr/bin/env bash
# Shared environment preamble for scripts/sim/run.sh and scripts/bsim/run.sh.
#
# Sourced, never executed: it activates the simulation venv, puts the tools
# bootstrap.sh built on PATH, selects the "host" toolchain (native_sim and
# nrf52_bsim need only the host gcc, no Zephyr SDK) and refuses to continue
# when the west workspace is not usable.  A failed check exits the *calling*
# script with status 2.
#
# Expects REPO_ROOT.  Honours (and fills in the defaults of) SIM_WS,
# ZMK_APP_DIR and SIM_VENV, and defines die().
#
# shellcheck shell=bash

: "${REPO_ROOT:?scripts/sim/env.sh must be sourced with REPO_ROOT set}"
SIM_WS="${SIM_WS:-$REPO_ROOT/.sim/ws}"
ZMK_APP_DIR="${ZMK_APP_DIR:-$SIM_WS/zmk/app}"
SIM_VENV="${SIM_VENV:-$REPO_ROOT/.sim/venv}"

die() { echo "error: $*" >&2; exit 2; }

if [[ -f "$SIM_VENV/bin/activate" ]]; then
    # shellcheck disable=SC1091
    source "$SIM_VENV/bin/activate"
fi
# Tools built by bootstrap.sh (e.g. GNU make when the host lacks it).
if [[ -d "$REPO_ROOT/.sim/tools/bin" ]]; then
    export PATH="$REPO_ROOT/.sim/tools/bin:$PATH"
fi
export ZEPHYR_TOOLCHAIN_VARIANT="${ZEPHYR_TOOLCHAIN_VARIANT:-host}"
unset ZEPHYR_BASE # let west/cmake resolve it from the workspace

command -v west >/dev/null 2>&1 ||
    die "west not found. Run scripts/sim/bootstrap.sh first or set SIM_VENV."
[[ -f "$ZMK_APP_DIR/CMakeLists.txt" ]] ||
    die "ZMK app not found at $ZMK_APP_DIR (set ZMK_APP_DIR or SIM_WS, or run scripts/sim/bootstrap.sh)."
# west finds its workspace by walking up from the cwd, so every build below
# runs from inside $SIM_WS (all other paths passed to it are absolute).
[[ -d "$SIM_WS/.west" ]] ||
    die "$SIM_WS is not a west workspace (no .west/); run scripts/sim/bootstrap.sh or set SIM_WS."
