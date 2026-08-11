# Mycelium M22 Release Closure Specification

**Status:** implementation baseline
**Milestone:** M22
**Parent architecture:** `2026-08-09-mycelium-astra-architecture-product-design.md`

## 1. Outcome

M22 turns the physically qualified M12–M21 system into a reviewable release candidate.
It closes the rich-product audit, packages durable services and reviewer onboarding,
pins source/binary/model identities, proves a useful locally present model larger than
1.5B when measured capacity permits, and exposes the retained result in the existing
eight-workspace UI. The release record is one closed, privacy-reduced contract; a
checklist, fixture, local model file, or running foreground terminal is not proof.

No model or missing artifact may be downloaded without a new explicit operator
approval. Qwen2.5-3B-Instruct is the first local usefulness target. Qwen3-8B remains a
supported adapter/catalog entry but must stay visibly ineligible until a measured
swarm can fit and qualify it.

## 2. Release evidence

`mycelium.m22_release_closure.v1` binds the exact source revision, contract manifest,
SBOM, UI audit, service package, physical deployment, model revision, assignments,
runtime classes, request proof, privacy audit, test matrix, reviewer bundle, and known
exclusions. Unknown, missing, stale, simulated, illustrative, or unexecuted inputs
cannot be promoted to `qualified`.

The public projection contains pseudonymous member IDs and digests only. It excludes
credentials, raw EndpointIDs, private addresses, usernames, private paths, prompt or
response text, token arrays, tensors, activations, and KV contents. Prompt/response
remain available only through the separately bounded inference history authority.

## 3. Durable services and lease renewal

Seed, native membership agent, and live supervisor receive versioned launchd/systemd
descriptors with owner-only configuration, bounded restart, log rotation, health
checks, graceful termination/drain, and explicit upgrade/rollback commands. Service
generation is deterministic and idempotent; installing a node service never creates a
second seed authority.

The platform descriptors invoke one shared, shell-free service runner. Its
owner-only state persists the start timestamps for the configured restart window, so
launchd and systemd enforce the same fail-closed budget even if the platform manager
itself relaunches the wrapper. A managed-restart claim is valid only when
`mycelium.managed_service_restart.v1` binds child replacement, continuous manager
ownership, restored health, coordinator renewals, and post-restart route frames.

Native agents randomize renewal timing and retry transient coordinator/network failure
with capped exponential backoff while the current lease remains valid. Authorization,
revocation, generation mismatch, stale heartbeat, expired lease, and malformed signed
responses fail closed. Retry reuses the same signed heartbeat so ambiguous delivery is
idempotent. Membership availability never changes route eligibility.

The product states are `online`, `temporarily_disconnected`, `lease_at_risk`,
`expired`, `quarantined`, and `revoked`. Nodes, Readiness, and Incidents show the last
signed observation, renewal deadline, generation, reconnect action, and placement
impact without exposing endpoint identity.

## 4. UI and release audit

Every original Observatory/Product UI requirement maps to an implementation/test or a
named exclusion in a machine-readable audit. The eight stable workspaces remain
Inference, Device Lab, Network, Nodes, Plans, Readiness, Incidents, and Settings.
Release Closure is a shared evidence panel, not a ninth workspace.

The audit covers responsive layouts, keyboard flow, reduced/high-contrast modes,
accessible tables, stable replay/navigation/history, graph clustering and large-fleet
performance, pseudonymized export, plan comparison/frontier/pruning/replication/
speculation, node onboarding/detail, readiness history/diff, incident replay, model
attribution, and Device Lab claim boundaries. Fixture/replay/live modes remain visibly
distinct and cannot satisfy one another's gates.

## 5. Reviewer path

The `astras-macbook` bundle works without a source checkout, shared LAN, private SSH,
or Tailscale. One idempotent preflight reports supported OS/architecture, coordinator
and relay reachability, invitation/identity state, resources, runtime compatibility,
assigned artifact bytes, cache reuse, qualification, and actionable failures. Tailscale
may be documented only as optional operator convenience; EndpointID-authenticated Iroh
is the product transport.

The readiness proof may use a clean surrogate Mac. Astra's actual laptop remains
post-build reviewer acceptance and must independently pass qualification before route
placement. Membership alone is never described as inference participation.

## 6. Gates

1. Contract, provenance, Python, Rust, UI, production build, Chromium, Firefox,
   WebKit, accessibility, performance, privacy, security, and claim-boundary gates
   pass from a clean source tree and approved offline caches.
2. A deterministic SBOM/checksum manifest covers Python, Rust, Node, binaries, source,
   model, and tokenizer inputs. A second build reuses the same local model content and
   transfers zero duplicate assignment bytes.
3. At least three physical hosts remain in the serving registry, at least two runtime
   classes qualify, and one instruct model larger than 1.5B completes real distributed
   browser inference. Any unmet dimension is a visible, separately approved exclusion.
4. Continuous agent renewal survives bounded restart and transient loss tests; stale,
   duplicate-generation, expired, revoked, and quarantined states fail closed.
5. All eight workspaces show the release evidence or their relevant projection, retain
   terminal inference across refresh/Back/Forward/reconnect/second session, and pass
   responsive/accessibility/performance checks.
6. A clean surrogate reviewer joins, qualifies, contributes an assigned stage, runs a
   real prompt, and can reconstruct the proof in the UI; the negative unavailable-node
   case blocks or selects a separately qualified alternative with the exact reason.
7. The final Astra handover is at most 250 words, links governing specs, milestone
   commits, evidence, runbook, reviewer entry point, limitations, and the separately
   proposed future decisions for stronger KV, continuous batching, autoscaling, and
   hybrid parallelism.

M22 is not complete while any unapproved release-gate exclusion or critical/important
review finding remains.
