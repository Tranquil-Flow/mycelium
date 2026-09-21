# Mycelium A7 successor-recovery implementation contract

**Status:** `design_only` — scaffold-lane contract refinement; no promotion
**Gate:** A7
**Parent specification:** `2026-08-18-mycelium-a7-fenced-kv-successor-recovery.md`
**Frozen acceptance inventory:** `tests/a7_acceptance/inventory.v1.json`
**Machine source (scaffold):** `mycelium_a7_kv_successor.py`
**Baseline commit:** `b90edaf` (branch `integration/core-pilot`)

This document refines the A7 specification into an implementation contract where the
specification is ambiguous — for exactly the part of A7 that does not depend on A6
integration. It invents no new policy: every decision below is derived from the A7
specification (§ numbers are that document's sections) and the already-frozen
acceptance inventory, and each decision names the specification text it narrows. It
widens no claim boundary: A7 remains `design_only`; nothing here is integrated,
executed, qualified or physically proven; **A6 is untouched and its atomic completion
remains the blocking dependency for the real A7 gate close**.

## 1. Scope

**Covered by this contract and implemented in the scaffold** (pure, in-memory,
deterministic — no I/O, no processes, no threads):

- closed-shape validation for the two pre-integration A7 records,
  `mycelium.private_kv_checkpoint.v1` and `mycelium.private_kv_checkpoint_ack.v1`;
- the route-wide acknowledged-watermark algebra (monotonic, contiguous, idempotent
  repeats, fail-closed rejections) over the inventory's frozen `watermark_contract`;
- the exact compatibility-identity comparison over the inventory's frozen identity
  groups;
- the monotonic fence ledger, fence-install gating, and the single-publication CAS;
- the closed cutover-eligibility predicate that maps every rejection to A6
  `full_context_replay` or an honest `aborted`;
- the closed standby lifecycle transition table;
- the honest claim boundary (`scaffold_claim_boundary()`).

**Deliberately not covered — every seam raises `A7ScaffoldBoundary`** (see §4):

- the four record shapes the specification names but does not freeze
  (`kv_standby_intent`, `kv_successor_runtime`, `kv_successor_qualification`,
  `kv_successor_recovery_gate`);
- private-KV payload sealing/opening, checkpoint transport, durable fence
  persistence, cutover execution, and recovery orchestration;
- process lifecycle, product integration, browser surfaces, physical qualification.

The scaffold introduces no production wiring and imports only the Python standard
library, so it cannot disturb A6, the Router, the gateway, or the UI.

## 2. Contract decisions

Each decision is a narrowing of ambiguous specification text; none is a widening.

**D1 — Route watermark agreement key (spec §5, §10).** The route-wide acknowledged
watermark is the triple `(request_attempt, committed_token_count,
committed_prefix_digest)` shared by *every* replaced stage at its latest accepted
acknowledgement. It is `None` until every replaced stage has at least one accepted
acknowledgement and all accepted values are identical. It advances only upward and
never regresses. A stage-level acceptance that would contradict a formed watermark
(same count with a different prefix digest, or a lower count) fails closed with
`conflicting_stage_agreement`; partial or mixed sets report lag and grant no
cutover authority.

**D2 — Per-stage chain contiguity (spec §5).** "A skipped predecessor" is defined per
stage: accepted checkpoint sequences for a stage start at 0 and advance by exactly
one; each accepted acknowledgement's `predecessor_digest` must equal the previously
accepted `checkpoint_digest`; the committed count must be non-decreasing. An exact
repeat of the latest accepted `(sequence, digest, count, prefix)` is idempotent and
changes no state; the same sequence with any different content is a
`conflicting_repeat` and fails closed. An acknowledgement for an unknown stage, a
stale attempt, or a non-current standby incarnation fails closed.

**D3 — Committed-position semantics (spec §6, §8).** A checkpoint eligible for v1
no-replay continuation must satisfy `kv_position == committed_token_count`. A gap or
an excess makes the checkpoint ineligible for KV continuation; it is rejected at the
record boundary rather than interpreted, because "unknown compatibility data is
incompatible, never assumed" (spec §3).

**D4 — Records are closed and self-bound (spec §8).** The checkpoint and ack records
carry exactly the field sets frozen in §3; every field is validated for type and
bounds; the record carries a self-binding `evidence_digest` computed over canonical
JSON (sorted keys, compact separators) of every other field. Unknown fields, missing
fields, malformed digests, booleans-as-integers, negative or non-finite values, and
digest mismatch all raise `ValueError` with a stable code
(`a7_checkpoint_invalid`, `a7_checkpoint_ack_invalid`).

**D5 — Stored-digest verification (spec §5).** An acknowledgement is valid only when
`stored_checkpoint_digest_verified` is true and the stored digest byte-equals the
presented `checkpoint_digest`; the acknowledgement is otherwise rejected at the
record boundary (verification happens "before atomic promotion").

**D6 — Rejection taxonomy is closed.** The ledger rejects with exactly one of:
`ack_shape_invalid`, `ack_unknown_stage`, `stale_attempt`, `stale_incarnation`,
`conflicting_repeat`, `regressed_position`, `skipped_predecessor`,
`predecessor_mismatch`, `conflicting_stage_agreement`. These raise
`A7ContractViolation` and mutate no ledger state (rejections are atomic: validation
runs against a candidate position before anything is stored).

**D7 — Gateway equality is exact (spec §5, §10).** `fenced_kv_successor` eligibility
requires the frozen gateway committed count *and* prefix digest to equal the route
watermark. A watermark ahead of the gateway is rejected
(`watermark_ahead_of_gateway`); behind the gateway requires the A6 path or an abort
(`watermark_behind_gateway`); equal count with a different digest is a
`prefix_digest_mismatch` rejection. Neither ahead nor behind is treated as equal.

**D8 — Compatibility comparison (spec §3, §10).** Compatibility is exact equality
over the inventory's frozen identity groups, evaluated field by field. A field that
is absent or `None` on either side is *unknown* and therefore incompatible; a
differing field is incompatible; both are reported to callers as bounded field
*names* only — never values — so a compatibility result can cross a projection
boundary without leaking identity material. Identity maps are closed: extra keys
raise `compatibility_identity_invalid`.

**D9 — Fencing monotonicity and single publication (spec §6, §10).** `request_attempt`,
`path_generation` and `fencing_generation` are positive integers that advance
together, monotonically. `accepts_generation(g)` is true only for the current
generation; every other generation (older *or* newer) is rejected. A publication
grant is a compare-and-swap: exactly one grant per fencing generation; a repeated
grant raises `duplicate_publisher`; a grant for any non-current generation raises
`stale_generation`. Advancing the fence voids previous install receipts and the
previous publication grant.

**D10 — Fence installation is all-participant (spec §6).** `fence_installed` is true
only when every installer frozen in the inventory (`request_gateway`, `router`,
`surviving_stage_runtimes`, `transport_ingress`, `standby_runtime`) has confirmed the
current generation; cutover eligibility withholds otherwise.

**D11 — Outcomes are disjoint and honest (spec §7).** The eligibility predicate
returns one of exactly three outcomes: `fenced_kv_successor` when every gate passes;
`full_context_replay` when any gate fails and A6 is available; `aborted` when any
gate fails and A6 is unavailable. Every rejection carries a closed reason
(`route_watermark_unavailable`, `watermark_ahead_of_gateway`,
`watermark_behind_gateway`, `prefix_digest_mismatch`, `compatibility_incompatible`,
`qualification_not_current`, `fence_not_installed`). A KV rejection can never be
labelled as continuity.

**D12 — Standby lifecycle (spec §4).** States and transitions are exactly the
specification's chain `proposed -> acquiring -> loaded -> challenged -> qualified ->
shadowing -> cutover_pending -> active_successor -> draining -> released`, plus
`rejected`/`failed` reachable from any state; `released`, `rejected` and `failed` are
terminal. Artifact presence, load, membership or a received shadow checkpoint does
not make a standby qualified: only `qualified` may become `shadowing`.

**D13 — Claim boundary (spec §1, §10).** The scaffold declares, machine-checkably,
the 12 A7 closure requirements it has not earned (everything in
`docs/handover/mycium-completion-checklist.v1.json`'s A7 row except
`specification`) and reports `gate_state=design_only`, `runtime_executed=false`,
`integrated=false`, `qualification_claim=false`, `promotion_authorized=false`,
`a6_dependency_integrated=false`. No scaffold test can promote A7.

**D14 — Unimplemented seams raise explicitly (spec §10).** Every seam in §4 raises
`A7ScaffoldBoundary`, a subclass of `NotImplementedError`, with a message of the
form `a7_scaffold_boundary:<name>:<reason>`. No seam may return a value, fabricate
state, or silently succeed. Boundary failure is a first-class, tested contract
surface, not an accident to be patched away.

## 3. Record shapes frozen by this contract

### `mycelium.private_kv_checkpoint.v1` (source runtime, private)

| field | type | rule |
| --- | --- | --- |
| `protocol` | text | exactly `mycelium.private_kv_checkpoint.v1` |
| `checkpoint_id` | digest | sha256 identity |
| `request_id`, `session_id` | digest | request/session identity |
| `request_attempt` | int | ≥ 1 |
| `source_placement_id`, `source_membership_incarnation` | digest | source authority |
| `layer_range` | list[int, int] | `0 <= start < end <= 4096` (half-open) |
| `checkpoint_sequence` | int | ≥ 0; per-stage chain position |
| `predecessor_digest` | digest or null | null only for sequence 0 |
| `committed_token_count` | int | ≥ 0 |
| `committed_prefix_digest` | digest | digest of the committed prefix |
| `kv_position` | int | ≥ 0; must equal `committed_token_count` (D3) |
| `byte_size` | int | ≥ 1 |
| `encrypted_payload_reference` | text | 1..256 chars, opaque |
| `expires_at_unix_ms` | int | ≥ 1 |
| `source_authority_generation` | int | ≥ 1 |
| `evidence_digest` | digest | self-binding canonical digest (D4) |

### `mycelium.private_kv_checkpoint_ack.v1` (standby runtime, private)

| field | type | rule |
| --- | --- | --- |
| `protocol` | text | exactly `mycelium.private_kv_checkpoint_ack.v1` |
| `ack_id` | digest | sha256 identity |
| `request_id`, `session_id` | digest | request/session identity |
| `request_attempt` | int | ≥ 1 |
| `stage_id` | digest | must be a replaced stage (D1) |
| `standby_placement_id`, `standby_membership_incarnation` | digest | standby authority |
| `checkpoint_sequence`, `checkpoint_digest`, `predecessor_digest` | int/digest | chain position (D2) |
| `committed_token_count`, `committed_prefix_digest` | int/digest | the acked position (D1) |
| `stored_checkpoint_digest_verified` | bool | must be `true` (D5) |
| `stored_checkpoint_digest` | digest | must equal `checkpoint_digest` (D5) |
| `resource_reservation_generation` | int | ≥ 1 |
| `acknowledgement_authority` | text | exactly `standby_runtime` |
| `ack_authority_generation` | int | ≥ 1 |
| `evidence_digest` | digest | self-binding canonical digest (D4) |

## 4. Boundary registry — what must be implemented before A7 can leave `design_only`

| seam | reason code | unblocked by |
| --- | --- | --- |
| `validate_kv_standby_intent` | `record_shape_not_frozen` | owner freeze of the intent field set |
| `validate_kv_successor_runtime` | `record_shape_not_frozen` | A6-integrated atomic base |
| `validate_kv_successor_qualification` | `record_shape_not_frozen` | qualifier integration |
| `validate_kv_successor_recovery_gate` | `record_shape_not_frozen` | executed gate evidence |
| `seal_checkpoint_payload` / `open_checkpoint_payload` | `private_kv_payload_not_implemented` | byte-exact stage-local KV schema on the runtime |
| `transfer_shadow_checkpoint` | `checkpoint_transport_not_implemented` | dedicated bounded authenticated channel |
| `persist_fence_generation` | `durable_fence_persistence_not_implemented` | durable store before successor decode |
| `perform_fenced_cutover` | `cutover_execution_not_implemented` | A4 failure observation + qualified successor |
| `run_successor_recovery` | `recovery_orchestration_not_implemented` | A6 atomic completion (blocking) |

## 5. What passing these tests does not prove

Passing the scaffold's tests proves exactly that the deterministic contract surface
behaves as this document states, and that every unimplemented seam raises instead of
silently succeeding. It does **not** prove: KV transport; private payload sealing;
durable fencing; any cutover; any successor continuation; any A6 fallback execution;
product integration; the eight workspaces; browser behaviour; performance; exact
cleanup; physical positive/negative gates; or any of the 12 unearned closure
requirements. The scaffold cannot promote A7, and A7's real gate close still depends
on A6 being integrated atomically plus a physically qualified byte-compatible standby.
