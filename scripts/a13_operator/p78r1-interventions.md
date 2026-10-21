# p78r1 -- the interventions and the order to do them in

## What this round is for

C5's apparatus needs, inside ONE round window: sequential + concurrent requests, >=10 confirmed
cancellations, one worker restart per Mac, and one network change -- with zero stuck or unproven
requests.

**C4 (the laptop on a different network) is now ORCHESTRATOR-EXECUTED.** The owner lifted v3's
human-only boundary on 2026-09-20 ("fully authorised to go ahead whenever"), so the network change is
done inside this round rather than waiting for a separate window. It is executed as a DETACHED,
SELF-RESTORING toggle -- the link must drop and come back without a human:

    nohup sh -c 'networksetup -setairportpower en0 off; sleep 25; networksetup -setairportpower en0 on' \
      > /tmp/net-toggle.log 2>&1 &

The worker restart is a pre-authorised physical intervention on both Macs and is done inside the soak
window.

So p78r1 runs: chain -> boot-id gate -> smoke -> soak, with the restart AND the network change
executed inside the same window.

## EVIDENCE REQUIRED FOR C4 (do not claim the network change without it)

- the toggle's own log on the laptop (`/tmp/net-toggle.log`), plus the before/after readings
  (`networksetup -getairportpower en0`, `ipconfig getifaddr en0`);
- the host RETURNS: SSH and the Tailscale path answer again within a bounded deadline after the ~25 s
  outage -- a link that does not come back is a failed intervention, not a proven one;
- no request left stuck across the drop, and no command executed twice (the route's own counters);
- the soak's counters are attributed across the outage: requests issued before/after it are counted
  separately so a gap in the middle cannot be read as either success or failure.

## What is new in this round's bundle (185b17f + 555945d + 6c8b318, ERRATA-20/21)

p77r1 reached a QUALIFIED deployment and its chain request completed, then its smoke request was
blocked with `command_cleanup_receipt_missing` at 18:17:05 -- the receipt-sealing family. ERRATA-19
traced it: the advertised cleanup bound was arithmetic (2,000 - 500 - 200 - 100 = 1,200 ms advertised,
1,300 admitted) while the same file recorded the peer's MEASURED cleanup at 1.83 s / 3.18 s, so the
admission predicate could never refuse a peer that physically cannot meet the window, and the
shortfall landed mid-request.

Shipped numbers in this bundle: budget 8.0 s, window 4,500 ms, admitted 4,300 ms, advertised
4,200 ms -- and, for the first time, the owner's OWN mints use the same record, in the route, in the
router port, and in the command controller's ceiling. Unmigrated, the controller would have refused the
route's re-derived deadline with `cleanup deadline exceeds the original cancellation budget`.

## The one open question this round can answer

Does the smoke request now seal its cleanup receipt and reach a `succeeded` terminal? If it does, the
soak runs. If it does not, read the SHIPPED advertisement and the peer's measured cleanup from the
round's own artifacts before theorising: the offline boundary measured on the round-50 fixture is
<= 1.5 s final, >= 2.0 s proven, so a window of 4.5 s should be well clear of the peer's 1.83 s.

## Order, after the round launcher has started

1. Confirm the round is live and detached: the launcher's pid, the supervisor log path
   (`runs/wp8-02-useful-two-mac/roundp78r1-supervisor.log`), and the pre-round sweep's
   `stray_count 0` with both surfaces absent.
2. Wait for the chain to reach the serving phase. The boot-id gate decides by VERDICT CLASS: a
   documented refusal proceeds; a defect or an unknown class blocks; a missing log blocks (exit 7).
3. The smoke request must reach a SUCCESSFUL terminal (`succeeded: true`) or the soak is refused and
   the round ends honestly.
4. If the smoke passes, the soak begins. DURING the soak, in this order:
   a. restart each Mac's worker once -- replace-not-restart for the runtime's purposes
      (`worker.py:741`), no node-command sender exists in the adapter except the shutdown path
      (`control_session.py:640`), so this is a physical intervention and is done as one. Per node:
      `restart_count` advances by EXACTLY 1 in `membership.state.json`, read from the artifact on the
      owner's local-contributor and on the laptop's target root -- never from a console line, and with
      run id AND timestamp checked together;
   b. run the network change (C4) with the self-restoring toggle above, and record its evidence;
   c. read the soak's counters around both interventions so their effect is attributable.
5. After the soak: quit both apps, sweep both Macs, verify `stray_count 0` with both surfaces absent.
6. Record: the round dir gets the deployed exe digest AND the bundle's build-time identity digest, the
   two `restart_count` readings per node, the network-change evidence, and the soak's own counters.
   C5 is claimed only from the soak's counters plus the restart evidence -- never from the round
   merely finishing.

## What NOT to do in this round

- Do not clear a wedged node's persisted command state; if the target wedges, that is a finding, and
  `uncertain-ack.private.json` / `in_flight_commands` exist so the next occurrence names itself.
- Do not relax any identity check. The cleanup budget was re-derived from measurement by explicit
  owner decision (the same pattern as b7f0e07); nothing else gets widened to get past a step.
- Do not run a state-changing command as a privilege probe again: on 2026-09-20 that powered the
  laptop's Wi-Fi off and cost a physical restore. Probe with `-get`/`-l`/read-only forms only.
- Do not edit the tree while the round runs.
