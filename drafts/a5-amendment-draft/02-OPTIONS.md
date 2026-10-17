# 02 — The two admissible purposes for a future A5 run (drafted, not authorised)

> **Status: DRAFT. NOT APPROVED. NOT APPLIED. Neither option authorises a run.** Both are *conditions* an
> owner may attach to a future A5 grant. Neither is granted by `01-AMENDMENT.md`; that amendment only
> **registers the permissible set of purposes** so the decoupling cannot be read as a door left open.
>
> **What this file is not:** it is not a proposal to run anything now. Running nothing is the default, and
> it is also the honest default given the program's current critical path. See §4 (recommendation) and §6
> (falsifiers).

Both options are required to satisfy the programme's existing constraints **before** they are drafted
further, so they are restated here as binding on either option:

- **the threshold is not relaxed** — 10 % point estimate **and** paired 95 % lower bound, unchanged
  (`docs/superpowers/specs/2026-08-18-mycelium-a5-multistage-replication.md:211`, `:239-243`);
- **no identical scored retry** — a rerun of the §9 workload expecting a different bound is forbidden
  (`2026-08-20-mycelium-a5-finish-plan.md:189`; `ledger:5465 "identical_scored_retry_authorized": false`);
- **the candidate binds an immutable commit**, not the integration branch head;
- **replica tracks stay disabled by default** (`PLAN.md:33`, D3);
- **nothing is relabelled** — `design_only`, V23 and Stage B are untouched.

---

## 1. Option (a) — a functional NON-PROMOTING requalification, bound to an immutable commit

### 1.1 Declared purpose (to be written **before** the run; this is the whole point of the option)

> **Purpose declaration (`mycelium.a5_functional_requalification_purpose.v1`).** This run exists to
> re-establish that the multi-stage replica machinery **functions** on a named, immutable integrated
> candidate — legal tracks, distinct concurrent tracks on distinct placements, per-placement work evidence,
> cancellation, replica loss with a surviving incumbent, exact cleanup, and the eight-workspace browser
> path. It is a **functional** requalification. It is **not** a materiality measurement, it **does not**
> run the frozen §9 materiality benchmark, it **does not** compare primary-only against replicated
> throughput, and it **can never** promote A5. Its output is a **disposition** (`functional_pass` /
> `functional_fail`) carrying `qualification_claim: false` and `promotion_authorized: false`. Whichever way
> it lands, A5 remains `design_only`.

### 1.2 Binding

| field | value (to be filled by the granting owner) |
|---|---|
| candidate commit | **immutable**, named in the declaration; e.g. an integrated commit such as `b9001e6`, or a named successor — **never a branch head** |
| candidate tree / package digest | the archived package built from *that* commit, digest recorded |
| model / representation | the frozen A5 bindings already in the record (`a5-completion-record.v1.json:17-24`), or new ones if the model changes |
| fleet | the already-planned two/three-host window; **bundled**, not created for A5 (see §1.5) |
| evidence root | outside the repo, per `PLAN.md:154` |
| attempts | **one**; a failure goes to RCA and needs a NEW local RED→GREEN test before another attempt (`PLAN.md:119`) |

### 1.3 Acceptance criteria (functional, and already proven once — this is the option's strength)

The seven retained functional evidence shapes in the A5 record are the template
(`a5-completion-record.v1.json:72-136`, `sealed_evidence`):

| name | what it asserts |
|---|---|
| `physical_positive` | two overlapping complete requests used **distinct legal tracks**; every listed node performed real work; final reservations, queue and stage-local KV deltas zero |
| `concurrent_cancellation_repeat` | 24/24 campaigns, 72/72 cancellations, maximum cancel-to-terminal 676.876 ms, zero incidents, zero live resources |
| `illegal_replica_negative` | memory, parity and stale-authority failures rejected fail-closed; incumbent preserved and restored; zero final resources |
| `replica_loss_negative` | replica loss cancels the affected request (797.296 ms); the unaffected request and follow-up completed; reservations and queue zero |
| `browser_positive` | Chromium, Firefox and WebKit each verified all eight workspaces, distinct complete tracks, refresh, back/forward, reconnect, terminal reconstruction, clean second session, zero browser failures |
| `browser_lifetime_replica_loss` | loss of a placement cancelled only the affected request (916.503 ms) |
| `browser_degraded_survivor` | all three engines verified all eight workspaces after loss; a surviving incumbent-track request completed |

**Why this option has a real, non-cosmetic value:** the A5 record's own `limitations` say *"The foreground
physical route was intentionally stopped after evidence capture, so retained evidence is not current
admission authority"* (`a5-completion-record.v1.json:156`). A functional requalification on a named
integrated candidate is the only thing that can turn that retained-but-not-current evidence into
current admission authority for the replica path — and that matters because the replica code path shares
hot runtime files with the A5/A6/A13 lifecycle edits (the review's own A4 row notes requalification is
needed after exactly those edits: `PROGRESS_MATRIX.md:28`).

### 1.4 What Option (a) explicitly does NOT do

- It does **not** run the frozen materiality benchmark, and it does **not** re-derive the threshold.
- It **cannot** promote A5 and it **cannot** change `state: design_only`. This is not a policy choice —
  spec §11 makes material measured gain a **necessary** condition of A5 completion
  (`spec:305-308`: *"A5 is complete only when a multi-stage physical graph … produces material measured
  gain … Until then it remains `design_only`."*), and a functional run does not measure material gain.
- It does **not** authorise different network paths, sealing, push or release claims.
- It does **not** retire the fleet serialisation in `P06`.

### 1.5 Cost, and the one honest objection to it

| cost | detail |
|---|---|
| fleet windows | it needs the physical fleet. `P06` (A6 *physical*) is serialised against fleet use (`PLAN.md:32`), so this competes with post-pilot work. |
| authorisation | it needs its own scoped grant. The 2026-09-07 owner decision **explicitly did not** authorise this class of work: `ledger:5466 "additional_downstream_functional_browser_execution_authorized": false`. |
| programme attention | the program's live blocker is the A13 critical path, not A5. |
| **mitigation** | **bundle** it into a fleet window that is already being opened for `P06` or `P22`-style qualification, rather than opening a window for A5. This is the difference between "a cost" and "a marginal cost", and it is why the recommendation in §4 is *register, don't schedule*. |

### 1.6 The record already contains an attempt at this option — and it failed closed

This is the most important fact in this file, and it changes the option's price. An "A5 integration
requalification" cycle was **run repeatedly** by the programme, and **every candidate failed**:

- **21 distinct candidate roots** exist under one run root
  (`/Users/evinova-self/mycelium-physical-run/a5-integration-requalification-20260831/run-091603-ce31/phase5/candidate-*`,
  counted from the ledger itself — the same 21 the review notes at `REVIEW.md:213`). **I counted them; I did
  not re-execute any of them.**
- **Six distinct failure reason codes** appear in that record: `gate_state_timeout`,
  `route_not_alive`, `qualification_install_http_403`, `staged_sidecar_not_executable`,
  `operator_authorization_missing`, `test_process_umask_contamination`.
- The **functional** failures are the ones that matter for Option (a), because Option (a) measures the
  functional path:
  - `gate_state_timeout` — `578` requests observed, route alive, `0` incidents, `0` replica losses, but
    **`64` nonterminal request records retaining `128` placement reservations** against a `240 s`
    zero-resource wait bound. The handover's own words: *"This is a product cleanup-invariant failure, not
    an external network failure. **The exact source-level root cause is not yet established.**"*
    (`A5_INTEGRATION_REQUALIFICATION_V16_BLOCKED_HANDOVER_2026-09-03.md:41,42-48,50-52`.)
  - `route_not_alive` with incident `route_peer_process_lost` on `placement-001`, after `395` requests, when
    both peer nodes left the tailnet simultaneously; *"The benchmark attempt is consumed. No v15 rerun or
    downstream gate is authorized."* (`A5_INTEGRATION_REQUALIFICATION_V15_BLOCKED_2026-09-03.md:11-20`.)
- The chain terminated in a recorded **failed closure**: `goal_complete=false`, `qualification_claim=false`,
  `promotion_authorized=false`, `integration_commit_created=false`, `push_performed=false`
  (`A5_INTEGRATION_REQUALIFICATION_BLOCKED_2026-09-02.md:13-20`).

**What that means for Option (a):** it is **not** a cheap maintenance pass over a proven protocol. It is the
*same shape of run that already failed closed on this fleet, for a cause that has not been localised*. So
Option (a) carries a **pre-condition that must be satisfied locally first**:

> **Pre-condition for Option (a).** Root-cause the `gate_state_timeout` cleanup invariant — nonterminal
> request records retaining placement reservations past the zero-resource bound — **locally**, with a
> RED→GREEN test, before any window is opened (`PLAN.md:91` §5.1: *"A local reproduction test that is RED on
> the failing bytes before any fix"*). The owner's standing rule is explicit that physical rounds must be
> **minimised** and that a round should not be spent discovering what could have been resolved by reading
> code and docs (`status/decisions.log:66`).

With that pre-condition met, Option (a) becomes a *confirmation* run rather than a *discovery* run — which
is exactly the discipline the programme already imposes on itself. Without it, Option (a) is a repeat.

---

## 2. Option (b) — a NEW performance protocol, motivated by a declared architecture or variance change

### 2.1 Declared purpose (to be written **before** the run)

> **Purpose declaration (`mycelium.a5_new_protocol_purpose.v1`).** This run exists because a **named,
> dated change** has invalidated the reasoning that made the 2026-08-30 protocol adequate — either (b1) an
> **architecture change** to the measured path (the runtime, the component-cost observability, the
> transport, or the placement machinery), or (b2) a **measured variance change** in the paired-window
> statistic. The new protocol is frozen and published as a **new manifest with a new digest**, before any
> candidate measurement is inspected. The old protocol and its `inconclusive` outcome are **superseded, not
> edited** — spec §9 is explicit that changing the manifest *creates a new benchmark protocol and
> invalidates measurements captured under the earlier digest* (`spec:204-205`).

### 2.2 What must be named in the declaration (the option is inadmissible without this)

| (b1) architecture change | the exact commit/file that changed the measured path, and *why that change moves the metric's meaning* — not merely that code changed |
| (b2) variance change | a **measured** reduction in the per-window variance (or a measured demonstration that the effect is smaller than the window noise at n=6 pairs), measured *before* scoring, not asserted |

**Both must preserve:** the **metric** (completed generated tokens per wall-clock second), the **frozen
decision vocabulary** (`material` / `not_material` / `inconclusive`), the **materiality threshold of 10 %
on both the point estimate and the paired 95 % lower bound**, and the invalidations (any of: server
restart, route reselection, workload or session change, missing/duplicate/reordered window, fewer than 60
requests, incomplete outcome accounting, stale authority, member/runtime incarnation change, thermal or
power drift, software/configuration/instrumentation drift, non-finite measurement, post-hoc exclusion, or
any threshold/workload change → the window is **invalidated**, never zero-valued)
(`spec:207-211`, `:226-243`).

**Inadmissible variants, named so nobody proposes them later:**
- rerunning the same §9 workload with more pairs, hoping for a better bound — this is the forbidden
  identical retry (`2026-08-20-…-finish-plan.md:189`);
- relaxing, re-deriving, or "modernising" the 10 % threshold. **This draft does not propose a threshold
  change, does not justify one, and does not pre-authorise one.** The standing rule is that thresholds stay
  unchanged (`PLAN.md:33`, D3), and any proposal to move one would have to be made **explicitly, on its own,
  as its own owner decision** — never smuggled in as "a new protocol". Say it plainly: a new protocol that
  changes the threshold is a **different proposal** and is out of scope here;
- relabelling the old `inconclusive` result under the new digest. The old measurement is superseded and
  stays `inconclusive`.

### 2.3 Candidate motivations visible today — and why none of them fires yet

| candidate | status |
|---|---|
| **A9 component-aware instrumentation.** The completion plan requires *"component-aware compute/serialization/transport instrumentation; frozen reference-versus-candidate single-request corpus and performance floor"* (`completion-plan.md:210`). Observability that separates component costs is exactly the kind of change that could make a throughput protocol resolvable where a window statistic was not. | **Does not fire today.** A9's candidate `ba6abbe` is **not an ancestor** of pilot HEAD `9d68291` (verified), and its roadmap credit was demoted to `spec` on 2026-09-16 (`status/decisions.log:52`). An unintegrated candidate cannot be an architecture change to the measured path. |
| **Measured variance change.** The 2026-08-30 result is a wide-bounded `inconclusive`: point estimate `+16.71 %`, paired 95 % lower bound `−41.97 %`, against 10 % (`a5-completion-record.v1.json:8-10`, `:94-95`). The bound is wide relative to the effect, which is a statement about the paired-window statistic, not about replication. | **Does not fire today.** No variance measurement exists under a declared purpose. Producing one is itself work, and it must precede the protocol, not follow it. |
| **A5/A6/A13 lifecycle edits to the route.** The review notes requalification is needed after these edits (`PROGRESS_MATRIX.md:28`). | This is a **functional** motivation — it belongs to Option (a), not to a performance-protocol change. It changes the *code under test*, not the *protocol*. |

### 2.4 What Option (b) costs

- a new frozen manifest, its bindings, and the harness for it: design + review + deterministic tests
  before a single physical window;
- a fresh physical campaign (warmups + ≥12 windows × ≥60 requests, per the frozen shape, or whatever the
  new manifest freezes);
- **and it can still land `inconclusive`** — that is the honest possibility a new protocol does not remove,
  because the effect may genuinely be smaller than 10 % on this fleet. None of these costs buys a promotion;
  they buy a *valid* measurement.

---

## 3. Side-by-side

| | **(a) functional non-promoting requalification** | **(b) new performance protocol** |
|---|---|---|
| What changes | nothing about the measurement; it re-runs a **proven functional protocol** on a named candidate | the **protocol itself** — new manifest, new digest |
| Pre-conditions | a named immutable integrated candidate; one fleet window; a scoped owner grant | a **named, dated** architecture or variance change, declared and evidenced **before** scoring |
| Threshold impact | **none** (no materiality measurement at all) | **none** (10 % kept; a threshold change would be a separate proposal, not this one) |
| Can it change `design_only`? | **no — structurally impossible** (spec §11 requires material measured gain) | **in principle yes**, but only on a valid `material` result; and it may well land `inconclusive` again |
| Ready to run today? | **no** — the protocol exists, but the same run shape has failed closed 21 times and the cleanup invariant's root cause is not localised (§1.6); it needs a local RED→GREEN root-cause, then a grant, a candidate name and a window | **no** — the motivating change does not exist yet |
| Main risk | **it is the same shape of run that already failed closed 21 times on this fleet, with the cleanup invariant's root cause still unestablished** (§1.6); competes with fleet windows | cost without a result; invites threshold-drift proposals that must be refused |
| Main value | turns retained-but-not-current replica evidence into **current admission authority** on real integrated bytes, **and** it would exercise the unresolved `gate_state_timeout` cleanup invariant on real hardware | the **only** route by which A5 could ever be promoted |
| Honest verdict | a bounded but **not-yet-earned** run: admissible only after the cleanup invariant is root-caused locally | a research-grade run whose trigger is absent |

---

## 4. Recommendation

**Recommend registering both purposes, scheduling neither now, and — if and when a run is to be
authorised — authorising Option (a), bundled into a fleet window that is already open for other reasons.**

Reasons, in order of weight:

1. **The amendment's job is already done by the decoupling.** The roadmap is unblocked by removing the
   stale condition (`01-AMENDMENT.md` C1/C2). Neither option unblocks anything. So the correct default is
   that **no A5 run is scheduled**, and the options exist only so the decoupling cannot be read as
   "A5 is forgotten".
2. **Option (b) is inadmissible today on its own terms.** Its two admissible motivations are a declared
   architecture change or a declared variance change, and neither exists: the one architecture candidate
   (A9 instrumentation) is not integrated (verified: `ba6abbe` is not an ancestor of `9d68291`), and no
   variance measurement has been produced under a declared purpose. Drafting it further today would be
   inventing a motivation — the exact failure mode the finish plan forbids.
3. **Option (a) is the only one of the two that could be earned today — but it must be *earned*.** Its
   acceptance criteria are functional, already proven once, and its protocol exists. It also retires two
   specific named risks: retained replica evidence is not current admission authority
   (`a5-completion-record.v1.json:156`), and the replica path shares the hot runtime files edited by
   A5/A6/A13 (`PROGRESS_MATRIX.md:28`). **But** the same run shape already failed closed on this fleet,
   repeatedly, with an unlocalised cleanup invariant (§1.6). So the recommendation is conditional and
   ordered: **(a) becomes fundable only after the `gate_state_timeout` cleanup invariant is root-caused
   locally with a RED→GREEN test.** Once that is done, (a) is a confirmation run over an existing fleet
   window and the marginal cost argument holds.
4. **Option (a) cannot be mistaken for a promotion attempt**, which matters for honesty: spec §11 makes
   material measured gain necessary for A5 completion, so a functional run is structurally incapable of
   promoting A5. That makes it a safe thing to fund.
5. **Doing the local root-cause first is cheap and is required by the programme's own rules anyway.** It is
   offline work — reading the request/placement cleanup path and reproducing the reservation leak in the
   harness — and `PLAN.md §5.1` already requires a RED reproduction before any fix while the owner's
   standing rule requires physical rounds to be minimised (`status/decisions.log:66`). Whether or not
   Option (a) is ever authorised, that defect is worth localising: an unlocalised cleanup invariant on the
   replica path is a live risk to any future replica work.

**Explicitly:** "authorise neither, now or later" is a fully legitimate owner answer, and it is the state
this draft leaves the world in if the owner does nothing.

---

## 5. Trade-offs the owner is actually choosing between

| if the owner… | gain | cost / risk |
|---|---|---|
| registers (a)+(b), schedules neither | the decoupling cannot be misread as a loophole; the admissible set is frozen in writing | none material |
| additionally authorises (a) later, bundled | current admission authority for the replica path on real integrated bytes; marginal fleet cost | one bounded run; attention off the A13 critical path; still changes no gate state |
| additionally authorises (b) later | the only possible route to A5 promotion | a full protocol + campaign, possibly landing `inconclusive` again; **invites threshold-drift pressure that must be refused** |
| authorises neither, ever | zero cost; A5 stays a documented, honest dead end | A5's `design_only` posture becomes permanent and unaudited; the "future purpose" clause becomes decorative |

---

## 6. Falsifiers — what would change this recommendation

- **A named architecture change lands and is integrated** (e.g. A9's component-aware instrumentation
  appears in the integrated branch). → Option (b) becomes admissible; this draft's recommendation should
  be revisited, and the protocol drafted against the *integrated* change with a fresh manifest digest.
- **A measured variance study** shows the per-window statistic can separate a ≥10 % effect at a feasible
  sample size. → Option (b) becomes admissible on evidence rather than hope.
- **The `gate_state_timeout` cleanup invariant is root-caused locally with a RED→GREEN test.** → Option (a)
  becomes a fundable *confirmation* run rather than a repeat of a failed shape, and the fleet-bundling
  argument holds on its own. (This is the nearest-term, highest-value change to the options picture, and it
  is offline work.)
- **The owner declares A5 permanently out of scope.** → the "future purpose" clause should then be
  *deleted from the roadmap's completion definition* explicitly, rather than left as a decorative escape
  hatch. That is a different amendment and this draft does not make it.
