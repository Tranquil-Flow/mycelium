# p81r1 — the interventions, the order, and what is different from p79r1/p80r1

**Round:** p81r1 · **preview:** preview.81 · **rpc round:** rpc-round81 · **label:** p81r1 (fresh: the
historical `81r1`/`82r1` logs are different file names, `round81r1-*` vs `roundp81r1-*`).

The rules in `p79r1-interventions.md` are **unchanged and still binding** — no unlogged physical action;
intervene on observed progress, never on a timer; one variable at a time with a capture between; confirmed
cancellation means the product confirmed it; restart evidence is the artifact twice over; C4 needs its own
evidence and a link that does not return is a FAILED intervention. Read that file; this one only adds what
p81r1 changes.

## What is new in p81r1 (the .81 change, and why the round exists)

p80r1 died at the identity check: the plan's bound `boot_id` (`boot-615e5402…`) came from the round's **own
live observation at 11:44:14**, the node answered with `boot-424573b4…` at **11:56:46**, and the app-side
refresh that exists to prevent exactly that **fired once, early, and timed out**
(ERRATA-25 §3; `physical_inference_qualification.py:1720`). preview.81 carries the fix: the collection is
**retried inside one bounded budget (180 s, backoff 2 s → 30 s)** and the builder **prefers the live hello
observation** over any recorded topology identity, with placeholders still refused.

**What p81r1 therefore tests, stated so it can fail:** the boot-id gate class for this round must be the
SUCCESS class — the app log must carry `boot_id_observations_collected nodes=2` and the refusal line must be
absent — and qualification must not refuse with `node_hello_identity_invalid` / `kind='boot_id_mismatch'`.

Three verdict classes are distinguished by `scripts/a13_operator/bootid_gate.sh`, and the soak is gated on
them, not on silence: a **DEFECT** (or no log) blocks the round; a **documented-inert refusal** is recorded
and the soak proceeds; **collected** is the class this round is built to reach. **No round may claim the fix
fired without the success line.**

## If qualification refuses again (decided in advance, not in the moment)

Use the ERRATA-25 bracket, never a guess:

1. **Discriminating test:** re-run with `A13_ROTATE_RELAY=0` and the replacement code **unchanged**. If the
   identity still moves, the **worker replacement** is implicated and must be made identity-preserving; if
   it does not move, the **relay rotation** is implicated (`managed_runtime.py:181`, "re-admits the SAME node
   with a newer incarnation").
2. **Never** relax an identity check (`node_id`/`host_id` mismatch stays refused), and **never** widen a
   timeout to pass a round.
3. Record the bracket, not a conclusion: attribution between replacement and rotation was explicitly left
   unresolved by ERRATA-25, and this program has corrected itself three times by reasoning ahead of the read.

## Order for p81r1

1. **Pre-launch.** Laptop online; disk headroom on BOTH Macs; 0 strays and both surfaces absent; fresh label
   `p81r1`; gate GO **for this head** (receipt head == build head); preview.81 deployed with LOCAL == REMOTE
   exe sha256; the fix's own bytes proven present in the shipped bundle by a POSITIVE control (not a marker
   name). All of it committed **before** launching — the round's identity assertion fails on a dirty tree.
2. **Launch** `chain-preview81e.sh` DETACHED (`detach_run.py`; macOS has no `setsid`). Watch for
   `chain_finished`, the boot-id verdict class, and the smoke's terminal.
3. The chain captures `pre-soak` by itself, then starts the soak.
4. Sample until the first requests show **healthy** phases; then, one at a time, each bracketed by a
   `round_status_capture.py` before/after pair:
   a. **owner worker restart** → before/after captures → `restart_count` **+1** in `membership.state.json`
      plus a new incarnation in the pump binding, read from the artifact on both sides;
   b. **laptop worker restart** → the same;
   c. **C4's network change** → `c4_network_toggle.sh arm` (the restorer is armed BEFORE the drop and does
      the restoring from its own process), then `drop`, then `await`. Its own log on the laptop is the
      primary evidence; a link that does not return inside the deadline is a FAILED intervention and is
      reported as one.
5. The chain captures `post-soak` after the soak returns and before the trap quits the app.
6. Quit both apps, sweep both Macs, verify `stray_count 0` (re-sweep: the trap's own sweep can race the
   daemons' shutdown). Then the collector: `scripts/a13_operator/p78r1_evidence.py post p81r1`.

## C5's counter, restated (the semantics question the owner has been asked and which does NOT block)

The worker replacement keeps the **CURRENT member incarnation** — an executor process is not a re-enrollment —
so C5's "one worker restart per Mac" is satisfied by the fleet **surviving** a mid-soak worker restart with
the counter unchanged and zero stuck requests, which is reading (i) of the two recorded in
`status/decisions.log`. A deliberate restart that also re-enrolled would collide with the churn gate's
`restart_count == rounds observed` invariant. Record the counter **and** the replacement record
(`mycelium.a13.worker_replacement.v1`) for each Mac either way, and let the artifact say which reading fired.

## What not to do (unchanged, repeated because it is the expensive kind)

Do not clear a wedged node's persisted command state. Do not relax an identity check. Do not widen a deadline
to pass a step. Do not use a state-changing command as a privilege probe (`networksetup -getairportpower`
only; the toggle in `c4_network_toggle.sh` is the authorised exception and it is logged). Do not edit the tree
while the round runs. Do not overwrite a round log.
