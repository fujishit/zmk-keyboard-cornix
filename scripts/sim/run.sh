#!/usr/bin/env bash
# Build and run the Cornix keymap simulation tests (tests/sim/**) on the host.
#
# Each test case is a directory containing at least:
#   native_sim.keymap        keymap + mock kscan events (ZMK_MOCK_PRESS/RELEASE)
#   events.patterns          sed script selecting the log lines to compare
#   keycode_events.snapshot  expected output
# and optionally native_sim.overlay, native_sim.conf, extra-cmake-args, pending.
# This is exactly the layout used by ZMK's own app/run-test.sh, and the build
# is invoked the same way (board native_sim//zmk_test_mock, -DCONFIG_ASSERT=y,
# -DZMK_CONFIG=<case dir>, -DZMK_EXTRA_MODULES=<this repo>).
#
# Usage:
#   scripts/sim/run.sh [options] [case-dir ...]
#     case-dir       one or more directories (searched recursively); default: tests/sim
#     --verbose      print the filtered key events of every case
#     --auto-accept  overwrite keycode_events.snapshot with the actual output
#     --no-build     skip `west build`, re-run the existing executables
#     --keep-going   run all cases even if one fails to build (default)
#     -h, --help     this text
#
# Environment overrides (all optional):
#   SIM_WS          west workspace to use   (default: <repo>/.sim/ws)
#   ZMK_APP_DIR     path to zmk/app          (default: $SIM_WS/zmk/app)
#   SIM_VENV        venv to activate         (default: <repo>/.sim/venv; skipped
#                                             if it does not exist, e.g. in CI)
#   SIM_BUILD_DIR   build output root        (default: <repo>/.build/sim)
#   ZEPHYR_TOOLCHAIN_VARIANT  defaults to "host" (no Zephyr SDK needed)
#
# Exit status: 0 when every case passes (or is marked pending), 1 otherwise.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SIM_WS="${SIM_WS:-$REPO_ROOT/.sim/ws}"
ZMK_APP_DIR="${ZMK_APP_DIR:-$SIM_WS/zmk/app}"
SIM_VENV="${SIM_VENV:-$REPO_ROOT/.sim/venv}"
SIM_BUILD_DIR="${SIM_BUILD_DIR:-$REPO_ROOT/.build/sim}"
TESTS_ROOT="$REPO_ROOT/tests/sim"
BOARD="native_sim//zmk_test_mock"

verbose=0
auto_accept=0
no_build=0
paths=()

usage() { sed -n '2,/^set -euo/p' "${BASH_SOURCE[0]}" | grep -v '^set -euo' | sed 's/^# \{0,1\}//'; }

while [[ $# -gt 0 ]]; do
    case "$1" in
        --verbose) verbose=1 ;;
        --auto-accept) auto_accept=1 ;;
        --no-build) no_build=1 ;;
        --keep-going) ;;
        -h|--help) usage; exit 0 ;;
        --) shift; paths+=("$@"); break ;;
        -*) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
        *) paths+=("$1") ;;
    esac
    shift
done
[[ ${#paths[@]} -eq 0 ]] && paths=("$TESTS_ROOT")

# --- environment ----------------------------------------------------------
if [[ -f "$SIM_VENV/bin/activate" ]]; then
    # shellcheck disable=SC1091
    source "$SIM_VENV/bin/activate"
fi
# Tools built by bootstrap.sh (e.g. GNU make when the host lacks it).
if [[ -d "$REPO_ROOT/.sim/tools/bin" ]]; then
    export PATH="$REPO_ROOT/.sim/tools/bin:$PATH"
fi
if ! command -v west >/dev/null 2>&1; then
    echo "error: west not found. Run scripts/sim/bootstrap.sh first or set SIM_VENV." >&2
    exit 2
fi
if [[ ! -f "$ZMK_APP_DIR/CMakeLists.txt" ]]; then
    echo "error: ZMK app not found at $ZMK_APP_DIR (set ZMK_APP_DIR or SIM_WS, or run bootstrap.sh)." >&2
    exit 2
fi
# native_sim only needs the host gcc; do not require a Zephyr SDK.
export ZEPHYR_TOOLCHAIN_VARIANT="${ZEPHYR_TOOLCHAIN_VARIANT:-host}"
# west finds its workspace by walking up from the cwd, so builds run from
# inside $SIM_WS (all other paths passed to it are absolute).
if [[ ! -d "$SIM_WS/.west" ]]; then
    echo "error: $SIM_WS is not a west workspace (no .west/); run bootstrap.sh or set SIM_WS." >&2
    exit 2
fi
unset ZEPHYR_BASE # let west/cmake resolve it from the workspace

# --- collect cases ----------------------------------------------------------
cases=()
for p in "${paths[@]}"; do
    [[ -d "$p" ]] || { echo "error: not a directory: $p" >&2; exit 2; }
    while IFS= read -r km; do
        cases+=("$(dirname "$km")")
    done < <(find "$(cd "$p" && pwd)" -name native_sim.keymap | sort)
done
if [[ ${#cases[@]} -eq 0 ]]; then
    echo "error: no test cases (native_sim.keymap) found under: ${paths[*]}" >&2
    exit 2
fi

mkdir -p "$SIM_BUILD_DIR"
pass_fail="$SIM_BUILD_DIR/pass-fail.log"
: > "$pass_fail"
failed=0

run_case() {
    local case_dir="$1"
    local name build_dir build_log exe
    # Build-dir name: path relative to tests/sim, else relative to the repo,
    # else "external/<basename>" for ad-hoc case dirs living elsewhere.
    if [[ "$case_dir" == "$TESTS_ROOT"/* ]]; then
        name="${case_dir#"$TESTS_ROOT"/}"
    elif [[ "$case_dir" == "$REPO_ROOT"/* ]]; then
        name="${case_dir#"$REPO_ROOT"/}"
    else
        name="external/$(basename "$case_dir")"
    fi
    build_dir="$SIM_BUILD_DIR/$name"
    build_log="$build_dir/build.log"
    exe="$build_dir/zephyr/zmk.exe"

    echo "Running $name:"
    if [[ $no_build -eq 0 ]]; then
        mkdir -p "$build_dir"
        local -a cmd=(west build -s "$ZMK_APP_DIR" -d "$build_dir" -b "$BOARD" -p --
            -DCONFIG_ASSERT=y "-DZMK_CONFIG=$case_dir" "-DZMK_EXTRA_MODULES=$REPO_ROOT")
        if [[ -f "$case_dir/extra-cmake-args" ]]; then
            # shellcheck disable=SC2207
            cmd+=($(tr '\n' ' ' < "$case_dir/extra-cmake-args"))
        fi
        if ! (cd "$SIM_WS" && "${cmd[@]}") > "$build_log" 2>&1; then
            echo "FAILED: $name did not build (see $build_log)" | tee -a "$pass_fail"
            tail -n 30 "$build_log"
            return 1
        fi
    elif [[ ! -x "$exe" ]]; then
        echo "FAILED: $name has no executable at $exe (drop --no-build)" | tee -a "$pass_fail"
        return 1
    fi

    "$exe" |
        sed -e "s/.*> //" |
        tee "$build_dir/keycode_events_full.log" |
        sed -n -f "$case_dir/events.patterns" > "$build_dir/keycode_events.log"

    if [[ $verbose -eq 1 ]]; then
        echo "--- $name events:"
        cat "$build_dir/keycode_events.log"
        echo "---"
    fi

    if ! diff -auZ "$case_dir/keycode_events.snapshot" "$build_dir/keycode_events.log"; then
        if [[ -f "$case_dir/pending" ]]; then
            echo "PENDING: $name" | tee -a "$pass_fail"
            return 0
        fi
        if [[ $auto_accept -eq 1 ]]; then
            echo "Auto-accepting failure for $name"
            cp "$build_dir/keycode_events.log" "$case_dir/keycode_events.snapshot"
        else
            echo "FAILED: $name" | tee -a "$pass_fail"
            return 1
        fi
    fi
    echo "PASS: $name" | tee -a "$pass_fail"
}

for c in "${cases[@]}"; do
    run_case "$c" || failed=1
done

echo
echo "==== summary ($(wc -l < "$pass_fail") cases) ===="
sort -k2 "$pass_fail"
exit $failed
