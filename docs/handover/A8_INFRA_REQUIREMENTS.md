# A8 Infrastructure Requirements — Physical and Browser Gates

**Gate:** A8 Internet-native control and activation
**State:** `design_only` — no physical gate below has been executed
**Owner input required:** operator-supplied public origin, certificate, and
off-tailnet peer (spec §10)
**Deterministic status:** contract + deterministic gates implemented and
green (see `tests/a8_acceptance/inventory.v1.json` `deterministic_gates`)

This document is the precise handover for the infrastructure that the A8
physical positive, physical negative, and browser gates require. It is a
deliverable, not a placeholder: every gate below names the exact host,
network, DNS name, certificate, and peer configuration it needs, what it
will prove, and the verification procedure that turns it into executed
evidence. Nothing here claims execution.

Claim boundary: no physical, network, or browser result is asserted
anywhere in this document. Until the gates below run on the specified
infrastructure, A8 remains `design_only` (spec §12).

---

## 0. Global operator inputs (spec §10 preflight)

| # | Input | Requirement |
| - | ----- | ----------- |
| 1 | Public HTTPS origin | One canonical `https://host[:port]` value; a controllable DNS record pointing at it; no userinfo/path/query/fragment; form exactly as validated by `mycelium_internet.bootstrap.canonical_https_origin` |
| 2 | Certificate | Currently valid, publicly trusted; an automated renewal path (ACME or operator-managed); renewal grants NO seed authority (spec §10.1 `seed_key_pinning`) |
| 3 | Firewall/NAT | Inbound TCP 443 (or the chosen port) reachable from the external peer's network |
| 4 | TLS termination | Reverse proxy or dedicated listener terminating TLS for the canonical origin and forwarding ONLY the five closed routes to the loopback seed listener (`mycelium_seed.http.SeedHTTPServer` with `public_seed_url=<canonical origin>` and `policy=PublicBootstrapPolicy(...)`) |
| 5 | Iroh relay/rendezvous | Reviewed relay configuration supporting FORCED-RELAY testing as a physical test control (never a claimed production path label) |
| 6 | External peer | One off-tailnet native peer whose owner accepts the external-tester boundary; Tailscale disabled before any gate; no SSH server/client/key required |
| 7 | Revocation + evidence roots | Operator-private revocation access (offline CLI or loopback administration) and owner-private evidence/output roots |
| 8 | Test window | A window that does not share peers with destructive A3/A4 physical gates |

## 0.1 Public origin topology (chosen route: Cloudflare Tunnel)

The A8 public origin is provisioned through Cloudflare Tunnel: the client's
TLS terminates at Cloudflare's publicly trusted edge, the tunnel forwards
the single canonical hostname to the loopback seed listener, and no inbound
ports are opened. No tailnet participates; the seed's
`PublicBootstrapPolicy` remains the sole path-level authority. This
satisfies inputs 1-4 above; firewall/NAT (input 3) is not needed.

- Template: `release/a8-tls-bootstrap/cloudflared-config.yml.template`
- Provisioner: `scripts/a8_provision_public_origin.sh <hostname> [--dry-run|--check]`
- Runtime credentials: `~/.mycelium/a8-tls` (mode 700, never in repo)
- Operator inputs still required: a Cloudflare-hosted public hostname and
  one interactive `cloudflared tunnel login`.

An alternative nginx/ACME topology is documented in
`release/a8-tls-bootstrap/README.md` for a port-forwardable host with a
domain.

## 0.2 Rehearsal boundary (same-LAN)

Until the external peer sits on a genuinely unrelated network, every gate
procedure may be REHEARSED against the live public origin from a same-LAN
device with Tailscale stopped. Rehearsal output is labelled and can never
be sealed. Spec §12 voids same-LAN Iroh observations and tailnet paths as
gate evidence.

---

## 1. Physical positive gates

### 1.1 `unrelated_https_invite_without_tailscale`

**Infrastructure:** external peer on a genuinely unrelated network (cellular
tether, separate ISP, or a friend's home network — never the operator LAN,
never the tailnet), Tailscale down (`tailscale down`, then verify
`tailscale status` shows stopped and no tailnet interface has an address).
One owner-delivered invite minted by `SeedCoordinator.mint_invite` with the
seed's `seed_url` bound to the public origin (minting stays on the
owner-private/loopback administration plane; spec §4).

**Proves:** public HTTPS bootstrap, pinned seed identity verification, one
single-use invite redemption, membership renewal, and visible ineligibility —
all with zero Tailscale involvement.

**Verification:**
1. `GET /seed/identity` over the canonical origin; verify seed signature
   against the invitation pin (the `PublicBootstrapClient.preflight` path).
2. `POST /seed/join` with the invite token only in the canonical JSON body.
3. `POST /seed/message` heartbeat; confirm lease renewal in the seed DB.
4. Confirm the member projects `route_ready=false` and activation
   ineligible; record the projection.
5. Record: tailnet interface absent during the whole window; DNS resolution
   of the origin via public resolver only; no SSH attempted or required.

### 1.2 `direct_path_qualified_browser_inference`

**Infrastructure:** same external peer; current signed capabilities; exact
assigned artifacts acquired through the signed acquisition grant (spec §7);
the external peer's Iroh sidecar running with a stable EndpointID
(`--endpoint-secret-file`); a live Iroh connection that reports a DIRECT
path (record the sidecar's own connection report — this is the only
authoritative source; never infer direct from same-LAN membership).

**Proves:** one exact external role qualifies; an ordinary browser request
completes over the direct path with positive physical counters.

**Verification:**
1. Feed the bound live connection report into
   `mycelium_internet.activation.ActivationObservations.record`; assert
   `path_class == "direct"`, `path_source == "bound_live_connection"`.
2. Run the ordinary browser inference through the selected qualified route.
3. Assert positive counters (tokens completed, requests admitted) from the
   runtime ledger projection; seal the observation.

### 1.3 `forced_relay_privacy_reduced_browser_inference`

**Infrastructure:** same peer and role; the reviewed transport control
forces relay (e.g. relay-only sidecar mode or network policy on the peer);
browser gateway authority remains current.

**Proves:** another browser inference completes over a forced relay path and
only the privacy-safe relay projection (HMAC reference + reviewed region or
`unknown`) is exposed.

**Verification:**
1. Confirm the sidecar reports a RELAY path (not a configuration flag —
   the live connection report).
2. Complete the browser inference.
3. Inspect the public projection: relay reference is
   `hmac-sha256:<64 hex>` only; region is reviewed or `unknown`; assert via
   `mycelium_internet.privacy.ensure_privacy_clean` with the raw relay URL
   as a forbidden needle.

### 1.4 `observed_path_transition_and_reconnect`

**Infrastructure:** one current qualified path that changes between direct
and relay across bound connection generations (e.g. force relay, then
unforce).

**Proves:** transition generations retained (history never rewritten),
persistent connection reuse within a generation, bounded reconnect, and a
subsequent completed request.

**Verification:**
1. Capture observations at generations N (direct), N+1 (relay), N+2
   (direct); assert `ActivationObservations.history()` retains all three in
   order.
2. Record reuse counters per generation from the sidecar report.
3. Interrupt connectivity; run
   `PublicBootstrapClient.reconnect(max_attempts=..., backoff_seconds=...)`;
   assert same canonical origin used throughout and a subsequent request
   completes.

---

## 2. Physical negative gates

Every negative gate below needs: the external peer on the unrelated
network, Tailscale down, and the public origin reachable. "Expected
outcome" must be observed before the gate is sealed.

| # | Case | What it proves | Verification |
| - | ---- | -------------- | ------------ |
| 2.1 | `cleartext_or_redirect_bootstrap` | Cleartext HTTP, redirect, downgrade, and non-allowlisted routes are refused with bounded errors and no state mutation | Against the live origin: attempt `http://` form of the origin; attempt any redirect; attempt non-allowlisted paths/methods; assert bounded error codes and that the seed DB shows no new member and the invite remains unconsumed |
| 2.2 | `certificate_without_seed_authority` | A TLS-valid connection to a host that cannot produce the pinned seed signature fails closed BEFORE any invite secret is transmitted | Point a test client at a TLS-valid endpoint that serves an unsigned/foreign identity envelope; assert `pin_mismatch`/`seed_signature_invalid` and zero join transmissions |
| 2.3 | `invalid_or_replayed_invitation` | Expired, reused, forged, wrong-swarm, wrong-seed, and changed-retry joins fail closed with no partial member and no single-use corruption | Exercise each variant through the adapter; assert `coordinator.members()` unchanged, invite registry state unchanged |
| 2.4 | `revoked_active_member` | Revocation rejects the next control message and removes activation admission; an already-open Iroh connection cannot outlive revoked authority | Revoke via owner-private administration while the member has an open authenticated Iroh connection; send the next control message (rejected), then attempt further activation traffic (rejected by admission) |
| 2.5 | `endpoint_identity_mismatch` | A dialed EndpointID differing from the signed membership EndpointID is rejected; path stays `unknown`; no activation frame is accepted | Configure a rogue dial to a different EndpointID; assert mismatch rejection and `path_class == "unknown"` |
| 2.6 | `missing_or_stale_path_measurements` | Absent/expired/rejected measurements project `unknown` and block any planning/readiness objective that requires them | Clear or age out observations; assert every metric is `unknown` (never 0) and the required objective stays blocked |
| 2.7 | `raw_relay_identity_injection` | Candidate projections containing relay URL/DNS/IP/port/credentials/exact location are rejected and nothing is emitted | Inject each raw value into a projection; assert `relay_projection_invalid` / privacy-scan violation and no public emission |
| 2.8 | `unqualified_external_member` | An enrolled but unqualified member cannot receive artifacts, placement, activation, selection, or prompt traffic | Attempt each authority for a member lacking current artifact/load/topology/activation/qualification evidence; assert every one rejected, member visible but ineligible |
| 2.9 | `tailscale_unavailable` | The supported path works with Tailscale fully disabled; no tailnet address or evidence appears anywhere | Re-verify gates 1.1-1.4 with Tailscale down; scan every emitted projection for CGNAT `100.64/10` and `*.ts.net` (privacy scan patterns) |
| 2.10 | `ssh_unavailable` | The supported path works with no SSH server, client, key, or remote shell on the external peer; only the signed artifact path is used | Remove/never-install SSH on the external peer; re-run enrollment, artifact acquisition, activation, and serving checks; assert no remote-shell attempt in logs |

---

## 3. Browser gates (spec §11)

**Infrastructure:** the same public origin and qualified external peer, plus
a browser on the operator side (or the peer's owner side) pointed at the
product UI. Tailscale down on the external peer throughout.

- **All eight workspaces via direct navigation:** Inference, Device Lab,
  Network, Nodes, Plans, Readiness, Incidents, Settings — each must render
  the A8 internet-native projection (bootstrap state, pin state, path
  class, nullable metrics, relay reference) without raw identities.
- **Refresh / Back / Forward / reconnect / stale-degraded bootstrap /
  direct-relay transition / terminal incidents / clean second session:**
  the same public generation reconstructs; a second session cannot read
  invitation secrets or another session's private onboarding state.

**Verification:** record per-workspace DOM assertions against the live
projection endpoints, plus the privacy-scan needles (invite token, raw
EndpointID, hostname, relay URL) asserted absent from every rendered frame.

---

## 4. Execution sequence when infra arrives

1. Provision DNS + certificate + reverse proxy; verify
   `PublicBootstrapPolicy` canonical origin and allowlist on the live
   listener (`GET /seed/identity` returns, everything else bounded).
2. Bind the seed: `SeedHTTPServer(..., public_seed_url=<origin>,
   policy=<policy>)` behind the TLS terminator.
3. Mint one owner-delivered invite bound to the public origin.
4. Prepare the external peer: Tailscale down, Iroh sidecar with stable
   EndpointID, no SSH.
5. Run physical positive gates 1.1 -> 1.4, then negative gates 2.1 -> 2.10,
   then the browser gates — in that order, sealing each with the
   `mycelium.internet_native_qualification.v1` owner-private record and its
   privacy-reduced public projection (spec §12).
6. Only then may A8 leave `design_only`.

## 5. Explicit prohibitions (spec §12, restated)

A Tailscale route, same-LAN Iroh observation, HTTPS unit test, written
proxy configuration, or sealed history CANNOT satisfy any physical gate
above. The deterministic suites in `tests/a8_acceptance/` prove the
software boundary; they are not substitutes for the executed physical
gates.
