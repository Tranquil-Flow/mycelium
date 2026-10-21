# wave2-a11-seams — report

**Outcome.** The three seams in `mycelium_speculative_decode_execution.py` that used to
refuse with a coded exception and never return a document now run as deterministic
in-process behaviour over injected dependencies, and the module is reachable from the
router decode path through one narrow seam. Everything is verified against real
command output; no physical, network, browser, qualification or benefit claim is made.

| | |
| --- | --- |
| Base | `0fe60377430e6609715e79b87aeb6706c85cdebe` |
| Implementation commit | `e18a00c5016367066262aabadbdc261442416c74` (tree `24fbf70281af1a9490b96840f91ddec1fb400086`) |
| Evidence commit | the branch tip after `RESULT.json` + this file landed (`git rev-parse HEAD` on `fix/wave2-a11-seams-20260926`) |
| Branch / worktree | `fix/wave2-a11-seams-20260926` @ `…/playground/mycelium-wave2-a11-seams-20260926` |
| Author | `Tranquil-Flow <tranquil_flow@protonmail.com>` — sole author, no trailers, not pushed |

Two commits on purpose: an implementation commit (code + tests + receipts) and an
evidence commit that records its SHA and tree. Nothing else differs between them.

## 1. The three seams

They keep their frozen scaffold names (the public surface the existing suite and the
boundary registry reference) and now run real `run_speculative_cycle` work over the
**injected** transport, the **injected** target decision source and the **injected**
cancellation scope. Each returns a document stamped
`evidence_class="deterministic_in_process"` in which every physical, qualification,
promotion and product-surface flag is false.

| Seam | Before | After |
| --- | --- | --- |
| `run_live_route_benchmark` | raised `live_route_benchmark_not_available`, never returned a document | paired target-only / speculative arms per request, protocol `mycelium.speculative_decode_execution_benchmark.v1`, measured counters (`verification_calls`, `committed_positions`, `proposal_bytes`) and an explicit "not a latency, throughput or benefit measurement" note |
| `execute_physical_qualification` | raised `physical_qualification_not_available` | strict positive arm (every admitted position verified *and* committed) plus an **injected** negative arm that must really fail parity and commit nothing; verdict `deterministic_gates_met` / `gate_not_met`, `blocked_on` the physical/browser boundaries, `hardware_touched=false` |
| `project_live_workspace_state` | raised `live_workspace_projection_not_available` | the frozen eight workspaces, in the order `ui/web/src/app/navigation.ts` publishes (a test reads that file and binds the tuple), projected from one in-process cycle's counts, positions and digests, with a code-level privacy scan that refuses a forbidden key |

Design rules that keep this honest:

* **A seam never invents an injection.** No transport, no target decision source, no
  cancellation scope, no draft ⇒ a closed `SpeculativeDecodeExecutionError` contract
  refusal (code registered in `SEAM_REFUSALS`), *not* a capability boundary. The draft
  is never derived from the target: a seam that invented its own proposal could
  "verify" a proposal no draft ever made.
* **No fabricated document.** A run in which the speculative arm never reached the
  verifier refuses (`route_benchmark_requires_executed_cycles`) instead of publishing
  a table of zeros; a gate whose negative arm does not fail reports `gate_not_met`
  rather than passing; the test suite re-derives every published counter from a direct
  cycle to prove the numbers are measured.
* **The claim boundary is enforced in code.** Each seam takes `evidence_class`; the
  only accepted value is `deterministic_in_process`, and any refused class
  (`physical`, `live_route`, `authenticated_network`, `browser`) raises the matching
  `SpeculativeDecodeExecutionUnavailable` boundary. `EXECUTION_BOUNDARIES` was narrowed
  to exactly those four, so the registry now names what really stays unavailable and no
  implemented seam can re-enter it (guarded by a test that calls each seam bare and
  requires a *contract* refusal, never a boundary).

## 2. The router decode seam

`mycelium_router/decoding.py` — the router's decode contract, imported by the shipped
route/decode scripts and the node decode loops — gains one function,
`resolve_speculative_decode`, with three honest outcomes:

| Input | Outcome |
| --- | --- |
| no bound plan | `dispatched=false`, `authority=target_only`, `reason=no_bound_speculative_plan` — nothing dispatched |
| plan + the three injections (+ `width`, draft) | `dispatched=true`, `authority=a11_authoritative_execution`, `execution_module=mycelium_speculative_decode_execution`, `execution_stage=deterministic_source`, full cycle document |
| plan, but the execution cannot be driven | `dispatched=false`, `authority=wired_helper`, `execution_module=mycelium_speculative_decode` (the existing wired helper supplies the admission decision), `reason=injected_execution_incomplete`, `fallback_detail=<registered refusal code>` |

Both imports are lazy (`importlib` inside the seam), so the decode contract stays
dependency-free for the runtime adapters and no existing import path changes. A
malformed *plan* propagates (`SpeculativeDecodeContractError`) — it is not swallowed as
a decode outage. The integration point stays narrow: a test asserts no other module
under `mycelium_router/` mentions `mycelium_speculative_decode_execution`.

## 3. RED → GREEN

`tests/speculation/test_a11_router_decode_reachability.py::test_the_router_decode_path_dispatches_to_the_a11_execution`

* **RED at base** — in a pristine `0fe6037` extract (`git archive 0fe6037 | tar -x`)
  with only the lane's new test files copied in, the whole reachability file fails:
  `12 failed in 0.32s`, every case with
  `AttributeError: module 'mycelium_router.decoding' has no attribute 'resolve_speculative_decode'`.
  The in-process seam suite does not even collect there
  (`… has no attribute 'LIVE_SEAMS'`) — a weaker, collection-level RED, reported as
  such. Same extract, the 62 pre-existing `tests/speculation` tests still pass
  (`12 failed, 62 passed, 1 error`), so no failure is caused by anything but the new
  behaviour being absent.
* **GREEN at `e18a00c5`** — `dispatched=true`; the record names the execution module and
  its stage; the injected target decision source records **exactly one** bounded A10
  call; the cycle's own commit advances the target watermark by the admitted width and
  releases the verified tokens; the runtime document re-validates against the frozen
  `mycelium.speculative_decode_runtime.v1` contract.

## 4. Verification (real output, `evidence/receipts/`)

| Scope | Result |
| --- | --- |
| `tests/speculation` at base (unmodified pilot tree) | `62 passed in 0.24s` |
| `tests/speculation` at lane HEAD | `110 passed in 0.70s` |
| touched suites (`tests/speculation tests/a11_acceptance tests/a10_acceptance tests/a10_deterministic tests/router tests/a11_scaffold/test_a11_not_implemented_boundaries.py test_router_entry.py test_router_payloads.py test_router_numpy_runtime.py`) | `304 passed, 12 deselected, 18 subtests passed in 3.99s` |
| `tests/a11_scaffold` with `-m scaffold_red` | `4 failed, 92 passed` — the four RED-by-design cases that exercise **`mycelium_speculative_decode`'s own** boundaries; identical set at base, so the lane causes no regression |
| `tests/a6_acceptance tests/claims tests/contracts` | `112 passed in 9.82s` |
| post-commit re-run at `e18a00c5` | `tests/speculation` 110 passed; touched suites 304 passed, 12 deselected, 18 subtests passed; unit marker 110/110 with 0 integration; a11 scaffold `-m scaffold_red` 4 failed, 92 passed |

Runner (every invocation, serial, under the shared flock at
`runs/v2/m1-l2-repair/test-owner.lock`):

```
JAIL_CWD=$PWD env -u PYTHONPATH -u PYTHONHOME PYTHONDONTWRITEBYTECODE=1 \
  /opt/homebrew/bin/python3.14 /tmp/lane-jail/jailed.py \
  /opt/homebrew/bin/python3.14 -B -m pytest -q -p no:cacheprovider <paths>
```

Frozen inputs untouched: `git diff --stat 0fe6037..HEAD -- tests/a11_acceptance
docs/superpowers/specs tests/speculation/test_a11_target_authoritative_execution.py`
is empty. That pre-existing suite needed no edit: its own boundary test still asserts
the physical/live refusal and now covers four claim boundaries instead of three (60
collected items instead of 59), and its `CLAIM_BOUNDARY` assertions still hold.

## 5. Honest boundary — what this does NOT prove

* No physical draft/target route, no hardware: `hardware_touched=false` on every
  emitted document and a boundary code for the claim.
* No authenticated network proposal transfer: the transport is an in-process stand-in
  that reports `authenticated_network_transport=false`; the claim has its own boundary.
* No live-route paired benchmark. The comparison publishes deterministic counters and
  says so; it is not a latency, throughput or benefit measurement, and it has no
  confidence bound, no window count and no promotion authority.
* No browser and no eight-workspace product surface. The projection *names* the frozen
  workspaces and reports derived counters; it renders nothing, `browser_rendered=false`.
* No qualification, no benefit, no promotion, no capability advertisement, and **no A11
  closure requirement is earned**.
* The seam is reachable from the router decode module, and a caller-crafted decode
  reaches the authoritative execution — but this repo's live request path does not yet
  bind an A11 plan, so **a live product request does not exercise the authoritative
  dispatch end to end**. That integration is owner work, and calling the current state
  "live speculative decoding" would be false.

Owner work not done here: A6 source-manifest regeneration at landing (forbidden path);
the A11 implementation contract still describes the scaffold stage for these seams
(frozen doc); the three seams keep their scaffold names.

## 6. Integration-tip note

`integration/core-pilot` advanced from `0fe6037` (the briefed base) to `43bea82` while
this lane ran — a test-only L2 repair (`test_m22_service_runner.py`,
`tests/a13_macos/test_work_http_parent.py`, the mutation-corpus test). The overlap check
`git diff --stat 0fe6037..43bea82 -- mycelium_speculative_decode_execution.py
mycelium_router/decoding.py tests/speculation/` is empty, so nothing needs reconciling
and the lane was not rebased onto a moving tip.
