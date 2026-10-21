#!/bin/bash
# SPDX-License-Identifier: AGPL-3.0-only
# Verify the fix against the exact thing that was failing: the readiness gate's seeded soak.
set -u
cd $HOME/Documents/playground/mycelium-core-pilot || exit 1
echo "=== HEAD soak x3 (2 of 3 failed before the fix) ==="
for i in 1 2 3; do
  env -u PYTHONPATH -u PYTHONHOME -u VIRTUAL_ENV /opt/homebrew/opt/python@3.14/bin/python3.14 -B \
    -m mycelium_testbed.soak --seeds 2 --events 20 --run-id fixed-soak-$i \
    --out /tmp/asm/head-soak-out --json > /tmp/asm/fixed-soak-$i.log 2>&1
  code=$?
  errs=$(grep -c delivery_cancellation_shutdown_timeout /tmp/asm/fixed-soak-$i.log || true)
  echo "  run $i: exit=$code timeout-errors=$errs"
done
echo "=== the gate's own parameters: 5 seeds x 50 events ==="
env -u PYTHONPATH -u PYTHONHOME -u VIRTUAL_ENV /opt/homebrew/opt/python@3.14/bin/python3.14 -B \
  -m mycelium_testbed.soak --seeds 5 --events 50 --run-id fixed-gate-soak \
  --out /tmp/asm/head-soak-out --json > /tmp/asm/fixed-gate-soak.log 2>&1
echo "  gate soak: exit=$? timeout-errors=$(grep -c delivery_cancellation_shutdown_timeout /tmp/asm/fixed-gate-soak.log || true)"
