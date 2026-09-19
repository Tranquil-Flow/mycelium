# p79r1 -- the interventions, the order, and the two rules p78r1 paid for

## What this round is for

C5's apparatus inside ONE window: >=100 requests, >=10 cancellations **confirmed by the product**,
sequential + concurrent, one worker restart per Mac, one network change -- with zero stuck, zero
cleanup-unproven, zero failed terminals, zero duplicates, zero loop deaths. C4 is the network change and is
orchestrator-executed under the owner's authorisation, as a detached self-restoring toggle.

## The p78r1 lessons, itemised (each one is a rule below)

p78r1 withheld 103 terminals and the round could not explain them. Re-read on 2026-09-20 from its own
artifacts: 6 healthy successes (55-62 s, 32 tokens), then a published failed/request_cleanup_unproven, then
103 blocked with no terminal -- while the managed-control ledger shows ALL work minting and even the health
probes stopping at ~19:45:40 and the laptop node continuing to serve cleanup to 19:48:03. Three defects in
HOW the round was run made that unreadable:

1. **ONE INTERVENTION WAS NEVER WRITTEN DOWN.** `roundp78r1-interventions.log` (341 B) records the owner-side
   kill at 19:46:36Z and nothing else: no laptop-side action, no AFTER line. So "did an intervention cause
   the wedge?" is unanswerable from the artifacts, although the wedge began within seconds of the first
   block. **An unrecorded intervention is an unmeasurable variable.**
2. **THE RECORDED KILL CAME ~110 s AFTER THE FIRST BLOCK**, from budgeting the soak window off a comment
   rather than off observed progress. **Interventions run on observed soak progress, never on a wall-clock
   guess.**
3. **THE ROUND HAD NO APP VIEW.** Every status snapshot predates the soak and the app was gone by the time
   anyone asked, so the coordinator's incident list and the per-request failure codes died with it. The
   chain now captures `/__mycelium/runtime/admission-status` pre- and post-soak, and this round brackets
   EVERY intervention with the same capture.

## Rules for this round

* **No unlogged physical action.** Every intervention is appended to
  `roundp79r1-interventions.log` with a UTC timestamp, BEFORE and AFTER, and is bracketed by
  `round_status_capture.py --label p79r1 --stage <before|after>-<what>`. An intervention without a pre/post
  pair is not evidence and will not be reported as having been executed.
* **Healthy window, not a timer.** Sample the status and the app log's `live route terminal blocked` count.
  Intervene while the round is demonstrably healthy. If health collapses, STOP and record it: the p78r1
  transition was one-way, so a dead path cannot absorb the remaining interventions meaningfully.
* **One variable at a time, with a capture between.** Restart, capture, confirm the counter moved, then the
  next action. Never two interventions in the same breath -- p78r1 lost the ability to attribute because the
  actions were not separated by evidence.
* **Confirmed means confirmed.** A cancellation counts for C5 only when the product confirms it
  (`cancel_status` AND a terminal). p78r1 had 4 requested / 0 confirmed; report requested and confirmed
  separately and never call a request confirmed because the endpoint answered 200.
* **Restart evidence is the artifact, twice over.** `restart_count` advances by EXACTLY 1 in
  `membership.state.json`, read from the artifact on both sides, with run id AND timestamp checked together,
  plus a new incarnation in the pump binding. "Replacement is not instantaneous" (measured: the owner held at
  26 for >60 s), so the wait is bounded and its outcome recorded either way.
* **C4 needs its own evidence**: the toggle's log on the laptop, before/after `networksetup
  -getairportpower en0` and `ipconfig getifaddr en0`, the host RETURNING within a bounded deadline, and no
  request left stuck across the drop. A link that does not come back is a failed intervention, not a proven
  one.

## Order

1. Pre-launch: laptop online; disk headroom on BOTH machines; 0 strays and both surfaces absent; fresh label
   (`p79r1`); gate GO for the head; deploy preview.79 + three-way content verify; the instrument's own bytes
   proven present in the shipped bundle.
2. Launch `chain-preview79e.sh` DETACHED. Watch for `chain_finished`, the boot-id verdict class, and the
   smoke's SUCCESSFUL terminal (the soak is refused otherwise, honestly).
3. The chain captures `pre-soak` by itself, then starts the soak.
4. Sample until the first requests show healthy phases; then, one at a time:
   a. owner worker restart -> before/after captures -> restart_count +1 + new incarnation;
   b. laptop worker restart -> the same;
   c. C4's toggle -> before/after captures + the toggle's own log + the return check.
5. The chain captures `post-soak` after the soak returns and before the trap quits the app.
6. Quit both apps, sweep both Macs, verify `stray_count 0`. Then the collector:
   `p78r1_evidence.py post p79r1`.

## What NOT to do (unchanged)

Do not clear a wedged node's persisted command state (a wedge is a finding; `uncertain-ack.private.json` and
`in_flight_commands` exist so it names itself). Do not relax an identity check. Do not widen a deadline to
pass a step. Do not use a state-changing command as a probe -- 2026-09-20 that powered the laptop's Wi-Fi off
and cost a physical restore; probe with `-get` / `-l` / read-only forms only. Do not edit the tree while the
round runs.
