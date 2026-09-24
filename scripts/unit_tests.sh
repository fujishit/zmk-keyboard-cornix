#!/usr/bin/env bash
# Every hardware-free unit-test suite of this repository, in one place.
#
# `just check-test` and .github/workflows/test.yml both call this, so a new
# tests/<name>/ directory cannot be added to one and forgotten in the other.
#
# Usage:
#   scripts/unit_tests.sh [suite ...]   # default: every suite below
#
# Each suite is a directory under tests/ that `python3 -m unittest discover`
# understands.  Everything is stdlib-only python except tests/sim (native_sim,
# scripts/sim/run.sh) and tests/bsim (BabbleSim): the latter is included here
# but skips itself when BabbleSim or its executables are missing, which is the
# case in CI.
#
# Exit status: 0 when every suite passes.
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# In dependency-free-first order; tests/bsim last because it is the slow one.
ALL_SUITES=(static log model vial flash remap cheatsheet bsim)

suites=("$@")
[[ ${#suites[@]} -eq 0 ]] && suites=("${ALL_SUITES[@]}")

# tests/bsim shells out to scripts/bsim/run.sh; without this it would rebuild
# all three nrf52_bsim images first.  Re-run scripts/bsim/run.sh by hand after
# changing a bsim .conf so that the executables match the sources again.
export BSIM_TEST_NO_BUILD="${BSIM_TEST_NO_BUILD:-1}"

failed=()
for suite in "${suites[@]}"; do
    dir="$REPO_ROOT/tests/$suite"
    if [[ ! -d "$dir" ]]; then
        echo "error: no such suite: tests/$suite" >&2
        exit 2
    fi
    echo
    echo "==== tests/$suite ===="
    if ! (cd "$REPO_ROOT" && python3 -m unittest discover -s "tests/$suite" -v); then
        failed+=("$suite")
    fi
done

echo
if [[ ${#failed[@]} -gt 0 ]]; then
    echo "==== FAILED: ${failed[*]} ===="
    exit 1
fi
echo "==== all ${#suites[@]} suites passed ===="
