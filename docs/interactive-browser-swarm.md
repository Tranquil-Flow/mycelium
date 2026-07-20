# Interactive browser swarm

## Claim boundary

This console is an operator-driven, local-evidence test surface. A browser peer executes one exact tiny-GPT-2 decoder stage in JavaScript; the host checks every returned hidden state against the local Python Pixel stage and checks final logits against the monolithic MLX reference before selecting a token.

It does **not** use the production Router path, does not establish device authority, and does not make this deployment production-ready. `route_ready` stays `false` in stage packs, work, results, status, token records, and inference records. The Network Observatory remains read-only and separate.

## Loopback quick start

From the repository root:

```bash
python3.14 -m mycelium_demo serve \
  --mode live \
  --host 127.0.0.1 \
  --port 8787
```

This is the supported one-command live stack. It serves frontend and API from one origin and needs no Tailscale service or dependency. Startup prints one JSON document. Open its `operator_url` exactly; do not open only `public_origin`. The operator URL carries a generated control capability in its URL fragment. The page consumes the fragment into memory and clears it from the address bar.

Then:

1. Set **Peer-session target** to 2 and select **Create 2 unique worker links**.
2. Copy Worker 1 to the first contributing browser and distinct Worker 2 to the second. For a physical-device test, send one link to each device; never reuse one link across peer sessions.
3. Open each link once. Each peer checks secure-context/WebCrypto support before exchanging its fragment capability, clears the address bar, loads the assignment-bound stage pack, and starts long-polling for work.
4. Wait for **Peer sessions joined: 2**, **Workers ready: 2**, **Peer-session target: READY**, and five PASS checks on each worker. A joined peer is not counted ready until it has an active work poll.
5. Enter a prompt, select at least as many new tokens as target peer sessions, and select **Run through browser swarm**. Active-request count and cancellation remain responsive while inference runs.
6. Require **Peer sessions proven: 2 / 2**, then inspect generated labels, peer/job bindings, per-token parity rows, intermediate error, and final-logit error. **Download local JSON** saves an unsigned local summary; it is recoverable after reopening the operator URL and remains `local_evidence_only=true` and `route_ready=false`.
7. To test cancellation, start a multi-token request and select **Cancel request**. The request must settle without an evidence record, and workers must return to ready state rather than fail on a late result.

For the first N token jobs, request-bound exclusions prevent reuse until N distinct peers contribute. The runtime then freezes that exact N-peer cohort and restricts every remaining token job to it. Within the frozen cohort, lowest completed-job count and then stable peer ID decide assignment. Success therefore neither leaks to an extra peer nor depends on poll-thread wake order.

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
     --port 8787
   ```

   macOS may ask whether Python can accept incoming connections; allow it for this test. The command emits a `mycelium.device_lab_prepared.v1` record followed by the normal live-server startup record. Keep the emitted `operator_url` on the host.
4. Transfer **only** the `ca_cert` file from the preparation record to the operator host trust store and each worker device over an authenticated local channel. Never transfer `ca-key.pem`, `server-key.pem`, the state directory, or the operator URL.
5. Trust the CA on the operator host and each test device. On the host, verify the exact HTTPS `operator_url` opens without a certificate warning before creating links:
   - iPhone/iPad: open the `.crt`, install the downloaded profile, then enable full trust under **Settings → General → About → Certificate Trust Settings**.
   - Android: use **Settings → Security → Encryption & credentials → Install a certificate → CA certificate**; wording varies by vendor.
   - macOS: import the `.crt` in Keychain Access, open it, and set SSL trust to **Always Trust**.
   - Windows: install the `.crt` for the current user in **Trusted Root Certification Authorities**.
6. Open the host-only `operator_url`. Select a peer-session target, create the batch of unique links, and send exactly one link to each physical device. Open each link once and keep every worker page foregrounded and awake.
7. Wait until every worker shows five PASS checks and the operator shows **Peer-session target: READY**. Run at least as many new tokens as target peers. The runtime—not only the UI—acquires N distinct peers and then freezes that exact cohort.
8. Require rendered **Peer sessions proven: N / N**. Download local JSON and confirm `required_distinct_peers == observed_distinct_peers == N`, exactly N expected peer IDs appear, parity stays within displayed bounds, and `route_ready=false`. This proves N distinct authenticated browser sessions. Physical-device count remains an operator-observed fact; record device/browser/OS separately.

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
3. Confirm **Peer-session target: READY** before inference and manually verify one live peer page per expected physical device.
4. Run at least N new tokens and require rendered **Peer sessions proven: N / N** plus `required_distinct_peers == observed_distinct_peers == N` in downloaded local JSON.
5. Download the unsigned local JSON summary before stopping workers.
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
