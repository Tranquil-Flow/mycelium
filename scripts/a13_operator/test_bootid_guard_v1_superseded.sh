#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-only
# Unit-test the boot-id guard against real and synthetic app logs, BEFORE spending a physical round.
# Every fixture has an expected verdict; a guard that cannot fail is worse than no guard.
GUARD=/tmp/asm/bootid_guard.sh
FIX=/tmp/asm/bootid-fixtures
mkdir -p "$FIX"
PASS=0; FAIL=0

# (a) the real defect: soak04's app log, the round that proved the fix was in the bundle AND broken
REAL=$HOME/Documents/mycelium-core-program/runs/wp8-02-useful-two-mac/roundsoak04-owner-daemon.log

# (b) the fix working
printf 'boot_id_observations_collected nodes=2\nboot_id_refreshed {"remote-mac-920a": ["boot-old", "boot-new"]}\n' > "$FIX/fired.log"

# (c) shipped and running, but the producer legitimately refused
printf "boot_id_observation_unavailable ManagedObservationsError('managed_observation_session_failed')\n" > "$FIX/refused.log"

# (d) nothing at all
printf 'nothing to see here\n' > "$FIX/silent.log"

check() {
  local label="$1" log="$2" want="$3"
  bash "$GUARD" "$log" > /tmp/asm/bootid-guard-test.out 2>&1
  local got=$?
  if [ "$got" = "$want" ]; then
    PASS=$((PASS+1)); printf '  PASS  %-34s exit=%s (wanted %s)\n' "$label" "$got" "$want"
  else
    FAIL=$((FAIL+1)); printf '  FAIL  %-34s exit=%s (wanted %s)\n' "$label" "$got" "$want"
    sed -n '1,6p' /tmp/asm/bootid-guard-test.out | sed 's/^/        /'
  fi
}

echo "=== boot-id guard unit tests ==="
check "soak04 real log (defect)"    "$REAL"            6
check "synthetic: fix fired"        "$FIX/fired.log"   0
check "synthetic: producer refused" "$FIX/refused.log" 6
check "synthetic: silence"          "$FIX/silent.log"  6
check "missing log"                 "$FIX/absent.log"  7

echo "  --- the defect fixture's reason line, for the record ---"
bash "$GUARD" "$REAL" 2>&1 | grep -E 'reason=|collected_lines' | head -3

echo
echo "guard tests: $PASS passed, $FAIL failed"
[ "$FAIL" = 0 ] || exit 1
