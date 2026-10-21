# Interactive browser swarm

## Claim boundary

This console is an operator-driven, local-evidence test surface. A browser peer executes one exact bounded, decoder-shaped synthetic matrix fixture in JavaScript; the host checks every returned vector against the matching local Python fixture before selecting a fixture label. Legacy API and record fields retain inference-shaped names, but no production model weights or model inference participate. Synthetic browser work is never model inference.

It does **not** use the production Router path, does not establish device authority, and does not make this deployment production-ready. `route_ready` stays `false` in stage packs, work, results, status, token records, and legacy inference-named request records. The Network Observatory remains read-only and separate.

## Loopback quick start

From the repository root:

```bash
(cd ui/web && npm run build)
python3.14 -m mycelium_demo serve \
  --mode live \
  --host 127.0.0.1 \
  --port 8787 \
  --static-root ui/web/dist \
  --worker-static-root mycelium_interactive/static
```

The build step creates the rich React operator shell. The following supported live-server command mounts that shell at `/` and the genuine browser worker at `/device`, all on one origin and with no Tailscale service or dependency. Startup prints one JSON document. Open its `operator_url` exactly; do not open only `public_origin`. In split mode the operator URL uses `#lab/operator/<capability>`; the React shell consumes the capability into memory, immediately replaces the address bar with `#lab`, and sends it only in same-origin authorization headers.

Then:

1. In **Device Lab**, set **Invite count** to 2 and select **Create 2 one-use links**.
2. Copy Device 1 to the first contributing browser and distinct Device 2 to the second. For a physical-device test, send one link to each device; never reuse one link across peer sessions.
3. Open each link once. `/device` checks secure-context/WebCrypto support before exchanging its fragment capability, clears the address bar, loads the assignment-bound stage pack, and starts long-polling for work.
4. Wait for **2 joined**, **2 ready**, and **Minimum 2 distinct peer sessions met**, plus five PASS checks on each worker. This is a readiness minimum, not an exact-cohort claim. A joined peer is not counted ready until it has an active work poll.
5. Enter a prompt seed, select at least as many **Maximum fixture tokens** as **Minimum distinct peer sessions**, then select **Run local evidence request**. Active-request count and cancellation remain responsive while the synthetic matrix exercise runs.
6. Require **2 / 2 exact peer sessions**, then inspect fixture labels, peer/job bindings, per-token parity rows, intermediate error, and the legacy `logit_error` fixture-score field. **Download local evidence JSON** saves an unsigned local summary; it is recoverable from the in-memory recent-request projection while the server runs and remains `local_evidence_only=true` and `route_ready=false`.
7. To test cancellation, start a multi-token request and select **Cancel active request**. Once cancellation returns accepted, no browser may obtain a new compute permit for that request; the request must settle without an evidence record, and workers must return to ready state rather than fail on a late result.

For the first N fixture-token jobs, request-bound exclusions prevent reuse until N distinct peers contribute. The runtime then freezes that exact N-peer cohort and restricts every remaining fixture-token job to it. Within the frozen cohort, lowest completed-job count and then stable peer ID decide assignment. Success therefore neither leaks to an extra peer nor depends on poll-thread wake order.

A join link is single-use and expires after at most five minutes. Reloading a joined peer loses its in-memory session; create a new link. Reloading the operator page loses its in-memory operator capability; reopen the original `operator_url` printed at startup. A peer that stops polling expires after 45 seconds, so closed tabs do not hold swarm capacity for the full one-hour absolute session limit.

## Physical device-lab quick start

Non-loopback browsers require a trusted secure context for Web Crypto. The `device-lab` command creates a private local CA once, rotates a short-lived LAN server certificate on each launch, verifies its exact SAN and CA chain, and then starts the same genuine live stack. Both stock macOS LibreSSL and newer OpenSSL are exercised. It needs no Tailscale service or dependency.

1. Put the host and worker devices on the same trusted LAN. Avoid guest Wi-Fi or access-point client isolation.
2. Determine the host IPv4 LAN address. IPv6 advertisement is rejected in this release because the device-lab listener is IPv4-only. On a typical Wi-Fi-connected Mac:

   ```bash
   ipconfig getifaddr en0
   ```

3. From the repository root, substitute that address:

   ```bash
   python3.14 -m mycelium_demo device-lab \
     --advertise-host <LAN_IP> \
     --port 8787 \
     --static-root ui/web/dist \
     --worker-static-root mycelium_interactive/static
   ```

   macOS may ask whether Python can accept incoming connections; allow it for this test. The command emits a `mycelium.device_lab_prepared.v1` record followed by the normal live-server startup record. Keep the emitted `operator_url` on the host.
4. Transfer **only** the `ca_cert` file from the preparation record to the operator host trust store and each worker device over an authenticated local channel. Never transfer `ca-key.pem`, `server-key.pem`, the state directory, or the operator URL.
5. Trust the CA on the operator host and each test device. On the host, verify the exact HTTPS `operator_url` opens without a certificate warning before creating links:
   - iPhone/iPad: open the `.crt`, install the downloaded profile, then enable full trust under **Settings → General → About → Certificate Trust Settings**.
   - Android: use **Settings → Security → Encryption & credentials → Install a certificate → CA certificate**; wording varies by vendor.
   - macOS: import the `.crt` in Keychain Access, open it, and set SSL trust to **Always Trust**.
   - Windows: install the `.crt` for the current user in **Trusted Root Certification Authorities**.
6. Open the host-only `operator_url`. Select **Invite count**, create the batch of one-use links, and send exactly one link to each physical device. Open each link once and keep every worker page foregrounded and awake.
7. Wait until every worker shows five PASS checks and the operator shows **Minimum N distinct peer sessions met**. This only says at least N sessions are ready. Run at least as many new tokens as required peers. The runtime—not only the UI—then acquires N distinct peers and freezes that exact completed-request cohort.
8. Require rendered **N / N exact peer sessions**. Download local evidence JSON and confirm `required_distinct_peers == observed_distinct_peers == N`, exactly N expected peer IDs appear, parity stays within displayed bounds, and `route_ready=false`. This proves N distinct authenticated browser sessions. Physical-device count remains an operator-observed fact; record device/browser/OS separately.

The default state directory is `~/.mycelium/device-lab`, mode `0700`. Reusing it preserves the CA already trusted by devices while rotating the server leaf key/certificate. To end the test, press Ctrl-C. Remove the CA from device trust stores when the lab is no longer needed; deleting the state directory permanently retires that CA.

Common failures:

- Browser cannot connect: confirm the LAN IP, host firewall allowance, port 8787, and absence of guest-network client isolation.
- Certificate warning: install and fully trust the emitted `.crt`; do not bypass warnings and assume Web Crypto will work.
- Worker shows secure-context/WebCrypto WAIT: trust the CA, close the page, and reopen the original unconsumed one-use link.
- Link expired or page reloaded: create a fresh unique link. Peer sessions intentionally remain memory-only.
- Request times out before N/N proof: confirm all selected workers remain foregrounded, awake, and actively polling; create fresh links for any expired or reloaded worker.

## Advanced external HTTPS

To use an existing certificate or reverse proxy instead of the local device-lab CA, restrict access at the network/firewall layer and set the exact externally reachable origin.

Direct TLS:

```bash
python3.14 -m mycelium_demo serve \
  --mode live \
  --host 0.0.0.0 \
  --port 8787 \
  --public-origin https://swarm.example.net:8787 \
  --tls-cert /secure/path/fullchain.pem \
  --tls-key /secure/path/private-key.pem
```

For reverse-proxy TLS, omit `--tls-cert` and `--tls-key`, proxy the external HTTPS origin to the bound HTTP listener, and do not expose that upstream listener to an untrusted network.

For any non-loopback bind, `--public-origin` is mandatory and startup fails without it; do not rely on the request `Host` header. The origin contains only scheme, host, and optional port. Plain HTTP is accepted only for `localhost`, `127.0.0.1`, or `::1`.

Physical N-device checklist (operator-observed physical identity):

1. Keep the operator URL on the host only; send each device only its unique join link.
2. Confirm every device clears `#join/...` and shows five PASS checks plus **State: running**.
3. Confirm **Minimum N distinct peer sessions met** before starting the matrix exercise and manually verify one live peer page per expected physical device. Treat the minimum as readiness only; exact-N evidence begins after a completed request freezes its cohort.
4. Run at least N fixture tokens and require rendered **N / N exact peer sessions** plus `required_distinct_peers == observed_distinct_peers == N` in downloaded local JSON.
5. Download the unsigned local evidence JSON before stopping workers.
6. On each device, select **Stop peer worker** and confirm **State: stopped** without an alert.
7. Record browser/OS, physical device count, LAN topology, exits, parity, and any failures. Keep `route_ready=false`; only qualifier-owned accepted evidence can change authority.

This is a trusted-private-network test server, not an internet-facing multi-tenant service. Firewall policy, proxy connection limits, and access logging remain deployment responsibilities.

## Capability handling

- Operator endpoints require the generated control capability in the HTTP `Authorization` header.
- The operator capability starts in a URL fragment, is never part of the initial HTTP request, is held only in page memory, and is server-stored only as a SHA-256 digest.
- An optional operator token file must be regular, non-symlinked, mode `0600`, ASCII URL-safe, and 32–512 characters:

  ```bash
  umask 077
  python3.14 -c 'import secrets; print(secrets.token_urlsafe(48))' > /secure/path/operator-token
  python3.14 -m mycelium_demo serve --mode live \
    --operator-token-file /secure/path/operator-token
  ```

- Join capabilities are fragment-only, single-use, digest-only at rest, and exchanged for a one-hour in-memory peer session.
- Peer session credentials are sent only to peer join/poll/result/leave endpoints. Operator capability is not sent to those endpoints.
- Static and JSON responses are `no-store`; CSP, no-referrer, frame denial, and restrictive permissions headers are enabled.
- A peer receives stage tensors and hidden activations. It does not receive raw prompt text, but hidden activations are not a formal privacy boundary. Use only peers allowed to see the assigned stage and its intermediate values.

## Resource and lifecycle bounds

The coordinator defaults to 16 simultaneously connected peers, 64 active jobs, 64 retained peer records, and 256 retained job records. Request bodies, prompt length, token count, matrix dimensions, polling time, dispatch time, invite lifetime, one-hour absolute session lifetime, and 45-second peer idle lifetime are bounded. Terminal peer/job history is pruned oldest-first. Browser departure, idle/session expiry, operator cancellation, and result replay all fail closed.

## Verification

Focused feature gates:

```bash
python3.14 -m pytest -q tests/demo/test_serve_cli.py tests/demo/test_device_lab.py tests/interactive
python3.14 scripts/generate_browser_stage_vectors.py --check
node scripts/interactive_browser_e2e.mjs
cd ui/web
npm run check
npm run test:interactive-multibrowser
```

The Chrome E2E launches three independent headless processes with isolated profiles: one operator and two peer workers. It verifies one-use capabilities, 390×844 layout, two-token distribution, downloaded/reloaded evidence, active cancellation, worker recovery, clean stops, and zero browser console errors.

The multibrowser E2E launches separate Chromium, Firefox, and WebKit contexts, with Firefox and WebKit doing the genuine browser-stage jobs. It repeats evidence download and 390×844 overflow checks in all three engines. Both are one-machine browser proofs: `physical_devices=0`, no physical cross-device or network-path qualification, and no route-readiness authority.

Repository-wide gates remain:

```bash
python3.14 -m pytest -q
python3.14 scripts/contract_audit.py
python3.14 -m compileall -q .
git diff --check
(cd native/iroh_transport && cargo fmt --check && cargo clippy --all-targets --all-features -- -D warnings && cargo test)
```
