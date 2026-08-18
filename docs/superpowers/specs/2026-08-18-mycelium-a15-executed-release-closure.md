# Mycelium A15 Executed-Artifact Release Closure Specification

**Status:** `design_only`; approved acceptance boundary; execution waits for A3–A14
**Gate:** A15
**Parent:** `2026-08-11-mycelium-astra-completion-plan.md`
**Supersedes for final closure:** `2026-08-11-mycelium-m22-release-closure.md`

## 1. Outcome and claim boundary

A15 computes release readiness from verified executed artifacts. It does not ask an
operator, script, or UI to assert that a gate passed. Every required result binds exact
source, contract, model/representation, runtime, environment, authority generation,
execution time, output digest, and verifier policy. Missing, stale, altered, unsigned,
fixture-only, replay-only, manually authored, or source-mismatched evidence withholds the
release decision.

A checklist is useful navigation but is not evidence. A passing unit suite cannot
replace a physical run; a replay cannot replace live evidence; one browser engine cannot
replace the required engine matrix; a local model file cannot replace qualification; and
an owner-approved exclusion cannot silently become a completed capability.

A15 makes no public-security, Byzantine, anonymous-compute, or multi-tenant claim beyond
the approved cooperative private-swarm threat model. Any public or complete-Astra claim
is withheld while a required gate or unapproved critical/important review finding
remains.

## 2. Release input registry

The frozen machine-checked acceptance inventory is
`tests/a15_acceptance/inventory.v1.json`, using protocol
`mycelium.a15_acceptance_inventory.v1`. It defines acceptance inputs and rejection
behavior only. It is not an executed-result graph, release manifest, signature,
governance decision, or assertion that any release is ready.

The inventory freezes these decision authorities:

- `content_addressed_executed_result_graph` derives the decision only from verified
  content-addressed nodes and exact dependency digests;
- `exact_source_clean_tree_binding` requires the exact source commit and a clean tracked
  tree for every clean-build or release result;
- `atomic_gate_commit_graph` requires one distinct atomic commit for every A3–A14 gate
  and validates the corrected exact direct-prerequisite graph;
- `provenance_non_substitution` keeps live, replay, fixture, and historical evidence
  distinct and applies the provenance required by each result kind;
- `complete_digest_binding` requires test, audit, physical, browser, model, contract,
  package, and SBOM digests;
- `bounded_signed_exclusions` permits only narrowly scoped, signed, expiring exclusions
  that cannot suppress mandatory integrity or provenance requirements;
- `external_reviewer_reproduction` requires an independent clean-checkout reproduction
  from public release inputs without operator-private state;
- `automatic_revocation` emits a new non-ready generation when required evidence
  expires or graph, source, digest, policy, or provenance inputs become inconsistent;
  and
- `derived_decision_only` rejects handwritten completion/readiness booleans, affidavits,
  labels, and any unsupported completion claim.

The exact A3–A14 direct prerequisites are A3←A2, A4←A3, A5←A4, A6←A4,
A7←A6, A8←A3, A9←A8, A10←A4, A11←A10, A12←(A4,A9),
A13←A12, and A14←A8. A2 is the sole external prerequisite represented in this
A15 inventory; it is not reclassified as an A3–A14 atomic commit. The A15 closure root
depends directly on every gate A3 through A14. Missing and extra nodes or edges fail
closed, including an extra transitive edge represented as if it were a direct edge.

A11 is a conditional physical prerequisite only for A12's optional
`speculative_draft_worker` claim. It is not a direct dependency of generic A12 closure,
and an A12 gate result cannot substitute for the exact A11 target-workload binding.

`mycelium.executed_gate_result.v1` is the common envelope for an executed result. It
contains:

- gate/capability ID, result kind, live/replay/fixture provenance, pass/fail/blocked
  outcome, execution start/end, tool identity/version, command or harness digest, and
  bounded environment facts;
- source commit/tree, contract manifest, relevant spec, package/SBOM, model revision,
  serving representation, assignment/route/qualification, and policy digests;
- privacy-reduced artifact locations/digests, verifier identity/version, freshness and
  invalidation rules, approved exclusions, and bounded failure reason; and
- dependency result digests required to interpret the outcome.

Every node requires non-empty, validated bindings for the contract, model revision,
serving representation, runtime build, execution environment, and authority generation,
plus an exact subject and privacy-safe artifact reference. The presence of field names
is insufficient: digests must be well formed and consistent, generations must be
positive and current, and the node's canonical content digest must validate.

The release assembler accepts only allowlisted result kinds and verifier policies. Each
kind declares whether it must be clean-tree, live, physical, same-run, browser-engine,
platform-specific, destructive-negative, or independently reviewed. It validates the
artifact itself rather than trusting the envelope outcome.

Results form an acyclic content-addressed graph rooted in one release candidate. Cycles,
duplicate conflicting IDs, missing dependencies, unknown fields, digest mismatch,
non-finite metrics, clock/freshness violations, or an input from another source/model/
generation fail closed. Historical results remain inspectable but cannot satisfy a
current invalidated gate.

## 3. Provenance states and exclusions

Every UI audit item and closure requirement has one explicit state:

- `verified_live` — executed through the current ordinary product path with required
  current physical authority;
- `verified_replay` — deterministic rendering/analysis of a sealed executed artifact;
- `verified_fixture` — bounded test-data behavior only;
- `failed`, `blocked`, `stale`, `excluded_approved`, or `not_applicable`.

These states never satisfy one another. One item may retain multiple results, but the
release policy names the required provenance. `unknown` is never converted to pass.

An exclusion is a separately signed owner decision binding exact requirement, scope,
reason, risk, reviewer, issue/expiry, affected public claims, and compensating behavior.
Exclusions cannot cover source integrity, owner authority, privacy/credential leakage,
qualified-only selection, fail-closed execution, or falsified provenance. No exclusion
is inferred from a skipped test. Expired or changed-scope exclusions withhold release.

## 4. Required executed matrices

The release graph includes current results for:

- clean Python and Rust suites, contract compatibility/canonicalization, governance,
  claim boundary, privacy, security, dependency/license/SBOM, and release provenance;
- frontend unit/integration, production build, Chromium/Firefox/WebKit, responsive,
  keyboard, screen-reader, contrast, reduced-motion, and performance/accessibility;
- cold bootstrap, managed restart, lease renewal, reconnect, corruption, storage/network
  interruption, exact checkpoint resume, and zero-transfer warm reuse;
- dynamic model discovery, feasibility blockers, representation authorization, parity,
  physical qualification, selector negatives, model switching, and useful larger-model
  browser inference using only approved local artifacts;
- concurrency/liveness, multi-stage replication, positive/negative replay recovery,
  fenced KV recovery/fallback, continuous batching/overlap, and speculative promotion or
  measured disabled decision;
- unrelated-network HTTPS membership, direct and forced-relay Iroh activation,
  transition/reuse/reconnect/revocation, platform-neutral capability upgrade, and no
  Tailscale/SSH dependency for the external user path;
- exact advertised Android/iOS/iPadOS eligibility levels, lifecycle/thermal/network
  negatives, signed installation/onboarding, and invite replay/revocation; and
- route explorer privacy/unknown behavior and live direct/relay/recovery visualization.

Every required physical case binds before/after Router frames, per-placement work,
resource cleanup, request/history identity, and relevant UI generation. Performance
comparisons bind identical route, workload, sessions, offered concurrency, token limits,
and measurement windows. Required destructive tests run in isolated windows and cannot
reuse their state as a later positive gate without fresh authority.

## 5. Reproducible release and service artifacts

The source release binds a deterministic checksum/SBOM manifest for Python, Rust, Node,
frontend assets, native binaries, platform packages, service descriptors, source,
contracts, approved local model/tokenizer inputs, and representation manifests. Secret,
cache, evidence, model, and device-specific paths are excluded from source packages.

A second clean build from approved offline inputs must reproduce every reproducible
artifact within its declared policy and reuse exact model content without duplicate
assignment transfer. Any non-reproducible platform signing/notarization output records
its deterministic unsigned payload plus external signing receipt and verifier result.

Seed, membership agent, activation sidecar, live supervisor, and supported native apps
use the reviewed A13 service/package lifecycle: least privilege, owner-only state,
bounded restart, health, log rotation/redaction, graceful drain, upgrade/rollback, and
uninstall. A foreground terminal is not a release service.

## 6. Physical positive independent reviewer path

From a clean supported off-tailnet device with no source checkout, private SSH, or
Tailscale, a reviewer:

1. verifies and installs the signed package;
2. completes A13 private pairing and explicit consent;
3. joins but remains ineligible;
4. observes capability and qualification blockers in the frontend;
5. qualifies only the assigned supported role, acquires only its assignment, and serves
   only after artifact/load/challenge/qualification gates;
6. submits a real browser prompt to a qualified useful model and observes exact route,
   placement work, streaming, terminal history, and release provenance; and
7. reproduces a required negative case where an unavailable/unqualified choice remains
   blocked or a separately qualified alternative is selected with the exact reason.

The reviewer records actionable usability findings. Critical and important findings must
be fixed and the invalidated matrices rerun. Astra's actual laptop may be final reviewer
acceptance but is not pre-enrolled or assumed available by the release artifact.

## 7. UI closure

Release Closure is a shared evidence panel, not a ninth workspace. Each existing
workspace exposes only its relevant executed results in plain product language:

- **Inference:** selected qualified model, request/path/runtime provenance, recovery,
  batching/speculation state, history, and current release binding.
- **Device Lab:** installation, invitation, platform/class qualification, consent, and
  reviewer outcomes.
- **Network:** measured topology/transport/frames and route-explorer provenance.
- **Nodes:** membership/capability/placement/artifact/load/qualification/service state.
- **Plans:** planner inputs/objectives, allocation/replication/recovery/batching/
  speculation decisions, predicted versus measured results, and rejected alternatives.
- **Readiness:** dependency graph of executed results, freshness, invalidations,
  exclusions, and reproducible final decision.
- **Incidents:** retained negative executions, cleanup, rollback, and resolved review
  findings without leaking private evidence.
- **Settings:** source/contracts/packages/SBOM, policy versions, owner approvals,
  exclusions, redacted export, update/rollback, and reviewer entry point.

The product audit maps every planned UI responsibility to implementation, test, live
evidence, replay behavior, fixture behavior, or explicit exclusion. Direct navigation,
refresh, Back/Forward, workspace switching, reconnect, stale/degraded evidence, second
session privacy, responsive layouts, accessibility, and large-history/fleet performance
are executed—not checked by affidavit.

## 8. Physical negative release tests

The assembler and UI are tested with altered artifacts, absent files, stale generations,
wrong source tree, wrong model/representation, wrong route/qualification, unsigned or
unknown verifier, conflicting duplicates, fixture substituted for live, replay
substituted for physical, forged manual pass, expired exclusion, unresolved review
finding, privacy leak, incomplete SBOM, and dirty-tree clean-build claim. Every case
withholds release and identifies the narrow missing or invalid result.

Release revocation is append-only: discovering a compromised key/package, invalid
evidence, critical defect, or expired mandatory result produces a new non-ready
generation. It never edits the former historical decision.

The design-only executable validator in `tests/a15_acceptance/validator.py` evaluates a
canonical synthetic executed-result graph rather than accepting acceptance-case names.
Its positive case validates node and root content addresses, exact result dependencies,
source/tree identity, all required node bindings and digest classes, provenance,
freshness, exclusions, and reviewer reproduction. Its mutation cases change the actual
candidate graph and reject dirty trees, missing or extra result dependencies, replay-
for-live substitution, missing SBOM, unsigned exclusions, expired evidence, inconsistent
artifacts, missing reviewer reproduction, and handwritten readiness fields. These tests
prove the acceptance validator's behavior only; they do not assemble or approve a
release.

## 9. Completion

`mycelium.astra_release_decision.v1` binds the complete validated executed-result graph,
source/package/SBOM identities, exact model and serving representations, physical and
browser evidence, exclusions, reviewer findings, public claim boundary, final decision,
and release generation. Its public projection is privacy-reduced and contains no
credentials, raw identities/addresses, usernames, private paths, prompts, outputs,
tokens, activations, KV, model weights, or invitation material.

A15 completes only when A3–A14 atomic commits and current required executions validate;
all eight live workspaces pass; a clean external reviewer succeeds and reproduces the
negative case; no unapproved required exclusion or critical/important finding remains;
the final handover/runbook links exact evidence and limitations; and one atomic A15
feature commit seals the decision. Until then the release state is not ready.
It remains `design_only` until implementation begins under the A15 primary gate, and
never becomes complete without every executed closure above.
