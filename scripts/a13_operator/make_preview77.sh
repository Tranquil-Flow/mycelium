#!/bin/bash
# Make the preview.77 pipeline + round launchers by EDITING the proven .76 pair (launch law),
# then show every remaining old-version literal so nothing is missed silently.
#
# .76 -> .77 carries the three commits that must reach the laptop with it:
#   b7f0e07  the RSS bound rebuilt from the 7B admission peak (the p76r1 unblock)
#   9bba4b8  the peer's reason kept across an honest "unknown" ack
#   bbe5283  a native exit names the commands it left unanswered
set -u
cd /Users/evinova-self/Documents/playground/mycelium-core-pilot/scripts/a13_operator || exit 1

# 76 -> 77 first, then 75 -> 76, so nothing is converted twice.
sub() {
  sed -e 's/preview\.76/preview.77/g' \
      -e 's/preview76/preview77/g' \
      -e 's/76r1/77r1/g' \
      -e 's/rpc-round76/rpc-round77/g' \
      -e 's/pre76/pre77/g' \
      -e 's/--version 76/--version 77/g' \
      -e 's/round15_supervisor\.py 76/round15_supervisor.py 77/' \
      -e 's/preview\.75/preview.76/g' \
      -e 's/preview75/preview76/g' \
      -e 's/pre75/pre76/g' \
      -e 's/--version 75/--version 76/g' \
      -e 's/round15_supervisor\.py 75/round15_supervisor.py 76/' \
      "$1"
}
sub chain-preview76.sh  > chain-preview77.sh
sub chain-preview76e.sh > chain-preview77e.sh
chmod +x chain-preview77.sh chain-preview77e.sh

echo "=== diff chain-preview76.sh -> chain-preview77.sh (substitutions only) ==="
diff chain-preview76.sh chain-preview77.sh
echo "=== diff chain-preview76e.sh -> chain-preview77e.sh ==="
diff chain-preview76e.sh chain-preview77e.sh | head -40
echo "=== changed lines (e): $(diff chain-preview76e.sh chain-preview77e.sh | grep -c '^[<>]') ==="
echo
echo "=== every remaining old-version literal in the NEW pair (must all be deliberate) ==="
grep -n "7[4-6]" chain-preview77.sh chain-preview77e.sh | grep -v '^[^:]*:[0-9]*:#' | head -16
echo
echo "=== the parameters that must be fresh ==="
grep -n "A13_LOG_LABEL=\|A13_TARGET_PREVIEW=\|A13_RPC_ROUND=\|ROUND 7\|version 7" chain-preview77.sh chain-preview77e.sh
