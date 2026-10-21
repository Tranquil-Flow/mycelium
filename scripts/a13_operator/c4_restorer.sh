#!/bin/bash
# SPDX-License-Identifier: AGPL-3.0-only
# Runs ON THE LAPTOP (pushed there by c4_network_toggle.sh).  ARMED BEFORE the drop on purpose: it is a
# separate process with its own sleep, so the radio returns even if the orchestrator's ssh dies with the
# link -- the failure mode that cost a physical restore on 2026-09-20 was a drop with no armer
# (status/decisions.log:6155 -- the same toggle took the laptop off-network at 04:45 and it stayed down
#  until a human reached the machine; a link that does not return is a FAILED intervention).
# Read-only probes only: -get forms.  Never a state-changing command used as a privilege probe.
set -u
LABEL=${1:?label}
DOWN=${2:?down_seconds}
LOG=${3:?log_path}
RETURN_DEADLINE=${4:-120}
stamp() { date -u +%Y-%m-%dT%H:%M:%SZ; }
echo "$(stamp) RESTORER_START label=$LABEL down_seconds=$DOWN return_deadline=$RETURN_DEADLINE" >>"$LOG"
sleep "$DOWN"
/usr/sbin/networksetup -setairportpower en0 on
echo "$(stamp) RESTORE_ISSUED on" >>"$LOG"
deadline=$((SECONDS + RETURN_DEADLINE))
addr=""
while [ "$SECONDS" -lt "$deadline" ]; do
  addr=$(ipconfig getifaddr en0 2>/dev/null || true)
  [ -n "$addr" ] && break
  sleep 2
done
echo "$(stamp) RESTORE_RESULT power=$(/usr/sbin/networksetup -getairportpower en0) ipv4=${addr:-NONE}" >>"$LOG"
if [ -n "$addr" ]; then echo "$(stamp) RESTORE_OK" >>"$LOG"; else echo "$(stamp) RESTORE_FAILED" >>"$LOG"; fi
exit 0
