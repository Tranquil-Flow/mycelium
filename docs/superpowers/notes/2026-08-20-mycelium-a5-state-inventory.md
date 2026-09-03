# A5 State Inventory — 2026-08-20

**Spec:** `docs/superpowers/specs/2026-08-18-mycelium-a5-multistage-replication.md` (status: `design_only` until physical gates pass)
**Plan:** `docs/superpowers/plans/2026-08-20-mycelium-a5-finish-plan.md`
**Branch:** `codex/flexible-swarm-catalog` (A4 at `487a1b4`)

## Phase status

| Phase | Status | Evidence |
|---|---|---|
| 0 pre-flight | ✅ | HEAD 487a1b4, branch correct, node-3 SSH green (`ssh mycelium-laptop` alias) |
| 1 workload freeze | ✅ | `tests/a5_acceptance/` — manifest digest `db4467d1…`; 18 manifest tests; harness wired |
| 2 closed contracts | ✅ | `mycelium_replica_contracts.py` (4 shapes); 47 tests; fixtures regenerated; `contract_audit.py` OK 75 contracts |
| 3 isolated leaf modules | ✅ | `enumerate_legal_tracks` (replication.py ext), `assign_flow_over_legal_tracks` (flow.py ext), `mycelium_qualification/replica.py` (new); 47 tests; A4 dispatcher untouched (zero diff in mycelium_live/request_gateway/ui_gateway) |
| 4 third node | ◑ provisioned, serve BLOCKED on A4 seal | laptop staged byte-parity; membership lease live (heartbeat ~20 s); sidecar configured (endpoint `51947b11…`, assignment `6089cca8…`, load proof `1a2b9128…`); replicated plan bound at seed (`/tmp/a5-plan-bound.json`, 3 offers) |
| 5.0 UI gate fix | ✅ code+tests (not deployed; revised per Evi's authority rule) | `cancel_unconfirmed` non-terminal phase (server terminal frame only, no client timer); gate cleared on cancel ack; schedule PROVISIONAL pending A4 discarded_through coordination; vitest 18/18 session file, inference suite 40/40, tsc clean |
| 5.1-5.3 | ⏸ after A4 seal | dispatcher track selection + UI projections |
| 6 physical gates | ⏸ after A4 seal | positive / benchmark / replica-loss / illegal-state |
| 7 seal+commit | ⏸ | — |

## The A4-lane gate (Evi decision 2026-08-20)

A4-closure serve (PID 67542, another Hermes lane) holds port 8791 + live node-0/node-2 staging roots, mid browser gate. A4 blocks A5, A6, A10, A12, A15; A5 blocks only A15 → A4 has fleet priority.

**A5 serve start precondition (both):**
1. Port 8791 free
2. A4 lane reports browser gates SEALED (not just port dropped) — A5 controller open wipes every peer staging root (cleanup → prepare), so a premature start destroys A4 evidence.

Serve command (ready to run): `mycelium_demo serve --mode live --operator-plan /tmp/a5-plan-bound.json --seed-state-root .../seed-live --host 127.0.0.1 --port 8791 --static-root ui/web/dist <4x a4 evidence flags>` (see plan).

## Live physical state (node-3 = M4 16GB laptop)

- Workspace `/Users/evinova/mycelium-a4-concurrency-node3-v6/` (staged from node-0 + node-3 docs; controller will re-stage from transfer bundle at serve open)
- Sidecar key `/Users/evinova/mycelium-a4-node3-keys/identity/sidecar.key` (0600; OUTSIDE staging root — survives controller cleanup)
- Membership data dir: `/Users/evinova/mycelium-a4-concurrency-node3-v6/membership-data` — NOTE: this sits INSIDE the staging root and will be wiped by the controller's cleanup→prepare at serve open. The membership daemon keeps its key in memory and the lease keeps renewing, but the daemon's data dir must be MOVED OUT (or re-joined) before the A5 serve starts. Pending action.
- Transfer bundle extended: `mycelium-physical-run/a4-concurrency-20260818/transfer-bundle/control/{node-3-assignment.json,node-3-stage-pack.json}` + source/node-3 manifests in `/tmp/a5-replicated-operator-plan.json`

## Pending before A5 serve start

1. Move node-3 membership data dir outside the staging root (e.g. `/Users/evinova/mycelium-a4-node3-membership/`), re-join via owner invite if needed, keep heartbeat alive.
2. Wait for A4 browser-gate seal + port 8791 free.
3. Rebind plan (offers expire ~285 s after each bind; rebind right before serve start).
4. Start serve; verify live-status: `route_alive=true`, replica track qualified, node-3 listed.

## 2026-08-20 (evening session) updates

- Item 3 resolved: A5's earlier "3072 passed" was `pytest tests/` (3083
  collected). The canonical suite is plain `pytest` from repo root with
  python3.14: 4438 passed / 13 skipped / 121 subtests on the A5 working tree
  (before the replica-loss wiring edits); A4's 4309 was the same invocation
  on frozen A4 bytes. No real gap.
- Item 2 resolved (root cause + FIX LANDED 2026-08-20 evening): see
  `docs/superpowers/notes/2026-08-20-a5-flaky-linearization-root-cause.md`.
  The serial model now models the terminal-publication window
  (`publication_pending_kind` + explicit `publish` action): worker-delivered
  completions may be outrun by a cancel (backend counter bump, terminal
  unchanged); cancelled/failed terminals absorb or precede it. Bounded
  generator inserts publish after terminal transitions; race generator
  emits both window-open and settled variants (90 traces). Conformance
  suite 55/55 green; the flake tuple is now permitted alongside the settled
  variant.
- Item 1 prepared: rebase bundle at `/tmp/a5-rebase-bundle/`
  (a5-tracked-changes.patch, a5-untracked-list.txt, base-commit.txt).
  Conflict map (recomputed 23:16 vs A4's 26 dirty files): supervisor.py
  (A4EvidenceError-import removal in both lanes; A5's install endpoint lives
  here too — layer it onto A4's bytes) + tests/live/test_supervisor.py
  (BOTH lanes now edit it — A5's 3 endpoint tests are additive, merge
  carefully) + contract-manifest (regenerate on merged bytes) + service.py
  (A4-only). A4 does NOT touch mycelium_request_conformance/* or
  tests/request_conformance/{test_model,test_trace_generator,
  test_generated_production_traces}.py.
  MODEL-FIX surface (evening): mycelium_request_conformance/{model,trace}.py
  + tests/request_conformance/{test_model,test_trace_generator,
  test_generated_production_traces}.py join the change set. A4's dirty set
  does NOT touch these files. REBASE-TIME CHECK: A4's updated
  test_lifecycle_public.py + tests/request_gateway/test_stream.py may pin
  pre-fix model behavior — run the full conformance slice against the merged
  bytes and reconcile before sealing.
  DRESS REHEARSAL DONE (23:12): patch applies clean on pristine 487a1b4;
  with the 28 untracked files carried over, conformance 55/55 green on the
  rehearsal bytes. The rehearsal tree (archive extract) initially failed
  23 tests with ModuleNotFoundError on untracked sources — methodology
  artifact, resolved by copying the untracked files in; not a patch defect.
- Replica-loss wiring CLOSED (was: mark_replica_loss_placement had zero
  callers): `placements_lost_by_liveness` helper in
  `mycelium_qualification/replica_track.py`; `_sync_replica_loss_from_liveness`
  in `PhysicalLiveRoute.public_status`; live-status gains
  `replica_loss_placement_ids`; selector now drops a track when ANY of its
  placements is lost (was primary-only). Deterministic tests added in
  `tests/a5_acceptance/test_replica_dispatch.py`.
- Dispatcher rotation FIXED: `_choose_track_id` rotates over
  [incumbent (plain A4 path), replica tracks]. Without this, ONE physical
  replica pins every request to the same track — positive gate "distinct
  tracks" impossible. Round-robin tests updated to incumbent-first.
- OPEN DESIGN QUESTION (Evi): baseline vs candidate benchmark windows need
  a per-request mode through the ordinary product path — see
  `docs/superpowers/notes/2026-08-20-a5-dispatcher-mode-design-decision.md`.
  RESOLVED 2026-08-20 per Evi: (A) rotation includes incumbent — LANDED.
  (B) NO `requested_track` field; instead A5-owned operator endpoint
  `POST /__mycelium/replica-qualification/install` calls the existing
  `set_replica_track_qualification` at runtime (empty set = baseline
  incumbent-only; restored = candidate). No A4 contract change. (C) per-window
  field = `bindings.qualified_track_policy_digest` (allowed_mode_specific_
  binding_fields; harness requires it per record) — quoted + harness-proven
  with real synthetic run (material, no binding_mismatch).
- UI dist rebuilt (includes A5ReplicaTrackPanel); vitest 91/529 green.
- Phase 6 gate scripts drafted (fleet-free; exercised for real at gate open):
  `scripts/run_a5_product_gate.py` (positive: 2 concurrent requests, distinct
  tracks via admission-status placement_ids, per-node frame/KV deltas, zero
  cleanup) and `scripts/run_a5_benchmark_gate.py` (frozen workload + protocol;
  mode switch via the install endpoint; fixture validated by the frozen
  harness). Known constraint documented in the driver: frozen qos_mix names
  interactive/background/bulk but the qualified product path admits only the
  interactive profile — all requests submit as interactive_chat_v1/interactive;
  the qos_mix_digest binding remains the frozen manifest digest.
- Remaining Phase 6 gate scripts ALSO drafted: `scripts/run_a5_replica_loss_gate.py`
  (kills the node-3-r2 data sidecar via SSH/lsof on
  /private/tmp/sc-node3-r2/i.sock; proves explicit termination without
  migration, unaffected completion, loss marking via liveness quarantine,
  surviving-track usability) and `scripts/run_a5_negative_illegal_gate.py`
  (qualifier leaf rejections; tampered-digest doc -> install 400 with live set
  unchanged; closed-but-rejected parity doc -> installed but unselectable,
  requests incumbent-only; restoration -> candidate selection recovered).
  Both cursor-parity-robust (rotation alternates, so 2 requests always cover
  both tracks). All four scripts: ruff clean, py_compile clean, 6/6 ad-hoc
  verification checks passed on final bytes (temp verifier, deleted after).
- node-3-r2 membership data dir confirmed OUTSIDE staging root
  (`/Users/evinova/mycelium-a4-node3-membership/`) — pending action #1 above
  is resolved; daemon live, lease ~+290s, socket `/private/tmp/sc-node3-r2/i.sock`.

## Digests (this session)

- Replicated execution graph content digest: `sha256:78b2367458a709cde89a2fc9509ca564c76e8c4e31a2146c2ac138e0e5dedabc`
- node-3 assignment: `6089cca8-d309-5f5c-9178-cf845375182f`
- node-3 stage pack: `sha256:5bc6ff091c43bcedb7590734e3ce730a13b1cda90a36695e24e5093234fa3108`
- node-3 stage pack verification: `sha256:5f1d44b05ef1314523d1bb032d65700d6069b5297f9517c5e1efe002103ada91`
- node-3 load proof: `sha256:1a2b9128e9acb13e4330f81858cc8ce71e40d93fda807c477d82b3b0b7c764a2`
- node-3 activation endpoint: `51947b11014ee32d67a90f708b991584f48af5002a751481f9b0c55dde6dc94a`
- node-3 sidecar key: `sha256:12177fc9273ec7b6f0b84d1b1f61f091aebf66beb0912ca164241f6af5796d71`
