#!/bin/bash
# D: gate the current head, build preview.78, prove per module what shipped, deploy, verify.
# Built by EDITING the proven chain-preview73.sh (launch law), with the PYZ step replaced by the
# program's per-module ledger (tools/pyz_ledger.py) -- the old differential checked one module and
# reported a green that said nothing about the module the change was in (ERRATA-6, verification law 1).
set -u
REPO=/Users/evinova-self/Documents/playground/mycelium-core-pilot
RUN=/Users/evinova-self/Documents/mycelium-core-program/runs/wp8-02-useful-two-mac
DIST=/Users/evinova-self/Documents/mycelium-core-program/runs/wp8-02-g1/dist
TOOLS="$REPO/scripts/a13_operator"  # the ledger is tracked here (it lived outside the repo)
VENV=/Users/evinova-self/Documents/playground/mycelium-a13-macos-demo/.venv/bin/python
PY=/opt/homebrew/bin/python3
NEW=0.1.0-preview.78
OLD=0.1.0-preview.77
cd "$REPO" || exit 1

HEAD=$(git rev-parse HEAD); echo "=== HEAD === $HEAD"
echo -n "worktree: "; git status --porcelain | wc -l | tr -d ' '; echo " dirty file(s)"
df -h /System/Volumes/Data | tail -1

echo "=== [0/6] preserve the previous receipts ==="
for b in readiness-gate.json readiness-gate.txt PACKAGE.json gate-run.json; do
  [ -f "$RUN/$b" ] || continue
  cp -p "$RUN/$b" "$RUN/${b%.*}-pre78.${b##*.}" && echo "  preserved $b"
done

echo "=== [1/6] GATE ==="
env -u PYTHONPATH -u PYTHONHOME -u VIRTUAL_ENV PYTHONDONTWRITEBYTECODE=1 \
  "$PY" -B scripts/pilot_readiness_gate.py --run-id wp8-02-useful-two-mac --seeds 5 --events 50 \
  > "$RUN/gate-$NEW-${HEAD:0:7}.log" 2>&1
echo "gate_exit=$?"
VERDICT=$("$PY" -c "import json;print(json.load(open('$RUN/readiness-gate.json'))['verdict'])")
RH=$("$PY" -c "import json;d=json.load(open('$RUN/readiness-gate.json'));print([c for c in d['checks'] if 'package' in c.get('id','')][0]['detail']['git_head'])")
echo "verdict=$VERDICT receipt_head=$RH"
[ "$VERDICT" = "GO" ] && [ "$RH" = "$HEAD" ] || { echo "STOP: gate not GO for this head"; echo "PIPELINE_DONE early"; exit 3; }

echo "=== [2/6] BUILD $NEW ==="
env -u PYTHONPATH -u PYTHONHOME -u VIRTUAL_ENV PYTHONDONTWRITEBYTECODE=1 \
  # Fail closed by name if a previous artifact already holds this root: build_a13_macos.py refuses it
  # ("fresh_output_root_required", historical previews are immutable) and the old chain then read the
  # STALE identity and died at [3/6] with "identity head != build head" -- the wrong name for the cause.
  [ -e "$DIST/$NEW" ] && { echo "STOP: $DIST/$NEW already exists -- preserve it aside (rename, never delete) first"; echo "PIPELINE_DONE early"; exit 5; }
  "$VENV" -B scripts/build_a13_macos.py --output-root "$DIST/$NEW" > "$RUN/build-$NEW-${HEAD:0:7}.log" 2>&1
  BUILD_EXIT=$?
  echo "BUILD_EXIT=$BUILD_EXIT"; tail -3 "$RUN/build-$NEW-${HEAD:0:7}.log" | head -c 600; echo
  [ "$BUILD_EXIT" = 0 ] || { echo "STOP: the build refused or failed (see the log tail above)"; echo "PIPELINE_DONE early"; exit 5; }

echo "=== [3/6] IDENTITY RECORDED BY THE BUILD (where the sources came from) ==="
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
[ "$("$PY" -c "import json;print(json.load(open('$IDENT')).get('head'))")" = "$HEAD" ] || { echo "STOP: identity head != build head"; echo "PIPELINE_DONE early"; exit 4; }

echo "=== [4/6] PER-MODULE PYZ LEDGER ($NEW vs $OLD, control needle each) ==="
"$PY" -B "$TOOLS/pyz_ledger.py" \
  --exe "$DIST/$NEW/Mycelium A13 Demo.app/Contents/MacOS/Mycelium A13 Demo" --label "$NEW" \
  --baseline "$DIST/$OLD/Mycelium A13 Demo.app/Contents/MacOS/Mycelium A13 Demo" 2>&1 | tee "$RUN/pyz-ledger-pre78.txt" | tail -14

echo "=== [5/6] DEPLOY $NEW ==="
export A13_OWNER_ROOT=/Users/evinova-self/mycelium-wp8-02/owner-lhr A13_REMOTE_ROOT=/Users/evinova/mycelium-wp8-02
export A13_EVIDENCE_DIR="$RUN" A13_DIST_ROOT="$DIST"
env -u PYTHONPATH -u PYTHONHOME -u VIRTUAL_ENV PYTHONDONTWRITEBYTECODE=1 \
  "$PY" -B scripts/a13_operator/deploy_preview_to_laptop.py --version 78 2>&1 | tail -8

echo "=== [6/6] VERIFY DEPLOYED (digest) + record it ==="
env -u PYTHONPATH -u PYTHONHOME -u VIRTUAL_ENV PYTHONDONTWRITEBYTECODE=1 \
  "$PY" -B scripts/a13_operator/verify_preview_deployed.py --version 78 2>&1 | tail -8
LOCAL=$(shasum -a 256 "$DIST/$NEW/Mycelium A13 Demo.app/Contents/MacOS/Mycelium A13 Demo" | awk '{print $1}')
REMOTE=$(ssh -T -o BatchMode=yes mycelium-laptop "shasum -a 256 '/Users/evinova/mycelium-wp8-02/preview78/Mycelium A13 Demo.app/Contents/MacOS/Mycelium A13 Demo'" 2>/dev/null | awk '{print $1}')
echo "LOCAL  exe sha256: $LOCAL"
echo "REMOTE exe sha256: $REMOTE"
[ -n "$REMOTE" ] && [ "$LOCAL" = "$REMOTE" ] && echo "DIGEST MATCH (copy proven -- NOT content: see the PYZ ledger above)" || echo "DIGEST MISMATCH"
df -h /System/Volumes/Data | tail -1
echo "PIPELINE_DONE"
