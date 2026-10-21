#!/bin/bash
# SPDX-License-Identifier: AGPL-3.0-only
# C4's network change, ORCHESTRATOR-EXECUTED (owner-authorised): a self-restoring en0 down/up on the
# laptop.  The drop is ~A13_C4_DOWN_SECONDS; the radio is armed to return BEFORE it goes down.
#
#   arm    push the restorer to the laptop and start it (it sleeps, then turns en0 back on)
#   drop   record BEFORE, power en0 off, record the drop instant (this ssh will not come back)
#   await  poll until the laptop answers again with a non-empty ipv4 on en0, bounded; record AFTER
#   check  read-only: power state + ipv4 (never state-changing)
#
# Evidence C4 needs (p79r1-interventions.md): the toggle's log ON THE LAPTOP, before/after
# `networksetup -getairportpower en0` and `ipconfig getifaddr en0`, the host RETURNING within a bounded
# deadline, and no request left stuck across the drop.  A link that does not come back is a FAILED
# intervention, not a proven one -- this script says so in its own log rather than reporting success.
set -u
LAPTOP=${A13_LAPTOP:-member-mac}
EV=${A13_EVIDENCE_DIR:-$HOME/Documents/mycelium-core-program/runs/wp8-02-useful-two-mac}
LABEL=${A13_LOG_LABEL:?A13_LOG_LABEL must be set (the round label, e.g. p81r1)}
TOOLS=${A13_TOOLS:-$HOME/Documents/playground/mycelium-core-pilot/scripts/a13_operator}
RR=${A13_LAPTOP_ROOT:-/Users/member/mycelium-wp8-02}
DOWN=${A13_C4_DOWN_SECONDS:-25}
DEADLINE=${A13_C4_RETURN_DEADLINE:-120}
LOG="$RR/c4-toggle-$LABEL.log"
LOCAL_LOG="$EV/c4-toggle-$LABEL.log"
RESTORER="$RR/c4_restorer-$LABEL.sh"
SSH="ssh -T -o BatchMode=yes -o ConnectTimeout=10"
stamp() { date -u +%Y-%m-%dT%H:%M:%SZ; }

probe() {  # read-only, on the laptop
  $SSH "$LAPTOP" "echo power=\$(/usr/sbin/networksetup -getairportpower en0) ipv4=\$(ipconfig getifaddr en0 2>/dev/null || echo NONE)"
}

case "${1:?usage: c4_network_toggle.sh arm|drop|await|check}" in
check)
  echo "$(stamp) CHECK label=$LABEL $(probe)"
  ;;
arm)
  B64=$(base64 <"$TOOLS/c4_restorer.sh")
  $SSH "$LAPTOP" "echo \"$B64\" | base64 -d > '$RESTORER' && chmod +x '$RESTORER' && \
    echo \"$(stamp) ARM label=$LABEL down_seconds=$DOWN\" >> '$LOG' && \
    nohup '$RESTORER' '$LABEL' '$DOWN' '$LOG' '$DEADLINE' >/dev/null 2>&1 & echo ARMED_$LABEL"
  echo "$(stamp) ARM_BEFORE $(probe)"
  ;;
drop)
  echo "$(stamp) DROP_BEFORE $(probe)" | tee -a "$LOCAL_LOG"
  $SSH "$LAPTOP" "echo \"$(stamp) DROP_ISSUED by=orchestrator\" >> '$LOG'; /usr/sbin/networksetup -setairportpower en0 off" \
    || echo "$(stamp) DROP_SSH_ENDED_EARLY (expected: the link just went down)"
  echo "$(stamp) DROP_ISSUED (link down; the armed restorer returns it in ${DOWN}s)"
  ;;
await)
  deadline=$((SECONDS + DEADLINE))
  ok=""
  while [ "$SECONDS" -lt "$deadline" ]; do
    if out=$($SSH "$LAPTOP" "echo power=\$(/usr/sbin/networksetup -getairportpower en0) ipv4=\$(ipconfig getifaddr en0 2>/dev/null || echo NONE)" 2>/dev/null); then
      echo "$(stamp) AWAIT_HOST_ANSWERED $out"
      case "$out" in *ipv4=NONE*) : ;; *) ok=yes; break ;; esac
    fi
    sleep 3
  done
  if [ -n "$ok" ]; then
    echo "$(stamp) AWAIT_RESULT RETURNED $(probe)" | tee -a "$LOCAL_LOG"
    $SSH "$LAPTOP" "cat '$LOG'" >"$LOCAL_LOG.tmp" 2>/dev/null && cat "$LOCAL_LOG.tmp" >>"$LOCAL_LOG" && rm -f "$LOCAL_LOG.tmp"
    echo "C4_RETURNED"
  else
    echo "$(stamp) AWAIT_RESULT NOT_RETURNED within ${DEADLINE}s -- FAILED INTERVENTION, not a proven one"
    echo "C4_NOT_RETURNED"
    exit 9
  fi
  ;;
*)
  echo "usage: c4_network_toggle.sh arm|drop|await|check" >&2
  exit 2
  ;;
esac
