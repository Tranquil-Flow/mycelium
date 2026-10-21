#!/bin/bash
# SPDX-License-Identifier: AGPL-3.0-only
# A9 adjudication lane, 2026-09-21: sequential GREEN-baseline runner.
# Runs each suite one at a time (no parallelism: sibling lanes have caused
# port conflicts and stalls on this host). Every line is real pytest output.
set -u
LANE=$HOME/Documents/playground/mycelium-a9-adjudication-lane
EV=$HOME/Documents/mycelium-core-program/runs/a9-adjudication/evidence-2026-09-21/suites
PY=/opt/homebrew/bin/python3
cd "$LANE" || exit 1

run() {
  local name="$1"; shift
  echo "=== SUITE: $name"
  echo "=== CMD: env -u PYTHONPATH -u PYTHONHOME -u VIRTUAL_ENV PYTHONDONTWRITEBYTECODE=1 $PY -B -m pytest -q -p no:cacheprovider $*"
  echo "=== START $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  env -u PYTHONPATH -u PYTHONHOME -u VIRTUAL_ENV PYTHONDONTWRITEBYTECODE=1 \
    "$PY" -B -m pytest -q -p no:cacheprovider "$@" 2>&1 | tail -25
  echo "=== EXIT ${PIPESTATUS[0]}  END $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo
}

{
  run "iroh_conformance+request_conformance" tests/iroh_conformance tests/request_conformance
  run "node_agent" tests/node_agent
  run "contracts" tests/contracts
  run "product_spine" tests/product_spine
  run "governance" tests/governance
  run "security" tests/security
  run "seed_coordinator_deselected" tests/seed_coordinator --deselect tests/seed_coordinator/test_local_two_node_native_process_e2e.py::test_local_two_node_native_process_e2e
  run "lane_audit" tests/lane_audit
} > "$EV/baseline-2026-09-21.txt" 2>&1

echo "RUNNER DONE $(date -u +%Y-%m-%dT%H:%M:%SZ)"
