<!-- SPDX-License-Identifier: AGPL-3.0-only -->
# L7 planner component

A bounded dense-Qwen placement/compiler component, not a running service or a
release-qualified authority implementation. The A13 registry intentionally refuses
unregistered model/revision pairs, including MoE, just as v0.1.4 does. Qwen3.5/MoE
backend support elsewhere does not authorize an A13 resource plan. Replicated
graph compilation, signed load-proof production and live normal/fast serving
remain open.

## Real shared boundaries

- `discovery::MembershipVerifier` uses L5 strict ingress, canonical bytes and real
  Ed25519 membership verification. It pins swarm, recipient, node, endpoint, key,
  generation and incarnation, and requires the selected backend/precision. Wire
  expiry is floored to local milliseconds, never rounded up. Membership reports
  describe hardware, **not consent**: caps supplied to this low-level adapter must
  be trusted current policy. `CapabilityVerifier` remains an injectable boundary
  for other authenticated report families; its test doubles are not signatures.
- `control_adapter::prepare` checks admitted identities against the real L6 core,
  intersects report constraints with current durable consent/local-owner policy,
  runs actual placement, and passes exact shares into L6 `plan`/`start`. L6 checks
  authority and commits reservations before returning start effects. There is no
  second durable reservation database. The temporary in-memory planner book is
  only a proposal evaluator; the core must be exclusively owned during the call.
- Owner local proposal remains distinct from standing consent. Old membership,
  missing/withdrawn consent, short phases and inadequate RAM/storage/staging disk
  refuse before external effects. A core with existing workers refuses another
  cohort; reopening retains cleanup-unproven reservations. Cross-core contention
  is fenced by L6's persistent revision CAS, not a planner-local mutex.
- `Prepared::compile` is a single-attempt **cooperative** compiler boundary. Its
  deadline is fixed at preparation, capped by the declared compile phase and L1
  compile budget, and checked before and after the callback. Failure returns the
  primary error and the distinct result of requesting durable L6 stop. It never
  releases capacity. Current owner, worker ticket, membership and phase are checked
  before callback entry. No compiler output is published on late completion.
  The driver must compile before dispatching returned worker-start effects, enforce
  the same deadline on its isolated compiler process, and dispatch returned stop
  effects. This synchronous callback does not preempt a hung process or establish
  status-lock responsiveness. Cleanup remains `node::Supervisor` observation →
  `Core::cleaned`; a cancellation/error string is not cleanup proof.

- `control_adapter::continue_rollover(core, input, members, reports, now, phases,
  &restart)` is the timed continuation counterpart. After L6 `tick` and proven
  cleanup, pass one complete authenticated remote report set (no owner report)
  and fresh admitted capabilities, including local-owner policy. L6 performs the
  report/plan/start in one persisted transition. Missing/changed/duplicate reports
  consume the pending continuation with `rollover_members_changed`; a later call
  returns `rollover_not_pending`. Capability and phase validation run inside the
  planner invocation, after L6's terminal membership gate and before reservation.
  The returned `Prepared` has the same compile/stage-before-start contract. This
  wrapper neither collects reports nor retries, spawns, or proves cleanup.

- `Prepared::check_rollover_members` / `Prepared::replan` are additive to the
  S16 `prepare` / `Prepared` / `RestartBudget` interface. The gate compares node,
  generation, incarnation, endpoint and verification-key identity as a set, so
  ordering alone is harmless. Placement still uses request order. A changed set
  returns `rollover_members_changed` without touching L6. An unchanged set is
  freshly prepared against current consent, capacity and time; active or
  cleanup-unproven workers still block it. There is no retry/rollover driver.

The standalone `ReservationBook` is still available for exclusive in-memory
admission and aggregate footprint checks. It retains dropped tickets and refuses
stale cleanup. It is not durable crash recovery and must not be mistaken for the
L6-composed reservation owner.

## Policy and identities

Pure `plan` accepts trusted policy inputs, not arbitrary HTTP policy JSON. L1
control archive, staged-copy and slice-header allowances are enforced; the latter
uses the exact slice-acquisition registry source referenced by `model_resources`.
Membership TTL/skew use their dedicated L1 IDs, not same-valued lease budgets.
The registry-read test verifies actual accessor calls. Remaining ratios, resource
factors and defensive bounds need L1 declarations; see `registry-seams.md`. No
unreviewed local operational defaults are supplied. Phase/restart inputs are
explicit bounds, not measurements or a claim of calibrated p99 admission.

`plan` binds supplied model geometry and measured floors to the pinned registry;
a caller cannot retain the name/revision while reducing its costs. The additional
wrapper refusal is `model_registry_mismatch`; unsupported pairs retain the oracle's
`model_resources_unregistered`. The lower-level synthetic splitter remains generic.
`member_resources` derives complete partition/whole-model rows, including the
oracle's absent-memory semantics, without inventing a live admission observation.
`profile::read_header` / `from_headers` account bounded dense-Qwen safetensors
headers. Header parsing does not verify tensor payloads or qualify a model load.

Assignment UUIDv5 and graph/load-proof digests use L5 canonical serialization.
Compiled graph metadata must also pass L5 structural validation. Legacy `ACTIVE`
is metadata, never authenticated load/consent/route readiness. `LegacyGraphCosts`
remain explicit compatibility placeholders. Normal/fast operator precedence and
owner provisioning are oracle-tested; neither implies actual route execution.
Graph compilation is replayed against a 1,762-case oracle corpus (`tests/graph_parity.rs`,
`tests/fixtures/graph-corpus.json`): identical metadata wherever v0.1.4 builds a graph from
proofs its validator accepts (`bfloat16` activations, load generation 0 and dtype-string
stage mixing follow v0.1.4); the inputs Rust refuses although the oracle's builder alone
lets them through (hand-built assignment sets only) are an explicit table in that test.
L8 descriptor binding preserves the artifact/numerics identities and still
requires backend admission. The pinned L8 implementation refuses full MoE stages.

## Evidence and integration

Rust tests consume recorded JSON; external Python only records pinned oracle
fixtures. The continuation adds real L5 signatures → real L7 placement → real
L6 SQLite reservations/deadlines/reopen/cleanup, plus controlled compiler/worker
faults. Environment doubles do not establish native model cleanup. A loopback
HTTP component host now composes the real API Desktop/auth/status, real L7/L6
admission and actual private artifact publication: inadequate current consent or
physical disk refuses before staging; status completes while the real cooperative
compile callback holds the host's Core mutex; compiler and staging-retirement
failures remain public and retain reservations. This host is test-only wiring,
uses the saved 2 GB oracle case and inert recorded cache bytes, and does not prove
SLICE-1's production host, fresh-default GUI, native compilation or model loading.
The timed continuation tests use L6 tick/cleanup/continuation with fresh L5-signed
capabilities and prove successor planning plus terminal member-loss behavior.
Full application F5/F14/F15/F16, normal/fast serving and offline startup remain
integration gates.

The lane changes only planner/artifacts-owned paths. Preview 2 is merged and all
round-4 gates run directly in this worktree using its shared, unchanged offline
lock and retained private target. No source mirror or private lock is used.
Recorded calls from the original A13 split/resource tests now supplement the
older generated vectors; this is **not full legacy general-planner test coverage**.
No new dependency or download was acquired. Exact source pins, commands, counts,
RED/GREEN/mutation evidence and remaining obligations live in the external L7
lane report.
