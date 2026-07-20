# Two-device interactive UI-test handover — 2026-07-19

Historical snapshot. Current labels and acceptance rules live in `docs/interactive-browser-swarm.md`; distinct peer sessions do not prove physical-device identity.

## Integration state

- Worktree: `/Users/evinova-self/Projects/mycelium-wt-interactive-ui`
- Branch: `integration/mycelium-interactive-ui`
- Two-device implementation commit: `e16da25` (`feat: prepare two-device browser UI tests`)
- Earlier interactive commits retained:
  - `09c20e1` — browser swarm implementation and tests
  - `90b01eb` — operating/security documentation
  - `1b47002` — initial interactive handover
- Canonical `main` remains outside this worktree. Nothing was merged or pushed.

## UI now ready for two standby devices

The operator UI keeps two distinct one-use links visible at once. It reports:

- **Peer sessions joined**: authenticated browser sessions accepted by the server
- **Workers ready**: joined browsers currently long-polling and eligible for work
- **Peer-session target**: `READY` only when the selected number of workers is actually eligible

The current inference button stays disabled until the selected peer-session target is ready. A request acquires N distinct peers, freezes that exact cohort, then balances later jobs within the cohort; assignment does not depend on poll-thread wake order.

Each peer UI:

- consumes and clears its fragment before joining;
- shows running state, peer ID, completed jobs, and last job;
- has a mobile-safe Stop control;
- reaches `stopped` without surfacing the expected `peer_left` race as failure;
- renders consumed-link reuse as a real join failure rather than a false stopped state.

## Physical UI-test sequence

Physical devices require an externally reachable HTTPS origin. Do not expose this test server directly to the public internet.

1. Start the server with either direct TLS or a trusted HTTPS reverse proxy, an exact `--public-origin`, and a private-network/firewall boundary. See `docs/interactive-browser-swarm.md`.
2. Open the emitted operator URL only on the host.
3. Use the generated batch of two unique worker links; do not reuse a link across devices.
4. Send the Device 1 link to the first standby device and the distinct Device 2 link to the second. Each link is one-use and expires after at most five minutes.
5. Confirm both device address bars clear the `#join/...` fragment and both peer pages show **State: running**.
6. Wait for **Peer sessions joined: 2**, **Workers ready: 2**, and **Peer-session target: READY**.
7. Request at least two new tokens. Confirm **Peer sessions proven: 2 / 2** and `required_distinct_peers == observed_distinct_peers == 2` in downloaded local JSON. Manually record which physical device hosted each peer.
8. Select **Stop peer worker** on each device. Confirm both show **State: stopped** without an alert.

Do not share the operator URL with peers. Send only one peer join link to each device.

## Observed automated evidence

All commands exited `0` unless noted as the intentionally observed RED tests repaired during development.

### Python

- `python3.14 -m pytest -q tests/interactive`
  - `39 passed in 5.61s`
- Balanced scheduling focused test:
  - passed `25/25` repeated runs
- `python3.14 -m pytest -q`
  - `1738 passed, 3 skipped, 121 subtests passed in 111.38s`

### Three-browser UI E2E

`node scripts/interactive_browser_e2e.mjs` launched one operator and two peer Chrome processes with three isolated browser profiles. Final expanded run observed:

- two distinct one-use links joined;
- operator and both join fragments cleared;
- host plus two peer 390×844 mobile-overflow checks passed;
- two ready workers completed `[1, 1]` jobs;
- both peers stopped cleanly;
- consumed invite reuse rejected and rendered as failure;
- browser console errors: `0`;
- maximum intermediate error: `1.1102230246251565e-16`;
- maximum logit error: `0.0000013262033462524414`;
- `route_ready=false`;
- `local_evidence_only=true`.

Three additional consecutive two-peer runs also produced `[1, 1]` jobs, two clean stops, zero console errors, and `route_ready=false`.

### UI, contracts, compile, and native transport

- `cd ui/web && npm run check`
  - 10 Vitest files / 102 tests passed
  - 3 contract-diff tests passed
  - interactive bundle parity, typecheck, and production build passed
  - existing non-fatal Vite chunk-size warning remains
- `python3.14 scripts/generate_browser_stage_vectors.py --check`
  - browser stage vectors OK
- `python3.14 scripts/contract_audit.py`
  - 14 contracts passed
- `python3.14 -m compileall -q .`
  - passed
- `git diff --check`
  - passed
- `native/iroh_transport`
  - `cargo fmt --check` passed
  - strict `cargo clippy` passed
  - 21 Rust tests passed

The temporary dogfood server, token, state directory, and dependency symlink were removed. Port `127.0.0.1:18787` was verified clear.

## Claim boundary and remaining gaps

Current evidence proves same-machine browser-process UI behavior and exact browser-stage parity. It does not yet prove:

- physical second-device behavior;
- LAN/Tailscale/Wi-Fi reachability;
- certificate trust or direct/reverse-proxy TLS configuration;
- browser background-throttling behavior on the two real devices;
- production Router/native-iroh routing;
- device authority, remote attestation, or production readiness.

A peer receives stage weights and hidden activations. Hidden activations are not a formal privacy boundary. Use only trusted standby devices on a private network.

Keep the Network Observatory read-only and separate. Every claim and artifact remains local evidence only with `route_ready=false`.
