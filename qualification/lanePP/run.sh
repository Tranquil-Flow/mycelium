#!/bin/bash
# SPDX-License-Identifier: AGPL-3.0-only
# Model-free lane PP checks only. Native libraries are linked, never a model run.
set -uo pipefail
cd /Users/mycelium-dev/Projects/mycelium-v02-m4-lanePP || exit 1
export CARGO_TARGET_DIR="$PWD/target" CARGO_NET_OFFLINE=true
export TMPDIR="$HOME/.hermes/cache/scratch"
export MLXC_INCLUDE_DIR="$HOME/Projects/mycelium-spike-mlxc/vendor/mlx-c-57731b00e8bed710e0750e7356da9f18f8eb3e20"
export MLXC_LIB_DIR="$HOME/Projects/mycelium-native-macos14/install/lib"
export MYCELIUM_BOOTSTRAP_TEST_CONFIG="$HOME/Projects/mycelium-m4-program/admitted/hello-bootstrap-program.json"
renice -n 5 -p $$ || exit 1
if compgen -G "$HOME/mycelium-demo-run/*.busy"; then
    printf 'BLOCKED: release busy marker\n' >&2
    exit 2
fi
if ! ps -axo command | perl -ne 'exit 1 if /\/PARENT-cert-[^ ]*-when-quiet[.]sh(?: |$)/'; then
    printf 'BLOCKED: release wrapper waiting\n' >&2
    exit 3
fi
label=$1
shift
/usr/bin/time -p env -u PYTHONPATH nice -n 19 env \
    DYLD_FALLBACK_LIBRARY_PATH="$MLXC_LIB_DIR" cargo "$@" \
    > "qualification/lanePP/$label.log" 2>&1
result=$?
printf '%s exit=%s\n' "$label" "$result"
exit "$result"
