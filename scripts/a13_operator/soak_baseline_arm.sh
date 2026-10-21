#!/bin/bash
# SPDX-License-Identifier: AGPL-3.0-only
# Baseline soak experiment: is the delivery-cancellation shutdown timeout (2 of 3 runs at HEAD)
# pre-existing at 97fa04b, or introduced by this session's changes?
#
# The worktree shipped no native sidecar binary, and the harness pins the path inside its own root,
# so copy the PILOT tree's binary in: the Rust source is byte-identical between 97fa04b and HEAD
# (this session changed only Python/TS), so the binary is the same artifact.
set -u
PILOT=$HOME/Documents/playground/mycelium-core-pilot
BASE=/private/tmp/asm/baseline-97fa04b
SRC="$PILOT/native/iroh_transport/target/debug/mycelium-iroh-sidecar"
DST="$BASE/native/iroh_transport/target/debug"
mkdir -p "$DST" || exit 1
cp -p "$SRC" "$DST/" || exit 1
echo "sidecar sha256: $(shasum -a 256 "$DST/mycelium-iroh-sidecar" | awk '{print $1}' | cut -c1-16)"
echo "=== baseline (97fa04b) soak x3 ==="
cd "$BASE" || exit 1
for i in 1 2 3; do
  env -u PYTHONPATH -u PYTHONHOME -u VIRTUAL_ENV /opt/homebrew/opt/python@3.14/bin/python3.14 -B \
    -m mycelium_testbed.soak --seeds 2 --events 20 --run-id base-soak-$i \
    --out /tmp/asm/baseline-soak-out --json > /tmp/asm/base-soak-$i.log 2>&1
  code=$?
  errs=$(grep -c delivery_cancellation_shutdown_timeout /tmp/asm/base-soak-$i.log || true)
  echo "  baseline run $i: exit=$code timeout-errors=$errs"
done
