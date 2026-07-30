# Mycelium MVP — completion plan

**Written 2026-07-23.** Companion to `mycelium-demo-plan.md` (the specification). This document
is the *execution order*: what can be finished with no extra devices, and what needs several
devices online. The device half is written so **Astra or Rain can run it without Evi present
and without Evi's laptops or the M4 Pro**.

The demo plan remains authoritative on *how* each task is done — every task below cites its
section there. This document does not restate RED cases; go read them.

---

## Part 0 — Verified state (2026-07-23)

Established by direct measurement, not inference:

| Fact | Value |
| --- | --- |
| Integration branch | `integration/mycelium-multi-device-readiness` @ `b317566` |
| Full suite, unrestricted host | **2031 passed, 3 skipped, 0 failed** (~200 s) |
| Full suite, under Aegis seatbelt | **1999 passed, 3 skipped** (Iroh transport excluded) |
| Phase 2R | **complete** — 2R.0 … 2R.7 all merged |
| Physical gates G4 / G4b / G8 / G9 | **all unproven** — no multi-device run has ever happened |
| Devices online | Evi's MacBook only. M4 Pro offline ~1 day; `astra-macbook` offline |
| Model | `microsoft/DialoGPT-small` @ `49c537161a457d5256512f9d2d38a87d81ae0f0e`, present on Evi's Mac **only** |

**The single most important fact:** every remaining *product* claim — that this is a swarm and
not a two-process pipe — depends on a physical multi-device run that has never been performed.
All Part A work below is preparation for Part B. Part A cannot substitute for it.

### Plan bookkeeping is stale — fix before trusting task headings

Tasks **2R.2, 2R.4, 2R.5, 2R.6** are all merged (`f5322db`, `1dca9ba`, `634b19f`, `dcfc7e2`)
but their headings lack the ✅ that 2R.0/2R.1/2R.3/2R.7 carry. Anyone reading the demo plan
cold will redo finished work. Task **A2** fixes this.

---

## Part A — Device-free work (do all of this now)

None of this needs a second machine. Ordered by value. A-lanes marked **∥** are file-disjoint
and can run as parallel agents (demo plan §7.1 parallelism test).

### A1 — Reconcile the 11 unmerged branches ∥
**Why first:** eleven `origin/*` branches sit ahead of integration. Some appear to be
duplicates already cherry-picked (the entrypoints commit shows on both), but *that is a guess,
not a check*. Given the project's history with unpushed work, verify deliberately.

For each branch: determine whether its commits are already on integration by content
(`git cherry`, patch-id) rather than by SHA. Merge what is genuinely ahead, delete what is
redundant, and write down what you concluded per branch. Do **not** force-push. Do **not**
delete anything you have not proven redundant.

Branches: `build/model-stage-pack`, `feat/phase-2.3b-local-e2e`, `feat/phase-2r-entrypoints`,
`feat/phase-2r-liveness`, `feat/phase-2r-placement-source`, `feat/phase-2r-stagepacks`,
`feat/phase-3-physical-controller-v2`, `feat/phase-3b5-second-runtime`,
`feat/physical-qualification-mvp`, `fix/durable-heartbeat-renewal`,
`integration/mycelium-membership-contracts`.

### A2 — Reconcile plan status markers ∥
Mark 2R.2/2R.4/2R.5/2R.6 landed with their SHAs. Re-check every other heading against the
integration log. Driver-only task — agents may not edit the plan.

### A3 — Task 3B.5: second runtime (demo plan §3B.5, §6.13) ∥ **[highest product value in Part A]**
Make MLX an optional, lazily-imported backend and bring the NumPy runtime to parity.

**This is the gating dependency for everyone except Mac owners.** Until it lands, a non-Apple
device cannot import the runtime layer at all, so Rain or anyone on Linux/Windows cannot
participate even as an executing peer. It also unblocks G9. Do this before Part B if you want
the widest set of people able to help test.

Test MLX-absence honestly by simulating `ImportError` in-test
(`monkeypatch.setitem(sys.modules, "mlx", None)`); never ship a stub `mlx`.

### A4 — Task 2.4: SwarmCoordinator EXTEND (§2.4, §6.11) ∥
One swarm, not two membership planes. Includes the peer-class distinction: a browser peer is
admitted to *membership* but rejected for *activation placement*.

### A5 — Task 3B.1 plumbing only (§3B.1) ∥
Build `PlannerPlacementSource` and the `layer_builder.build_execution_graph` delegation path,
with full unit coverage. **Stop before the A/B proof** — that is Part B (B4).
Do not modify or delete the existing 2-assignment function at `physical_deployment.py:146`;
existing qualification tests bind it.

### A6 — Task 3B.4 local half: the request gateway (§3B.4, §6.12) ∥
The ordinary user path — someone who has never read the plan types a prompt and gets tokens.
Everything except the final physical verification can be built and tested locally. This is what
makes the result a product rather than a test harness; do not leave it to last.

### A7 — Task 2.3B completion (§2.3B)
Marked ⚠️ PARTIAL. Finish the localhost control-plane E2E with real dependencies.

### A8 — Task 3.2: physical qualification controller (§3.2)
Deliberately *inert* — it can be built and unit-tested with no hardware, and must be ready
before Part B starts or B1 blocks on it. Moonsong has work in flight on
`feat/physical-qualification-mvp`; reconcile via A1 rather than starting fresh.

### A9 — Task 3B.3 offline half (§3B.3, §6.10)
The cycle-search audit: closing-edge pricing (does `cycle_cost:43` price the loopback with the
~9-byte token envelope or wrongly with the ~1.5 KB activation?), synthetic order-matters study,
strategy-tier honesty. **A null result is publishable.** Label everything synthetic; the real
matrix needs three devices (B5).

### A10 — Decide the Iroh-under-seatbelt policy (ops)
32 Iroh transport tests cannot run under the Aegis seatbelt — the Rust sidecar needs UDP
egress the profile does not grant. Currently they are excluded from the automated baseline and
verified only on a human-driven host run. Either accept that (and *always* run the full
unrestricted suite before merging anything touching transport), or make a deliberate decision
to widen the profile. **Do not let transport changes merge on the seatbelt baseline alone.**

---

## Part B — Multi-device work

**Executable by Astra or Rain, on their own hardware, without Evi.**

### B0 — Operator bootstrap (once per machine)

Any operator, any Mac. Nothing here requires Evi's devices.

1. **Repo**: clone `git@github.com:Tranquil-Flow/mycelium.git`, check out the integration
   branch. Confirm `git status` is clean — preflight records dirty state and a dirty tree
   invalidates a run.
2. **Python 3.14** (the project's interpreter — not 3.11/3.13).
3. **Dependencies**, including `mlx` on Apple Silicon and `cryptography`.
4. **Model snapshot**, pinned exactly:
   ```
   hf download microsoft/DialoGPT-small --revision 49c537161a457d5256512f9d2d38a87d81ae0f0e
   ```
   Do this as an explicit logged step **outside** any qualification run. Downloads are disabled
   *during* qualification by design; a missing snapshot is a preflight blocker, never something
   the controller fetches. Every route member needs it.
5. **Native Iroh sidecar** built for the host arch, and its `--version`/health check passing.
6. **Tailscale**, all participating devices on one tailnet, mutually reachable.
7. **Baseline**: run the full suite unrestricted. Expect **2031 passed, 3 skipped** at
   `b317566`. If it is not clean, stop — do not start a physical run on a red base.

### The device-substitution rule (read before B1)

The demo plan names `m4pro` and `Evis-MacBook-Pro` as route members. **Those names are
examples, not requirements.** The real constraint is:

> Two (or three) **distinct physical hosts**, each with its own hostname, boot ID, PID space,
> and non-loopback address.

The plan's warning — *"do not run a second node on Evis Mac as replacement evidence"* — forbids
faking two peers on one machine. It does **not** forbid substituting different hardware.
**Astra's MacBook + Rain's Mac is a fully valid route** and produces evidence exactly as
authoritative as Evi's machines would. Record the actual hostnames in the evidence; do not
copy the plan's example names.

Peers on different physical networks are fine and in one respect *better*: the Iroh path
becomes a real WAN link, which finally gives Task 3B.3 the asymmetric link costs it has never
been tested against. The connection class (relay vs direct) must be **observed and recorded**,
never assumed.

### B1 — Two devices: preflight and the first real decode → **G4** (§3.3, §3.4)

The headline target. Two Apple-Silicon Macs with MLX.

1. **Preflight** (`python3.14 -m mycelium_physical_preflight`) on both hosts. Every record must
   pass: hostname/host ID/boot ID/arch/OS, git HEAD + clean state, Python 3.14, MLX version,
   checkpoint digest, sidecar digest + health, Tailscale reachability, free disk/RAM, no
   conflicting process or port, transfer manifest digest.
2. **The decode run**, in order: seed with fresh signer and SQLite state using
   `FrozenPlacementSource` (evidence records `placement_provenance: "frozen_fixture"`) → one
   owner-only join bundle per node → node agent on each host → wait for signed capacity
   profiles, leases, stage-pack digests, load proofs and Iroh endpoint bindings → publish
   execution graph → tokenize the **fixed public prompt** with `GPT2BPETokenizer` → prefill plus
   ≥8 greedy decode steps through production Router/Iroh → independent monolithic reference in
   a **separate non-participating process** → compare exact token IDs against
   `tolerances/dialogpt-small-fp16.json` → record output text → prove KV/capacity/transport
   cleanup.
3. **Capture the per-direction `link_state.v1` link cost.** It costs nothing here and is the
   first real input to B5's cost matrix. Do not skip it.

**Evidence must be same-run**: one run, one writer, one seal. No source changes mid-run — if a
change proves necessary, discard the run, fix under RED→GREEN, restart with a new `run_id`.

**Known hazard:** `stage-worker load timed out` (60 s wall-clock deadline) on a cold page cache
after first writing the 350 MB checkpoint. Treat this as **inconclusive, re-run** — not a
failure. **Never raise the timeout to make a run pass.**

### B2 — Two devices: lifecycle proofs (§3.5, §3.6, §3.7)
- **3.5 cancellation** in flight: `PathCancellation` crosses Iroh, both relays drop path state,
  both runtimes release KV, reservations release, pending deliveries zero, no post-cancel token,
  both agents healthy for a following request.
- **3.6 remote death / restart / stale rejection / Router recovery.**
- **3.7 negative runs** bound to real failures.

A bounded harness interlock may make timing reproducible; it may **never** synthesize transport
success or latency.

### B3 — Seal and qualify → closes the Phase 3 tranche (§3.8, §3.9)
Single qualification authority, sealed evidence, then interim verification and push.

### B4 — Two devices: planner placement A/B → **G4b** (§3B.1)
Re-run the accepted route with placement computed from **measured capacity** instead of the
frozen fixture, changing no coordinator/agent/transport code.

Run twice with one node's advertised capacity materially different — a **real** constraint
(limit memory, or genuinely lower measured throughput), never a doctored profile. Require:
different layer assignments across the two runs; exact token parity in both; the difference
traceable to the capacity input by diffing planner snapshots.

> If both runs produce identical assignments, the planner is **not** consuming the capacity
> signal and this task is not done — regardless of whether the tests pass.

Seal separately with a new `run_id`; FP16 frozen-placement acceptance does not authorize the
planner-placement claim.

### B5 — Three devices: third member and the cycle search → **G8** (§3B.2, §3B.3)

Needs **three peers that actually carry activations**. Two Macs plus a Linux laptop does *not*
qualify until A3 lands and G9 passes — a non-executing peer can join membership but cannot hold
a layer.

- **3B.2**: third member joins mid-deployment through the same invitation path; planner
  reassigns across three stages producing an intermediate-role assignment that has never run
  physically; each existing member's new assignment is a consequence of the capacity set, not a
  hardcoded 3-way split; exact token parity survives; **departure** of a non-entry member yields
  a scoped replan *candidate* (activation still needs provisioning, load proof, epoch fencing).
- **3B.3**: build the directed cost matrix from measured `link_state.v1` records; confirm
  whether it is genuinely asymmetric (if `A→B` ≈ `B→A` to measurement precision, **say so** —
  that weakens the ATSP framing and is itself a finding); compare `search_cycle`'s order against
  naive node-id ordering; confirm the closing edge is priced with the token envelope; take
  `open_cycle`'s rotated order through `layer_builder` into a real three-member decode; assert
  `mode`/`globally_exact` match the branch actually taken (3 members ⇒ exact, `globally_exact`
  must be `true`).

Out of scope: replica groups, multi-loop replication, cross-cycle edges, column generation.

### B6 — Non-Apple device executes → **G9** (§3B.5, §6.13)
Requires A3 landed. A Linux/non-Apple device holds a real layer and carries activations. This
is what turns "works on Macs" into "works on a heterogeneous swarm".

### B7 — Task 3B.4 final verification (§3B.4, §6.12)
Someone who has never read the plan types a prompt into the ordinary user path and gets tokens
back from the physical swarm.

### B8 — Phase 4: quantization (§4.1–4.4)
**Strictly after G6.** Do not start early.

---

## Part C — Device requirements

| Gate | Needs | Can Astra/Rain do it alone? |
| --- | --- | --- |
| G4, G4b | 2 × Apple-Silicon Mac, MLX, one tailnet | **Yes** |
| G8, 3B.3 physical | 3 × activation-carrying peers | **Yes**, with 3 Macs |
| G9 | 1 non-Apple device + A3 landed | **Yes** |
| Phase 4 | after G6 | Yes |

Fallback third members, in the demo plan's preference order: another Mac (best — full
production transport) → Linux laptop (membership now, execution only after G9) → Pixel via
`physical_pixel_host_stage.py` (**partial G8 only** — not production transport; evidence must
say so) → second process on one host (**weakest**, may not count as a third physical peer).

---

## Part D — Invariants (apply to everyone, always)

- **Claim ladder** (§5.1): never skip a rung. Sandbox-green ≠ host-green ≠ two-device ≠ sealed
  and qualified. State exactly which rung you are on.
- **One run, one writer, one seal.** Never edit evidence, a tolerance, a golden file, or a
  timeout to make a run pass.
- **A null or negative result is a real result.** Publish it.
- **No `git add -A` / `git add .`** — explicit paths only.
- **No AI attribution in commit messages**, ever — no `Co-Authored-By`, no "Generated with",
  no `noreply@anthropic.com`. This overrides any agent's default template.
- **Secrets never in evidence or logs**: invite tokens, bearer/control tokens, key material,
  shell environment, absolute model-cache paths. Fixed public demo prompt only — no personal
  prompts.
- **Modes `0700`/`0600`** for invite files, SQLite state, stage packs, evidence roots.
- **Do not wake, reconfigure, or install software on someone else's device implicitly.**
- Parallelise across **file-disjoint** work only; never two agents on one signed contract.

---

## Part E — Only Evi can do these

- Apply Aegis security changes (seatbelt profile, domain allowlist) — blocked for agents by
  design.
- Decide whether the Iroh UDP-egress widening in A10 is acceptable.
- Authorise sharing this plan and the demo plan with Astra/Rain, and decide whether either
  belongs in the repo (currently deliberately local and untracked).
- Bring the M4 Pro back online, if it is to be a route member.

---

## Suggested order

1. **Now, no devices:** A3 → A1/A2 → A5, A6, A4, A9 in parallel → A7, A8, A10.
2. **The moment two Macs are online:** B0 → B1 (**G4** — the headline) → B2 → B3 → B4 (**G4b**).
3. **Three Macs:** B5 (**G8**, cycle search).
4. **A non-Apple device, after A3:** B6 (**G9**) → B7 → B8.

A3 is deliberately first: it is the only Part A item that changes *who can participate* in
Part B.
