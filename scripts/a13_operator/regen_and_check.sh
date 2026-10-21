#!/bin/bash
# SPDX-License-Identifier: AGPL-3.0-only
set -u
cd $HOME/Documents/playground/mycelium-core-pilot || exit 1
VENV=$HOME/Documents/playground/mycelium-a13-macos-demo/.venv/bin/python
PY=/opt/homebrew/bin/python3
echo "=== regenerate compatibility fixtures (expect no change) ==="
env -u PYTHONPATH -u PYTHONHOME -u VIRTUAL_ENV "$VENV" -B scripts/generate_contract_fixtures.py 2>&1 | tail -2
echo "=== regenerate the contract manifest (re-pins iroh.py + routeStatus.ts owner sources) ==="
env -u PYTHONPATH -u PYTHONHOME -u VIRTUAL_ENV "$VENV" -B scripts/generate_contract_manifest.py 2>&1 | tail -2
echo "=== contract audit ==="
env -u PYTHONPATH -u PYTHONHOME -u VIRTUAL_ENV "$PY" -B scripts/contract_audit.py --json 2>&1 | tail -3
echo "=== governance gate ==="
env -u PYTHONPATH -u PYTHONHOME -u VIRTUAL_ENV "$PY" -B scripts/governance_gate.py --repo-root . --json 2>&1 | tail -10
echo "=== governance/claims/contracts pytest ==="
env -u PYTHONPATH -u PYTHONHOME -u VIRTUAL_ENV "$PY" -B -m pytest -q tests/governance tests/claims tests/contracts -p no:cacheprovider 2>&1 | tail -6
echo "=== git status ==="
git status --porcelain | head
