#!/bin/bash
# Make the preview.76 pipeline + round launchers by EDITING the proven .75 pair (launch law),
# then show every remaining old-version literal so nothing is missed silently.
set -u
cd /Users/evinova-self/Documents/playground/mycelium-core-pilot/scripts/a13_operator || exit 1

# 75 -> 76 first, then 74 -> 75, so nothing is converted twice.
sub() {
  sed -e 's/preview\.75/preview.76/g' \
      -e 's/preview75/preview76/g' \
      -e 's/p75r1/p76r1/g' \
      -e 's/rpc-round75/rpc-round76/g' \
      -e 's/pre75/pre76/g' \
      -e 's/--version 75/--version 76/g' \
      -e 's/round15_supervisor\.py 75/round15_supervisor.py 76/' \
      -e 's/preview\.74/preview.75/g' \
      -e 's/preview74/preview75/g' \
      -e 's/pre74/pre75/g' \
      -e 's/--version 74/--version 75/g' \
      -e 's/round15_supervisor\.py 74/round15_supervisor.py 75/' \
      "$1"
}
sub chain-preview75.sh  > chain-preview76.sh
sub chain-preview75e.sh > chain-preview76e.sh
chmod +x chain-preview76.sh chain-preview76e.sh

echo "=== diff chain-preview75.sh -> chain-preview76.sh (substitutions only) ==="
diff chain-preview75.sh chain-preview76.sh
echo "=== diff chain-preview75e.sh -> chain-preview76e.sh ==="
diff chain-preview75e.sh chain-preview76e.sh | head -40
echo "=== changed lines (e): $(diff chain-preview75e.sh chain-preview76e.sh | grep -c '^[<>]') ==="
echo
echo "=== every remaining 7[3-6] literal in the NEW pair (must all be deliberate) ==="
grep -n "7[3-6]" chain-preview76.sh chain-preview76e.sh | grep -v "^\S*:[0-9]*:#" | head -12
echo
echo "=== the two parameters that must be fresh ==="
grep -n "A13_LOG_LABEL=\|A13_TARGET_PREVIEW=\|A13_RPC_ROUND=\|NEW=\|OLD=" chain-preview76.sh chain-preview76e.sh
