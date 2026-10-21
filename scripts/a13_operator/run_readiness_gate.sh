#!/bin/bash
# SPDX-License-Identifier: AGPL-3.0-only
# Re-run the readiness gate after the UI + transport fixes (log written under a NEW name: gate logs are
# evidence and are never overwritten).
set -u
cd $HOME/Documents/playground/mycelium-core-pilot || exit 1
LABEL="${1:-$(git rev-parse --short HEAD)}"
LOG="/tmp/asm/gate-${LABEL}.log"
[ -s "$LOG" ] && { echo "REFUSING: $LOG exists (a gate log is evidence, never overwritten)"; exit 4; }
echo "HEAD=$(git rev-parse --short HEAD)  log=$LOG"
printf "worktree dirty files: "; git status --porcelain | wc -l | tr -d " "; echo
env -u PYTHONPATH -u PYTHONHOME -u VIRTUAL_ENV PYTHONDONTWRITEBYTECODE=1 \
  /opt/homebrew/bin/python3 -B scripts/pilot_readiness_gate.py --run-id wp8-02-useful-two-mac \
  --seeds 5 --events 50 > "$LOG" 2>&1
echo "gate_exit=$?"
echo "=== per-check verdict ==="
/opt/homebrew/bin/python3 -c "
import json
d = json.load(open('$HOME/Documents/mycelium-core-program/runs/wp8-02-useful-two-mac/readiness-gate.json'))
print('verdict:', d.get('verdict'))
for c in d['checks']:
    st = c.get('status') or c.get('verdict') or ('pass' if c.get('ok') else 'FAIL')
    print('  ', c.get('id'), st)
"
echo "=== failing checks in words ==="
grep -n "failing checks\|verdict:" "$LOG" | tail -4
