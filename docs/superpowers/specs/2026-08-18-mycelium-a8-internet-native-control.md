# Mycelium A8 Internet-Native Control and Activation Specification

**Status:** `design_only`; dependency-ready infrastructure decision boundary
**Gate:** A8
**Parent:** `2026-08-11-mycelium-completion-plan.md`
**Direct prerequisite:** A3 atomic completion before integration
**Shared authority:** A1 product authority; coordinate final shared-runtime wiring with
the current A4 integration owner
**Architecture:** Synthesized architecture sections 4.12 and 4.14

## 1. Outcome and claim boundary

A8 removes Tailscale as an operational requirement for joining, renewing membership,
measuring an activation path, and serving a qualified route across unrelated networks.
The membership/control plane is exposed only through a bounded authenticated HTTPS
bootstrap surface. Model activation and inference remain on authenticated Iroh direct or
relay connections bound to exact EndpointIDs.

A8 does not create permissionless membership, anonymous contribution, Byzantine
resistance, multi-tenant isolation, automatic placement after enrollment, or a public
administration API. Invited peers remain cooperative devices controlled by known users.
Membership still grants no artifact, placement, load, qualification, or selection
authority.

No hostname, certificate authority/provider, relay provider, public IP, or deployment
account is selected by this specification. Those are operator inputs to physical
preflight. Missing infrastructure blocks the physical gate; it does not justify an HTTP
fallback, a Tailscale claim, a fixture, or a written success record.

## 2. Frozen plane separation

The product has four distinct planes:

1. **Operator administration:** loopback or owner-private access only. Seed creation,
   invite minting, backup, rotation, revocation, policy edits, and raw audit records never
   cross the public bootstrap listener.
2. **Membership control:** public HTTPS, narrowly exposing seed identity, invite join,
   durable resume, signed heartbeat/member messages, and seed-rotation discovery.
3. **Activation data:** Iroh EndpointID-authenticated persistent connections carrying
   probes, Router frames, and inference traffic over direct or relay paths.
4. **Artifact acquisition:** assignment-scoped signed grants and receipts through the
   reviewed member artifact transport. External members never require SSH.

The HTTPS bootstrap cannot dispatch inference, select a deployment, mint an invite,
revoke a member, return private inventory, serve a model file, or proxy Iroh traffic.
The Iroh plane cannot redeem an invite or change membership authority. The browser UI
projects privacy-reduced state and is not a control-plane credential carrier.

```text
invited device ── HTTPS :443 ──> bounded public bootstrap ──> loopback durable seed
       │
       └──── authenticated Iroh EndpointID ── direct or relay ──> qualified peers

operator ── loopback/owner-private administration ──> invite, revoke, backup, policy
```

## 3. Public HTTPS bootstrap boundary

The durable seed continues to own signer, membership database, invitation consumption,
generation, leases, acknowledgements, and rotation. A reverse proxy or dedicated TLS
listener terminates a publicly trusted certificate and forwards only the closed route
allowlist to a loopback seed listener:

- `GET /seed/identity`;
- `GET /seed/rotation`;
- `POST /seed/join`;
- `POST /seed/resume`;
- `POST /seed/message`.

Every other path and method returns a bounded error. Redirects, cleartext HTTP, protocol
downgrade, query credentials, URL fragments, cookies, form encoding, transfer encoding,
websocket upgrade, directory listing, and static-file serving are rejected. The public
origin is one canonical `https://host[:port]` value with no userinfo, path, query, or
fragment. Clients never follow redirects.

The listener retains the existing one-MiB canonical-JSON frame limit and two-second
connection read bound, adds bounded concurrent-request and per-invite attempt limits,
and returns `Cache-Control: no-store`. Access logs omit bodies, query strings, invitation
identifiers, member IDs, EndpointIDs, signatures, and remote addresses from product
evidence. Operational abuse telemetry, if enabled, remains owner-private and bounded.

TLS supplies confidentiality and ordinary hostname authentication. It does not replace
Mycelium authority: the invitation pins the durable seed verification key, and every seed
response is still signature-verified against that pin. Certificate success without a
valid seed signature fails closed. Seed-key rotation uses the existing dual-signed
transition and overlap rules; TLS certificate rotation grants no seed authority.

## 4. Invitation and initial join

The external native invitation is an owner-private closed document delivered over a
separate authenticated channel. It binds:

- canonical HTTPS seed origin and swarm ID;
- current seed verification-key digest and verification record;
- unique invite nonce and single-use secret;
- issue/expiry and maximum lifetime;
- allowed enrollment class or an explicit unrestricted-within-supported-classes value;
- invitation protocol and canonical digest.

The secret is never placed in a URL, QR query, browser fragment, shell argument, log,
clipboard projection, crash report, or telemetry. A later A13 installer may receive the
same payload through an OS-protected deep link or QR handoff, but A8 physical tests use an
owner-private file or stdin and do not claim normal-user installation UX.

Before transmitting the invite secret, the new device:

1. creates and fsyncs its own durable Ed25519 identity in an owner-private directory;
2. opens the exact HTTPS origin without redirect;
3. fetches `/seed/identity` and verifies swarm ID, seed node ID, URL, lifetime, signature,
   and the invitation's seed-key pin;
4. creates a signed generation-zero join request binding its new key, incarnation,
   EndpointID/address set, software version, peer class, and runtime capability; and
5. sends the secret only in the canonical JSON body of `POST /seed/join`.

The seed atomically consumes the invite and commits the member generation with the
existing replay fence. Exact idempotent retry may return the same acceptance; a changed
request under the same identity, nonce, or message ID fails closed. Expired, reused,
forged, wrong-swarm, wrong-seed, changed-key, changed-endpoint, unsupported-class, and
over-limit joins disclose only bounded reason codes and never create a partial member.

The accepted member begins `route_ready=false`. It cannot change the Router, active
deployment, model selection, topology, artifact grants, or qualification.

## 5. Post-join control and revocation

Resume, heartbeat, capability, probe, assignment result, drain acknowledgement, and
rotation acknowledgement remain canonical signed membership envelopes. HTTPS protects
transport confidentiality, while the member and seed signatures own message authority.
No ambient IP address, TLS session, connection reuse, or cookie grants membership.

Every request binds the current swarm, node, incarnation, member generation, sender and
recipient, issue/expiry, and protocol. A stale lease, revoked generation, prior
incarnation, changed key, replay-conflicting message, or old seed pin is rejected before
state mutation. Revocation is effective on the next control request and is also consumed
by activation admission; an already open Iroh connection cannot outlive revoked
membership authority.

Public bootstrap unavailability makes membership freshness unknown or stale according to
the signed lease. It does not fabricate peer death, rewrite historical evidence, or
silently fall back to a tailnet address. Bounded reconnect uses the same canonical HTTPS
origin and durable device identity.

## 6. Internet-native activation and observations

After membership, the Iroh sidecar publishes its exact EndpointID through signed
membership authority. Activation connections use EndpointID authentication and one
persistent connection per selected peer/path generation. Route selection may observe:

- `direct` when the live Iroh connection reports a direct path;
- `relay` when the live connection reports a relay path; or
- `unknown` when current evidence does not identify the path.

Path class is observation, not configuration inference. Same-LAN membership does not
prove direct, different-network membership does not prove relay, and OS/platform does not
prove either. Persistent-connection reuse, reconnect count, and direct/relay transition
come only from bound activation-plane observations.

RTT, warm RTT, jitter, goodput, loss, sample count, connection generation, and freshness
are nullable measurements. Missing, expired, rejected, or unmeasured values project as
`unknown`; they are never encoded or rendered as zero. A zero loss value is valid only
when a current observation contains samples and explicitly measured zero lost frames.

A relay is projected as a stable privacy-safe reference plus an optional coarse region.
The reference is an HMAC of the canonical relay identity under an owner-private persistent
projection key. Raw relay URLs, IP addresses, ports, credentials, and DNS names remain
private. Region is accepted only from reviewed relay metadata or operator declaration;
otherwise it is `unknown`. Region never becomes location or placement authority.

## 7. Artifact staging and SSH boundary

SSH remains an explicitly labelled operator staging tool for owner-controlled machines.
It is not installed, requested, or assumed for an external reviewer or ordinary invited
member. An off-network member acquires only its assignment-local artifacts through the
signed acquisition grant and reviewed HTTPS/Iroh member agent, verifies them locally,
and returns signed byte and promotion receipts.

Enrollment may truthfully show that artifact transport is unavailable. That member stays
visible and ineligible; the product never falls back to copying a full model, coordinator
relay, shared filesystem, or hidden origin download. A member cannot serve until its exact
artifacts, runtime, topology, activation observations, load proof, challenge, and
qualification all pass.

## 8. Closed product contracts

A8 introduces capability-named contracts rather than milestone-labelled public records:

1. `mycelium.internet_bootstrap_status.v1` — public-origin readiness, TLS state,
   seed-pin verification, route availability, bounded counters, and freshness without
   hostname or address disclosure.
2. `mycelium.internet_activation_observation.v1` — directed EndpointID-pseudonymized
   direct/relay/unknown observation, nullable metrics, connection generation/reuse, and
   evidence lifetime.
3. `mycelium.relay_projection.v1` — HMAC relay reference and optional coarse region.
4. `mycelium.internet_native_qualification.v1` — owner-private executed positive and
   negative gate with a privacy-reduced public projection.

The existing signed membership messages, invite bundle, seed identity, rotation,
receipts, artifact grants, Router frames, and deployment qualification remain their own
authorities. A8 does not opportunistically introduce the platform/capability membership
version owned by A9.

Every new contract is closed, canonically encoded, size/count bounded, and rejects
unknown fields, non-finite numbers, invalid nullability, stale generations, raw network
identity, private paths, credentials, prompts, outputs, tokens, tensors, activations, or
KV content.

## 9. Product and eight-workspace behavior

Product copy uses capability language and contains no internal milestone labels.

- **Inference:** selected deployment and observed path class; no implication that
  Internet membership alone makes inference available.
- **Device Lab:** HTTPS bootstrap reachability, seed-pin verification, invite state,
  membership versus activation eligibility, and external-device preflight.
- **Network:** directed direct/relay/unknown path, nullable measurements, connection
  reuse, transition history, and privacy-safe relay reference/region.
- **Nodes:** current membership incarnation/generation, EndpointID pseudonym, selected
  path class, lease freshness, and qualification state without address or hostname.
- **Plans:** path costs only from current measurements; unknown inputs remain unknown and
  block any objective that requires them.
- **Readiness:** separate HTTPS, seed pin, membership, activation transport, artifact,
  load, topology, qualifier, and selection checks.
- **Incidents:** bootstrap unreachable, pin mismatch, invite rejection, lease stale,
  direct/relay transition, reconnect, revocation, and bounded outcome.
- **Settings:** operator-visible public-origin readiness, certificate freshness,
  bootstrap/relay policy, invite/revocation entry points, and advanced private
  diagnostics. Raw credentials never render.

Refresh, navigation, Back/Forward, reconnect, stale/degraded evidence, terminal history,
and a clean second browser session reconstruct the same public generation. A second
session cannot read invitation secrets or another session's private onboarding state.

## 10. Preflight and implementation ownership

Implementation cannot begin shared wiring until the current integration owner reserves
membership, supervisor, activation, contracts, and cross-workspace source composition.
Before physical qualification the operator must supply:

- one canonical public HTTPS origin and controllable DNS record;
- a currently valid publicly trusted certificate with an automated renewal path;
- firewall/NAT access to the HTTPS origin;
- reviewed Iroh relay/rendezvous configuration capable of forced-relay testing;
- one off-tailnet native peer whose owner accepts the external-tester boundary;
- revocation access and owner-private evidence/output roots; and
- a test window that does not share peers with destructive A3/A4 physical gates.

The seed binds loopback behind the TLS boundary. Administrative operations remain on a
separate loopback/owner-private listener or offline CLI. The reverse proxy configuration,
certificate metadata, and public routes are source-controlled templates with secrets and
provider account identifiers excluded.

### 10.1 Frozen infrastructure decisions

These decisions are provider-neutral but implementation-binding. Changing one requires
an A8 specification revision and acceptance-inventory update before shared wiring.

| Decision ID | Frozen selection | Explicitly rejected substitutes |
| --- | --- | --- |
| `public_https_bootstrap_authentication` | A publicly trusted TLS origin authenticates the canonical host; Mycelium authenticates the seed by its signed identity and invitation-bound key pin. Join uses the single-use secret only in the canonical body. Resume and later control use signed member envelopes. Pre-join mTLS, cookies, bearer sessions, IP allowlists, and TLS alone are not membership authority. | Cleartext HTTP, redirects, private-CA assumptions for external users, query/fragment secrets, ambient network identity, or exposing operator administration publicly. |
| `seed_key_pinning` | The invitation carries the SHA-256 digest of canonical Ed25519 seed verification-key bytes. The client verifies the fetched identity signature and exact digest before transmitting the invite secret. Rotation is accepted only through the existing dual-signed transition, bounded overlap, and monotonic generation. | Trust on first use, certificate-key equivalence, hostname-only trust, silent repinning, or certificate rotation changing seed authority. |
| `invitation_and_revocation` | Invitations are owner-minted through offline or owner-private administration, bounded, class-scoped, expiring, and atomically single-use. Revocation advances signed membership authority, rejects subsequent control, removes activation admission, and closes or quarantines already-open Iroh use for that member generation. | Public invite minting, reusable secrets, partial member creation, lease extension after revocation, or an open data connection surviving revoked authority. |
| `iroh_direct_relay_activation` | Activation dials only an EndpointID authorized by current signed membership. `direct`, `relay`, and transitions are accepted only from the bound live Iroh connection; forced-relay is a physical test control, never a claimed production path label. | Address-only trust, operator-typed EndpointIDs, OS/network inference, configured-path claims, or HTTP/WebSocket inference fallback. |
| `unknown_not_zero_measurements` | Path class and RTT, warm RTT, jitter, goodput, loss, sample count, generation, and freshness remain nullable. Missing, stale, rejected, or unmeasured observations are `unknown`. Numeric zero requires a current measured sample that can truthfully produce zero. | Default-zero coercion, same-LAN/direct inference, different-network/relay inference, or using unknown measurements in a readiness or planning objective that requires them. |
| `privacy_safe_relay_projection` | Public relay identity is `HMAC-SHA256(projection_key, domain_separator || canonical_relay_identity)` using an owner-private persistent key and versioned domain separator. Only the stable reference and reviewed coarse region or `unknown` are public. | Raw relay URL, DNS name, IP, port, credentials, provider account, exact location, reversible hashing, or a per-process key that destroys continuity. |
| `ssh_tailscale_independence` | Public bootstrap and Iroh direct/relay operation use ordinary Internet reachability and require neither Tailscale nor SSH. SSH remains an optional labelled owner-staging tool and cannot satisfy join, activation, measurement, qualification, or serving acceptance. | Tailnet fallback, tailnet-derived path evidence, SSH enrollment or serving, remote shell credentials, hidden file copying, or treating either tool's absence as peer failure. |

### 10.2 Machine-checked acceptance inventory

`tests/a8_acceptance/inventory.v1.json` is the closed machine-readable inventory for
these seven decisions and their required physical negative cases. Its protocol is
`mycelium.a8_acceptance_inventory.v1`; its claim boundary is frozen design and future
acceptance input only. It contains no executed observation, endpoint, provider, account,
credential, evidence, readiness, or completion claim. Repository-local tests validate
its exact decision set, bounded shape, required negative coverage, privacy vocabulary,
specification binding, and `design_only` state.

## 11. Verification matrix

### Contract and deterministic gates

- canonical HTTPS origin, redirect, downgrade, content type, method/path allowlist,
  frame/concurrency/rate bound, timeout, and no-store tests;
- seed identity pin success, wrong pin, rotated overlap, expired rotation, certificate
  success without seed signature, and TLS success without invitation authority;
- invite expiry, replay, changed retry, wrong swarm/seed, duplicate key/EndpointID,
  unsupported class, and atomic no-partial-member behavior;
- resume, heartbeat, lease expiry, stale/revoked generation, changed incarnation, and
  reconnect tests through the public HTTPS adapter;
- direct, relay, unknown, transition, persistent reuse, nullable measurement, explicit
  measured zero loss, and relay-redaction contract tests;
- privacy scans proving no secret, raw EndpointID/address, hostname, relay URL, private
  path, prompt, output, token, tensor, activation, or KV content crosses projections.

### Physical positive gates

1. Disable Tailscale on the external peer before joining. From a genuinely unrelated
   network, verify HTTPS and pinned seed identity, redeem one owner-delivered invite,
   renew membership, and remain visibly ineligible.
2. Collect fresh signed capabilities and a direct Iroh observation, qualify the member
   for its exact role, acquire only assigned artifacts, and complete a browser inference
   with positive physical counters over the direct path.
3. Force a relay path using the reviewed transport control, complete another browser
   inference, and expose only the privacy-safe relay reference/region.
4. Observe one direct-to-relay or relay-to-direct transition, persistent connection
   reuse within a path generation, bounded reconnect, and a subsequent completed request.

### Physical negative gates

- With Tailscale disabled, cleartext/bootstrap fallback remains impossible.
- Unauthorized, expired, reused, forged, wrong-seed, and stale/revoked identities fail
  closed without a partial member or route mutation.
- Bootstrap interruption expires membership truthfully; it does not invent zero metrics
  or a peer-failure source.
- Missing path measurements render unknown and cannot satisfy planning or readiness.
- Raw relay/address values injected into a projection are rejected.
- An enrolled but unqualified external member cannot receive artifacts, placement,
  activation, selection, or prompt traffic.
- SSH absence on the external peer does not affect the supported onboarding or serving
  path.

### Browser and regression gates

All eight live workspaces are verified through direct navigation, refresh, Back/Forward,
reconnect, stale/degraded bootstrap, direct/relay transition, terminal incidents, and a
clean second session. Accessibility, privacy, contracts, governance, release security,
frontend, full Python, transport, cold-bootstrap, restart, and revocation suites pass
after evidence shape stabilizes.

## 12. Completion

The owner-private `mycelium.internet_native_qualification.v1` artifact binds source and
specification digests; public HTTPS/certificate facts; seed identity and invitation
digests; membership generations; direct, forced-relay, transition, reuse, and reconnect
observations; artifact/load/qualification identity; browser request counters; negative
results; UI checks; and regression/audit outputs.

A8 is complete only when unrelated-network join, renewal, measurement, qualification,
assigned acquisition, direct and forced-relay serving, transition, reconnect, revocation,
all-eight-workspace verification, and one atomic A8 feature commit are executed. Until
then it remains `design_only`; a Tailscale route, same-LAN Iroh observation, HTTPS unit
test, written proxy configuration, or sealed history cannot satisfy the gate.
