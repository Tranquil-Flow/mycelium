# Mycelium A12 Generic Android and iOS/iPadOS Activation Specification

**Status:** `design_only`; dependency-ready mobile qualification boundary
**Gate:** A12
**Parent:** `2026-08-11-mycelium-astra-completion-plan.md`
**Depends on:** A4 scoped lifecycle and A9 membership/capability v2 physically closed.
A8 is transitive through A9. A11 is not a blanket gate prerequisite; it must be
physically closed before an optional mobile draft-worker claim.
**Reviewed design input:** A9 acceptance commit
`0f79a2b1a7f553499579b24c6c9362e801809dab`; its separated authority,
signed-capability, unknown-ineligible, native/synthetic, role-specific qualification,
and dynamic-projection decisions are adopted as design inputs, not execution evidence
**Architecture:** Astra sections 4.5, 4.10, 4.12, 4.14, and 4.15

## 1. Outcome and claim boundary

A12 proves that native mobile devices can participate through the ordinary Mycelium
product path at only the eligibility level each exact platform/runtime/backend has
earned. Android and iOS/iPadOS use the same signed A9 membership, capability,
qualification, artifact, activation, and product contracts as desktop peers. Mobile is
not a separate swarm, coordinator, protocol, planner, or hard-coded device inventory.

Every mobile role is evaluated through four separate authority rungs: signed
membership, capability eligibility, artifact readiness when applicable, and operation
qualification. Probe/evidence contributor, optional speculative draft worker, and
qualified model stage are independently qualified roles; satisfying one rung or role
cannot author another. A platform can close a lower rung while later rungs remain
visibly blocked. No broad “mobile supported” claim is allowed.

A12 does not claim tensor parallelism, cross-backend KV migration, background iOS
inference, unrestricted battery use, arbitrary Android support, or public-store release.
A13 owns normal-user installation, signed distribution, update, and invitation UX.
Termux and an authenticated command bridge remain Android development/conformance tools;
they are not the final Android installation or ordinary external-user path.

## 2. Platform-neutral implementation rule

Production code branches on registered A9 platform, transport, runtime, operation, and
qualification capabilities—not device brand or model. The first Pixel is only one
Android conformance device. General Android activation requires positive evidence from
two independently manufactured device families using the same product contracts. One
passing phone cannot establish a generic Android claim.

iOS and iPadOS share Apple platform foundations where appropriate but retain their exact
platform family, hardware/runtime evidence, lifecycle behavior, and qualification
binding. Passing on iPadOS does not silently qualify iOS, and vice versa; the public
claim names exactly which available test platform and role passed.

Unknown manufacturers and models do not require frontend or protocol changes. A new
device joins with an A9 profile and remains ineligible until the relevant registered
class qualifier accepts fresh native evidence.

## 3. Native mobile member architecture

Each supported mobile application contains or securely invokes:

- a durable device-owned Ed25519 membership identity stored with operating-system
  protected key/storage facilities where available;
- the A8 HTTPS seed-pin/bootstrap client and signed A9 membership v2 client;
- an embedded authenticated Iroh transport bound to the signed EndpointID record;
- a native capability/evidence collector with explicit user consent and bounded fields;
- an assignment-local A2 artifact client with digest verification, resumable acquisition,
  quota, thermal/power pause, and atomic promotion;
- a separately registered native model runtime or probe/draft runtime;
- lifecycle, cancellation, drain, generation fencing, and cleanup supervision; and
- a privacy-reduced product status/event adapter.

The application never requires Python, Termux, a shell, `exec`, ADB, SSH, a shared
filesystem, a raw seed URL, EndpointID knowledge, or manual model copying for an
ordinary external user. Termux, ADB, and SSH are explicitly excluded as normal product
paths. Operator-only development tools must be labelled and cannot satisfy native
installation, lifecycle, artifact, operation, or activation gates.

Mobile enrollment starts with `route_ready=false`. Joining does not alter Router state,
active deployment, selected model, placement, topology, artifact grants, or
qualification.

## 4. Four separate eligibility and readiness rungs

These rungs have different mutation owners, generations, blockers, and expiry. A later
rung consumes prior proofs but never rewrites them. Browser state, device declarations,
brand/model text, cache presence, or success at another rung cannot advance one.

### 4.1 Signed membership

The A9 membership authority requires successful A8 invite/bootstrap, a durable device-
owned key, signed A9 v2 profile, current lease/incarnation/generation, revocation checks,
consent, and reconnect. This rung grants membership only. It grants no capability
eligibility, artifact access, operation qualification, placement, model content, or
inference traffic.

### 4.2 Capability eligibility

The A9 role/class qualifier requires the current signed member; exact registered
platform/runtime/transport/build; fresh source-kind-labelled native observations;
role-specific constraints; and the relevant thermal, power, memory, lifecycle, network,
cancellation, and cleanup evidence. It grants only eligibility for one exact role. It
does not grant an artifact, prove that an artifact is ready, qualify an operation,
create planner placement, or make a deployment selectable.

### 4.3 Artifact readiness

The A2/Provisioner authority independently requires an assignment-scoped grant, exact
artifact/model/representation identity, digest and component integrity, supported inert
format, bounded resumable acquisition, quota/headroom, thermal/power/storage policy,
cancellation, and atomic promotion. Cache presence, eligibility, membership, or manual
copying cannot satisfy this rung. Artifact-free probe roles record this rung as
`not_applicable` with a closed reason; they do not fabricate artifact success.

### 4.4 Operation qualification

The exact role qualifier consumes current applicable rung proofs and physically proves
the registered native operation. Probe/evidence operation qualification requires exact
probe integrity, bounded resources, native source kind, lifecycle cancellation, and
privacy review. Synthetic browser/fixture or Termux conformance output remains separate
and cannot qualify a native operation.

An optional speculative draft-worker operation additionally requires compatible
tokenizer/vocabulary/position semantics, registered native draft runtime, exact
proposal/cancellation/rollback behavior, sustainable thermal/power/lifecycle limits,
current A9 class qualification, and a physically closed A11 target-authoritative
`qualified_enabled` binding for the exact target workload. Draft qualification may
precede full-stage qualification. Draft loss or mobile drain returns the target to
target-only execution without target-KV corruption.

This is a conditional claim prerequisite, not a dependency of generic A12 closure.
When A11 is incomplete, missing, stale, disabled, or bound to another target workload,
the draft-worker claim fails closed while probe/evidence, stage, and generic platform
claims retain their independently earned A12 state.

A qualified model-stage operation additionally requires exact model revision, serving
representation, adaptor, backend/build, assignment-local stage pack, layer range,
tensor/component digests, operation set, context/concurrency, KV schema, sustainable
memory/workspace, native parity, activation transport, lifecycle, thermal/power,
network-loss, cancellation, cleanup, and load proof. Operation qualification grants
only that exact role. Planner placement, Provisioner grants, deployment qualification,
registry activation, and user selection remain separate.

Mobile stage-local KV remains local under the normal path. A suspend, process death, or
backend change invalidates that process/generation's KV. Continuation requires a
separately compatible A7 successor or truthful A6 replay; A12 never infers cross-backend
KV compatibility.

## 5. Shared mobile evidence contract

A12 specializes the A9 contracts with these capability-named records:

1. `mycelium.mobile_runtime_observation.v1` — native-observed runtime, memory,
   thermal, power, lifecycle, network, parity, cancellation, integrity, and cleanup
   results bound to the A9 profile and evidence generation.
2. `mycelium.mobile_role_qualification.v1` — one platform/runtime/build and one exact
   eligibility role, constraints, positive/negative evidence digests, validity, and
   blocker list.
3. `mycelium.mobile_activation_qualification.v1` — owner-private executed physical and
   browser gate, with a privacy-reduced product projection.

Records are closed, canonical, signed where authoritative, size/count/time bounded, and
reject unknown fields, non-finite values, stale generations, mixed device runs,
changed builds, or mismatched model/representation/assignment identities. Every metric
records source kind, observed time, measurement duration/sample count, freshness, and
authority. Missing platform APIs or denied consent produce `unknown` and a blocker, not
zero, healthy, or inferred support.

The privacy-reduced projection excludes membership/invite secrets, raw EndpointIDs,
private addresses, advertising IDs, phone numbers, usernames, account identifiers,
serial numbers, precise location, SSID, private paths, unrestricted logs, prompts,
outputs, token IDs, tensors, activations, and KV content. Device-family diversity is
proved in owner-private evidence and exposed publicly only as pseudonymous family
references and exact supported platform/role counts.

## 6. Android requirements

The production Android path is a signed native application with embedded A8/A9 clients,
Iroh transport, lifecycle supervisor, evidence collector, artifact client, and each
registered runtime. A Termux implementation may validate protocol portability during
development, but cannot satisfy native installation, native lifecycle, background,
thermal/power, or general Android gates.

Before any Android role is physically qualified:

- test two device families from different manufacturers, with distinct relevant
  hardware/runtime profiles;
- bind Android/API level, ABI, application/runtime/transport build digests, and current
  security-relevant capability versions without exposing hardware identifiers;
- prove durable identity across ordinary app restart and generation-fenced identity
  continuity across process death/update;
- exercise foreground, user-requested drain, background transition, suspend/process
  termination, resume/reconnect, network handoff/loss, airplane-mode loss, cancellation,
  revocation, and cleanup;
- measure sustainable rather than instantaneous memory, runtime workspace, thermal
  behavior over a frozen duration, battery/power policy, and low-power withdrawal; and
- for draft or stage roles, prove exact native parity, artifact integrity, load/unload,
  bounded context/concurrency, KV cleanup, and no publication from a stale process.

Android background execution is claimed only to the exact operating-system service mode
physically demonstrated under current policy. If the OS kills or restricts the app, the
member drains or becomes stale and later reconnects; the system does not label ordinary
mobile lifecycle as corruption.

## 7. iOS and iPadOS requirements

The Apple-mobile path is a native signed application. It embeds authenticated transport
and a separately qualified native model runtime; no Python, Termux, shell, or executable
download assumption is permitted. Model artifacts remain inert assignment-local data and
are accepted only in formats the signed application/runtime explicitly supports.

Initial inference eligibility is `foreground_active_only`. On impending background or
suspension the app:

1. stops accepting new work;
2. emits a bounded signed drain/lifecycle transition when the OS permits;
3. cancels or finishes only within its declared grace budget;
4. releases reservations and local request/KV state;
5. fences the old process/incarnation before later publication; and
6. reconnects with a fresh incarnation/generation when foreground-active again.

Suspension, OS termination, thermal pressure, memory pressure, and power-policy
withdrawal are normal mobile drain/availability outcomes when observed as such. They are
not automatically peer-failure, corruption, or deployment-fatal incidents. Unexpected
loss during assigned work still enters A4 scoped liveness and, where applicable, A6/A7
recovery.

For each exact iOS or iPadOS backend/role claimed, re-prove native parity, available and
sustainable memory, OS memory-warning/termination behavior, thermal state, foreground
lifecycle, power state, network handoff/loss, cancellation, artifact integrity,
revocation, stale-generation rejection, and cleanup. Simulator results are
`synthetic_conformance` only and cannot satisfy a physical device or native-performance
gate.

## 8. Admission, drain, and resource policy

Mobile contribution is opt-in and independently configurable by role, charging state,
battery threshold, network policy, maximum storage, context/concurrency, thermal ceiling,
foreground/background policy, and schedule. Owner/swarm policy may be stricter than the
member's preference; neither side can broaden the other's bound.

Admission uses fresh sustainable capacity, not marketing memory or a single free-memory
sample. Serious/critical thermal state, insufficient headroom, low battery, disallowed
power/network state, stale lifecycle, unavailable background guarantee, or revoked
consent withholds new work. Active work receives a bounded drain/cancel outcome under A4;
the app cannot continue publishing after its generation is fenced.

Artifact acquisition uses separate bounded queues and respects serving bandwidth,
thermal, power, storage, and cancellation budgets. An ineligible or unassigned mobile
member cannot receive model chunks. Cache presence never grants placement. Revocation
removes grants, activation admission, and future work without pretending secure erasure
where the operating system cannot prove it; local retention/eviction outcome is reported
truthfully.

## 9. Product and eight-workspace behavior

UI copy names platforms and earned roles, not phone brands, internal milestones, or
Termux implementation details.

- **Inference:** shows when a qualified mobile stage or draft participates, its bounded
  foreground/drain state, and fallback/recovery without exposing device payloads.
- **Device Lab:** provides consent and separately shows signed membership, capability
  eligibility, artifact readiness, and operation qualification. Probe, optional draft,
  and stage remain independent role decisions layered on those rungs. Each view shows
  native versus synthetic evidence, freshness, platform/runtime/build, physical gate
  status, constraints, and blockers.
- **Network:** shows current direct/relay/unknown mobile edges, nullable measurements,
  reconnect/transition, and network-loss state from A8 observations only.
- **Nodes:** shows pseudonymous platform family, architecture, runtime, transport,
  lifecycle, sustainable capacity, thermal/power class, membership, and current role
  eligibility as separate facts.
- **Plans:** shows candidate mobile placement/draft intent, exact constraints and
  exclusions, artifact need, and why mobile capacity was or was not chosen. Intent is not
  assignment or serving.
- **Readiness:** separately shows signed membership, capability eligibility, artifact
  readiness, operation qualification, load/activation, deployment qualification,
  registration, and selection.
- **Incidents:** records thermal/power withdrawal, lifecycle drain, suspension,
  termination, network loss/reconnect, cancellation, revocation, stale generation,
  artifact/runtime failure, and recovery outcome with correct scope.
- **Settings:** exposes opt-in role, charging/battery/network/storage/thermal/lifecycle
  policy, consent, privacy/retention, revoke/remove, and redacted diagnostics.

Refresh, direct navigation, Back/Forward, workspace switching, reconnect, stale/degraded
mobile evidence, terminal drain history, and a clean second browser session reconstruct
the same durable product generation. A second session cannot read onboarding secrets,
device diagnostics, prompts, output, or another tab's private inference history.

## 10. Deterministic and adversarial gates

### 10.1 Frozen acceptance decisions and inventory

The A9 acceptance commit
`0f79a2b1a7f553499579b24c6c9362e801809dab` is a reviewed design input. A12 adopts
its separated capability authority, signed binding, unknown-ineligible, native versus
synthetic, class-specific qualification, and dynamic projection decisions. It does not
reuse the A9 commit as mobile execution, device, qualification, or completion evidence.

| Decision ID | Frozen A12 boundary |
| --- | --- |
| `four_rung_authority` | Signed membership, capability eligibility, artifact readiness, and operation qualification remain separately authored and generation-bound. |
| `generic_platform_architecture` | Registered platform/runtime/transport/operation capabilities drive behavior; Pixel or other brand/model text never does. |
| `two_android_family_proof` | A general Android role requires two independently manufactured families using the same native product contracts and role gates. |
| `apple_foreground_initial` | Initial iOS/iPadOS inference eligibility is foreground-active-only and exact-platform-bound. |
| `sustainable_mobile_gates` | Thermal, power, memory, lifecycle, network, artifact, and parity gates remain independent, fresh, sustainable, and fail closed. |
| `a11_bound_optional_draft` | Mobile draft work is optional and requires the exact physically closed A11 target-authoritative binding; it never grants stage eligibility. |
| `native_product_path_only` | The signed native application is the ordinary product path; Termux, ADB, and SSH remain development/conformance tools only. |
| `dynamic_mobile_projection` | All eight workspaces share one current public generation and show rungs, roles, sources, blockers, constraints, and withdrawals without brand branches or private device data. |

`tests/a12_acceptance/inventory.v1.json` is the closed machine-readable inventory for
these decisions, the four rung definitions, generic Android and Apple-mobile platform
rules, positive/negative cases, A11 draft binding, native-path exclusions, and all-
workspace projections. Its protocol is
`mycelium.a12_mobile_acceptance_inventory.v1`; its claim boundary is frozen design and
future acceptance input only. Passing
`tests/a12_acceptance/test_a12_inventory.py` proves inventory closure, not a native
application, physical device result, eligibility, artifact, operation qualification,
readiness, or completion claim.

Contract tests cover closed schemas, bounds, signatures, profile/build/generation
binding, source kinds, nullability, privacy redaction, stale evidence, unknown runtime,
unsupported operation, mixed platform/device runs, role independence, and exact
constraint propagation.

Native adapter tests cover identity durability, invite/resume/revoke, Iroh authentication,
artifact grant/integrity/promotion, runtime load/unload, parity, context/concurrency,
cancellation, KV cleanup, drain, process restart, network reconnect, and stale-generation
publication. Synthetic harnesses are useful RED/integration evidence but remain labelled
and cannot set a physical qualification result.

Adversarial tests include forged/replayed membership, changed EndpointID/profile/build,
unassigned chunk request, corrupt/partial artifact, incompatible runtime, parity drift,
memory warning/OOM, thermal escalation, battery-policy crossing, background/suspend,
process kill, airplane-mode/network handoff, late result after fencing, duplicate
terminal event, revocation during work, cancellation timeout, cleanup failure, and
device loss during route activity. Each has a frozen bounded outcome and failure scope.

## 11. Physical qualification matrix

The physical positive and physical negative gates below are independent for each exact
platform and role; passing one platform or lower role cannot satisfy another.

### Android positive

On two device families, use the ordinary signed native application path with no SSH or
ADB dependency during the gate. Join off-tailnet, reconnect, publish native capability
evidence, and qualify each device only to the exact selected role. For any draft/stage
claim, acquire only assigned artifacts, pass independent reference parity and load
proof, exercise real Router frames and stage-local KV where applicable, complete a
browser request, and record physical counters. Run the frozen sustainable thermal/power
window and lifecycle/network transitions on both families.

### Android negative

Prove Termux-only and synthetic evidence cannot qualify the native role; one family
cannot produce a general Android claim; and low memory, thermal pressure, battery/power
withdrawal, background restriction, process death, network loss, revocation, corrupt
artifact, incompatible runtime, or stale generation withholds or drains safely without
unassigned data, duplicate output, route-global failure, or incumbent displacement.

### iOS/iPadOS positive

On an available physical iOS or iPadOS device, use the signed native app with embedded
authenticated transport and native runtime. Join off-tailnet with no SSH, prove
foreground-active membership and the selected exact role, execute native parity and
resource gates, and complete the role's ordinary product-path operation. For a stage or
draft claim, require real assigned acquisition, runtime work, browser-visible evidence,
and output authority appropriate to that role.

### iOS/iPadOS negative

Background/suspend the app during bounded work, trigger or observe memory and thermal
withdrawal using a reviewed non-destructive harness, interrupt network access, revoke
membership, and reject a stale process result. The device drains or becomes unavailable,
old generations cannot publish, and reconnect creates a fresh generation. Simulator or
Android evidence cannot satisfy this gate.

If the required second Android family, physical Apple-mobile device, native signing,
runtime, or lifecycle/thermal access is unavailable, the affected claim remains an
explicit external blocker. It is not replaced by a simulator, fixture, Termux result,
operator boolean, or written exclusion.

## 12. Browser, regression, and completion gates

All eight live workspaces are verified for each physically claimed platform/role through
direct navigation, refresh, Back/Forward, reconnect, lifecycle drain, network loss,
thermal/power withdrawal, stale evidence, revocation, terminal incidents, responsive
layouts, keyboard use, reduced motion, privacy redaction, and a clean second session.
No fixed device/model/topology list or vendor-specific conditional is permitted.

The owner-private `mycelium.mobile_activation_qualification.v1` artifact binds source,
contract, native application, transport, runtime and model/representation digests;
pseudonymous physical device/family references; membership/profile/evidence generations;
role qualifications; positive and negative executions; browser request and Router
counters; the A9 reviewed design-input hash and A12 acceptance-inventory digest; all-
eight UI checks; and contract, privacy, governance, security, Python, native-platform
and frontend regression outputs.

A12 closes only after its exact direct prerequisites A4 and A9 are closed; two Android
device families pass the exact general-Android role claimed; an available physical iOS
or iPadOS device passes
its exact named role; native versus synthetic truth remains separate; lifecycle,
thermal/power, memory, network-loss, cancellation, revocation, artifact, parity and
cleanup negatives pass; the UI verifies all relevant live states; and one atomic A12
feature commit exists. Until then A12 remains `design_only` and every mobile role not
physically proven remains visibly ineligible.

Generic A12 closure does not grant `speculative_draft_worker`. That optional claim is
rejected until A11 is physically closed for the exact target workload and all draft-
specific A12 evidence validates.
