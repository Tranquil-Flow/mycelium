# ERRATA-10 — `command_deadline_exceeded` identifies a *family*, not a *cause* (and a revision of my own first reading)

**Written 2026-09-19 late, at head `222a035`.** Retracts an attribution in `status/decisions.log` and in the
W3 framing of `GOAL-prompt-finish-pilot-v3.md`. **Revised within the hour**: §3 records what I first
concluded, and why the pinned tests prove me wrong. The retraction in §1–§2 stands; the policy claim in §3
does not.

## 1. What was recorded, and why it does not hold

The p74r1 evidence was reasoned about as: the request dies on a native failure of `infer_start` on the owner's
node → the route cannot prove owner-scoped cleanup inside its 2000 ms bound → incident
`request_cleanup_unproven`, **reason `command_deadline_exceeded`** fanned out to both nodes. The reading
attached to that string was *"the route's cleanup budget expired"*.

That reading does not survive contact with the code. The token is raised verbatim from at least eight sites:

| # | site | what actually happened |
|---|------|------------------------|
| 1–2 | `mycelium_live/route.py:456`, `:2440` | the route's **local slice guard** expired |
| 3 | `mycelium_a13_macos/worker.py:595` (`_deadline_check`) | the **command** deadline expired |
| 4 | `worker.py:604` (`_check`, guard lock) | the **guard lock** was contended; no command was dispatched |
| 5 | `worker.py:680`, `:703` (`_group_rss`) | the guard's **`/bin/ps` memory observation** exceeded its cap |
| 6 | `worker.py:464` | further site in the same family |
| 7 | `control_session.py:581` | the control session's own wait expired |
| 8 | `route.py` peer-transport path | attempt expiry surfaced as `<code>@<node>` |

**So a record reading "reason: `command_deadline_exceeded`" identifies a family of eight conditions, not one
cause.** The p74r1 attribution to the route's cleanup budget picked one member of the family with no evidence
that it was the member. That is the retraction, and it is the same error family as ERRATA-6/7/8/9: trusting a
string instead of the producer behind it.

## 2. The law this adds

**A token that several code paths can emit is not evidence about which one did.** Before reading a reason
string as a cause, `grep` the string; if its producers outnumber one, the honest statement is the *family*.
This generalises verification law 2 (artifacts over console lines) to *tokens over nothing*.

Consequence for W3, which is now correctly posed: not "why did the route's cleanup budget expire", but
**"which producer consumed the handed deadline, and can the evidence say?"** — with §3 fixing what may
*not* be changed while answering.

## 3. REVISION — the policy claim I first made, and why it was wrong

My first draft of this erratum went further: it argued that `route.py:113-121`'s
`_CLEANUP_RETRYABLE_ATTEMPT_CODES` predicate is an "identity check that cannot distinguish its subject",
because it matches `command_deadline_exceeded@` **by substring**, and that the route therefore applies a
retry policy written for its own slice guard to worker-side guard failures — burning owner window on retries
that cannot succeed. **That conclusion is wrong, and I retract it before it reached a commit.** Two pieces of
evidence falsify it:

**(a) The route's own definition covers every producer, literally.** `route.py:113-116` states the meaning:
*"Local, node-suffixed codes that mean 'this attempt expired against the deadline it was handed', as opposed
to an authoritative proof failure."* For each of the eight sites above, the attempt **did** fail to complete
against the deadline it was handed. Site 4 (guard lock held) and site 5 (`ps` slow) satisfy that definition
exactly as sites 1–3 do. The classification is coherent, not confused.

**(b) The behaviour is deliberate and pinned by a test.**
`tests/a13_macos/test_worker_concurrency.py:383-392` holds the guard lock open, then asserts that a command
attempt with a live deadline raises `command_deadline_exceeded`. The guard-lock path is *supposed* to report
this way. My change would have required overwriting that pin — i.e. changing a designed contract to suit an
argument I had reasoned rather than measured. That is the failure mode the plan's WHAT NOT TO DO names ("do
not relax an identity check to make a round pass"), and it is the second time this session that reading the
*test* rather than the *code alone* overturned my reading.

**(c) A retry can legitimately succeed.** The guard lock is usually transiently held and `ps` is usually
fast; a bounded retry while owner time remains is a reasonable recovery for both. My "retries that cannot
succeed" was an assumption about transience dressed as a measurement. It is not mine to assert.

**Therefore: no change to the retry predicate, and no change to the token raised by sites 4 and 5.** The
retryable set stays as written. If anyone later revisits this, the burden is on fresh evidence, not on this
erratum.

## 4. What is actually wrong, then — and it is narrower and real

Nothing about the *classification*. The gap is **attribution in the evidence**: the shared token cannot say
which step consumed the handed deadline, and the observed p74r1 record (`exception_types: []`, `frames: []`,
`scoped_failure_code: "none"` — the third such gap) says neither which step nor why. A round that fails with
this reason currently cannot distinguish "the native overran" from "the guard's own `ps` was slow" — and the
difference decides whether the remedy is a bigger budget (forbidden), a fixed product defect, or nothing at
all.

So W3/W4's deliverable is **attribution, not reclassification**: the next round's evidence must name the step.
W4's landed `_native_error_code` (`worker.py:173-190`) does this for the *native's* own bounded code, and it
is the right first move because it is where the native's answer already existed and was being discarded.

## 5. Unchanged by this erratum

* `1704ac2` (publish `failed/request_cleanup_unproven` instead of suppressing the terminal) — unaffected; it
  asserts a failure to **prove**, true whichever producer fired.
* p74r1's *sequence* (owner-node `infer_start` native failure → cleanup unproven → no terminal) — stands.
* The `_CLEANUP_RETRYABLE_ATTEMPT_CODES` predicate and both pinned behaviours — deliberately left alone.
