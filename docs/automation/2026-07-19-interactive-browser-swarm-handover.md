# Interactive browser-swarm handover — 2026-07-19

## State

- Worktree: `/Users/evinova-self/Projects/mycelium-wt-interactive-ui`
- Branch: `integration/mycelium-interactive-ui`
- Canonical `main`: `f2bc55b62c5e3103eda584c1c68c222244bde489`
- Feature parent: `de79d05cf97833f7f034990f7a4923559a705927`
- Implementation: `09c20e1bc4ff9513177f70f28dca6c46470ff162`
- Usage/security guide: `90b01eb38e4a378b2fd2bb2baa86bacc95f1d104`
- This lane has not been merged into `main` and has not been pushed.

The interactive commits depend on the integrated local distributed-proof and Pixel-stage ancestry below `de79d05`; they are not standalone cherry-picks onto canonical `main`.

## Delivered behavior

The operator starts one local server, opens its emitted fragment-bearing `operator_url`, and creates a single-use join web link. Another browser opens that link, exchanges the fragment capability for an in-memory peer session, clears the fragment from browser history, loads the assignment-bound tiny-GPT-2 decoder stage, and long-polls for work.

For each generated token:

1. host pre-stage produces hidden state;
2. joined browser executes the exact decoder stage in JavaScript;
3. host verifies browser hidden state against the Python Pixel stage;
4. host runs post-stage and verifies final logits against monolithic MLX reference;
5. host selects token only after both parity checks pass.

The operator UI displays connected peers, work state, generated labels, peer/job bindings, maximum intermediate error, maximum final-logit error, `local evidence only`, and `route_ready=false`.

## Start and use

Loopback:

```bash
cd /Users/evinova-self/Projects/mycelium-wt-interactive-ui
python3.14 scripts/interactive_swarm_server.py --host 127.0.0.1 --port 8787
```

Open the emitted `operator_url` exactly. Select **Create one-use join link**, send that link to the contributing browser, wait for one connected peer, then run the prompt from the operator form.

Another physical device requires HTTPS because browser Web Crypto requires a secure context. Either terminate TLS directly:

```bash
python3.14 scripts/interactive_swarm_server.py \
  --host 0.0.0.0 \
  --port 8787 \
  --public-origin https://swarm.example.net:8787 \
  --tls-cert /secure/path/fullchain.pem \
  --tls-key /secure/path/private-key.pem
```

or place an HTTPS reverse proxy in front of the unexposed HTTP listener. Non-loopback bind without explicit `--public-origin` is rejected before bind. Full usage lives in `docs/interactive-browser-swarm.md`.

## Security and lifecycle boundary

- Operator and join capabilities begin in URL fragments, so fragments do not enter initial HTTP requests.
- Browser consumes and clears operator/join fragments from address bar and history.
- Server stores capability digests, not plaintext capabilities.
- Operator API requires its control capability; peer API requires peer ID plus session capability.
- Optional operator token file must be regular, non-symlinked, URL-safe ASCII, and mode `0600`.
- Join link is one-use and at most five minutes old.
- Peer session has one-hour absolute lifetime and 45-second idle lifetime; closed tabs release capacity without waiting one hour.
- Public origins reject credentials, paths, query strings, fragments, control characters, malformed ports, and insecure non-loopback HTTP.
- Static path traversal and malformed/oversized JSON fail closed.
- Responses are `no-store` and carry CSP, no-referrer, frame denial, content-type, and permissions headers.
- Status and evidence omit prompt text, hidden matrices, and plaintext capabilities.
- UI suppresses overlapping status refreshes during inference, avoiding status-thread buildup behind the runtime inference lock.
- Peer receives stage tensors and hidden activations. Hidden activations are not a privacy boundary; use only trusted peers.
- Trusted private-network test server only. Firewalling, reverse-proxy limits, certificate trust, and access logging remain operator responsibilities.

## Observed gates

All commands exited `0` on this worktree unless explicitly noted.

| Gate | Observed result |
|---|---|
| `python3.14 -m pytest -q tests/interactive` | `38 passed in 5.61s` |
| repeated server lifecycle race test | 10/10 passes, each about 0.62–0.65s |
| `python3.14 -m pytest -q` | `1737 passed, 3 skipped, 121 subtests passed in 111.43s` |
| `python3.14 scripts/generate_browser_stage_vectors.py --check` | `browser stage vectors OK` |
| `python3.14 scripts/contract_audit.py` | `contract audit OK: 14 contracts` |
| `python3.14 -m compileall -q .` | exit `0`, no output |
| `git diff --check` | exit `0`, no output |
| `ui/web: npm run check` | 10 Vitest files / 102 tests passed; 3 contract tests passed; interactive bundle parity passed; TypeScript passed; Vite build passed |
| `native/iroh_transport: cargo fmt --check` | exit `0` |
| `native/iroh_transport: cargo clippy --all-targets --all-features -- -D warnings` | exit `0` |
| `native/iroh_transport: cargo test` | 21 Rust tests passed; 0 failed |
| `node scripts/interactive_browser_e2e.mjs` | two independent Chrome processes/profiles; one-use link joined; 2 browser jobs completed; 0 browser console errors |

Observed two-browser parity:

```text
max_intermediate_error = 1.1102230246251565e-16
max_logit_error        = 0.0000013262033462524414
route_ready            = false
local_evidence_only    = true
```

One intentionally parallel gate run exposed a shutdown race in the HTTP-worker test: server closure could reset the worker connection before the worker joined. Cleanup now stops and joins the worker before closing the server. The isolated regression passed 10/10 times, focused tests passed, and the final full Python gate passed.

`npm run check` emitted only the existing non-fatal Vite chunk-size warning for bundles over 500 kB.

## Remaining semantic and physical gaps

- No physical second-device browser was used in this evidence run; both browser processes ran locally with isolated Chrome profiles.
- Direct-TLS and reverse-proxy HTTPS paths were not exercised.
- Browser computation does not travel through production Router or native-iroh request routing.
- Stage/model fixture is deterministic tiny-GPT-2-shaped local evidence, not a production model deployment.
- No production device-authority, admission, physical network-path, reconnect, or token-continuity qualification was performed.
- No internet-facing, multi-tenant, adversarial load, proxy-limit, certificate-lifecycle, or access-log qualification was performed.
- Browser code is served by the host and output is parity-checked; this is not remote-code attestation.
- Peer sees assigned weights and intermediate activations.

These gaps prohibit any readiness promotion. All interactive artifacts and records remain local evidence only with `route_ready=false`. Network Observatory code remains separate and read-only.
