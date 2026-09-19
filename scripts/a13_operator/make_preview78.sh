#!/bin/bash
# Make the preview.78 pipeline + round launchers by EDITING the proven .77 pair (launch law),
# then show every remaining old-version literal so nothing is missed silently.
#
# .77 -> .78 carries the one commit that must reach the laptop with it:
#   185b17f  the owner's cancellation/cleanup window minted from the registry (ERRATA-20)
set -u
cd /Users/evinova-self/Documents/playground/mycelium-core-pilot/scripts/a13_operator || exit 1

# 77 -> 78 first, then 76 -> 77 (the new baseline), so nothing is converted twice.
sub() {
  sed -e 's/preview\.77/preview.78/g' \
      -e 's/preview77/preview78/g' \
      -e 's/77r1/78r1/g' \
      -e 's/rpc-round77/rpc-round78/g' \
      -e 's/pre77/pre78/g' \
      -e 's/--version 77/--version 78/g' \
      -e 's/round15_supervisor\.py 77/round15_supervisor.py 78/' \
      -e 's/preview\.76/preview.77/g' \
      -e 's/preview76/preview77/g' \
      -e 's/pre76/pre77/g' \
      -e 's/--version 76/--version 77/g' \
      -e 's/round15_supervisor\.py 76/round15_supervisor.py 77/' \
      "$1"
}
sub chain-preview77.sh  > chain-preview78.sh
sub chain-preview77e.sh > chain-preview78e.sh
chmod +x chain-preview78.sh chain-preview78e.sh

echo "=== diff chain-preview77.sh -> chain-preview78.sh (substitutions only) ==="
diff chain-preview77.sh chain-preview78.sh
echo "=== diff chain-preview77e.sh -> chain-preview78e.sh ==="
diff chain-preview77e.sh chain-preview78e.sh | head -40
echo "=== changed lines (e): $(diff chain-preview77e.sh chain-preview78e.sh | grep -c '^[<>]') ==="
echo
echo "=== every remaining old-version literal in the NEW pair (must all be deliberate) ==="
grep -n "7[5-7]" chain-preview78.sh chain-preview78e.sh | grep -v '^[^:]*:[0-9]*:#' | head -16
echo
echo "=== the parameters that must be fresh ==="
grep -n "A13_LOG_LABEL=\|A13_TARGET_PREVIEW=\|A13_RPC_ROUND=\|ROUND 7\|version 7" chain-preview78.sh chain-preview78e.sh
