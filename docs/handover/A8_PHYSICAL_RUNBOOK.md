# A8 Physical Runbook — Operator Procedure

Status: **active requalification procedure; no current completion claim**.
This procedure supersedes prior unsigned browser-evidence steps. Current gate state
must be read from freshly generated owner-private qualification records and the
candidate-bound summary only after every case below is re-executed.
The procedure begins from two operator inputs: (1) a canonical public HTTPS
origin and (2) an external peer on a genuinely unrelated network with Tailscale
stopped.

Sealing is performed exclusively by `scripts/a8_run_physical_gate.py`
with `--seal` into an owner-private evidence root; the runner emits
`mycelium.internet_native_qualification.v1` records and refuses to pass
a case whose boundary cannot first be verified live (pin over the
canonical origin).

Hosts: SEED_HOST (operator side, runs the loopback seed + tunnel) and
PEER_HOST (the external peer, unrelated network, Tailscale down).
All commands are run as the owner; invite bundles and node roots live in
owner-private directories.

### Trusted product-browser HTTPS proxy boundary

The product gateway's `--trusted-https-proxy` mode trusts only the conjunction
of one authenticated-user assertion and one owner-private proxy capability over
the loopback hop. The public edge therefore MUST authenticate the qualification
operator and MUST overwrite, rather than forward, both headers. When a loopback
tunnel hides the browser's public address, require both the tunnel's loopback
source and Basic Auth.

Generate one raw capability and one Nginx include without placing the capability
in shell arguments or command history:

```bash
umask 077
A8_PROXY_CAPABILITY_FILE=<OWNER_PRIVATE_CAPABILITY_FILE> \
A8_PROXY_CAPABILITY_INCLUDE=<ROOT_READABLE_NGINX_INCLUDE> \
python3 -c 'import os,secrets,pathlib; token=secrets.token_hex(32); pathlib.Path(os.environ["A8_PROXY_CAPABILITY_FILE"]).write_text(token, encoding="ascii"); pathlib.Path(os.environ["A8_PROXY_CAPABILITY_INCLUDE"]).write_text(f"proxy_set_header X-Mycelium-Proxy-Capability \\"{token}\\";\\n", encoding="ascii")'
chmod 0600 <OWNER_PRIVATE_CAPABILITY_FILE> <ROOT_READABLE_NGINX_INCLUDE>
```

Start the product gateway with all three co-required options:

```text
--trusted-https-proxy \
--public-origin <CANONICAL_HTTPS_ORIGIN> \
--trusted-proxy-capability-file <OWNER_PRIVATE_CAPABILITY_FILE>
```

A bounded Nginx location for the qualification window is:

```nginx
location / {
    allow 127.0.0.1;
    deny all;
    auth_basic "Mycelium A8 qualification";
    auth_basic_user_file <OWNER_PRIVATE_HTPASSWD_FILE>;

    proxy_pass http://127.0.0.1:<PRODUCT_GATEWAY_PORT>;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto https;
    proxy_set_header X-Mycelium-Authenticated-User $remote_user;
    proxy_set_header Authorization "";
    proxy_set_header Proxy-Authorization "";
    include <ROOT_READABLE_NGINX_INCLUDE>;
}
```

Bind the product listener and Nginx qualification listener to loopback only. The
capability MUST be exactly 64 lowercase hexadecimal characters (32 random bytes).
The owner-private password and capability files MUST remain mode 0600. The password
file MUST contain only the `owner` user and use a randomly generated one-run
password supplied to the browser gate via `A8_BROWSER_HTTP_USERNAME` and
`A8_BROWSER_HTTP_PASSWORD`. Reload Nginx only after `nginx -t` passes.
Strip both `Authorization` and `Proxy-Authorization` before proxying so edge
credentials never enter the product application.
Never use `proxy_add_header` and never pass either
`$http_x_mycelium_authenticated_user` or
`$http_x_mycelium_proxy_capability`: those forms let an internet client choose a
trusted value. Stop the tunnel and Nginx immediately after qualification, then
delete the password, capability, and Nginx include files. A request without
successful edge authentication fails before reaching the application; missing,
duplicate, or incorrect edge-injected identity/capability headers fail closed in
the application with `access_denied`.

### Exact candidate, browser authority, and transport authority

After all source-sensitive tests pass, freeze and audit the exact closed candidate:

```bash
python3.14 scripts/a8_source_manifest.py --write
python3.14 scripts/a8_source_manifest.py --check
```

Export the emitted `<SOURCE_DIGEST>` and the SHA-256 digest of the A8 specification
as `<SPEC_DIGEST>`. Every browser collection and `run` invocation uses those exact
values. Any source change after this step invalidates all observations and requires
a fresh manifest and rerun.

Before starting either physical node, build the owner transport authority directly
from persistent Iroh endpoint keys—not from a transport report:

```bash
umask 077
python3.14 scripts/a8_run_physical_gate.py build-transport-authority \
  --deployment-id <DEPLOYMENT_ID> \
  --endpoint-secret-file <NODE_0_ENDPOINT_KEY> \
  --endpoint-secret-file <NODE_2_ENDPOINT_KEY> \
  --endpoint-secret-file <ENDPOINT_MISMATCH_RECEIVER_KEY> \
  --output-file <TRANSPORT_AUTHORITY_FILE>
```

Reuse these exact endpoint key files for direct, forced-relay, and reconnect
runs. Restarting a node with a different key under the same endpoint identity is
an authority failure, not a renewable observation.

Generate a distinct raw 32-byte browser collector key and derive its public
authority before collection:

```bash
umask 077
python3.14 -c 'import secrets,sys; open(sys.argv[1], "xb").write(secrets.token_bytes(32))' <BROWSER_SIGNING_KEY>
chmod 0600 <BROWSER_SIGNING_KEY>
python3.14 scripts/a8_run_physical_gate.py build-browser-authority \
  --signing-key-file <BROWSER_SIGNING_KEY> \
  --case-id <BROWSER_CASE_ID> \
  --origin <CANONICAL_HTTPS_ORIGIN> \
  --deployment-id <DEPLOYMENT_ID> \
  --spec-digest <SPEC_DIGEST> \
  --source-digest <SOURCE_DIGEST> \
  --request-count <1_OR_2> \
  --output-file <BROWSER_AUTHORITY_FILE>
```

Keep both generated authority files owner-private mode `0600`. Issue a fresh browser
authority immediately before each collection attempt. Direct and forced-relay cases
use request count `1`; transition uses exactly `2`. The authority expires after five
minutes and is single-attempt evidence: never copy, reuse, or substitute it. Collect
with its bound key, case, deployment, origin, and candidate:

```bash
cd ui/web
node scripts/a8-product-browser-gate.mjs \
  --origin <CANONICAL_HTTPS_ORIGIN> \
  --output <BROWSER_REPORT_FILE> \
  --evidence-signing-key <BROWSER_SIGNING_KEY> \
  --browser-authority <BROWSER_AUTHORITY_FILE> \
  --transport-output <SIGNED_TRANSPORT_REPORT>
```

Pass the signed browser report, browser authority, signed transport report, and
transport authority unchanged to each browser/transport `run`. The runner rejects
unsigned, stale, wrong-origin, wrong-candidate, or unauthorized observations.
It also rejects a report replayed under a newly issued challenge, any deployment or
case mismatch, fabricated/duplicate request IDs, and any transport-report digest
substitution.

For every case named below that carries inventory outcome claims beyond the
runner's direct control-path observation, pass an owner-controlled
`<LIVE_CASE_PROBE_PROGRAM>`. The runner executes it without a shell after the
relevant event; it receives the case id and enrolled member id, validates a
closed case-specific schema, and retains the exact canonical probe report at mode `0600`
in `<OWNER_PRIVATE_PROBE_REPORT>`. A missing executable, malformed or stale report,
identity mismatch, existing output path, or unsafe output directory fails closed.

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
   `a8_run_physical_gate.py run unrelated_https_invite_without_tailscale --origin <canonical> --bundle-file <bundle> --node-root <peer-node-root> --case-probe-program <LIVE_CASE_PROBE_PROGRAM> --case-probe-output-file <OWNER_PRIVATE_PROBE_REPORT> --evidence-root <root> --spec-digest <SPEC_DIGEST> --source-digest <SOURCE_DIGEST> --seal`.

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
4. Seal: `a8_run_physical_gate.py run direct_path_qualified_browser_inference --origin <canonical> --browser-report-file <BROWSER_REPORT_FILE> --transport-report-file <SIGNED_TRANSPORT_REPORT> --browser-authority-file <BROWSER_AUTHORITY_FILE> --transport-authority-file <TRANSPORT_AUTHORITY_FILE> --evidence-root <root> --spec-digest <SPEC_DIGEST> --source-digest <SOURCE_DIGEST> --seal`.

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
4. Seal: `a8_run_physical_gate.py run forced_relay_privacy_reduced_browser_inference --origin <canonical> --browser-report-file <BROWSER_REPORT_FILE> --transport-report-file <SIGNED_TRANSPORT_REPORT> --browser-authority-file <BROWSER_AUTHORITY_FILE> --transport-authority-file <TRANSPORT_AUTHORITY_FILE> --relay-projection-key-file <RELAY_PROJECTION_KEY> --evidence-root <root> --spec-digest <SPEC_DIGEST> --source-digest <SOURCE_DIGEST> --seal`.

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
4. Collector: repeat `--transport-output` twice, once as
   `<SIGNED_TRANSPORT_REPORT_BEFORE>` and once as
   `<SIGNED_TRANSPORT_REPORT_AFTER>`; its runner-issued authority must bind
   `--request-count 2`.
5. Seal: `a8_run_physical_gate.py run observed_path_transition_and_reconnect --origin <canonical> --browser-report-file <BROWSER_REPORT_FILE> --transport-report-file <SIGNED_TRANSPORT_REPORT_BEFORE> --transport-report-file <SIGNED_TRANSPORT_REPORT_AFTER> --browser-authority-file <BROWSER_AUTHORITY_FILE> --transport-authority-file <TRANSPORT_AUTHORITY_FILE> --relay-projection-key-file <RELAY_PROJECTION_KEY> --evidence-root <root> --spec-digest <SPEC_DIGEST> --source-digest <SOURCE_DIGEST> --seal`.

Not executed. Requires the unrelated-network peer.

## cleartext_or_redirect_bootstrap

Physical negative. Proves: no bootstrap data is obtainable over
cleartext HTTP, a redirect, or a non-allowlisted route.

1. SEED_HOST: origin live; mint invite.
2. Any host (the runner does the observation itself with redirects
   disabled and a live-boundary preflight first):
   `a8_run_physical_gate.py run cleartext_or_redirect_bootstrap --origin <canonical> --bundle-file <bundle> --evidence-root <root> --spec-digest <SPEC_DIGEST> --source-digest <SOURCE_DIGEST> --seal`.
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
2. Run: `a8_run_physical_gate.py run certificate_without_seed_authority --origin <canonical> --transport-origin <alternate-origin> --bundle-file <bundle> --evidence-root <root> --spec-digest <SPEC_DIGEST> --source-digest <SOURCE_DIGEST> --seal`.
3. Capture: `seed_pin_mismatch_before_invite_transmission`,
   `join_not_attempted`, zero transmissions.

Not executed.

## invalid_or_replayed_invitation

Physical negative. Proves: an exact retry stays idempotent (no second
member) and a changed retry under the same invite is rejected.

1. SEED_HOST: origin live; mint invite.
2. Run: `a8_run_physical_gate.py run invalid_or_replayed_invitation --origin <canonical> --bundle-file <bundle> --node-root <owner-private-node-root> --case-probe-program <LIVE_CASE_PROBE_PROGRAM> --case-probe-output-file <OWNER_PRIVATE_PROBE_REPORT> --evidence-root <root> --spec-digest <SPEC_DIGEST> --source-digest <SOURCE_DIGEST> --seal`.
3. Capture: `exact_retry_idempotent` and `changed_retry_rejected`
   outcomes; the seed database shows exactly one member.

Not executed.

## revoked_active_member

Physical negative. Proves: revocation removes control and activation
admission for an active external member.

1. Both hosts: enroll the external member fully and place it in the active
   production route.
2. OWNER: revoke through the product route's owner administration boundary;
   never touch the seed DB directly. The call must advance the seed member to
   `STOPPED`, record `active_member_revoked`, and close every managed physical
   node/sidecar before returning.
3. PEER_HOST: before revocation, establish an authenticated Iroh connection
   and retain a candidate-bound activation admission. After revocation,
   attempt heartbeat and activation; both are refused with bounded codes and
   the production route remains fatal and closed.
4. Capture: the before/after probe-session binding, exact before-report
   digest, node-signed resource evidence, the route incident/closed state,
   the member's advanced generation, and zero activation admissions after
   revocation. Probe-program stdout alone is not evidence authority.
5. Seal: `a8_run_physical_gate.py run revoked_active_member --origin <canonical>
   --bundle-file <bundle> --node-root <peer-node-root>
   --revoke-command <owner-administration-argv> --case-probe-program <LIVE_CASE_PROBE_PROGRAM> --case-probe-output-file <OWNER_PRIVATE_PROBE_REPORT> --evidence-root <root>
   --spec-digest <SPEC_DIGEST> --source-digest <SOURCE_DIGEST> --seal`.
   The runner invokes the live probe program first with
   `<case-id> <member-id> before`, executes the owner's revocation command,
   then invokes it with `<case-id> <member-id> after` while providing the
   canonical before report on stdin. It retains the before report beside the
   requested output as `<stem>.before<suffix>` and the after report at the
   requested output, both mode `0600`. It never mints revocation authority
   itself (peer-required; fails closed without the external peer).

Not executed. Requires the external peer.

## endpoint_identity_mismatch

Physical negative. Proves: a member claiming a different EndpointID than
its accepted identity is refused.

1. PEER_HOST: after enrollment, present control envelopes under a
   different endpoint identity. The runner derives the expected `node.key`
   and rogue `impostor.key` identities under `<peer-node-root>`.
2. Prepare one separate owner-only receiver endpoint key. Include it in
   `<TRANSPORT_AUTHORITY_FILE>` and pass the same file to the exact
   source-owned `scripts/a8_endpoint_mismatch_probe.py` executor. Do not
   supply an arbitrary probe program.
3. Capture: the executor launches three real native Iroh sidecars, configures
   the receiver to admit only the expected endpoint, and drives the rogue
   authenticated endpoint against it. Node-signed before/after snapshots must
   show candidate and total identity-rejection counters increasing, admitted
   frames unchanged, and the expected path still `unknown`. The signed report
   also binds the SHA-256 digest of the exact sidecar executable; the runner
   verifies the bytes race-safely before invocation and the executor verifies
   them before and after stimulus.
4. Seal:

   ```bash
   python3.14 scripts/a8_run_physical_gate.py run endpoint_identity_mismatch \
     --origin <canonical> \
     --bundle-file <bundle> \
     --node-root <peer-node-root> \
     --sidecar-binary <EXACT_SIDECAR_BINARY> \
     --receiver-endpoint-secret-file <ENDPOINT_MISMATCH_RECEIVER_KEY> \
     --transport-authority-file <TRANSPORT_AUTHORITY_FILE> \
     --case-probe-output-file <OWNER_PRIVATE_PROBE_REPORT> \
     --evidence-root <root> \
     --spec-digest <SPEC_DIGEST> \
     --source-digest <SOURCE_DIGEST> \
     --seal
   ```

   The control-plane refusal must also carry a bounded identity code and leave
   the accepted member record unchanged.

Not executed. Requires the external peer.

## missing_or_stale_path_measurements

Physical negative. Proves: missing or stale measurements project
`unknown`, never zero, and block objectives that require costs.

1. Any host (no network needed):
   `a8_run_physical_gate.py run missing_or_stale_path_measurements --origin <canonical> --evidence-root <root> --spec-digest <SPEC_DIGEST> --source-digest <SOURCE_DIGEST> --seal`.
2. Capture: `path_class_remains_unknown`,
   `missing_metrics_remain_unknown`, `required_objective_blocked`.

Not executed (the runner procedure is deterministic and sealed records
for this case live in the operator evidence root when run).

## raw_relay_identity_injection

Physical negative. Proves: a raw relay URL/DNS/IP never crosses any
projection.

1. Any host (no network needed):
   `a8_run_physical_gate.py run raw_relay_identity_injection --origin <canonical> --evidence-root <root> --spec-digest <SPEC_DIGEST> --source-digest <SOURCE_DIGEST> --seal`.
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
4. Seal: `a8_run_physical_gate.py run unqualified_external_member --origin <canonical> --bundle-file <bundle> --node-root <peer-node-root> --authority-probe-program <owner-probe-program> --evidence-root <root> --spec-digest <SPEC_DIGEST> --source-digest <SOURCE_DIGEST> --seal`.

Not executed. Requires the external peer and the wired UI.

## tailscale_unavailable

Physical negative. Proves: bootstrap, join, and measurement complete
with no tailnet path present anywhere in the window.

1. PEER_HOST: `tailscale down`; confirm no tailnet interface or route.
2. Enroll and measure over the public origin only.
3. Capture: interface absence during the whole window; the origin
   resolved via public DNS only.
4. Seal: `a8_run_physical_gate.py run tailscale_unavailable --origin <canonical>
   --bundle-file <bundle> --node-root <peer-node-root> --case-probe-program <LIVE_CASE_PROBE_PROGRAM> --case-probe-output-file <OWNER_PRIVATE_PROBE_REPORT> --evidence-root <root>
   --spec-digest <SPEC_DIGEST> --source-digest <SOURCE_DIGEST> --seal`.
   The runner observes this host's own interfaces before and after the window;
   the observation is not operator-asserted.

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
4. Seal: `a8_run_physical_gate.py run ssh_unavailable --origin <canonical>
   --bundle-file <bundle> --node-root <peer-node-root> --case-probe-program <LIVE_CASE_PROBE_PROGRAM> --case-probe-output-file <OWNER_PRIVATE_PROBE_REPORT> --evidence-root <root>
   --spec-digest <SPEC_DIGEST> --source-digest <SOURCE_DIGEST> --seal`.
   The runner records its own zero ssh invocations; an ssh binary present on
   the host is recorded truthfully and does not by itself fail the gate.

Not executed. Requires the external peer.
