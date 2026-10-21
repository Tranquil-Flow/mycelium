# Mycelium A12 implementation contract

**Status:** `design_only` (scaffold + RED-first suites; no behaviour, no closure
requirement, no state promotion)

**Gate:** A12

**Acceptance base:** `c573e0f4949a0d7541b608b2c3884b209ade0de9`

**Refines:** `2026-08-18-mycelium-a12-generic-mobile-activation.md`
(digest `sha256:bd825c5e3de166be43b730d647a164e692b978be378a23469e96184e7f6c5df8`)

**Frozen acceptance inputs:**
`tests/a12_acceptance/inventory.v1.json`
(digest `sha256:a65834dc0b78fedb949b38342220e453dd6caff6708d1e5c791daac5794c7c16`) and
`tests/a12_acceptance/scenarios.v1.json`
(digest `sha256:66d82d9683fc1132051dbd2d8a6fdb6d356a08d6b09cccb7172b25652b2fc9b2`)

**Machine source:** `mycelium_mobile/a12_activation.py`

**Boundary suite:** `tests/a12_scaffold/test_a12_scaffold_boundaries.py`
**Strong-RED suite:** `tests/a12_scaffold/test_a12_strong_red_absent_behaviour.py`
**Weak-RED suite:** `tests/a12_scaffold/test_a12_unimplemented_execution_red.py`

This document does not change the A12 specification, the acceptance inventory, the
scenario manifest, any contract, any manifest, or the completion checklist. It
resolves the implementation seams the specification left open, names the surfaces an
implementation lane must provide, and states exactly what the scaffold refuses to do.
Nothing here earns a closure requirement: A12 remains `design_only` until two
independently manufactured Android families and one available physical iOS or iPadOS
device pass the exact platform/role gates of specification section 11.

## 1. Claim boundary of this step

- The scaffold implements **no** mobile enrollment, **no** capability eligibility,
  **no** artifact readiness, **no** operation qualification, **no** optional draft
  binding, **no** runtime observation, **no** product projection and **no** physical
  gate.
- Every declared entry point validates its closed input shape, applies the honesty
  guards of section 2, and then raises `A12NotImplementedError` with the fixed
  message `not_implemented:<code>`. It never returns a value and never records
  success.
- The incumbent mobile path is untouched: the Pixel/Termux conformance adapters keep
  their existing behaviour, no production wiring is added, and
  `mycelium_mobile/__init__.py` still exports only the clean-room stage surface.
- No closure requirement of `scripts/mycelium_completion_audit.py` is claimed.
  `EARNED_CLOSURE_REQUIREMENTS` is empty and the boundary suite asserts that it stays
  empty against the audit script's canonical thirteen names.
- `contracts/`, `docs/handover/mycelium-completion-checklist.v1.json` and every
  manifest are untouched by this lane, and the boundary suite asserts that.

## 2. Resolved seams (normative)

Each resolution binds a future implementation lane. The named record, guard, decision
vocabulary or refusal code is the contract; renaming it is a contract change, not an
implementation detail.

1. **The four rungs are separately authored records.** The rung identifiers
   `signed_membership`, `capability_eligibility`, `artifact_readiness` and
   `operation_qualification`, their authorities (`a9_membership_authority`,
   `a9_role_class_qualifier`, `a2_provisioner`, `mobile_role_operation_qualifier`) and
   their single grants are taken verbatim from the frozen inventory. The scaffold
   pins them; a rung record may not be authored by, or on behalf of, another rung's
   authority.
2. **Rung consumption.** When authoring rung `k` the caller supplies the prior-rung
   decisions it consumes. Every supplied decision must name an earlier rung, must use
   the closed vocabulary of resolution 3, and must be current (`eligible` or
   `not_applicable`); a supplied proof that is stale, revoked, withheld or unknown
   blocks the rung instead of being rewritten. Current signed membership is required
   for every rung, and qualifying an operation additionally requires the exact role's
   current capability eligibility. Artifact readiness stays optional in this guard
   because an artifact-free role records it as `not_applicable`.
3. **Closed per-rung decisions.** `eligible`, `ineligible`, `not_applicable`,
   `withheld`, `unknown`, `stale`, `revoked`. Only `eligible` and `not_applicable` are
   current. A missing or denied observation is `unknown` and blocks; it is never zero,
   healthy or inferred support.
4. **Artifact-free roles record a closed not-applicable reason.** A probe/evidence
   role must record `not_applicable` with a non-empty reason code and may never record
   artifact success; a role with an artifact need may never record `not_applicable`.
5. **Source kinds are closed and native-only for rungs.** The vocabulary is
   `native_device_observation`, `synthetic_conformance`, `termux_conformance`,
   `operator_declared`, `unknown`. Only `native_device_observation` may reach a rung.
   Synthetic, Termux and operator-declared evidence keep their own label and are
   rejected with `non_native_source_cannot_qualify`; `unknown` is rejected with
   `source_kind_unknown_blocks_qualification`; anything else is
   `source_kind_invalid`. An operator boolean is never native evidence.
6. **Android two-family arithmetic.** A general Android claim requires at least two
   distinct pseudonymous family references (`family:<hex>` or `sha256:<hex>`).
   Repeats are `android_family_evidence_duplicated` and a single family is
   `android_general_claim_requires_two_families`. A raw manufacturer string, a model
   name, a simulator or a Termux peer is not a family reference.
7. **Apple platform families are isolated.** `ios` and `ipados` are separate families
   with `foreground_active_only` initial inference eligibility. A qualified sibling
   family state blocks reuse with `apple_platform_carryover_forbidden`; simulator
   results remain `synthetic_conformance` and can never satisfy a physical rung.
8. **The ordinary product path is undivided.** The closed path kinds are
   `signed_native_application` and `embedded_iroh_transport`. `termux`, `adb` and
   `ssh` are rejected with `excluded_development_path_cannot_qualify`; anything else
   is `product_path_kind_invalid`. ADB/Termux tooling may validate portability during
   development and may never satisfy an installation, lifecycle, artifact, operation
   or activation gate.
9. **Enrollment starts with `route_ready=false`.** Joining alters no Router state,
   active deployment, model, placement, topology, artifact grant or qualification.
   Anything other than literal `False` is rejected with `route_ready_must_start_false`.
10. **The optional draft claim fails closed.** With an absent, stale, disabled or
    foreign A11 binding the claim returns the closed ineligible decision
    `{claim_id: speculative_draft_worker, decision: ineligible, reason:
    a11_target_authoritative_binding_absent, failure_mode:
    claim_rejected_gate_closure_unchanged, other_roles_unchanged: true}`. Even a
    complete A11 binding cannot be qualified by this gate; that operation refuses with
    `not_implemented:draft_worker_qualification_absent`. Draft state never grants stage
    eligibility and never changes another rung's state.
11. **The observation record is closed.** `mycelium.mobile_runtime_observation.v1`
    carries exactly the fields `protocol`, `platform_family`, `runtime_build_digest`,
    `profile_generation`, `evidence_generation`, `source_kind`, `observed_at_unix_ms`,
    `measurement_duration_ms`, `sample_count`, `authority`, `metrics`. Unknown fields,
    wrong protocol, wrong platform family, non-digest builds, zero generations,
    zero-duration or zero-sample measurements, non-finite metric values and metric
    names outside `[a-z][a-z0-9_]{0,63}` are rejected with coded errors. A metric may
    be the string `unknown`; it may never be a fabricated number. The qualified metric
    vocabulary itself is owned by the implementation lane.
12. **Where the records live.** The three declared protocols
    (`mycelium.mobile_runtime_observation.v1`, `mycelium.mobile_role_qualification.v1`,
    `mycelium.mobile_activation_qualification.v1`) are owned by this gate while it is
    a scaffold. Moving them into a wire contract, registering them in the contract
    manifest, and exporting the scaffold's names from `mycelium_mobile/__init__.py`
    are owner-reviewed steps of the implementation lane; this lane deliberately
    changes no shared surface.

### 2.1 Phone load identity v2 (native-observed load)

Owner decision of 2026-10-11. This subsection is normative for the stage-only
phone (`mobile-rust-qwen2`) and replaces nothing else in this contract.

**Native counter.** The phone keeps one stage-load counter per node object. It
advances by exactly one each time a verified stage runtime is installed as the
applied stage, and at no other time: not on a control-session teardown or `stop`,
not when a route is dropped, not on a membership resume, and not when compute
hands the same runtime back. It is not `stage_revision`, the route generation,
the membership or applied generation, the deployment epoch, a peer or control
generation, a timestamp or a digest. A stage installed while compute still holds
the previous runtime stays applied; the old runtime is not restored over it and
its late output is not published.

**Configure.** `configure` is refused with `load_generation_mismatch` unless the
controller's `load_generation` equals that counter for the applied stage; nothing
is signed for a refused value. `start` is refused with the same code if the
applied load changed after `configure`. The unsigned `stage_load` command returns
`{protocol, stage_load_generation}` (null when no stage is applied) so a
controller can learn the number before its first `configure`. It is a hint and
carries no authority.

**A session lives on one load.** When the phone installs another stage while a
control session is configured or running (the seed delivered another assignment
or pack that still names the same controller), the phone ends that session in
the same step that applies the new stage: what the session admitted is revoked,
so no hop admitted under it executes or publishes on the new load, its request
controls and paths are dropped, and the route peers it installed are cleared by
the ordinary session teardown. The controller must open a new session and
configure it for the load that is applied now. No cleanup-time bound is claimed
for this; `maximum_total_cleanup_ms` stays null.

**Signed descriptor.** The `configured` observation carries the native counter
value and exactly this `load_identity_provenance`:

- `applied_fields`: `assignment_id`, `stage_id`, `phone_stage_pack_digest`,
  `runtime_backend`, `runtime_build_digest`, `golden_output_digest`,
  `load_generation`;
- `controller_claim_fields`: `manifest_digest`;
- `limitation`: `native_full_model_manifest_digest_not_independently_observed`;
- `load_generation_basis`: `native_stage_load_sequence_within_process_incarnation`.

A full `snapshot` signs `load_identity` (`assignment_id`, `stage_id`,
`phone_stage_pack_digest`, `load_generation` of what is applied now, or null) and
`configured_load_generation`. The earlier descriptor, which listed
`load_generation` as a controller claim, is no longer emitted and is never
promoted by any consumer.

**What the counter proves, and what it does not.** Within one process
incarnation (the `process_id` every observation signs) the pair
(incarnation, load generation) names one stage install: a later load of the same
or another assignment has a larger number, and an older signed record is
detectably stale. It does not prove which full-model manifest the stage came
from, it is not comparable across incarnations (a restarted process counts from
one again under a new incarnation), it is not durable, and it says nothing about
a device, an APK or a real model. It is a software fact of the node process.

**Digest.** `mycelium.phone_load_identity.v2` is the SHA-256 of the canonical
document holding the protocol name, `node_id`, `host_id`, `process_id`,
`endpoint_id`, `assignment_id`, `stage_id`, `phone_stage_pack_digest`,
`runtime_backend`, `runtime_build_digest`, `golden_output_digest`,
`load_generation`, `runtime_mode`, `manifest_digest` and the descriptor above.
Run, clock and peer-generation fields are excluded, so the same applied load has
the same identity at preparation and at a later route open. Any other
descriptor fails with `phone_load_provenance_unsupported`. v1 is not redefined.

**External manifest relation.** `manifest_digest` is bound outside the phone, and
every link is checked, never inferred:

1. the seed's current route row for the member is unwithdrawn, unexpired, and
   applied (within the existing 600 second application rule) under the member's
   current membership generation, endpoint and key, and was not re-issued after
   the seed authorization;
2. row assignment id = natively configured assignment id = seed-compiled
   preparation assignment id = graph placement assignment id;
3. the seed-signed native stage manifest equals the row's manifest, and its pack
   digest equals the row's pack digest and the natively applied pack;
4. the compiled assignment's `manifest_digest` equals the claimed one, the
   graph's, and the recomputed digest of the packaged full-model manifest;
5. the native manifest's source Safetensors file (hash and size) is the
   assignment's only weight file and a file of that packaged manifest, and at
   build time the native manifest is rebuilt from those exact bytes. Any other
   representation (sharded or transformed weights) is refused as
   `phone_packaged_source_mapping_unsupported`.

The seed's `route_generation` is recorded in the authorization as its own
peer-routing fact and is never compared with or substituted for the load
generation.

**Where it is enforced.** The builder refuses before emitting a proof, graph
digest or plan. The seed re-validates under its write lock before signing and
before projecting membership offers, whose phone `load_generation` is the
native-observed value. At final qualification and at every renewal the route
re-reads the member and applied-route rows in one transaction, verifies the
packaged proof files against the plan's transfer digests, and hands the
qualifier a validated authority; the qualifier compares it exactly with the
`configured` record signed in that route open and requires the signed snapshot
to name the same load. The authorization's 600 second window is checked at
preparation only; later freshness comes from the current seed rows and the
current native observations.

## 3. Scaffold boundary (what is implemented, and what is refused)

Declared entry points and their fixed refusal codes:

| Entry point | Refusal code | Real behaviour the lane must supply |
| --- | --- | --- |
| `enroll_mobile_member` | `mobile_enrollment_absent` | A9 signed-membership grant with durable device key, consent, current incarnation and `route_ready=false` |
| `qualify_capability_eligibility` | `capability_eligibility_absent` | exact role eligibility from fresh native observations, granting no artifact, operation or placement |
| `promote_assignment_artifact` | `artifact_readiness_absent` | assignment-scoped artifact grant, integrity, bounded resumable acquisition and atomic promotion |
| `qualify_mobile_operation` | `operation_qualification_absent` | physical positive/negative qualification of one exact platform/runtime/build operation |
| `bind_speculative_draft_worker` | `a11_draft_binding_absent` | optional draft binding proven against the exact physically closed A11 decision |
| `observe_mobile_runtime` | `mobile_runtime_observation_absent` | runtime-owned observation record produced by the device runtime |
| `project_mobile_rungs` | `mobile_projection_absent` | privacy-reduced all-eight projection of rungs, roles, source kinds, freshness and blockers |
| `run_mobile_physical_gate` | `mobile_physical_gate_absent` | owner-private executed physical positive and negative gate bundle |

Implemented as real, testable behaviour (and claimed as nothing more):

- closed-shape validation of a runtime observation (resolution 11);
- the source-kind, Android-family, Apple-isolation, product-path and rung-consumption
  guards of resolutions 2-8;
- `route_ready=false` enforcement at enrollment (resolution 9);
- the fail-closed optional-draft policy arithmetic (resolution 10);
- the boundary table itself: `scaffold_status().boundaries` enumerates every entry
  point with its code, and the suite asserts both directions (every declared code is
  raised; nothing is raised that is not declared).

Explicitly **not** implemented, and not implied by any passing test:

- any enrollment, eligibility, artifact, operation, projection or physical execution;
- freshness, expiry, revocation and generation *evaluation* (the scaffold only
  validates that generation and digest fields are well formed);
- thermal, power, memory, lifecycle, network, parity, cancellation and cleanup
  measurement of any kind;
- any privacy projection, redaction or owner-private evidence bundle;
- any product, browser, Router-state, placement or deployment effect.

## 4. RED evidence at `c573e0f`

All three suites, run from the lane worktree:

```
env -u PYTHONPATH python3.14 -m pytest tests/a12_scaffold -q --tb=no
17 failed, 42 passed
```

Every command clears ``PYTHONPATH`` because this host sets it to a Python 3.11
virtualenv whose pytest plugins cannot load under Python 3.14; the flag changes
nothing about the code under test.

Per suite:

| Suite | Command | Result |
| --- | --- | --- |
| Strong RED | `python3.14 -m pytest tests/a12_scaffold/test_a12_strong_red_absent_behaviour.py -q` | `9 failed, 2 passed` |
| Boundary (GREEN) | `python3.14 -m pytest tests/a12_scaffold/test_a12_scaffold_boundaries.py -q` | `39 passed` |
| Weak RED | `python3.14 -m pytest tests/a12_scaffold/test_a12_unimplemented_execution_red.py -q` | `8 failed, 1 passed` |

### 4.1 The strong-RED proof

The strong-RED suite imports no scaffold surface. It was reproduced against an
untouched archive of the base, with only its own two files copied in:

```
git archive c573e0f | tar -x -C /tmp/a12-base
mkdir -p /tmp/a12-base/tests/a12_scaffold
cp tests/a12_scaffold/__init__.py \
   tests/a12_scaffold/test_a12_strong_red_absent_behaviour.py \
   /tmp/a12-base/tests/a12_scaffold/
cd /tmp/a12-base && env -u PYTHONPATH python3.14 -m pytest \
    tests/a12_scaffold/test_a12_strong_red_absent_behaviour.py -q
9 failed, 2 passed
```

The failed test names and counts are **identical** with and without the scaffold
present, so none of the nine failures can be attributed to this lane's own code. One
of the nine is a plain assertion over real production behaviour; the others name
measured absences over the 570 production Python modules of the frozen tree:

| Test | Absent behaviour it names |
| --- | --- |
| `test_strong_red_no_a12_capability_named_records_exist` | none of the three capability-named A12 records appears anywhere; the only mobile evidence record is `mycelium.pixel_stage_evidence.v1` |
| `test_strong_red_mobile_evidence_record_carries_no_source_kind` | that record's exact 21 fields (`protocol`, `route_ready`, `run_id`, ..., `machine`, `python_version`, `duration_ms`) carry no source kind, authority, duration/sample count or sustainability metric, although `mycelium_evidence.SOURCE_KINDS` already exists |
| `test_strong_red_no_mobile_role_or_rung_record_surface_exists` | zero production modules mention a rung, and zero mention `probe_contributor`, `speculative_draft_worker` or `qualified_model_stage` |
| `test_strong_red_mobile_activation_eligibility_is_a_hardcoded_static_allow_list` | `peer_runtime_is_activation_eligible` returns `False` for `android_termux_iroh` and `pixel_http`; the eligible set is hardcoded to the desktop classes |
| `test_strong_red_native_product_path_absent_termux_bridge_is_the_android_surface` | the only Android control surface is a tailnet Termux bridge (100.64.0.0/10:9020) that refuses any ordinary origin; `mycelium_mobile.__all__` has no native surface |
| `test_strong_red_no_native_mobile_qualification_surface_exists` | no native qualification entry point exists; the only Android artifact is the software-only Termux sidecar whose `physical_execution` records are `False` |
| `test_strong_red_no_apple_mobile_surface_exists` | zero production modules mention iOS or iPadOS |
| `test_strong_red_android_family_identity_exists_only_behind_adb` | the only manufacturer/family identity is an ADB probe reading `getprop ro.product.manufacturer`; no pseudonymous family reference exists |
| `test_strong_red_product_projection_has_no_four_rung_ladder` | zero of the product-spine/UI-gateway modules and zero of 239 UI source modules mention a rung; the only mobile handling is a hard-coded `android_termux_iroh` label branch |

The two passing tests are guards, not claims: this suite imports no scaffold surface,
and the frozen acceptance inputs stay `design_only` with their claim boundaries
intact. The suite's module docstring records the single explicit exclusion
(`mycelium_mobile/a12_activation.py`) that keeps the scan honest in the lane worktree.

### 4.2 The weak-RED record

The weak-RED suite's eight failures are raised by this lane's own
`A12NotImplementedError`. That is honest incompleteness, not absent baseline
behaviour, and is recorded as such: each failure message starts with `WEAK RED
(raised by this lane's own scaffold...)` and points at the strong-RED suite and this
document for absence evidence. These tests can only turn GREEN by landing the real
execution path.

## 5. What this step does NOT prove

- No mobile enrollment, eligibility, artifact readiness, operation qualification,
  draft binding, observation, projection, physical gate, browser or product claim.
  Every strong-RED and weak-RED test above is still red at this commit.
- No A12 closure requirement is earned; the gate stays `design_only`, the completion
  checklist is untouched, and `EARNED_CLOSURE_REQUIREMENTS` is empty.
- The scaffold's validation is shape-level only, and its guards are policy
  arithmetic: nothing is executed, scheduled, measured, downloaded, loaded or
  qualified.
- The boundary suite passes only because the refusals exist; a passing guard is never
  evidence that a mobile role works.
- The strong-RED suite's product-projection test reads product source text; it proves
  no rung projection exists, not that a future projection is correct.
- Nothing here was verified on a physical Android device, a physical iOS or iPadOS
  device, a simulator, a browser, or an off-tailnet path.

## 6. Implementation slices (future lanes, each ending in its own commit)

Ordering follows the specification's own dependency shape. Each slice must flip its
named RED tests and re-run the frozen gates; no slice may widen the others' claims.

1. **S1 rungs and enrollment** — signed membership grant, four separately authored
   rung records, violation of the rung-consumption guard, `route_ready=false`, and the
   membership/CAS admission seams. Flips the enrollment, eligibility, artifact and
   operation weak-RED tests. Gate: the rung-isolation matrix and a negative
   stale/revoked/foreign-authority case.
2. **S2 native Android path and family diversity** — signed native application
   contract, native observation record, artifact acquisition/promotion, two-family
   pseudonymous evidence, sustainability and lifecycle negatives. Flips the Android
   strong-RED tests. Gate: specification section 11 Android positive and negative.
3. **S3 Apple-mobile** — signed native iOS/iPadOS application, foreground-only
   initial eligibility, drain/fence/reconnect, memory/thermal/power withdrawal,
   simulator exclusion. Flips the Apple strong-RED test. Gate: specification
   section 11 iOS/iPadOS positive and negative.
4. **S4 projection, physical gate and qualification** — all-eight privacy-reduced
   projection, owner-private `mycelium.mobile_activation_qualification.v1` bundle,
   contract/audit/regression outputs. Only S4 can move the gate out of `design_only`,
   and only through the existing authorities.

A lane that needs a path outside its slice stops for owner review rather than
widening itself, exactly as the A4 implementation packet requires.
