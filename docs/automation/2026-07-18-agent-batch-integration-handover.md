# Mycelium completed-agent batch integration handover

Status snapshot: 2026-07-18 13:35:37 CEST

## State

Canonical `main` remains clean at `f2bc55b62c5e3103eda584c1c68c222244bde489`.

Integration worktree: `/Users/evinova-self/Projects/mycelium-wt-agent-batch`

Integration branch: `integration/mycelium-agent-batch-20260718`

Verified code head before this documentation-only handover commit:
`932409a5b9feeddae798e66a8abc15096cdda837`.

No merge to `main`, push, PR, fetch, pull, package install, remote-host action,
credential access, or physical qualification occurred. Old source worktrees were
read-only provenance inputs. The completed Router-to-iroh source worktree remained
clean and was integrated by commit; three newer agent lanes remain isolated from
this branch.

## Integrated work

All selected commits were applied after the verified manual-driver head
`b3e2062e72a9e905698c5d538b4ea569d66bfe8a`.

| Source | Integration | Decision |
|---|---|---|
| `de83c860` | `e9af6cf8` | Stage-local KV decode integrated. |
| `3f258649` | `b01420e1` | Selected RouteQualificationV1 authority integrated; all 13 changed-path blobs match source. |
| `9167d6d9` | `eb0d7494` | Independent tiny GPT-2 reference oracle integrated. |
| `7341629` | `9a9f89ee` | Read-only release doctor integrated. |
| `1b91adf0` | `b7668fd3` | Release-doctor hardening integrated. |
| `915d8bfe` | `6a05f94a` | Bounded release-security audit integrated. |
| `4227c594` | `ff25afba` | Claim-boundary audit integrated. |
| `1c4a865` | `96361326` | Read-only lane-topology audit integrated. |
| uncommitted conformance source tranche | `3803f0cd` | Imported read-only, reproduced RED replay collisions, repaired, and committed with expanded tests. |
| `23c1679` | `a278e114` | Authenticated request/token-stream gateway integrated cleanly. |
| `ca2b59d` | `932409a5` | Authenticated production Router-to-iroh adapter integrated cleanly from its completed, clean source lane. |

The request-gateway source branch contains parent `db8df22`, an alternate
qualification implementation. It was deliberately not integrated: the batch
already contains the selected qualifier from `3f258649`. Only gateway commit
`23c1679` was cherry-picked. Its focused tests and the full suite pass against
the selected qualifier, proving local interface compatibility without replacing
the qualifier-owned readiness authority.

Historical overnight decisions remain unchanged: preserve `e9e6928` and
`cd7d1b1`; skip `ead89f2` because its A1 files are byte-identical to canonical
`main`; integrate `b759305` through `d412777` in order. Exact mapping and
clean-checkout document provenance remain in
`docs/automation/2026-07-18-manual-driver-handover.md`.

All previously completed parallel implementation sessions are now represented
in this integration history. Stable patch IDs match for the nine source commits
through the request gateway. The Router-to-iroh commit was then cherry-picked
directly and preserved its nine-path source diff and authorship.

## Authenticated Router-to-iroh adapter

The integrated adapter keeps the Python Router authoritative while Rust owns the
authenticated bounded carrier. It uses canonical `mycelium.router_wire.v1`
frames, EndpointID plus generation binding, stale-connection fencing,
dispatch-confirmed acknowledgements, replay/sequence rejection, absolute
deadlines, bounded queues, reconnect lifecycle fencing, and explicit shutdown.

Observed local integration uses one Python pytest process and two native sidecar
processes. The five-run stability gate exercised production Router prefill and
decode traversal; the focused suite also covered the exact 16 MiB canonical frame
boundary. This establishes local process-path evidence only. Delivery remains
process-lifetime `remote_router_dispatch_ack`, not crash-durable delivery.

## Router replay repair

The conformance model found that attempt-scoped replay caches used only the
idempotency key. Reusing one key with different payload bytes could replay a
cached success. The repair:

- binds cached prefill, hop, pending-hop, and manifest outcomes to canonical
  payload fingerprints;
- rejects same-key/different-payload conflicts with
  `idempotency_payload_mismatch` before runtime, transport, queue, token, or
  failure side effects;
- snapshots mutable accepted payloads before queued/executed work;
- returns exact cached outcomes in both `complete_context_replay` and
  `stage_local_kv` modes;
- rejects unsupported and recursive payload structures fail closed;
- adds deterministic lifecycle traces and minimized counterexample coverage.

## Observed gates

| Command | Exit | Result |
|---|---:|---|
| `python3.14 -m pytest -q` | 0 | 1002 passed, 2 skipped, 117 subtests passed. |
| integrated iroh focused suite | 0 | 49 passed. |
| local three-process iroh stability | 0 | 2 passed per run across 5 consecutive runs. |
| `python3.14 -m pytest -q tests/conformance` | 0 | 35 passed. |
| request gateway plus contract fixtures | 0 | 57 passed. |
| reference oracle | 0 | 7 passed. |
| qualification | 0 | 33 passed. |
| release doctor | 0 | 6 passed. |
| two-process inference qualification | 0 | 4 passed. |
| two-process runtime qualification | 0 | 4 passed. |
| Router MLX runtime | 0 | 30 passed. |
| `python3.14 scripts/contract_audit.py` | 0 | 14 contracts verified. |
| `python3.14 -m compileall -q .` | 0 | No diagnostics. |
| `git diff --check` | 0 | No whitespace errors. |
| `cargo fmt --check` | 0 | Passed. |
| `cargo clippy --all-targets --all-features -- -D warnings` | 0 | Passed. |
| `cargo test` | 0 | 21 passed, 0 failed. |
| `npm run check` | 0 | 70 Vitest plus 3 Node tests passed; typecheck and build passed. |
| release-security audit | 0 | 353 tracked files accepted. |
| claim-boundary audit | 0 | 138 source files accepted. |

A combined focused pytest invocation first exited 2 because the two test trees
both expose a top-level module named `conftest`. Running each authority in its
own pytest process passed; no test was suppressed or deselected. The first UI
run exited 127 because the fresh worktree lacked `node_modules`. Package and
lock files were byte-identical to canonical. The passing run used a temporary
symlink to canonical's existing dependencies, performed no install, then
removed the symlink. Vite emitted only its existing large-chunk warning.

## Weighted status

This is a planning indicator, not a readiness claim:

`[###############-----] 74% locally built/verified | 26% remaining`

- baseline/integration/contracts: 10/10
- control plane/planner/provision/load/graph: 14/15
- Router/runtime/stage-KV/conformance: 18/20
- native iroh plus Router adapter: 13/15
- qualification plus physical proof: 7/20
- request gateway plus Observatory: 8.5/10
- recovery plus release: 3/10

Physical qualification receives high weight because it is the defining MVP
acceptance gate. Local component completeness must not be reported as accepted
route readiness.

## Remaining gates and claim boundary

`route_ready=false` remains mandatory. Evidence is local only.

Remaining critical path:

1. Reconcile the three current isolated lanes only after each reaches a clean,
   committed, independently reviewed state.
2. With explicit authorization and a written staging/cleanup plan, run real
   authenticated two-Mac stage compute, cold-cache provisioning, at least eight
   decode steps, parity checks, negative runs, and immutable evidence sealing.
3. Only qualifier-owned accepted evidence may produce `route_ready=true`.
4. Feed qualification/request events into Observatory through a read-only
   adapter.
5. Integrate recovery only after the physical base route is stable.
6. Complete fresh-bootstrap/release orchestration and immutable evidence-bundle
   verification.

No accepted physical iroh delivery, two-host inference, two-Mac parity,
physical RouteQualificationV1 seal, real-route request stream, recovery proof,
or fresh-machine release proof exists yet. Observatory remains read-only.
