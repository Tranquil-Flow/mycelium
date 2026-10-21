# A8 Infrastructure Requirements — Physical and Browser Gates

**Gate:** A8 Internet-native control and activation
**Current state:** `complete`; all 14 exact-candidate physical/browser cases
are freshly sealed and all local closure gates are green.
**Fleet boundary:** A5 continues to own the fleet under run ID
`a5-v18-window-diag-retry-1787471702`. A8 used a bounded operator-authorized
side-by-side window on 2026-08-26 and did not stop or mutate A5's sessions.

## Current exact candidate — physically qualified and complete

- source manifest: `sha256:29a5a0dc9e2faf4e1dc53738ed283b2a0f6cd7a1644185d3393b03ca40adff97`
  (1,083 files; base commit `1aa70c64b14754b08abe55e8095c597d91b6d13c`);
- specification: `sha256:694e653c37eed8c31b21e73eb4a423aea9f66b6b00a6c4e9c2031470b13396d9`;
- contract manifest: `sha256:0f7727d3efbaa48d84d110c500df5a867a7ed9a61c09dcdfbba17cf8d3acc268`
  with 75 fixtures verified;
- rebuilt local macOS ARM64 sidecar:
  `sha256:de03e3da16faa20ec6331e9aa0d32a3728c7c9f127595b1bb60ce3956d5cca50`;
- staged Linux x86-64 sidecar:
  `sha256:a73bca6c6748ee27c9f31c20811f0c1680b299c17a81adeffef00038ebf8f38c`.

The final deterministic regression passed: 4,661 Python tests, 13 skipped,
and 121 subtests; frontend 99 Vitest files / 581 tests plus eight Node
subtests, typecheck, bundle check, and build; Rust format, 35 tests, Clippy
with warnings denied, and release build; changed-file Ruff lint and repository
Python compilation; and the focused 67-test contract/governance/claim-boundary/
release-security slice. The final post-evidence audit reruns are recorded in
`docs/handover/a8-completion-record.v1.json`.

Fresh staging and runner-sealed qualification output are retained in an
owner-private root outside the repository. Exact path/size/hash parity and
isolated startup checks passed on both selected architectures. All 14 selected
source/spec-bound seals are owner-only read-only files (mode 0400), have result
`passed`, and were independently enumerated before the privacy-reduced summary
was generated.

The physical positives include ordinary-browser direct inference, forced-relay
inference with only a privacy-safe relay reference, and an observed direct to
relay transition across connection generations 2 and 3. Both browser requests
completed, all eight workspaces rendered, the old connection was not reused,
and a subsequent request completed. The active-member negative used a real
enrolled member, successful pre-revocation qualified relay inference, owner
revocation, managed-session removal, and durable post-revocation refusal.

A8's product, proxy, tunnel, rehearsal-seed, browser, and controller resources
were stopped after sealing. No A8 process remains locally or on its staged
external peer. A5's concurrent sessions were neither stopped nor mutated.
Raw invitations, node identities, endpoint IDs, hostnames, addresses, keys,
signed browser reports, and transport reports remain outside Git.

## Historical superseded qualification — candidate `28c69e…` only

Earlier owner-private seals for
`sha256:28c69ef896866e6d24cf7acc6dc79728e8e8ed38d1add4eb2e7d7acabf91f19f`
remain superseded forensic history and were not relabelled. The repository
summary and completion record now describe only the freshly executed
`29a5a0dc…` candidate. Historical execution does not assert indefinite route
availability.

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

## 0.1 Public origin topology (executed route: owner-controlled HTTPS bridge)

Final qualification used the canonical public origin through an owner-controlled
nginx HTTPS edge. Publicly trusted TLS terminated at that edge; only the closed
seed/product routes were forwarded through a bounded reverse bridge to the
loopback listeners. No tailnet route participated and no inbound port was
opened on the operator workstation. The seed's `PublicBootstrapPolicy` remained
the path-level authority, while the public edge supplied transport reachability
only. Raw host, tunnel, and infrastructure addresses remain owner-private.

The repository also retains two reproducible alternatives for future windows:

- Cloudflare Tunnel template:
  `release/a8-tls-bootstrap/cloudflared-config.yml.template`
- Cloudflare provisioner:
  `scripts/a8_provision_public_origin.sh <hostname> [--dry-run|--check]`
- nginx/ACME topology: `release/a8-tls-bootstrap/README.md`
- Runtime credentials: `<OWNER_PRIVATE_RUNTIME_CREDENTIAL_ROOT>` (mode 700,
  never in repo)

Neither alternative grants seed authority; every deployment must still satisfy
the pin-first and closed-route policy above.

## 0.2 Rehearsal boundary (same-LAN)

Same-LAN rehearsal remained non-sealable throughout. Final evidence instead
used public HTTPS and observed Iroh direct/relay transport between distinct
physical hosts. The two no-Tailscale cases were rerun during a measured local
Tailscale-down window; Tailscale was restored only after both passed. Earlier
failed records showing `tailnet_path_present` remain retained and excluded
from the passing selection.

## 0.3 Executable runner (physical-era entry point)

Every physical case executes through
`scripts/a8_run_physical_gate.py`; the case registry is machine-checked
against this inventory (`tests/a8_acceptance/test_physical_gate_runner.py`).
The three seed-side negative cases execute once the origin and an
owner-delivered invite exist:

    scripts/a8_run_physical_gate.py run cleartext_or_redirect_bootstrap \
        --origin https://<canonical-origin> \
        --bundle-file <owner-private invite bundle> \
        --evidence-root <owner-private root> [--seal]

    scripts/a8_run_physical_gate.py run certificate_without_seed_authority \
        --origin ... --bundle-file ... --evidence-root ... [--seal]

    scripts/a8_run_physical_gate.py run invalid_or_replayed_invitation \
        --origin ... --bundle-file ... --node-root <owner-private node root> \
        --evidence-root ... [--seal]

The two projection-side negatives run without any adapter input.

All nine peer-required cases have execution paths. The six non-browser cases
use the live public seed adapter; the three browser cases consume an ordinary
browser report synchronized to independently signed transport reports. They
fail closed with `peer_required` until their physical inputs exist. Invoke the
non-browser cases with `--node-root <owner-private root>`;
`revoked_active_member` additionally needs `--revoke-command`, the owner's own
administration command, which the runner executes between enrollment and the
refusal check rather than minting any revocation authority of its own. Replay,
member-visibility, revocation, Tailscale-independence, and SSH-independence
claims additionally require `--case-probe-program` plus a new
`--case-probe-output-file` beneath an owner-private `0700` root. The executable
queries the live seed/runtime at the relevant event; the runner validates its
closed schema, binds its canonical digest, and retains the exact report at mode
`0600`. Revocation is two-phase: the executable receives
`<case-id> <member-id> before` prior to owner revocation, then
`<case-id> <member-id> after` with the exact canonical before report on stdin.
The two reports share a probe-session identifier and candidate bindings, and
the after report names the before-report digest. A static assertion document
is not an execution probe. The peer's before/after tailnet exposure and SSH
process audit are also observed by the runner on the host it runs on.

`endpoint_identity_mismatch` is the exception: it accepts only the repository's
exact `scripts/a8_endpoint_mismatch_probe.py`, not an arbitrary executable.
The run requires `--sidecar-binary`, an owner-only
`--receiver-endpoint-secret-file`, and `--transport-authority-file`. The
source-owned executor launches a real expected/receiver/rogue Iroh triad and
emits signed candidate-bound admission counters. Runner and executor bind the
exact native binary SHA-256, and the gate requires rejection counters to rise
while admitted frames remain unchanged and path class remains `unknown`.

`unqualified_external_member` consumes an owner-controlled executable passed
through `--authority-probe-program`; the runner mints no serving authority.
The three browser cases consume `--browser-report-file` and one or more
`--transport-report-file` values. Forced relay additionally consumes the
owner-private persistent relay-projection key. Transition reports are ordered
chronologically and generation-fenced.

Every executed case requires a live boundary first: the pin is verified over
the canonical origin before any observation is recorded, so an absent boundary
can never produce a pass.

The rehearsal seed's stdin-only owner console supplied live revocation and
unqualified-member probes without adding public administration routes. Every
final result was sealed only by `scripts/a8_run_physical_gate.py`.

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
1. Capture observations at generation N and N+1 with different observed path
   classes; assert `ActivationObservations.history()` retains both in order
   and the second generation is strictly greater.
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
| 2.5 | `endpoint_identity_mismatch` | A dialed EndpointID differing from the signed membership EndpointID is rejected; path stays `unknown`; no activation frame is accepted | Run the exact source-owned native triad executor; verify trusted node signatures, exact sidecar-binary digest, increasing candidate/global identity-rejection counters, unchanged admitted-frame counter, and `path_class == "unknown"` |
| 2.6 | `missing_or_stale_path_measurements` | Absent/expired/rejected measurements project `unknown` and block any planning/readiness objective that requires them | Clear or age out observations; assert every metric is `unknown` (never 0) and the required objective stays blocked |
| 2.7 | `raw_relay_identity_injection` | Candidate projections containing relay URL/DNS/IP/port/credentials/exact location are rejected and nothing is emitted | Inject each raw value into a projection; assert `relay_projection_invalid` / privacy-scan violation and no public emission |
| 2.8 | `unqualified_external_member` | An enrolled but unqualified member cannot receive artifacts, placement, activation, selection, or prompt traffic | Attempt each authority for a member lacking current artifact/load/topology/activation/qualification evidence; assert every one rejected, member visible but ineligible |
| 2.9 | `tailscale_unavailable` | The supported path works with Tailscale fully disabled; no tailnet address or evidence appears anywhere | Re-verify gates 1.1-1.4 with Tailscale down; scan every emitted projection for CGNAT `100.64/10` and `*.ts.net` (privacy scan patterns) |
| 2.10 | `ssh_unavailable` | The supported path requires no SSH; optional labelled owner staging cannot carry join, activation, measurement, qualification, or serving acceptance traffic | Observe the bounded window and assert no SSH invocation or remote-shell traffic while bootstrap and signed-path operation complete; the executed case reported `ssh_present_but_unused` and `no_ssh_invocation_in_window` |

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

## 4. Executed qualification sequence

1. Froze the 51-file candidate in
   `docs/handover/a8-source-manifest.v1.json` and deployed exact macOS arm64
   (`sha256:c6e72e3c0e33d72a912576cbeca52508bbf194f11456bb3d95b3bdc87556e9b4`)
   and Linux x86_64
   (`sha256:c77dd9e69ff2d2400867e34786ec55394f5df7e44a34c102cde32d376bc9a35b`)
   native sidecars.
2. Verified canonical public HTTPS bootstrap and pin-first enrollment.
3. Drove the ordinary Chromium product UI through all eight workspaces,
   back/forward/reload reconstruction, a clean second browser session, and
   completed inference.
4. Captured forced-relay generation 66, advanced durable membership authority,
   then captured direct generation 67 in the same browser process with the
   same endpoint pseudonym and fresh signed transport evidence.
5. Executed all seed, peer, process, privacy, and authority negative cases;
   used a separate publicly trusted foreign-key seed for the certificate-only
   authority refusal.
6. Sealed one passing source/spec-bound qualification per case. The aggregate
   audit found 14/14 cases and resolved every evidence digest to an
   owner-private artifact.

## 5. Explicit prohibitions (spec §12, restated)

A Tailscale route, same-LAN Iroh observation, HTTPS unit test, written
proxy configuration, or sealed history CANNOT satisfy any physical gate
above. The deterministic suites in `tests/a8_acceptance/` prove the
software boundary; they are not substitutes for the executed physical
gates.
