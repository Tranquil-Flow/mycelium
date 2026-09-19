#!/bin/bash
# E: preview.78, one round on the boot-id fix, then the C5 soak against the live route.
#   1. run ONE round with --no-quit so the serving route stays UP after the chain;
#   2. JUDGE THE BOOT-ID COLLECTION and gate the soak on the VERDICT CLASS (revised 2026-09-19): the
#      app log is graded by scripts/a13_operator/bootid_gate.sh, which blocks on a DEFECT or on having
#      no log, and records a documented-inert refusal without blocking. The earlier policy -- silence is
#      always a round failure, the soak does not start on it -- was written against the programming-error
#      class (ERRATA-6's UnboundLocalError reported as boot_id_observation_unavailable, two bundles inert)
#      and misfired on the producer's own refusal, because workflow.py:240-248 documents a refusal as the
#      binding's INERT DEFAULT ("the round proceeds byte-for-byte as it did before this change"). Blocking
#      on it coupled the C5 soak to a best-effort feature. A defect still stops the round; a refusal is
#      recorded and the soak proceeds, and NO round may claim the fix fired without the success line;
#   3. take timed requests against that live route and run the 110-request soak with cancellations;
#   4. ALWAYS quit both apps and sweep both Macs -- in a trap, so no early exit and no lost context
#      can leave strays.
set -u
# Label choice matters: the supervisor REFUSES to overwrite a round log (exit 4). Labels 63r2..82r1 and
# soak01-04 are taken; this one names its preview so it cannot collide with the historical sequence.
export A13_LOG_LABEL=p78r1 A13_PYTHON=/opt/homebrew/bin/python3
REPO=/Users/evinova-self/Documents/playground/mycelium-core-pilot
EV=/Users/evinova-self/Documents/mycelium-core-program/runs/wp8-02-useful-two-mac
export A13_OWNER_ROOT=/Users/evinova-self/mycelium-wp8-02/owner-lhr
export A13_REMOTE_ROOT=/Users/evinova/mycelium-wp8-02
export A13_EVIDENCE_DIR=$EV
export A13_DIST_ROOT=/Users/evinova-self/Documents/mycelium-core-program/runs/wp8-02-g1/dist
export A13_TARGET_PREVIEW=preview78 A13_RPC_ROUND=rpc-round78
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

echo "=== [2/5] ROUND 78r1 with --no-quit (serving stays UP) ==="
env -u PYTHONPATH -u PYTHONHOME -u VIRTUAL_ENV "$A13_PYTHON" -B \
  scripts/a13_operator/round15_supervisor.py 78 --max-seconds 1800 --no-quit \
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
# The decision lives in ONE tested place: scripts/a13_operator/bootid_guard.py (in the repo, with
# test_bootid_guard.py: 13/13 fixtures including synthetic positive controls, and a --corpus mode over
# every round log). It decides from the APP'S OWN VOCABULARY in an app log, never from chain artifacts:
# chain-runs/<run>/observations.json and the plan's boot_id are written by the CHAIN's managed-hello path
# and are present in all 23 runs including soak04's broken one, so they cannot discriminate.
# It supersedes /tmp/asm/bootid_guard.sh, which (a) decided from a log line rather than the producer's
# vocabulary -- and the harness QUOTES protocol strings, so a harness log could pass an app-signature
# check -- and (b) would not survive a reboot. Exit: 0 fired, 6 did-not-fire (named reason), 7 no log.
#
# WHICH VERDICTS THIS GATE BLOCKS, and why the list is short (revised 2026-09-19, decisions.log):
# the guard's own classes are not equivalent, so treating every non-zero exit as fatal coupled the C5 soak
# to a feature the producer documents as BEST-EFFORT. workflow.py:240-248 says of this very collection:
# "the nodes may not be talking yet at plan-compile time. If the producer refuses -- ManagedObservationsError
# is its ONLY refusal type: no session, stale run id, malformed or non-distinct hello -- the binding is left
# without observations, which is its inert default, and the round proceeds byte-for-byte as it did before
# this change." So a refusal is the product's own documented inert condition, and p74r1's observed class IS
# a refusal (managed_control_response_timeout), not the programming-error class the original brief was
# written against.
#   * class=defect  -> BLOCK. A programming error must be fixed, never soaked past. This is the class the
#                      original brief was about (UnboundLocalError reported as boot_id_observation_unavailable).
#   * exit 7        -> BLOCK. No app log means no evidence either way; do not guess.
#   * FIRED         -> proceed, and the success line is recorded in the round evidence.
#   * refused_expected / no_attempt -> proceed, RECORDED as the inert condition. Blocking here is what would
#                      make C5 unreachable, and it is not what "fail closed on a defect" means.
# The decision lives in ONE tested place: scripts/a13_operator/bootid_gate.sh, which calls the guard and
# then distinguishes the classes the guard reports. It blocks ONLY on a defect or on having no log at all,
# because the producer documents its refusal as the binding's inert default (workflow.py:240-248) while a
# programming error must never be inert (workflow.py:246-248) -- see that file for the full reasoning and
# decisions.log for the 2026-09-19 revision that narrowed this gate.
if ! bash scripts/a13_operator/bootid_gate.sh "$OWNER_APP_LOG"; then
  echo "REFUSING to spend the soak: the boot-id gate blocked (reason above)."
  exit 6
fi

echo "=== [3/5] TIMED REQUESTS + C5 SOAK against the live route ==="
env -u PYTHONPATH -u PYTHONHOME -u VIRTUAL_ENV "$A13_PYTHON" -B - <<'PY' 2>&1 | tail -30
import importlib.util, json, sys, time
sys.path.insert(0, 'scripts/a13_operator')
import os
os.environ.setdefault('A13_OWNER_ROOT', '/Users/evinova-self/mycelium-wp8-02/owner-lhr')
os.environ.setdefault('A13_REMOTE_ROOT', '/Users/evinova/mycelium-wp8-02')
os.environ.setdefault('A13_TARGET_PREVIEW', 'preview78')
os.environ.setdefault('A13_RPC_ROUND', 'rpc-round78')

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
# The label must come from the ENVIRONMENT, not from shell interpolation: this block is a QUOTED heredoc
# (<<'PY'), so a '${A13_LOG_LABEL}' written here reaches Python as those eleven literal characters and the
# path never exists -- which made the chain_finished wait below spin its full 2100 s before every soak.
# Every other reference in this block already reads os.environ; this one now does too.
LOG = ('/Users/evinova-self/Documents/mycelium-core-program/runs/wp8-02-useful-two-mac'
       '/round%s-supervisor.log' % os.environ.get('A13_LOG_LABEL', 'soak'))
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

# C5's PREREQUISITE, in the plan's own words: "soak only after one request completes end to end, or you
# will spend an hour measuring a broken path." Two things make this gate necessary rather than nice:
#   * 1704ac2 made a FAILED request publish a terminal too, so "a terminal arrived" no longer distinguishes
#     a working path from a refuse-everything path. The driver's `succeeded` field exists for exactly this
#     -- its own comment: ok=True means "the stream produced tokens and reached a terminal"; succeeded means
#     the terminal was the SUCCESSFUL one, and "criterion 5 needs the second";
#   * the soak spends 110 requests and ~an hour. Spending it on a path that cannot complete would produce a
#     large, honest, useless measurement -- and would blur a real C5 result with a broken prerequisite.
# The smoke request uses the SOAK'S OWN prompt and token count (wp8_05_soak.run_soak defaults), so it is the
# same shape of request the soak would make, not a different one. Its outcome is written to the round
# evidence either way: a refusal is a measurement, not a silence.
smoke_prompt = os.environ.get('SOAK_PROMPT', 'Describe the city of Berlin in three sentences.')
smoke_samples, smoke = measure.stream(base, smoke_prompt, int(os.environ.get('SOAK_TOKENS', '32')),
                                      timeout=int(os.environ.get('SOAK_TIMEOUT', '180')))
_smoke = {k: smoke.get(k) for k in ('ok', 'succeeded', 'terminal', 'terminal_code', 'has_terminal')}
print('SMOKE_REQUEST', json.dumps(_smoke))
_EV = '/Users/evinova-self/Documents/mycelium-core-program/runs/wp8-02-useful-two-mac'
_smoke_path = os.path.join(_EV, 'round%s-smoke.json' % os.environ.get('A13_LOG_LABEL', 'soak'))
with open(_smoke_path, 'w') as _fh:
    _fh.write(json.dumps({'protocol': 'mycelium.a13.soak_precondition_smoke.v1',
                          'run_label': os.environ.get('A13_LOG_LABEL', 'soak'),
                          'prompt': smoke_prompt,
                          'max_new_tokens': int(os.environ.get('SOAK_TOKENS', '32')),
                          'base': base, **_smoke}, indent=1))
print('SMOKE_WRITTEN', _smoke_path)
if not smoke.get('succeeded'):
    print(json.dumps({'ok': False, 'refused': 'soak_skipped_no_request_completed_end_to_end',
                      'detail': 'the smoke request did not reach the SUCCESSFUL terminal, so the soak was '
                                'not spent; a failure terminal still counts as failing this prerequisite',
                      'terminal': smoke.get('terminal'),
                      'terminal_code': smoke.get('terminal_code')}, indent=1))
    raise SystemExit(0)
print('SMOKE PASS -- one request completed end to end; the soak is now worth spending')

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
