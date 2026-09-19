#!/bin/bash
# E: preview.74, one round on the boot-id fix, then the C5 soak against the live route.
#   1. run ONE round with --no-quit so the serving route stays UP after the chain;
#   2. ASSERT THE BOOT-ID FIX ACTUALLY FIRED (new here): the round's app log must carry
#      `boot_id_observations_collected nodes=N` with N >= 1. Two defects made that fix inert in every
#      bundle that carried it (ERRATA-6), and the silent path looked exactly like an expected
#      condition, so silence is now a round failure and the soak does not start on it;
#   3. take timed requests against that live route and run the 110-request soak with cancellations;
#   4. ALWAYS quit both apps and sweep both Macs -- in a trap, so no early exit and no lost context
#      can leave strays.
set -u
# Label choice matters: the supervisor REFUSES to overwrite a round log (exit 4). Labels 63r2..82r1 and
# soak01-04 are taken; this one names its preview so it cannot collide with the historical sequence.
export A13_LOG_LABEL=p74r1 A13_PYTHON=/opt/homebrew/bin/python3
REPO=/Users/evinova-self/Documents/playground/mycelium-core-pilot
EV=/Users/evinova-self/Documents/mycelium-core-program/runs/wp8-02-useful-two-mac
export A13_OWNER_ROOT=/Users/evinova-self/mycelium-wp8-02/owner-lhr
export A13_REMOTE_ROOT=/Users/evinova/mycelium-wp8-02
export A13_EVIDENCE_DIR=$EV
export A13_DIST_ROOT=/Users/evinova-self/Documents/mycelium-core-program/runs/wp8-02-g1/dist
export A13_TARGET_PREVIEW=preview74 A13_RPC_ROUND=rpc-round74
export A13_ROTATE_RELAY=1
# The work path re-derives its preview and fails closed as work_preview_changed, so a
# bounded retry of a reachability refusal is safe (8751a19). 73r1 ran without this.
export A13_RETRY_REACHABILITY_PATHS=/api/work/accept,/api/work/preview
export A13_RETRY_REACHABILITY_BUDGET_S=90
cd "$REPO" || exit 1
# CRITERION 3 AUDIT: a PATH shim records every ssh invocation (scripts/a13_operator/ssh_audit.py,
# commits 7ae6678/74ef984). The acceptance window is measured as the DELTA across the timed
# requests, so orchestration ssh cannot be mistaken for the acceptance itself.
export A13_SSH_LOG=/tmp/asm/criterion3-ssh-calls.log
rm -f "$A13_SSH_LOG"
export PATH=/tmp/asm/ssh-shim:$PATH

cleanup() {
  echo "=== CLEANUP (trap): quit both apps, then sweep both Macs ==="
  env -u PYTHONPATH -u PYTHONHOME -u VIRTUAL_ENV "$A13_PYTHON" -B \
    scripts/a13_operator/round15_quit.py 2>&1 | tail -4 || true
  env -u PYTHONPATH -u PYTHONHOME -u VIRTUAL_ENV "$A13_PYTHON" -B \
    scripts/a13_operator/round15_strays.py 2>&1 | tail -2 || true
  echo "=== CLEANUP DONE ==="
}
trap cleanup EXIT

echo "=== [0/3] RECLAIM SUPERSEDED ROUND STAGING (a launch implies its housekeeping) ==="
env -u PYTHONPATH -u PYTHONHOME -u VIRTUAL_ENV /opt/homebrew/bin/python3 -B - <<'RECLAIM' 2>&1 | tail -3
import glob, os, shutil, subprocess
OWN = '/Users/evinova-self/mycelium-wp8-02/owner-lhr'
for base, sub in ((OWN + '/chain-runs', 'compiled/transfer-bundle'),
                  (OWN + '/local-contributor/managed', 'mycelium-stage')):
    runs = sorted(glob.glob(base + '/*'), key=os.path.getmtime)
    for run in runs[:-1]:
        payload = os.path.join(run, sub)
        if os.path.isdir(payload):
            shutil.rmtree(payload, ignore_errors=True)
print(subprocess.run(['df','-h','/System/Volumes/Data'], capture_output=True,
                     text=True).stdout.strip().splitlines()[-1])
RECLAIM

echo "=== [0b/3] STALE SURFACE GUARD ==="
# The app daemon REFUSES to start while native-control.json exists (packaged_app_daemon.py:8 asserts it
# away). Round 78r1's app was killed with exit_code -9, so it never unlinked its surface; 79r1 and 80r1
# then died at bootstrap_compile with `Connection refused` because the app never started. The sweep
# reports processes, not a stale FILE, so it did not catch this. Clear it only when NO live process owns
# it, and record its digest before removing it -- a surface is evidence until proven stale.
for R in "$A13_OWNER_ROOT"; do
  SURF="$R/native-control.json"
  if [ -f "$SURF" ]; then
    LIVE=$(pgrep -f "packaged_app_daemon.py.*--root $R" | head -1)
    if [ -z "$LIVE" ]; then
      sha=$(shasum -a 256 "$SURF" | cut -d' ' -f1)
      echo "stale_surface_removed root=$R digest=sha256:$sha bytes=$(wc -c < "$SURF" | tr -d ' ') no_live_process=true"
      rm -f "$SURF"
    else
      echo "surface_present_with_live_process root=$R pid=$LIVE (not touched)"
    fi
  fi
done

echo "=== [1/3] STRAY SWEEP BEFORE ==="
env -u PYTHONPATH -u PYTHONHOME -u VIRTUAL_ENV "$A13_PYTHON" -B \
  scripts/a13_operator/round15_strays.py 2>&1 | tail -2

echo "=== [2/5] ROUND 74r1 with --no-quit (serving stays UP) ==="
env -u PYTHONPATH -u PYTHONHOME -u VIRTUAL_ENV "$A13_PYTHON" -B \
  scripts/a13_operator/round15_supervisor.py 74 --max-seconds 1800 --no-quit \
  > /tmp/asm/${A13_LOG_LABEL}-launch.log 2>&1
echo "supervisor_exit=$?"
tail -4 /tmp/asm/${A13_LOG_LABEL}-launch.log

# WAIT, DO NOT CHECK INSTANTLY: the supervisor DETACHES itself (start_new_session) and returns in
# milliseconds, so an immediate test for the daemon log reads a stale view and wrongly concludes the round
# never launched -- which happened on 2026-09-19 and tripped this script's trap mid-startup. Poll instead.
OWNER_APP_LOG="$EV/round${A13_LOG_LABEL}-owner-daemon.log"
deadline=$((SECONDS + 180))
while [ ! -f "$OWNER_APP_LOG" ] && [ "$SECONDS" -lt "$deadline" ]; do sleep 3; done
if [ ! -f "$OWNER_APP_LOG" ]; then
  echo "SUPERVISOR_DID_NOT_RUN (no $OWNER_APP_LOG after 180s)"
  if grep -q 'SUPERVISOR_LOG_EXISTS' /tmp/asm/${A13_LOG_LABEL}-launch.log 2>/dev/null; then
    echo "reason=label already used -- round logs are never overwritten; pick a fresh label"
  fi
  exit 7
fi
# The daemon log appears while the chain is still compiling/serving: wait for the chain to finish before
# asserting anything about the round's behaviour, or the guard reads a log that is still being written.
SUP_LOG="$EV/round${A13_LOG_LABEL}-supervisor.log"
deadline=$((SECONDS + 2100))
while [ "$SECONDS" -lt "$deadline" ]; do
  grep -q 'chain_finished' "$SUP_LOG" 2>/dev/null && break
  sleep 10
done
echo "chain_finished seen: $(grep -c 'chain_finished' "$SUP_LOG" 2>/dev/null || echo 0)"

echo "=== [2b/5] ASSERT THE BOOT-ID FIX FIRED (fail closed; the soak does not start otherwise) ==="
# The decision lives in ONE tested place: /tmp/asm/bootid_guard.sh, unit-tested against soak04's real log
# (the defect case), a working round, a legitimate producer refusal, silence, and a missing log
# (/tmp/asm/bootid_guard_test.sh: 5/5). An inline copy of this logic already mis-tested once.
if ! bash /tmp/asm/bootid_guard.sh "$OWNER_APP_LOG"; then
  echo "REFUSING to spend the soak on a round whose plan binding was never refreshed"
  exit 6
fi

echo "=== [3/5] TIMED REQUESTS + C5 SOAK against the live route ==="
env -u PYTHONPATH -u PYTHONHOME -u VIRTUAL_ENV "$A13_PYTHON" -B - <<'PY' 2>&1 | tail -30
import importlib.util, json, sys, time
sys.path.insert(0, 'scripts/a13_operator')
import os
os.environ.setdefault('A13_OWNER_ROOT', '/Users/evinova-self/mycelium-wp8-02/owner-lhr')
os.environ.setdefault('A13_REMOTE_ROOT', '/Users/evinova/mycelium-wp8-02')
os.environ.setdefault('A13_TARGET_PREVIEW', 'preview74')
os.environ.setdefault('A13_RPC_ROUND', 'rpc-round74')

def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

measure = load('wp8_05_measure_stream', 'scripts/a13_operator/wp8_05_measure_stream.py')
rpc = load('round14_rpc', 'scripts/a13_operator/round14_rpc.py')

# The supervisor DETACHES, so this script reaches here while the chain is still starting. Wait for
# the live route instead of measuring too early, with a bounded deadline so a failed chain cannot hang.
# DO NOT poll the app's private API while the chain is driving it. Round 73r1 failed with
# `HTTP Error 403: Forbidden` at start_and_activate while this script was calling
# owner('/api/serving/status') every 3 s -- and 68r1/70r1/72r1, which had no such concurrent caller,
# all passed. The app's API is a CSRF/session-sensitive single-writer channel; a second caller is a
# plausible cause of the 403. So: wait on the chain's OWN log sentinel (no API traffic), then read the
# status exactly once.
LOG = '/Users/evinova-self/Documents/mycelium-core-program/runs/wp8-02-useful-two-mac/round${A13_LOG_LABEL}-supervisor.log'
deadline = time.monotonic() + 2100
while time.monotonic() < deadline:
    try:
        text = open(LOG).read()
    except OSError:
        text = ''
    if 'chain_finished' in text:
        break
    time.sleep(5)
print('chain_finished seen:', 'chain_finished' in open(LOG).read() if os.path.exists(LOG) else False)

base = ''
deadline = time.monotonic() + 120
while time.monotonic() < deadline:
    try:
        status = rpc.owner('/api/serving/status', {})
    except Exception as exc:
        status = None
        last_error = '%s: %s' % (type(exc).__name__, str(exc)[:120])
    serving = ((status or {}).get('serving') or {})
    if serving.get('state') == 'api_running':
        base = ((serving.get('startup') or {}).get('url') or '').rstrip('/')
        break
    time.sleep(3)
print('serving state:', serving.get('state'), 'base:', base,
      'waited_s:', round(2100 - (deadline - time.monotonic()), 1))
if not base:
    print(json.dumps({'ok': False, 'error': 'serving_not_running_before_deadline'}))
    raise SystemExit(0)

import importlib.util as _ilu
_spec = _ilu.spec_from_file_location('ssh_audit', 'scripts/a13_operator/ssh_audit.py')
audit = _ilu.module_from_spec(_spec); _spec.loader.exec_module(audit)
_s3 = _ilu.spec_from_file_location('soak', 'scripts/a13_operator/wp8_05_soak.py')
soak = _ilu.module_from_spec(_s3); _s3.loader.exec_module(soak)
def _route_alive(b):
    # the shape the product actually serves: status['serving']['state'] -- the same field the route poll
    # above waits on, so the soak's liveness sampler and its readiness check cannot disagree
    try:
        st = rpc.owner('/api/serving/status', {})
        return ((st or {}).get('serving') or {}).get('state') == 'api_running'
    except Exception:
        return False
_log = os.environ.get('A13_SSH_LOG', '/tmp/asm/criterion3-ssh-calls.log')
before = len(audit.invocations(_log))
print('ACCEPTANCE_WINDOW_SSH_BEFORE', before, 'total_in_run', before)
summary, path = soak.run_soak(
    base,
    requests=int(os.environ.get('SOAK_REQUESTS_N', '110')),
    cancel_every=int(os.environ.get('SOAK_CANCEL_EVERY', '10')),
    max_new_tokens=int(os.environ.get('SOAK_TOKENS', '32')),
    label=os.environ.get('A13_LOG_LABEL', 'soak'),
    sample_status=lambda: _route_alive(base),
    out_dir='/Users/evinova-self/Documents/mycelium-core-program/runs/wp8-02-useful-two-mac')
_after = len(audit.invocations(_log))
print('SOAK_WINDOW_SSH delta=%d total=%d' % (_after - before, _after))
print(json.dumps(summary, indent=1))
print('SOAK_WRITTEN', path)
print('MEASUREMENTS_WRITTEN')
PY
echo "MEASURE_SCRIPT_DONE"
