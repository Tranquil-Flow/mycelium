#!/bin/bash
# LOCAL-ONLY preflight for preview.79: steps [1/6]-[4/6] of chain-preview79.sh, VERBATIM, plus the
# local half of the runtime-source shipment proof.  The deploy ([5/6]) and verify ([6/6]) steps need
# the laptop, which is off-network (see decisions.log 04:45 and 05:05).  This runs everything that
# does not need it, so the build is de-risked and the PYZ-ledger answer is known before it returns.
# The full pipeline still runs later: this is a PREFLIGHT, never a substitute for it.
set -u
REPO=/Users/evinova-self/Documents/playground/mycelium-core-pilot
RUN=/Users/evinova-self/Documents/mycelium-core-program/runs/wp8-02-useful-two-mac
DIST=/Users/evinova-self/Documents/mycelium-core-program/runs/wp8-02-g1/dist
TOOLS="$REPO/scripts/a13_operator"  # the ledger is tracked here now (commit da033a0)
VENV=/Users/evinova-self/Documents/playground/mycelium-a13-macos-demo/.venv/bin/python
PY=/opt/homebrew/bin/python3
NEW=0.1.0-preview.79
OLD=0.1.0-preview.78
cd "$REPO" || exit 1

HEAD=$(git rev-parse HEAD); TAG=${HEAD:0:7}; echo "=== HEAD === $HEAD (tag $TAG)"
printf "worktree: "; git status --porcelain | wc -l | tr -d " "; echo " dirty file(s)"

echo "=== [0/6] preserve the previous receipts ==="
for b in readiness-gate.json readiness-gate.txt PACKAGE.json gate-run.json; do
  [ -f "$RUN/$b" ] || continue
  # Name the preserved copy after the head INSIDE it, not the head we happen to be on: a receipt's
  # provenance is its own.  Falls back to the run tag only when the file carries no head.
  SUF=$("$PY" -B "$REPO/scripts/a13_operator/receipt_head.py" "$RUN/$b" 2>/dev/null || true)
  SUF=${SUF:-$TAG}
  DST="$RUN/${b%.*}-preserved-${SUF}.${b##*.}"
  if [ -f "$DST" ] && cmp -s "$RUN/$b" "$DST"; then echo "  already preserved $b (as -preserved-$SUF)"; continue; fi
  cp -p "$RUN/$b" "$DST" && echo "  preserved $b -> -preserved-$SUF"
done

echo "=== [1/6] GATE ==="
env -u PYTHONPATH -u PYTHONHOME -u VIRTUAL_ENV PYTHONDONTWRITEBYTECODE=1 \
  "$PY" -B scripts/pilot_readiness_gate.py --run-id wp8-02-useful-two-mac --seeds 5 --events 50 \
  > /tmp/asm/gate-pre79-at-$TAG.log 2>&1
echo "gate_exit=$?"
VERDICT=$("$PY" -c "import json;print(json.load(open('$RUN/readiness-gate.json'))['verdict'])")
RH=$("$PY" -c "import json;d=json.load(open('$RUN/readiness-gate.json'));print([c for c in d['checks'] if 'package' in c.get('id','')][0]['detail']['git_head'])")
echo "verdict=$VERDICT receipt_head=$RH"
[ "$VERDICT" = "GO" ] && [ "$RH" = "$HEAD" ] || { echo "STOP: gate not GO for this head"; echo "PREFLIGHT_LOCAL_FAILED"; exit 3; }

echo "=== [2/6] BUILD $NEW ==="
if [ -d "$DIST/$NEW" ]; then
  if [ -z "$(ls -A "$DIST/$NEW" 2>/dev/null)" ]; then
    # The build mkdirs the output root BEFORE it can fail, so a failed build leaves an EMPTY root and every
    # later attempt refuses with "fresh_output_root_required".  Measured twice in a row on 2026-09-20.
    echo "  removing empty $DIST/$NEW -- debris a failed build left (it creates the root before it can fail)"
    rmdir "$DIST/$NEW"
  else
    echo "STOP: $DIST/$NEW exists and is NOT empty -- preserve it aside (rename, never delete) first"
    echo "PREFLIGHT_LOCAL_FAILED"; exit 5
  fi
fi
# The build's WORK dir (build/<name>, gitignored PyInstaller scratch) cannot be reused: its
# compiler-source staging does mkdir(exist_ok=False), so the second build for a version raises
# FileExistsError: .../compiler-sources -- measured 2026-09-20, after a preflight build's artifact was
# preserved aside and .78 was built again.  The output-root guard above does not cover this, and the chain
# would have aborted at [2/6].  Reclaiming superseded staging is allowed; the retained artifact under $DIST
# is untouched (different directory).
if [ -d "$REPO/build/$NEW" ]; then
  echo "  reclaiming scratch $REPO/build/$NEW ($(du -sh "$REPO/build/$NEW" 2>/dev/null | cut -f1))"
  rm -rf "$REPO/build/$NEW"
fi
env -u PYTHONPATH -u PYTHONHOME -u VIRTUAL_ENV PYTHONDONTWRITEBYTECODE=1 \
  "$VENV" -B scripts/build_a13_macos.py --output-root "$DIST/$NEW" > /tmp/asm/build-pre79-at-$TAG.log 2>&1
BUILD_EXIT=$?
  echo "BUILD_EXIT=$BUILD_EXIT"; tail -3 /tmp/asm/build-pre79-at-$TAG.log | head -c 600; echo
  [ "$BUILD_EXIT" = 0 ] || { echo "STOP: the build refused or failed (see the log tail above)"; echo "PREFLIGHT_LOCAL_FAILED"; exit 5; }

echo "=== [3/6] IDENTITY RECORDED BY THE BUILD ==="
IDENT="$DIST/$NEW/Mycelium A13 Demo.app/Contents/Resources/third_party/compiler-source-identity.json"
"$PY" -c "
import json,hashlib
d=json.load(open('$IDENT'))
print('  root          ', d.get('root'))
print('  head          ', d.get('head'))
print('  branch        ', d.get('branch'), '| worktree_dirty', d.get('worktree_dirty'))
print('  shipped files ', d.get('files'), '| compiled_not_shipped', len(d.get('compiled_not_shipped') or {}))
print('  sha256(self)  ', hashlib.sha256(open('$IDENT','rb').read()).hexdigest()[:16])
"
[ "$("$PY" -c "import json;print(json.load(open('$IDENT')).get('head'))")" = "$HEAD" ] || { echo "STOP: identity head != build head"; echo "PREFLIGHT_LOCAL_FAILED"; exit 4; }

echo "=== [4/6] PER-MODULE PYZ LEDGER ($NEW vs $OLD, control needle each) ==="
"$PY" -B "$TOOLS/pyz_ledger.py" \
  --exe "$DIST/$NEW/Mycelium A13 Demo.app/Contents/MacOS/Mycelium A13 Demo" --label "$NEW" \
  --baseline "$DIST/$OLD/Mycelium A13 Demo.app/Contents/MacOS/Mycelium A13 Demo" 2>&1 | tee /tmp/asm/pyz-ledger-pre79-at-$TAG.txt | tail -18

echo "=== the changed runtime sources: tree vs local bundle (remote half needs the laptop) ==="
for m in physical_inference_node.py mycelium_deadlines.py; do
  T=$(shasum -a 256 "$REPO/$m" | awk '{print $1}')
  L=$(shasum -a 256 "$DIST/$NEW/Mycelium A13 Demo.app/Contents/Resources/a13-runtime-source/$m" | awk '{print $1}')
  R=$([ "$T" = "$L" ] && echo MATCH || echo MISMATCH)
  echo "  $m  tree=${T:0:16}  bundle=${L:0:16}  $R"
done
echo "=== needles present in the shipped bytes? ==="
for m in physical_inference_node.py mycelium_deadlines.py; do
  F="$DIST/$NEW/Mycelium A13 Demo.app/Contents/Resources/a13-runtime-source/$m"
  for n in COMMAND_INTERRUPTION_AND_CLEANUP_MAX_MS CANCEL_AND_CLEANUP_BUDGET_SECONDS; do
    printf "  %-30s %-42s " "$m" "$n"
    if grep -q "$n" "$F"; then echo present; else echo ABSENT; fi
  done
done
echo "PREFLIGHT_LOCAL_DONE"
