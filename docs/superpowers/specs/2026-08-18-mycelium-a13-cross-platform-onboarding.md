# Mycelium A13 Cross-Platform Installation and Invitation Specification

**Status:** `design_only`; approved dependency-ready onboarding acceptance boundary;
implementation waits for A8, A9, and A12
**Gate:** A13
**Parent:** `2026-08-11-mycelium-completion-plan.md`
**Depends on:** A8 Internet-native bootstrap; A9 capability membership; A12 qualified
platform activation levels

## 1. Outcome and claim boundary

A13 lets a normal invited user install Mycelium, create a device-owned identity, join a
private swarm across unrelated networks, and understand every step without filesystem,
seed URL, EndpointID, sidecar, Tailscale, SSH, Termux, ADB, source checkout, repository,
or CLI knowledge. Membership remains invitation-gated and cooperative; installation
never implies inference eligibility.

The product remains dynamic: authorized owners can invite additional supported devices,
observe current evidence, and use newly qualified capacity without editing source,
fixtures, model lists, peer lists, or topology. A13 does not grant anonymous membership,
remote administration, automatic model placement, artifact access before assignment, or
background execution beyond the host platform's reviewed lifecycle.

Package metadata, UI mockups, a development build, a pre-enrolled image, Termux, a source
checkout, or an operator-run SSH command cannot satisfy this gate. Each platform claim is
limited to the exact signed package, service lifecycle, and eligibility level physically
tested through the normal onboarding path.

## 2. Distribution and platform boundary

Each supported target has a reproducible, versioned, signed installation artifact:

- notarized/signed macOS package and managed launch agent or daemon as appropriate;
- signed Linux packages for the declared distributions plus a hardened systemd user or
  system service;
- signed Windows installer plus a least-privilege Windows service;
- signed Android application using the A12 native capability path;
- TestFlight or signed native iOS/iPadOS application using the A12 lifecycle; and
- a clearly limited browser member that can probe only the capabilities it actually has.

The package manifest binds source revision, build inputs, platform/architecture, package
digest, signer/notarization identity, minimum OS, service descriptor, update channel,
rollback floor, capability protocol, privacy notice, and expiry/revocation policy. A
package never contains a swarm invite, seed private key, owner credential, model, fixed
peer, fixed topology, or hidden download URL.

Unsupported platform/architecture, invalid signature, expired/notarized-invalid package,
downgrade below the rollback floor, changed publisher identity, and missing lifecycle
support fail before identity or membership mutation.

## 3. Target-owned identity and private invitation handoff

The target creates and durably stores its Ed25519 identity before the owner mints the
device-bound invitation. Private keys use platform-protected storage when available and
remain non-exportable to the browser UI, logs, diagnostics, QR payloads, or coordinator.

The normal two-device pairing flow avoids putting a plaintext invite secret in a URL,
query, fragment, QR code, shell argument, clipboard projection, or log:

1. The new device displays a short-lived pairing request containing its ephemeral
   encryption key, permanent identity-key digest, supported invitation protocol, and a
   random pairing nonce. It contains no swarm credential.
2. The owner scans/imports that request in Settings, reviews platform/class, consent
   scope, expiry, quota, and seed identity, then explicitly authorizes one invite.
3. The owner authority mints the A8 single-use invitation and encrypts the complete
   bundle to the target's ephemeral key. The return QR/deep-link/file contains only the
   bounded authenticated ciphertext and public routing metadata.
4. The target decrypts locally, verifies seed-key pin and bundle bindings, consumes the
   secret only in the canonical HTTPS join body, erases plaintext handoff state, and
   persists the accepted member generation.

Deep links use an exact registered application scheme/universal link and a closed
payload; no web redirect, third-party tracker, pasteboard, cookie, or analytics SDK may
receive invitation material. QR frames are bounded, versioned, checksummed, and expire
with the pairing request. A manual recovery file is owner-private, mode-restricted where
the platform permits, and deleted only after durable acceptance.

Re-scanning an exact accepted result may show the existing outcome. Changed ciphertext,
identity, nonce, platform claim, seed pin, or expired/reused invite fails closed without
a partial member. The target identity is never silently regenerated to make retry work.

## 4. Consent and eligibility ladder

Before join, the user sees plain-language consent stating that an assigned device may
receive assigned model weights and observe activation sizes/timing, network metadata,
and the activation inputs required by its stage; prompts/outputs and intermediate data
are not promised confidential from an assigned cooperative peer. The UI separately asks
about battery/power, metered network, background operation, storage budget, thermal
limits, update channel, diagnostics, and revocation consequences.

After join the device begins only as a signed member. The product shows independent
rungs for bootstrap, identity, membership, lease, software update, capability probe,
class qualification, directed-link evidence, assignment, artifact acquisition, load,
startup challenge, deployment qualification, and selection. A later rung never rewrites
an earlier failure or grants an unearned capability.

Owners may restrict an invitation to evidence-only, draft-worker, or stage-eligible
classes. The accepted ceiling is not a qualification. Browser and synthetic workers
remain ineligible for native model stages unless a future separately approved backend
changes that boundary.

## 5. Managed lifecycle, update, and recovery

The installed agent owns bounded bootstrap, signed lease renewal, reconnect, capability
refresh, current software status, graceful drain, and local diagnostics. It uses the A8
HTTPS membership control and Iroh activation planes; it never falls back to Tailscale,
SSH, a public admin endpoint, or unencrypted control.

Install, update, rollback, restart, suspend, network loss, low storage, revoked
membership, and uninstall are explicit durable states. Updates download only reviewed
signed application artifacts, never models. An update cannot change swarm identity,
member identity, eligibility, assignment, or selected deployment. Capability or runtime
changes force re-evaluation and requalification before placement.

Uninstall first requests drain when reachable, stops admission, cleans request-owned
resources, and preserves or deletes identity/cache only according to the user's explicit
choice. Offline uninstall cannot claim coordinator revocation; the UI instructs the
owner to revoke the member separately. Reinstall with retained identity resumes only if
the generation and lease remain valid.

## 6. Closed contracts and privacy

A13 adds closed, bounded, canonical records:

1. `mycelium.installation_package_manifest.v1` — reproducible package identity and
   verified platform/signing/update inputs.
2. `mycelium.pairing_request.v1` — target ephemeral key, device identity digest,
   protocol, nonce, and expiry without a swarm secret.
3. `mycelium.encrypted_invitation_handoff.v1` — recipient-bound ciphertext and public
   protocol metadata only.
4. `mycelium.onboarding_progress.v1` — privacy-reduced durable state, current rung,
   bounded blocker/action, and authority provenance.
5. `mycelium.installation_qualification.v1` — executed package, onboarding, lifecycle,
   negative, UI, and audit results for one exact platform claim.

Public projections exclude invitation plaintext/ciphertext, pairing keys, raw identity
or EndpointID, seed origin/hostname, addresses, usernames, device names, private paths,
package account identifiers, prompts, outputs, tokens, activations, KV, and model bytes.
Owner-private diagnostics remain bounded and redact credentials by construction.

## 7. Eight-workspace product behavior

- **Inference:** membership never appears as serving capacity; newly qualified capacity
  enters only through normal planning, activation, qualification, and selection.
- **Device Lab:** installation/pairing wizard, consent, progress ladder, supported class,
  preflight, and next safe action.
- **Network:** membership reachability and later qualified activation paths remain
  distinct; onboarding traffic is not inference traffic.
- **Nodes:** pseudonymous member generation, platform, package version, lease, lifecycle,
  capability, eligibility, assignment, and current contribution state.
- **Plans:** a new member is merely a candidate input after current evidence; active
  routes remain immutable.
- **Readiness:** package, identity, seed pin, invite, membership, lease, capability,
  qualification, artifact, load, and serving rungs with exact blockers.
- **Incidents:** bounded install, pairing, invite, update, lifecycle, reconnect, revoke,
  and uninstall events with recovery action and no secret material.
- **Settings:** owner invite/approve/revoke/quota/audit controls, update policy, consent,
  retained-identity/cache policy, and advanced redacted diagnostics.

Direct navigation, refresh, Back/Forward, reconnect, degraded/stale evidence, terminal
history, responsive layouts, keyboard flow, reduced motion, and a clean second browser
session are verified. Private pairing state remains confined to its owning session.

## 8. Verification and completion

### 8.1 Frozen acceptance decisions and inventory

| Decision ID | Frozen A13 boundary |
| --- | --- |
| `target_device_owned_identity` | The target creates and retains its durable identity before invitation minting; coordinators, owners, packages, and retries cannot supply or silently replace it. |
| `recipient_bound_single_use_invitation` | Pairing requests contain no swarm secret; invitation bundles are encrypted to the target, short-lived, single-use, nonce/identity/seed-bound, and absent from URLs, logs, trackers, pasteboards, and analytics. |
| `platform_signed_package_update` | Installation and updates require the exact reviewed platform-signed artifact, publisher identity, digest, lifecycle support, update channel, and rollback floor before identity or membership mutation. |
| `explicit_consent_resource_limits` | Join and contribution require plain-language consent plus independently bounded power, network, background, storage, thermal, update, diagnostics, and revocation choices. |
| `normal_user_no_expert_tools` | A clean normal-user path requires no SSH, Tailscale, Termux, ADB, source checkout, repository knowledge, seed URL, EndpointID, sidecar, shell, or CLI setup. |
| `dynamic_onboarding_ladder` | Bootstrap through selection remains a live authority-derived ladder; invitation ceilings and earlier success cannot grant qualification, artifacts, placement, readiness, or selection. |
| `managed_lifecycle_revocation_removal` | Install, lease renewal, reconnect, update, rollback, restart, suspend, network loss, revoke, drain, uninstall, retained identity, and removal have bounded durable outcomes. |
| `unrelated_network_onboarding` | The normal A8 HTTPS bootstrap and authenticated activation planes join across unrelated networks without Tailscale fallback, public administration, or unencrypted control. |
| `live_all_workspace_projection` | All eight workspaces consume one current public generation and reconstruct progress, blockers, lifecycle, revocation, and terminal history without secrets or browser-authored eligibility. |

`tests/a13_acceptance/inventory.v1.json` is the closed machine-readable acceptance
inventory for these decisions, package and invitation trust boundaries, normal-user
exclusions, dynamic rungs, lifecycle/removal cases, clean-device physical gates, and
all-eight workspace projections. Its protocol is
`mycelium.a13_onboarding_acceptance_inventory.v1`; its claim boundary is frozen design
and future acceptance input only. Passing
`tests/a13_acceptance/test_a13_inventory.py` proves inventory closure, not an installer,
signature operation, external account, device run, service, onboarding execution,
qualification, evidence, or completion claim.

Deterministic gates cover package manifest/signature and downgrade rejection; pairing
expiry/replay/substitution; recipient-only decryption; seed pin; atomic join; bounded
progress transitions; update/restart/reconnect; drain/uninstall; privacy; and dynamic UI
projection. No test logs a plaintext invitation.

Physical positive: on a clean supported device with no source checkout, Tailscale, SSH,
or CLI setup, install the signed package, perform encrypted QR/deep-link pairing across
unrelated networks, accept consent, join, appear in all relevant live workspaces, run
preflight, and remain visibly ineligible until the exact class gates pass. Then qualify
only the claimed level and observe it through the ordinary product path.

Physical negative: exercise invalid package, expired/reused/forged/recipient-mismatched
handoff, duplicate identity, unreachable bootstrap, revoked member, failed update,
unsupported platform, interrupted onboarding, suspend/network loss, and uninstall. None
may mutate an active route, selector, or another member.

A13 completes only when every advertised platform path is physically executed at its
exact claim level, all-eight-workspace verification and full regressions pass, a clean
normal user succeeds without expert setup, and one atomic A13 feature commit is created.
Until then it remains `design_only`.
