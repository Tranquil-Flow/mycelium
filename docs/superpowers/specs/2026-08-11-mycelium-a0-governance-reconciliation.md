# Mycelium A0 governance reconciliation specification

**Status:** implementation target
**Governing plan:** `../plans/2026-08-11-mycelium-astra-completion-plan.md`
**Independent review amendment:** local review attachment dated 2026-08-11

## 1. Outcome and claim boundary

A0 establishes one executable account of what Mycelium has actually qualified. It
reconciles the architecture capability matrix with the milestone ledger, restores the
claim-boundary and contract audits, and makes those checks a mandatory aggregate
governance gate. A0 does not promote any runtime, recovery, replication, speculation,
mobile, relay, or release capability.

The accepted baseline is:

- M18: historical contracts and qualification-seam evidence exist; compliant
  multi-stage product-path replication is implemented but not integrated.
- M19: historical contracts and script-driven evidence exist; live scoped recovery is
  implemented but not integrated. Replay may use any newly qualified compatible path
  and does not depend on replicas.
- M20: target-authoritative contracts and a disabled-decision surface exist; real
  speculative execution is design-only until multi-position target verification is
  real and measured.
- M21: durable heterogeneous membership and a physical MLX/NumPy route are real;
  platform-neutral mobile and off-tailnet closure are partial.
- M22: the release bundle is sealed historical evidence, not a current complete-Astra
  or public-release claim.
- M23: heterogeneous stage-local KV is qualified only within its recorded topology,
  model representation, parity, and measurement boundary.

## 2. Opening evidence

At commit `16497f4`, before A0 implementation:

- `mycelium.claim_boundary_audit.v1` reports four unreviewed POST surfaces:
  deployment activation, deployment unload, model-capacity refresh, and
  representation-authorized model preparation.
- the contract audit reports source pin drift for the candidate-promotion,
  live-route-incident, and deployment-activation owners, plus an unregistered
  deployment-residency physical fixture.
- the architecture and scoped-recovery corrections and the governing plan are present
  as deliberate uncommitted work and must be preserved into the A0 gate.

These failures are evidence, not items to hide by refreshing hashes or extending a
path allow-list without review.

## 3. Executable governance ledger

A closed, versioned `mycelium.governance_ledger.v1` document is the machine-readable
authority for:

- Astra capability state;
- milestone state and the capability it claims;
- permitted state ordering;
- boundary protocol and owner-source versions;
- authorized browser product actions; and
- current release exclusions.

The human architecture document remains the review entry point. A deterministic
governance audit parses its M17-M23 milestone table and rejects a state or boundary that
disagrees with the ledger. State ordering is:

`absent < design_only < implemented_unintegrated < partial < qualified`.

A milestone may be narrower than its capability but may never exceed it. Historical
evidence is a source kind, not a state promotion.

## 4. Browser action-authority decision

The UI is an interactive product, not a read-only observatory. Browser mutation is
therefore authorized only through named product-action clients whose exact source path,
HTTP method, endpoint, request protocol, server authority, and user-consent requirement
are recorded in the governance ledger. A mutating method found anywhere else fails the
claim-boundary audit.

The reviewed model actions are:

- activate a prepared deployment;
- unload an idle non-selected candidate-backed deployment;
- refresh swarm capacity evidence; and
- prepare an exact owner-authorized model representation.

The existing inference, cancellation, membership, Device Lab, and swarm action clients
remain authorized only at their recorded endpoints. Representation conversion requires
the separate affirmative decision defined by
`mycelium.model_representation_decision.v1`; general action authority cannot substitute
for that decision.

## 5. Contract-owner review

Source pins are updated only after these ownership conclusions are tested:

- registry changes preserve fail-closed selection and add candidate-backed unload with
  incumbent-selection preservation;
- activation changes add unload state transitions without changing the closed
  `mycelium.deployment_activation.v1` response protocol;
- supervisor changes add closed same-origin product actions and the exact
  representation-decision request without weakening route qualification; and
- the deployment-residency physical record is registered as its own historical
  evidence contract rather than silently tolerated as an extra fixture.

Any wire-shape change requires a new protocol/fixture. A source-only compatible change
may refresh a pin after the owner suites and negative tests pass.

## 6. Mandatory aggregate gate and UI projection

One command runs, in order:

1. ledger consistency and boundary-protocol audit;
2. claim-boundary audit; and
3. contract audit.

CI invokes that command on every change to production source, UI source, contracts,
specs, plans, or the architecture ledger. The command emits a deterministic,
path-private `mycelium.governance_gate.v1` result and returns non-zero if any child gate
fails.

Settings and Readiness show the governance-ledger protocol/version, contract-manifest
protocol/digest, source commit, gate state, and current release exclusions in human
language. The surface must say **not release-ready** while any exclusion is open. It
must not infer a live capability from a spec, fixture, sealed artifact, or green static
audit.

## 7. Verification gate

1. Unit tests reject an unknown ledger field, invalid state, unsupported promotion,
   duplicate milestone, absent governing plan, unpinned boundary protocol, and an
   unlisted UI write surface.
2. Contract-owner suites cover registry selection, activation/unload, representation
   preparation, supervisor request closure, and fixture registry ownership.
3. The aggregate governance command and both child audits exit zero at the A0 commit.
4. Negative copies of the repository with one unlisted POST surface, one unregistered
   boundary protocol, and one promoted milestone each fail deterministically.
5. UI tests and a real browser-path check show versions and exclusions, survive refresh
   and workspace switching, and never show release-ready.
6. A0 lands as one atomic commit containing the governing plan, corrected architecture
   and recovery prose, executable ledger, audits, tests, CI gate, contract-owner pin
   refresh, and Settings/Readiness projection.
