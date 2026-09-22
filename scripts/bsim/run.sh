#!/usr/bin/env bash
# Build and run the Cornix split-latency BabbleSim simulation.
#
# Up to three full device images run as separate processes on Zephyr's
# `nrf52_bsim` board, i.e. with the real Zephyr BLE host *and* the real nRF52
# link-layer controller, talking over BabbleSim's simulated 2.4 GHz phy:
#
#   d=0  ZMK split BLE *peripheral*  (the right half) -- scripted key events
#   d=1  ZMK split BLE *central*     (the left half)
#   d=2  a plain Zephyr BLE central  (tests/bsim/host) standing in for the
#        computer the keyboard types into: it connects to the left half's HID
#        service and subscribes to its input reports, so the left half has to
#        schedule two connections at once, as it does on hardware.
#
# All processes share the simulated clock, so the log timestamps of every
# device are directly comparable and the peripheral-key -> central-keymap and
# peripheral-key -> host-HID-report delays can be measured in simulated time.
#
# The peripheral replays a scripted `zmk,kscan-mock` sequence
# (tests/bsim/split-latency/peripheral/nrf52_bsim.keymap) on the real Cornix
# 50-key transform; the central has no local key events.
#
# Usage:
#   scripts/bsim/run.sh [options]
#     --latency 30,0     comma separated CONFIG_ZMK_SPLIT_BLE_PREF_LATENCY
#                        values to build the central for (default "30,0")
#     --sim-length N     simulated seconds per variant (default 25)
#     --seed N           BabbleSim random seed (default 11)
#     --per X            synchronization packet error rate for the simulated
#                        radio, 0..1 (default 0 = the ideal BabbleSim "magic"
#                        modem). Anything > 0 switches the phy to the
#                        BLE_simple modem with -PER=X.
#     --host             run the simulated host computer too (default)
#     --no-host          two devices only, the pre-host behaviour
#     --host-interval N  connection interval the host asks for, in 1.25 ms
#                        units (default 12 = 15 ms, macOS-like)
#     --host-latency N   peripheral latency the host asks for (default 0)
#     --host-timeout N   supervision timeout the host asks for, in 10 ms units
#                        (default 400 = 4 s)
#     --host-accept-updates 0|1
#                        whether the host accepts the keyboard's own parameter
#                        update request (default 1, like macOS)
#     --central-pref-latency N
#                        override CONFIG_BT_PERIPHERAL_PREF_LATENCY on the
#                        central, i.e. the latency the left half asks the host
#                        for on the *host* link (ZMK's default is 30)
#     --scenario NAME    which peripheral key script to replay:
#                        tests/bsim/split-latency/peripheral-NAME/ (e.g.
#                        "fast", the fast-typing burst). Its image is built in
#                        $BSIM_BUILD_DIR/peripheral-NAME and the logs go to
#                        $BSIM_BUILD_DIR/logs-NAME. Default: the original
#                        slow script in tests/bsim/split-latency/peripheral/
#     --log-dir DIR      where to put the logs (default $BSIM_BUILD_DIR/logs,
#                        or logs-NAME with --scenario)
#     --no-build         reuse the existing executables
#     --no-measure       only run, do not call scripts/bsim/measure.py
#     --json FILE        pass --json FILE to measure.py
#     -h, --help         this text
#
# Environment overrides:
#   SIM_WS          west workspace         (default <repo>/.sim/ws)
#   ZMK_APP_DIR     path to zmk/app        (default $SIM_WS/zmk/app)
#   SIM_VENV        venv to activate       (default <repo>/.sim/venv)
#   BSIM_OUT_PATH   BabbleSim install      (default <repo>/.sim/bsim)
#   BSIM_BUILD_DIR  build output root      (default <repo>/.build/bsim)
#
# Exit status: 0 when every variant ran and the measurement succeeded.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SIM_WS="${SIM_WS:-$REPO_ROOT/.sim/ws}"
ZMK_APP_DIR="${ZMK_APP_DIR:-$SIM_WS/zmk/app}"
SIM_VENV="${SIM_VENV:-$REPO_ROOT/.sim/venv}"
BSIM_OUT_PATH="${BSIM_OUT_PATH:-$REPO_ROOT/.sim/bsim}"
BSIM_BUILD_DIR="${BSIM_BUILD_DIR:-$REPO_ROOT/.build/bsim}"
CASE_DIR="$REPO_ROOT/tests/bsim/split-latency"
HOST_DIR="$REPO_ROOT/tests/bsim/host"
BOARD="nrf52_bsim"
MULTILIB_DIR="$REPO_ROOT/.sim/tools/i386-multilib"

latencies="30,0"
sim_length=25
seed=11
per=""
with_host=1
host_interval=12
host_latency=0
host_timeout=400
host_accept_updates=1
central_pref_latency=""
scenario=""
log_dir=""
no_build=0
no_measure=0
json_out=""

usage() { sed -n '2,/^set -euo/p' "${BASH_SOURCE[0]}" | grep -v '^set -euo' | sed 's/^# \{0,1\}//'; }

while [[ $# -gt 0 ]]; do
    case "$1" in
        --latency) latencies="$2"; shift ;;
        --sim-length) sim_length="$2"; shift ;;
        --seed) seed="$2"; shift ;;
        --per) per="$2"; shift ;;
        --host) with_host=1 ;;
        --no-host) with_host=0 ;;
        --host-interval) host_interval="$2"; shift ;;
        --host-latency) host_latency="$2"; shift ;;
        --host-timeout) host_timeout="$2"; shift ;;
        --host-accept-updates) host_accept_updates="$2"; shift ;;
        --central-pref-latency) central_pref_latency="$2"; shift ;;
        --scenario) scenario="$2"; shift ;;
        --log-dir) log_dir="$2"; shift ;;
        --no-build) no_build=1 ;;
        --no-measure) no_measure=1 ;;
        --json) json_out="$2"; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
    esac
    shift
done

# --- environment ----------------------------------------------------------
if [[ -f "$SIM_VENV/bin/activate" ]]; then
    # shellcheck disable=SC1091
    source "$SIM_VENV/bin/activate"
fi
# .sim/tools/bsim-bin must come first: it holds the `gcc -m32` wrapper the
# 32-bit-only nrf52_bsim board needs (see scripts/bsim/bootstrap.sh).
export PATH="$REPO_ROOT/.sim/tools/bsim-bin:$REPO_ROOT/.sim/tools/bin:$PATH"
export BSIM_OUT_PATH
export BSIM_COMPONENTS_PATH="${BSIM_COMPONENTS_PATH:-$BSIM_OUT_PATH/components}"
export ZEPHYR_TOOLCHAIN_VARIANT="${ZEPHYR_TOOLCHAIN_VARIANT:-host}"
# glibc dlopen()s libgcc_s.so.1 for pthread_exit(); the 32-bit copy is not in
# any system path, and a DT_RUNPATH on the executable does not cover dlopen.
export LD_LIBRARY_PATH="$MULTILIB_DIR/usr/lib32${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
unset ZEPHYR_BASE

die() { echo "error: $*" >&2; exit 2; }
[[ -x "$BSIM_OUT_PATH/bin/bs_2G4_phy_v1" ]] || die "BabbleSim not built at $BSIM_OUT_PATH (run scripts/bsim/bootstrap.sh)"
[[ -d "$BSIM_OUT_PATH/nrf_hw_models" ]] || die "nrf_hw_models missing at $BSIM_OUT_PATH/nrf_hw_models (run scripts/bsim/bootstrap.sh)"
[[ -f "$ZMK_APP_DIR/CMakeLists.txt" ]] || die "ZMK app not found at $ZMK_APP_DIR (run scripts/sim/bootstrap.sh)"
[[ -d "$SIM_WS/.west" ]] || die "$SIM_WS is not a west workspace"
command -v west >/dev/null 2>&1 || die "west not found (run scripts/sim/bootstrap.sh or set SIM_VENV)"

IFS=',' read -r -a lat_list <<< "$latencies"
# --scenario NAME selects tests/bsim/split-latency/peripheral-NAME (its own
# peripheral image and log directory); the central and host images are shared.
periph_suffix=""
if [[ -n "$scenario" ]]; then
    periph_suffix="-$scenario"
    [[ -d "$CASE_DIR/peripheral$periph_suffix" ]] || die "no such scenario: $CASE_DIR/peripheral$periph_suffix"
fi
LOG_DIR="${log_dir:-$BSIM_BUILD_DIR/logs$periph_suffix}"
mkdir -p "$LOG_DIR"
HOST_EXE="$BSIM_BUILD_DIR/host/zephyr/zephyr.exe"

build() { # <build-dir> <zmk-config-dir> [extra cmake args...]
    local dir="$1" cfg="$2"; shift 2
    # Not inside $dir: `west build -p` wipes it, log file included.
    local log="$BSIM_BUILD_DIR/$(basename "$dir")-build.log"
    mkdir -p "$BSIM_BUILD_DIR"
    echo "  building $(basename "$dir") ..."
    if ! (cd "$SIM_WS" && west build -s "$ZMK_APP_DIR" -d "$dir" -b "$BOARD" -p -- \
            "-DZMK_CONFIG=$cfg" "-DZMK_EXTRA_MODULES=$REPO_ROOT;$BSIM_OUT_PATH/nrf_hw_models" \
            "$@") > "$log" 2>&1; then
        tail -n 30 "$log"
        die "build failed for $dir (see $log)"
    fi
}

build_host() { # the plain-Zephyr simulated computer
    local dir="$BSIM_BUILD_DIR/host"
    local log="$BSIM_BUILD_DIR/host-build.log"
    mkdir -p "$BSIM_BUILD_DIR"
    echo "  building host ..."
    # tests/bsim/host is not a ZMK application, so the ZMK modules of this
    # west workspace must be kept out of the build: several of them (e.g.
    # zmk-dongle-display) have Kconfig.defconfig files that only parse when
    # ZMK's own Kconfig tree is present, and Zephyr turns Kconfig warnings
    # into errors. Pass an explicit module list instead of the west-derived
    # one, minus everything named zmk*.
    local mods
    mods=$(cd "$SIM_WS" && west list -f "{name}|{abspath}" 2>/dev/null \
            | grep -v '^zmk' | grep -v '^manifest|' | grep -v '^zephyr|' \
            | cut -d'|' -f2 | paste -sd';')
    mods="$mods;$BSIM_OUT_PATH/nrf_hw_models"
    if ! (cd "$SIM_WS" && west build -s "$HOST_DIR" -d "$dir" -b "$BOARD" -p -- \
            "-DZEPHYR_MODULES=$mods") > "$log" 2>&1; then
        tail -n 30 "$log"
        die "build failed for $dir (see $log)"
    fi
}

# Build directory suffix so that a --central-pref-latency sweep does not keep
# rebuilding over the same tree.
central_suffix=""
central_extra=()
if [[ -n "$central_pref_latency" ]]; then
    central_suffix="-p$central_pref_latency"
    central_extra=("-DCONFIG_BT_PERIPHERAL_PREF_LATENCY=$central_pref_latency")
fi

if [[ $no_build -eq 0 ]]; then
    build "$BSIM_BUILD_DIR/peripheral$periph_suffix" "$CASE_DIR/peripheral$periph_suffix"
    for lat in "${lat_list[@]}"; do
        build "$BSIM_BUILD_DIR/central-$lat$central_suffix" "$CASE_DIR/central" \
            "-DCONFIG_ZMK_SPLIT_BLE_PREF_LATENCY=$lat" "${central_extra[@]}"
    done
    [[ $with_host -eq 1 ]] && build_host
fi

if [[ $with_host -eq 1 && ! -x "$HOST_EXE" ]]; then
    die "missing $HOST_EXE (drop --no-build, or pass --no-host)"
fi

run_variant() { # <latency>
    local lat="$1"
    local sim_id="cornix_sl${lat}_$$"
    local periph_exe="$BSIM_BUILD_DIR/peripheral$periph_suffix/zephyr/zmk.exe"
    local central_exe="$BSIM_BUILD_DIR/central-$lat$central_suffix/zephyr/zmk.exe"
    [[ -x "$periph_exe" ]] || die "missing $periph_exe (drop --no-build)"
    [[ -x "$central_exe" ]] || die "missing $central_exe (drop --no-build)"

    local n_dev=2
    [[ $with_host -eq 1 ]] && n_dev=3
    echo "  running latency=$lat (sim_length=${sim_length}s, seed=$seed, devices=$n_dev${scenario:+, scenario=$scenario}) ..."
    # -RealEncryption=1: the split link is encrypted, so the LL needs the real
    # AES from ext_libCryptov1 rather than plaintext PDUs.
    local -a pids=()
    "$periph_exe"  -s="$sim_id" -d=0 -RealEncryption=1 "-rs=$seed"        > "$LOG_DIR/peripheral-$lat.log" 2>&1 &
    pids+=($!)
    "$central_exe" -s="$sim_id" -d=1 -RealEncryption=1 "-rs=$((seed+11))" > "$LOG_DIR/central-$lat.log"    2>&1 &
    pids+=($!)
    rm -f "$LOG_DIR/host-$lat.log"
    if [[ $with_host -eq 1 ]]; then
        "$HOST_EXE" -s="$sim_id" -d=2 -RealEncryption=1 "-rs=$((seed+23))" \
            "-host_interval=$host_interval" "-host_latency=$host_latency" \
            "-host_timeout=$host_timeout" "-host_accept_updates=$host_accept_updates" \
            > "$LOG_DIR/host-$lat.log" 2>&1 &
        pids+=($!)
    fi
    local -a phy_args=(-s="$sim_id" "-D=$n_dev" "-sim_length=$((sim_length * 1000000))")
    if [[ -n "$per" ]]; then
        phy_args+=(-defmodem=BLE_simple -argsdefmodem "-PER=$per")
    fi
    (cd "$BSIM_OUT_PATH/bin" && ./bs_2G4_phy_v1 "${phy_args[@]}") \
        > "$LOG_DIR/phy-$lat.log" 2>&1 &
    pids+=($!)
    wait "${pids[@]}" 2>/dev/null || true
    rm -rf "/tmp/bs_${USER}/$sim_id" 2>/dev/null || true
}

start=$SECONDS
for lat in "${lat_list[@]}"; do
    run_variant "$lat"
done
echo "  simulation wall time: $((SECONDS - start))s"

if [[ $no_measure -eq 1 ]]; then
    echo "logs in $LOG_DIR"
    exit 0
fi

measure_args=(--log-dir "$LOG_DIR" --latency "$latencies")
[[ -n "$json_out" ]] && measure_args+=(--json "$json_out")
exec python3 "$REPO_ROOT/scripts/bsim/measure.py" "${measure_args[@]}"
