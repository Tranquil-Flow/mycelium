#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-only
# Does the boot-id fix actually fire in this round? Fail closed on silence.
#
# Usage: bootid_guard.sh <owner-app-log>
#   exit 0  FIRED       -- the plan binding received live observations
#   exit 6  DID_NOT_FIRE -- with a named reason (this is the defect class ERRATA-6 describes)
#   exit 7  NO_LOG      -- the round never produced an app log at all
#
# WHY SILENCE IS A FAILURE: two independent defects made e51a6ae's boot-id fix inert in EVERY bundle
# that carried it (a function-local import raising UnboundLocalError, and an audit field the plan's
# closed schema refuses). Both presented as an expected-looking `boot_id_observation_unavailable`,
# so no round log ever disagreed. A round that cannot show its fix fired has not tested the fix.
set -u
LOG="${1:?usage: bootid_guard.sh <owner-app-log>}"

if [ ! -f "$LOG" ]; then
  echo "bootid_guard: NO_LOG $LOG"
  exit 7
fi

# grep -c prints 0 AND exits 1 when nothing matches, so `|| echo 0` would yield two lines ("0\n0") and
# break the numeric tests. Capture the printed number and default it instead.
COLLECTED=$(grep -c 'boot_id_observations_collected nodes=[1-9]' "$LOG" 2>/dev/null | tr -d ' '); COLLECTED=${COLLECTED:-0}
UNAVAILABLE=$(grep -c 'boot_id_observation_unavailable' "$LOG" 2>/dev/null | tr -d ' '); UNAVAILABLE=${UNAVAILABLE:-0}
SCOPE_ERROR=$(grep -E -c "UnboundLocalError|NameError" "$LOG" 2>/dev/null | tr -d ' '); SCOPE_ERROR=${SCOPE_ERROR:-0}
REFRESHED=$(grep -c 'boot_id_refreshed' "$LOG" 2>/dev/null | tr -d ' '); REFRESHED=${REFRESHED:-0}

echo "bootid_guard: collected_lines=$COLLECTED unavailable_lines=$UNAVAILABLE scope_errors=$SCOPE_ERROR refreshed_lines=$REFRESHED"
echo "--- the app's own words ---"
grep -h 'boot_id_' "$LOG" 2>/dev/null | head -5

if [ "$SCOPE_ERROR" -ge 1 ]; then
  echo "BOOTID_FIX_DID_NOT_FIRE"; echo "reason=programming error in the collection path (the ERRATA-6 defect class)"; exit 6
fi
if [ "$COLLECTED" -lt 1 ]; then
  if [ "$UNAVAILABLE" -ge 1 ]; then
    echo "BOOTID_FIX_DID_NOT_FIRE"
    echo "reason=the producer refused (expected-condition path): the fix is running, there was nothing to collect"
  else
    echo "BOOTID_FIX_DID_NOT_FIRE"; echo "reason=no boot-id line at all in $LOG"
  fi
  exit 6
fi
echo "bootid_guard: FIRED -- collected=$COLLECTED refreshed=$REFRESHED"
exit 0
