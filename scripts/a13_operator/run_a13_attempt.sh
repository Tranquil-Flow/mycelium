#!/bin/bash
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
# Usage: run_a13_attempt.sh <version>     e.g. run_a13_attempt.sh 34
set -o pipefail
cd "$(dirname "$0")/../.." || exit 1
REPO="$(pwd)"
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
run scripts/a13_operator/round14_stream.py            || { echo "STEP_FAILED stream" | tee -a "$LOG"; exit 3; }
echo "CHAIN_COMPLETE" | tee -a "$LOG"
