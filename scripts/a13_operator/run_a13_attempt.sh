#!/bin/bash
# SPDX-License-Identifier: AGPL-3.0-only
# Drive the consented two-Mac A13 chain on a named preview version.
#
#   1. network recovery + descriptors + model-free bootstrap + native hello + compile
#   2. consume the compiled proposal, accept both work offers, activation barrier, start
#   3. submit the real public test prompt (validates physical qualification first)
#
# Fresh evidence names per version: earlier rounds' receipts are never overwritten.
# A retry against the SAME preview (no bundled change, e.g. tooling-only rounds) must
# still write a fresh log, so the label can be set independently of the preview tag.
# This driver REFUSES to run when the attempt log already exists (exit 4): re-running
# the same version would truncate the earlier round's only copy of the chain log.
# It also REFUSES to run outside the detached supervisor (exit 5) — see the guard below.
# Usage: run_a13_attempt.sh <version>     e.g. run_a13_attempt.sh 34
set -o pipefail
cd "$(dirname "$0")/../.." || exit 1
REPO="$(pwd)"
# A physical round must own its own session. On 2026-09-16 (attempt 63r1) this driver ran as a
# background child of an interactive chat session; when that session was torn down (provider
# exhaustion -> context length exceeded -> interrupt) SIGTERM took the round's daemons with it
# and the attempt was lost mid-compile. Post-mortem: runs/wp8-02-useful-two-mac/ERRATA-1.md.
# The supported entry point (which detaches: macOS has no setsid(1), so it uses
# start_new_session=True) is:
#   python3 -B scripts/a13_operator/round15_supervisor.py <version>
# A13_ALLOW_ATTACHED=1 is the documented escape hatch for a deliberate tooling-only retry.
if [ "${A13_ROUND_SUPERVISED:-0}" != "1" ] && [ "${A13_ALLOW_ATTACHED:-0}" != "1" ]; then
  echo "ROUND_NOT_SUPERVISED pid=$$ — refusing to spend a physical attempt outside the detached" >&2
  echo "supervisor: an interactive session's teardown would SIGTERM the round (63r1, ERRATA-1)." >&2
  echo "Run: python3 -B scripts/a13_operator/round15_supervisor.py <version>   (or A13_ALLOW_ATTACHED=1)" >&2
  exit 5
fi
VERSION="${1:?usage: run_a13_attempt.sh <version>}"
export A13_TARGET_PREVIEW="preview${VERSION}"
export A13_RPC_ROUND="rpc-round${VERSION}"
EVIDENCE="${A13_EVIDENCE_DIR:-${REPO}/outputs/a13-macos-evidence}"
LOG="${EVIDENCE}/round${A13_LOG_LABEL:-$VERSION}-attempt.log"
if [ -e "$LOG" ]; then
  echo "ROUND_LOG_EXISTS ${LOG}" >&2
  echo "refusing to truncate the existing attempt log; bump the version or A13_LOG_LABEL" >&2
  exit 4
fi
: >"$LOG"

run() {
  env -u PYTHONPATH -u PYTHONHOME -u VIRTUAL_ENV "${A13_PYTHON:-python3}" -B "$@" 2>&1 | tee -a "$LOG"
  return "${PIPESTATUS[0]}"
}

run scripts/a13_operator/round14_bootstrap_compile.py || { echo "STEP_FAILED bootstrap_compile" | tee -a "$LOG"; exit 1; }
run scripts/a13_operator/round14_start_and_activate.py || { echo "STEP_FAILED start_and_activate" | tee -a "$LOG"; exit 2; }
# The stream step is wrapped so the route's own refusal is READ rather than waited out. In p74r1 the app
# printed "live route terminal blocked request_id=... reason=command_cleanup_receipt_missing" at the moment
# of blocking, while the step sat on a socket read for its full timeout. The wrapper watches the daemon log,
# aborts the step when that line appears, and preserves the step's own exit status otherwise; a stale line
# from an earlier round cannot trigger it (it starts reading from the file's current end).
run scripts/a13_operator/stream_step_watch.py \
  --daemon-log "${EVIDENCE}/round${A13_LOG_LABEL:-$VERSION}-owner-daemon.log" \
  -- scripts/a13_operator/round14_stream.py              || { echo "STEP_FAILED stream" | tee -a "$LOG"; exit 3; }
echo "CHAIN_COMPLETE" | tee -a "$LOG"
