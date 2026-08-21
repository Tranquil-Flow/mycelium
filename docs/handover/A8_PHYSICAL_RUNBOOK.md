# A8 Physical Runbook — Operator Procedure

Status: **design_only**. This runbook is the operator-executable procedure
for the A8 physical gate matrix. Nothing in it has been executed; no gate
has been claimed; no qualification record below exists. Every case is
not executed until the two operator inputs exist: (1) a canonical public
HTTPS origin (Cloudflare Tunnel or nginx/ACME per
`A8_INFRA_REQUIREMENTS.md` §0.1), and (2) an external peer on a
genuinely unrelated network with Tailscale stopped.

Sealing is performed exclusively by `scripts/a8_run_physical_gate.py`
with `--seal` into an owner-private evidence root; the runner emits
`mycelium.internet_native_qualification.v1` records and refuses to pass
a case whose boundary cannot first be verified live (pin over the
canonical origin).

Hosts: SEED_HOST (operator side, runs the loopback seed + tunnel) and
PEER_HOST (the external peer, unrelated network, Tailscale down).
All commands are run as the owner; invite bundles and node roots live in
owner-private directories.

## unrelated_https_invite_without_tailscale

Physical positive. Proves: HTTPS bootstrap, pinned seed verification,
single-use invite redemption, membership renewal, visible ineligibility —
zero Tailscale involvement.

1. SEED_HOST: stand up the canonical origin (tunnel or proxy template)
   and the rehearsal seed bound to it; mint one owner-delivered invite.
2. PEER_HOST: `tailscale down`; verify with `tailscale status` (Stopped)
   and `ip addr` (no tailnet interface address).
3. PEER_HOST: `a8_run_rehearsal_peer.py --origin <canonical> --bundle-file <bundle>`.
4. Confirm the member projects `route_ready=false` and activation
   ineligible in the privacy-clean bootstrap status output.
5. Capture: the peer's full labeled output; `tailscale status`; the
   status document (bounded, privacy-clean).
6. Seal when a qualification record is warranted (physical-era
   qualification path; the rehearsal script itself never seals):
   `a8_run_physical_gate.py run unrelated_https_invite_without_tailscale --origin <canonical> --bundle-file <bundle> --node-root <peer-node-root> --evidence-root <root> --seal`.

Not executed. Requires the unrelated-network peer.

## direct_path_qualified_browser_inference

Physical positive. Proves: a direct Iroh path observation with the
browser inference completing over it.

1. Both hosts: enroll per the previous case; bring up the sidecars.
2. PEER_HOST: run the browser inference workload through the wired UI
   against the canonical origin while observing the Iroh direct path.
3. Capture: observation document with `path_class=direct`,
   `path_source=bound_live_connection`, current freshness, and a
   measured sample set (no defaults; unknown-not-zero holds).
4. Seal: `a8_run_physical_gate.py run direct_path_qualified_browser_inference ... --seal`.

Not executed. Requires the unrelated network AND the wired spine UI
(coordination per `a8-spine-wiring-plan.md`).

## forced_relay_privacy_reduced_browser_inference

Physical positive. Proves: a forced-relay observation completes the
browser inference with only the HMAC relay reference public.

1. Both hosts: enroll; configure the reviewed relay as a FORCED test
   control (never a claimed production path label).
2. PEER_HOST: run the browser inference; observe `path_class=relay`.
3. Capture: observation with relay redacted; the projected reference is
   `hmac-sha256:` + 64 hex only, plus a reviewed coarse region or
   `unknown`.
4. Seal: `a8_run_physical_gate.py run forced_relay_privacy_reduced_browser_inference ... --seal`.

Not executed. Requires the unrelated network AND the wired spine UI.

## observed_path_transition_and_reconnect

Physical positive. Proves: a real path transition with prior
observations retained and a bounded same-origin reconnect completing.

1. Both hosts: enroll; record a first observation.
2. PEER_HOST: change the transport condition (relay on/off); observe the
   transition; drop the connection and reconnect to the SAME canonical
   origin (never an alternate).
3. Capture: transition history (both observations retained, generations
   ordered); reconnect completed against the same origin.
4. Seal: `a8_run_physical_gate.py run observed_path_transition_and_reconnect ... --seal`.

Not executed. Requires the unrelated-network peer.

## cleartext_or_redirect_bootstrap

Physical negative. Proves: no bootstrap data is obtainable over
cleartext HTTP, a redirect, or a non-allowlisted route.

1. SEED_HOST: origin live; mint invite.
2. Any host (the runner does the observation itself with redirects
   disabled and a live-boundary preflight first):
   `a8_run_physical_gate.py run cleartext_or_redirect_bootstrap --origin <canonical> --bundle-file <bundle> --evidence-root <root> --seal`.
3. Capture: the probe outcome (`cleartext_refused`,
   `cleartext_redirect_observed`, or `cleartext_no_identity`), bounded
   public errors for the non-allowlisted paths, and zero invite-secret
   transmissions.

Not executed (no canonical origin yet; the runner is fail-closed
otherwise).

## certificate_without_seed_authority

Physical negative. Proves: a certificate-valid origin whose seed is not
signed under the pinned key is refused before any invite transmission.

1. SEED_HOST: serve a second origin/endpoint whose TLS cert is valid
   but whose seed identity is signed by a key NOT in the invitation.
2. Run: `a8_run_physical_gate.py run certificate_without_seed_authority --origin <canonical> --bundle-file <bundle> --evidence-root <root> --seal`.
3. Capture: `seed_pin_mismatch_before_invite_transmission`,
   `join_not_attempted`, zero transmissions.

Not executed.

## invalid_or_replayed_invitation

Physical negative. Proves: an exact retry stays idempotent (no second
member) and a changed retry under the same invite is rejected.

1. SEED_HOST: origin live; mint invite.
2. Run: `a8_run_physical_gate.py run invalid_or_replayed_invitation --origin <canonical> --bundle-file <bundle> --node-root <owner-private node root> --evidence-root <root> --seal`.
3. Capture: `exact_retry_idempotent` and `changed_retry_rejected`
   outcomes; the seed database shows exactly one member.

Not executed.

## revoked_active_member

Physical negative. Proves: revocation removes control and activation
admission for an active external member.

1. Both hosts: enroll the external member fully.
2. SEED_HOST: revoke via the owner-private administration plane
   (`advance_member_generation` path); never touch the seed DB directly.
3. PEER_HOST: attempt heartbeat and activation; both are refused with
   bounded codes.
4. Capture: the bounded rejection codes; the member's generation in the
   seed record; no serving occurred after revocation.
5. Seal: `a8_run_physical_gate.py run revoked_active_member ... --seal`
   (peer-required; fails closed without the external peer).

Not executed. Requires the external peer.

## endpoint_identity_mismatch

Physical negative. Proves: a member claiming a different EndpointID than
its accepted identity is refused.

1. PEER_HOST: after enrollment, present control envelopes under a
   different endpoint identity.
2. Capture: bounded rejection; member record unchanged.
3. Seal: `a8_run_physical_gate.py run endpoint_identity_mismatch ... --seal`.

Not executed. Requires the external peer.

## missing_or_stale_path_measurements

Physical negative. Proves: missing or stale measurements project
`unknown`, never zero, and block objectives that require costs.

1. Any host (no network needed):
   `a8_run_physical_gate.py run missing_or_stale_path_measurements --origin <canonical> --evidence-root <root> --seal`.
2. Capture: `path_class_remains_unknown`,
   `missing_metrics_remain_unknown`, `required_objective_blocked`.

Not executed (the runner procedure is deterministic and sealed records
for this case live in the operator evidence root when run).

## raw_relay_identity_injection

Physical negative. Proves: a raw relay URL/DNS/IP never crosses any
projection.

1. Any host (no network needed):
   `a8_run_physical_gate.py run raw_relay_identity_injection --origin <canonical> --evidence-root <root> --seal`.
2. Capture: `raw_relay_identity_rejected`,
   `no_public_projection_emitted`.

Not executed.

## unqualified_external_member

Physical negative. Proves: membership alone never grants serving
eligibility; an unqualified route refuses the workload.

1. PEER_HOST: enroll (membership only, no route qualification).
2. Attempt an inference request: refused with a bounded reason; no
   inference served.
3. Capture: the bounded refusal reason; the ineligible projection;
   zero inference completions.
4. Seal: `a8_run_physical_gate.py run unqualified_external_member ... --seal`.

Not executed. Requires the external peer and the wired UI.

## tailscale_unavailable

Physical negative. Proves: bootstrap, join, and measurement complete
with no tailnet path present anywhere in the window.

1. PEER_HOST: `tailscale down`; confirm no tailnet interface or route.
2. Enroll and measure over the public origin only.
3. Capture: interface absence during the whole window; the origin
   resolved via public DNS only.
4. Seal: `a8_run_physical_gate.py run tailscale_unavailable ... --seal`.

Not executed. Requires the external peer.

## ssh_unavailable

Physical negative. Proves: no SSH client/server/key is required at any
point of bootstrap, join, or measurement.

1. PEER_HOST: confirm no SSH client is used (SSH may exist only as the
   labelled owner-staging tool on SEED_HOST, never as gate evidence).
2. Enroll and measure; record that no ssh invocation occurred in the
   gate window.
3. Capture: the full enrollment window with zero ssh invocations; no
   ssh-dependent step anywhere in the procedure.
4. Seal: `a8_run_physical_gate.py run ssh_unavailable ... --seal`.

Not executed. Requires the external peer.
