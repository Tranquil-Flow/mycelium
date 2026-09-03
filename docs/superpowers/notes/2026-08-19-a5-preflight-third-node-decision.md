# A5 Preflight Decision — 2026-08-19, updated 2026-08-20 (SSH resolved), updated 2026-08-20 (correction)

> **Correction (2026-08-20):** A self-contradicting parenthetical called the SSH path "RESOLVED" in the line below while a separate section still named it as a Phase-4 blocker. The "SSH blocker, Phase 4 cannot start" section the A5 finish plan inherited is **stale** and is replaced by the "SSH access verified" section below. Phase 4 is no longer blocked on SSH.

**Third physical placement for A5 replica: the M4 16GB laptop.**

- Host: `evis-macbook-pro-1` @ Tailscale `100.126.111.123`.
- **SSH access verified 2026-08-20** (corrects the earlier "SSH blocker, Phase 4 cannot start" section in the A5 finish plan; that section was stale and has been removed):
  - node-3 (laptop): `ssh mycelium-laptop` — alias in `~/.ssh/config`: user `evinova`, key `~/.ssh/id_ed25519_m4pro_to_laptop`, BatchMode. Verified: `ssh mycelium-laptop hostname -s` → `Evis-MacBook-Pro`. ~31 GiB disk free (re-measured 2026-08-20).
  - node-2 (Surface Book 2): `ssh mycelium-node2` — linux, user `astra`, key `~/.ssh/id_ed25519_mycelium_linux`. Tailscale SSH policy refuses other usernames; the node-3 key cannot be used to SSH as `evinova` into this host.
  - **Seed leases renew only via heartbeat from a running membership daemon on each node** — there is no renewal CLI. If `bind_operator_plan_to_seed.py` fails with `operator_plan_member_ineligible`, query `last_heartbeat_sequence` in the seed database; if it has stopped advancing, the node's daemon is down and must be restarted, not retried.
- Laptop facts (measured): macOS 15.6.1, arm64, 10 CPUs, 16 GiB RAM (`hw.memsize=17179869184`), system python 3.9.6, `/opt/homebrew/bin/python3.14` with mlx 0.31.1 + numpy 2.4.3, ~31 GiB disk free. **No node/npm on the laptop** — it is a runtime-only host; provisioning flows must compile sidecar binaries elsewhere.
- Constraints from memory: local M4 16GB — no heavy compute without approval; queue work by memory budget; Python externally-managed on that host.
- 0.5B incumbent (`Qwen/Qwen2.5-0.5B-Instruct`) is small enough for the 16GB budget for ONE stage range replica (stage-local KV, contiguous subset of layers — never the whole model).
- **Replica carries stage0 (layers 0–22)** — locked 2026-08-20. Dominant compute; gives real materiality; spec §3 satisfied (same layer range + component roles within the replica group).
- This satisfies A5 spec §4/§10: "at least one replica of a contiguous stage on another eligible physical placement".
- Same-host/correlated-domain caveats do not apply — laptop is a distinct host AND distinct failure domain from both the M4 Pro 48GB (node-0) and the Surface Book (node-2).

## Locked recommendations (cross-reference)

The full A5 plan at `docs/superpowers/plans/2026-08-20-mycelium-a5-finish-plan.md` carries four locked decisions made on 2026-08-20:

1. **Replica carries stage0 (layers 0–22)** — recorded in this note above.
2. **Branch = `codex/flexible-swarm-catalog`** (same as A4 commit `487a1b4`) — atomic-commit-per-gate discipline, lowest blast radius.
3. **Inconclusive material-gain → stay `design_only` with all A5 artifacts shipped** — spec §1 is permissive on this; honest outcome, unblocks A6/A7/A8.
4. **Per-tab UI single-active-request gate → fix standalone as Phase 5.0** — real product bug, ships before Phase 5.1.

## Phase 4 provisioning state (2026-08-20)

Done and verified:

- Workspace `/Users/evinova/mycelium-a4-concurrency-node3-v6/` staged with byte-parity to node-0 (physical_inference_node.py `sha256:0da89e79…`, stage pack `sha256:7b4303ca…`, static embedding `sha256:24933169…`, config `sha256:18e18afc…`; sidecar binary `sha256:5b1ca19a…`)
- node-3 membership: joined `interactive-swarm` via owner-minted invite; endpoint `node-identity-e2aca6eb…`; lease heartbeat ~20 s via seed HTTP :8876 (seed DB `seed_members` shows rolling `lease_expires_at`, `last_heartbeat_sequence` advancing)
- Sidecar identity: secret key `/Users/evinova/mycelium-a4-node3-keys/identity/sidecar.key` (0600; parent dir must be `identity`/`identities` per controller validation); activation endpoint id `51947b11014ee32d67a90f708b991584f48af5002a751481f9b0c55dde6dc94a`
- Replica docs (verified on laptop hardware): assignment `6089cca8-d309-5f5c-9178-cf845375182f`, stage pack digest `sha256:5bc6ff09…`, pack verification `sha256:5f1d44b0…`, load proof `sha256:1a2b9128…` (runtime identity mlx 0.31.1 / float32 / int8-weight-only — parity with node-0)
- Replicated operator plan bound at seed: `/tmp/a5-replicated-operator-plan.json` → `/tmp/a5-plan-bound.json` (3 signed offers incl. node-3 replica)

BLOCKED until the A4 lane seals its browser gates:

- A5 serve start (Evi decision 2026-08-20): A4-closure serve (PID 67542) holds port 8791 + the live node-0/node-2 staging roots. The A5 controller's open flow wipes every peer staging root (cleanup → prepare), so starting it early destroys A4 evidence. Gate before start: 8791 free AND A4 browser gates sealed (not just port dropped).
- Serve will run on port 8791 with the A5 bound plan once the A4 lane reports sealed.

## Pre-A5 work still needed

- Phase 5.0 per-tab UI gate fix (safe to build + unit-test while A4 runs; do NOT rebuild `ui/web/dist`)
- Phase 5.1/5.2 dispatcher track selection + interleaving tests (after A4 sealed — touches `mycelium_live/route.py`)
- Phase 5.3 ReplicaPanel + 8-workspace projections (new UI files + vitest; no dist rebuild until A4 done)
- Phase 6 physical gates (positive, benchmark, replica-loss, illegal-state)
- Phase 7 seal + atomic commit
