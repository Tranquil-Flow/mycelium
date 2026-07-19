# Interactive browser swarm

## Claim boundary

This console is an operator-driven, local-evidence test surface. A browser peer executes one exact tiny-GPT-2 decoder stage in JavaScript; the host checks every returned hidden state against the local Python Pixel stage and checks final logits against the monolithic MLX reference before selecting a token.

It does **not** use the production Router path, does not establish device authority, and does not make this deployment production-ready. `route_ready` stays `false` in stage packs, work, results, status, token records, and inference records. The Network Observatory remains read-only and separate.

## Loopback quick start

From the repository root:

```bash
python3.14 scripts/interactive_swarm_server.py \
  --host 127.0.0.1 \
  --port 8787
```

Startup prints one JSON document. Open its `operator_url` exactly; do not open only `local_origin`. The operator URL carries a generated control capability in its URL fragment. The page consumes the fragment into memory and clears it from the address bar.

Then:

1. Select **Create one-use join link**.
2. Copy only that join link to the browser that should contribute compute.
3. Open it once. The peer exchanges the fragment capability, clears the address bar, loads the assignment-bound stage pack, and starts long-polling for work.
4. Wait until the operator page reports one connected peer.
5. Enter a prompt, select 1–8 new tokens, and select **Run distributed inference**.
6. Inspect generated labels, peer/job bindings, intermediate error, and final-logit error. Successful records still say `local evidence only` and `route_ready=false`.

A join link is single-use and expires after at most five minutes. Reloading a joined peer loses its in-memory session; create a new link. Reloading the operator page loses its in-memory operator capability; reopen the original `operator_url` printed at startup. A peer that stops polling expires after 45 seconds, so closed tabs do not hold swarm capacity for the full one-hour absolute session limit.

## Join from another device

Non-loopback browsers require a secure context for Web Crypto. Either provide a certificate and key directly or put an HTTPS reverse proxy in front of the HTTP listener. Restrict access at the network/firewall layer and set the exact externally reachable origin.

Direct TLS:

```bash
python3.14 scripts/interactive_swarm_server.py \
  --host 0.0.0.0 \
  --port 8787 \
  --public-origin https://swarm.example.net:8787 \
  --tls-cert /secure/path/fullchain.pem \
  --tls-key /secure/path/private-key.pem
```

For reverse-proxy TLS, omit `--tls-cert` and `--tls-key`, proxy the external HTTPS origin to the bound HTTP listener, and do not expose that upstream listener to an untrusted network.

Open the emitted HTTPS `operator_url`, create a join link, and send that one link to the other device. For any non-loopback bind, `--public-origin` is mandatory and the server rejects startup without it; do not rely on the request `Host` header. The origin must contain only scheme, host, and optional port. Plain HTTP is accepted only for `localhost`, `127.0.0.1`, or `::1`.

This is a trusted-private-network test server, not an internet-facing multi-tenant service. Firewall policy, proxy connection limits, and access logging remain deployment responsibilities.

## Capability handling

- Operator endpoints require the generated control capability in the HTTP `Authorization` header.
- The operator capability starts in a URL fragment, is never part of the initial HTTP request, is held only in page memory, and is server-stored only as a SHA-256 digest.
- An optional operator token file must be regular, non-symlinked, mode `0600`, ASCII URL-safe, and 32–512 characters:

  ```bash
  umask 077
  python3.14 -c 'import secrets; print(secrets.token_urlsafe(48))' > /secure/path/operator-token
  python3.14 scripts/interactive_swarm_server.py \
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
python3.14 -m pytest -q tests/interactive
python3.14 scripts/generate_browser_stage_vectors.py --check
node scripts/interactive_browser_e2e.mjs
cd ui/web && npm run check
```

The browser E2E launches two independent headless Chrome processes with isolated profiles. One is the operator; the other joins via the generated one-use web link, executes two browser-stage jobs, and returns outputs that the host verifies against both local stage and monolithic references. It is a device-style process/isolation proof on one machine, not physical cross-device or network-path qualification.

Repository-wide gates remain:

```bash
python3.14 -m pytest -q
python3.14 scripts/contract_audit.py
python3.14 -m compileall -q .
git diff --check
(cd native/iroh_transport && cargo fmt --check && cargo clippy --all-targets --all-features -- -D warnings && cargo test)
```
