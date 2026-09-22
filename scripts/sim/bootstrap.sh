#!/usr/bin/env bash
# Bootstrap a self-contained host-simulation workspace for the Cornix keymap
# tests (tests/sim/**). Idempotent: safe to re-run at any time.
#
# What it creates (all ignored by git):
#   .sim/venv   Python venv with west, cmake, ninja and the Zephyr requirements
#   .sim/ws     west workspace whose manifest is a copy of config/west.yml
#               (zmk, zephyr, hal modules, zmk-helpers, ... are cloned here,
#               never into the repo root, which already has a zephyr/ dir)
#
# Usage:
#   scripts/sim/bootstrap.sh            # create/refresh everything
#   SIM_SKIP_UPDATE=1 scripts/sim/bootstrap.sh   # skip `west update`
#
# Requirements on the host: python3 (>=3.10, with venv/ensurepip), git,
# gcc/g++. No Zephyr SDK is needed for native_sim builds. GNU make is needed
# to build Zephyr's native simulator runner; if it is not on PATH it is built
# from source into .sim/tools (no root required).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SIM_DIR="${SIM_DIR:-$REPO_ROOT/.sim}"
VENV_DIR="$SIM_DIR/venv"
TOOLS_DIR="$SIM_DIR/tools"
MAKE_VERSION="${MAKE_VERSION:-4.4.1}"
MAKE_URL="https://ftp.gnu.org/gnu/make/make-${MAKE_VERSION}.tar.gz"
WS_DIR="${SIM_WS:-$SIM_DIR/ws}"
MANIFEST_SRC="$REPO_ROOT/config/west.yml"
MANIFEST_DIR="$WS_DIR/config"
PYTHON="${PYTHON:-python3}"

log() { printf '\033[1;34m[bootstrap]\033[0m %s\n' "$*"; }

log "repo root:      $REPO_ROOT"
log "venv:           $VENV_DIR"
log "west workspace: $WS_DIR"

# --- 1. Python venv --------------------------------------------------------
if [[ ! -x "$VENV_DIR/bin/python" ]]; then
    log "creating venv"
    "$PYTHON" -m venv "$VENV_DIR"
else
    log "venv already exists"
fi
# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"
python -m pip install --quiet --upgrade pip
log "installing west, cmake, ninja"
python -m pip install --quiet --upgrade west cmake ninja

# --- 2. west workspace ------------------------------------------------------
# west init -l needs a directory *inside* the workspace that holds west.yml.
# Copy the manifest there (a symlink would be resolved to the repo root, which
# is exactly what we want to avoid) and keep it in sync on every run.
mkdir -p "$MANIFEST_DIR"
if ! cmp -s "$MANIFEST_SRC" "$MANIFEST_DIR/west.yml"; then
    log "copying config/west.yml -> $MANIFEST_DIR/west.yml"
    cp "$MANIFEST_SRC" "$MANIFEST_DIR/west.yml"
fi
if [[ ! -d "$WS_DIR/.west" ]]; then
    log "west init -l $MANIFEST_DIR"
    west init -l "$MANIFEST_DIR"
else
    log "west workspace already initialised"
fi

cd "$WS_DIR"
if [[ -z "${SIM_SKIP_UPDATE:-}" ]]; then
    log "west update (shallow, blob-filtered) - this takes a while the first time"
    west update --narrow --fetch-opt=--filter=blob:none
else
    log "SIM_SKIP_UPDATE set, skipping west update"
fi

# --- 3. GNU make (needed by Zephyr's native_simulator runner build) -------
if command -v make >/dev/null 2>&1 || [[ -x "$TOOLS_DIR/bin/make" ]]; then
    log "make available: $(command -v make || echo "$TOOLS_DIR/bin/make")"
else
    log "make not found on PATH; building GNU make $MAKE_VERSION into $TOOLS_DIR"
    build_tmp="$(mktemp -d "${TMPDIR:-/tmp}/cornix-make.XXXXXX")"
    trap 'rm -rf "$build_tmp"' EXIT
    curl -fsSL "$MAKE_URL" | tar -xz -C "$build_tmp"
    (
        cd "$build_tmp/make-$MAKE_VERSION"
        ./configure --prefix="$TOOLS_DIR" --disable-dependency-tracking >configure.log 2>&1
        ./build.sh >build.log 2>&1        # bootstrap without an existing make
        ./make install >install.log 2>&1
    )
    log "installed $("$TOOLS_DIR/bin/make" --version | head -1)"
fi

# --- 4. Zephyr python requirements + cmake package export ----------------
if [[ -f "$WS_DIR/zephyr/scripts/requirements-base.txt" ]]; then
    log "installing zephyr/scripts/requirements-base.txt"
    python -m pip install --quiet -r "$WS_DIR/zephyr/scripts/requirements-base.txt"
else
    log "WARNING: zephyr/scripts/requirements-base.txt not found (west update skipped?)"
fi
log "west zephyr-export"
west zephyr-export >/dev/null

log "done. Run tests with: scripts/sim/run.sh"
