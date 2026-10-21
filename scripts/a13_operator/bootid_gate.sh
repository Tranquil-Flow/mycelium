#!/bin/bash
# SPDX-License-Identifier: AGPL-3.0-only
# The boot-id gate's DECISION, in one tested place, so the launcher only reports it.
#
# WHY THIS EXISTS AS ITS OWN SCRIPT: a decision buried in a 200-line launcher cannot be tested against the
# classes it must distinguish, and this one must distinguish three of them. The guard
# (scripts/a13_operator/bootid_guard.py) already grades the app log; what was wrong was the launcher
# treating EVERY non-zero guard exit as fatal. That coupled the C5 soak to a feature the producer itself
# documents as best-effort:
#
#   mycelium_a13_macos/workflow.py:240-248, of this exact collection:
#     "AND WHY IT CANNOT HURT: the nodes may not be talking yet at plan-compile time. If the producer
#      refuses -- ManagedObservationsError is its ONLY refusal type: no session, stale run id, malformed
#      or non-distinct hello -- the binding is left without observations, which is its inert default, and
#      the round proceeds byte-for-byte as it did before this change."
#
#   mycelium_a13_macos/workflow.py:246-248, of the ONE case that must never be inert:
#     "AND WHAT MUST NOT BE INERT: a programming error. Catching bare Exception here hid a function-local
#      import bug for an entire session: UnboundLocalError was reported as boot_id_observation_unavailable,
#      i.e. as though the nodes were simply not talking, so no round log ever disagreed."
#
# So the two classes are not equivalent, and only the second one is a stop condition:
#
#   FIRED (exit 0)                 -> PROCEED. The success line is in the app log; the round records it.
#   DID_NOT_FIRE class=defect      -> BLOCK.   A programming error must be fixed, never soaked past.
#   DID_NOT_FIRE class=refused_*   -> PROCEED, recorded. The product's documented inert condition.
#   DID_NOT_FIRE class=no_attempt  -> PROCEED, recorded. Pre-feature rounds look like this; not a regression.
#   exit 7 (no log)                -> BLOCK.   No evidence either way; do not guess, and do not soak.
#
# Usage: bootid_gate.sh <app-log-path>
# Exit:  0 proceed (and prints the class) · 6 block (and prints why) · 7 usage error (treated as block)
set -u

A13_PYTHON="${A13_PYTHON:-/opt/homebrew/bin/python3}"
REPO="${REPO:-$HOME/Documents/playground/mycelium-core-pilot}"
GUARD="${GUARD:-scripts/a13_operator/bootid_guard.py}"
APP_LOG="${1:-}"

if [ -z "$APP_LOG" ]; then
  echo "bootid_gate: BLOCK -- no app log path given (usage: bootid_gate.sh <app-log-path>)"
  exit 7
fi
if [ ! -f "$APP_LOG" ]; then
  echo "bootid_gate: BLOCK -- app log does not exist: $APP_LOG"
  echo "  No evidence either way. Do not soak on a guess."
  exit 6
fi

cd "$REPO" || { echo "bootid_gate: BLOCK -- repo not found: $REPO"; exit 6; }

OUT=$(env -u PYTHONPATH -u PYTHONHOME -u VIRTUAL_ENV "$A13_PYTHON" -B "$GUARD" --app-log "$APP_LOG" 2>&1)
RC=$?
printf '%s\n' "$OUT"

if [ "$RC" -eq 0 ]; then
  echo "bootid_gate: PROCEED -- the collection FIRED; the success line above is the round's evidence."
  exit 0
fi

if printf '%s' "$OUT" | grep -q 'class=defect'; then
  echo "bootid_gate: BLOCK -- the collection raised a DEFECT, not a refusal."
  echo "  A programming error here (the UnboundLocalError class this gate was written for) must be fixed,"
  echo "  never soaked past: the binding would be observation-less for the wrong reason, and every round"
  echo "  log would agree with the defect."
  exit 6
fi

if [ "$RC" -eq 7 ]; then
  echo "bootid_gate: BLOCK -- the guard found no attempt in the log (exit 7). Nothing to judge."
  exit 6
fi

if printf '%s' "$OUT" | grep -q 'class=refused_expected\|class=no_attempt'; then
  echo "bootid_gate: PROCEED, RECORDED -- the collection did not fire, by a class the producer documents"
  echo "  as the binding's inert default (workflow.py:240-248). The soak is not coupled to it."
  echo "  This is NOT proof the fix fired, and no round may claim that it did."
  exit 0
fi

echo "bootid_gate: BLOCK -- unrecognised verdict from the guard (exit $RC). An unrecognised class must not"
echo "  be waved through as if it were the documented refusal."
exit 6
