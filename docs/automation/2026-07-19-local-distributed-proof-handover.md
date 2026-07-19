# Local distributed-inference proof handover — 2026-07-19

## Scope and immutable claim boundary

Worktree: `/Users/evinova-self/Projects/mycelium-wt-distributed-proof`

Branch: `integration/mycelium-distributed-proof-20260719`

Pre-proof HEAD: `6bd7c5449865acefa61cd766019012bf16bf01e0`

Proof commit: `116125a9554086a38ed9a5d3c7738007886574ba`

Canonical `main` and merge base: `f2bc55b62c5e3103eda584c1c68c222244bde489`

This tranche establishes **local same-host evidence** that two independent Python
Router processes, each owning an independent native iroh sidecar and a disjoint
assignment-bound MLX stage, can perform one coherent distributed request and
match the single-process token reference. It also exercises distributed
cancellation cleanup, peer-process loss with fail-closed request rejection, and
successful inference after both processes and endpoints restart.

It does **not** establish route readiness. `route_ready` remains `false`.
Observatory remains read-only and is not used by this harness.

## Implemented tranche

- `physical_inference_node.py`
  - strict JSON-lines command service for one assignment-bound Router/runtime;
  - generated local Ed25519 command observations;
  - native iroh transport startup and peer binding;
  - start/decode/cancel/snapshot/rotate command surfaces;
  - no simulator or full-model fallback.
- `physical_sqlite_capacity.py`
  - transactionally shared local SQLite lease coordinator for independent local
    processes;
  - `BEGIN IMMEDIATE` reservation, commit, release, expiry, idempotency, and
    identity checks;
  - explicit claim boundary:
    `shared_local_sqlite_coordinator_for_multi_process_qualification_not_remote_production_transport`.
- `mycelium_router/transports/iroh.py`
  - bounded dispatch worker so Router callbacks and nested confirmed sends do not
    deadlock the native receive session;
  - receiver-owned ACK queue so ACK uses the delivery-owning sidecar session;
  - no receive polling while an inbound delivery awaits Router dispatch/ACK;
  - reconnect after receive-session timeout invalidation;
  - duplicate deliveries ACKed once per delivery but dispatched only once;
  - bounded diagnostic trace and lock-coherent cancellation-quiescence probe;
  - terminal failure on dispatch, ACK, replay collision, stale generation, or
    shutdown failure.
- `tests/physical_qualification/test_node_service.py`
  - separate OS process and endpoint identities;
  - disjoint loaded layer ranges;
  - native iroh remote frames in both directions;
  - distributed token parity against the local reference;
  - cancellation and local/remote runtime cleanup;
  - abrupt peer kill and fail-closed request result;
  - full process/endpoint restart followed by successful inference.
- `tests/e2e_request_iroh/harness.py`
  - cancellation evidence now waits for lock-coherent transport quiescence rather
    than sampling private pending-send state.
- `README.md`
  - replaces stale “distributed inference under construction” statement with the
    exact local evidence and remaining gaps.

## Observed distributed path

1. Node A admits request and executes the first assigned MLX stage.
2. Node A sends canonical `ProgressivePrefillMessage` over native iroh.
3. Node B dispatches it through its Router and executes only its assigned stage.
4. Node B returns `ManifestLocked` and `TokenEvent` over native iroh.
5. Node A continues decode; emitted tokens match the independent full-model
   reference for the same prompt and sampling seed.
6. A second request is cancelled; both runtimes and transport path state quiesce.
7. Node B is killed; Node A rejects a new distributed request with
   `route_ready=false`.
8. Both node processes restart with new endpoints and complete another distributed
   request.

## Verification observations

Latest complete observations before final handover update:

- `python3.14 -m pytest -q`
  - exit `0`;
  - `1694 passed, 2 skipped, 121 subtests passed`.
- Physical two-process test alone
  - exit `0`;
  - `1 passed in 11.30s`.
- Final staged physical/transport/request-to-iroh tranche
  - exit `0`;
  - `36 passed in 22.53s`.
- Qualification suite
  - exit `0`;
  - `94 passed in 1.23s`.
- Sealer plus same-host fail-closed qualifier lane
  - exit `0`;
  - `10 passed in 0.18s`;
  - the qualifier rejects two stage proofs sharing one `process_host_id` with
    `process_identity_invalid`, so this local run cannot promote `route_ready`.
- `python3.14 scripts/contract_audit.py`
  - exit `0`; `contract audit OK: 14 contracts`.
- `python3.14 -m compileall -q .`
  - exit `0`.
- `git diff --check`
  - exit `0`.
- Ruff on changed Python files
  - exit `0`; `All checks passed!`.
- `native/iroh_transport: cargo fmt --check`
  - exit `0`.
- `native/iroh_transport: cargo clippy --all-targets --all-features -- -D warnings`
  - exit `0`.
- `native/iroh_transport: cargo test`
  - exit `0`; `21 passed` across unit/integration targets.
- `python3.14 scripts/claim_boundary_audit.py`
  - exit `0`; `claim boundary audit OK: 181 source files`.
- `python3.14 scripts/release_security_audit.py`
  - exit `0`; `release security audit OK: 485 tracked files`, including the staged
    physical harness and coordinator.
- `ui/web: npm run check`
  - exit `127`; existing dependency tree lacks `vitest` (`sh: vitest: command not
    found`). No install was attempted because this tranche forbids package
    installation.

An independent diff review reported one medium cancellation-evidence race and one
low ACK-boundary test-coverage concern. The medium finding was repaired by the
public lock-coherent `cancellation_cleanup_complete()` probe and verified by the
request-to-iroh cancellation test. Existing deterministic tests cover blocked
Router callback shutdown and peer rotation; dedicated receiver-loss-after-ACK-
enqueue coverage remains desirable.

## Remaining physical and semantic gaps

These gaps forbid `route_ready=true`:

1. Both Router workers ran on one host, not two independently administered physical
   devices.
2. Capacity authority is a shared local SQLite file. No authenticated remote,
   owner-authoritative reservation/commit protocol exists yet.
3. Restart recovery is a clean new request after full process/endpoint restart. It
   does not preserve an in-flight token stream across peer failure.
4. Physical peer-generation rotation, topology-version rotation, path-attempt
   increment, stale-frame rejection after replacement, and recovery-prefill token
   continuity required by `mycelium.route_qualification.v1` remain unobserved in
   this harness.
5. No actual physical authority-document set was generated, sealed, and accepted by
   `qualify_sealed_evidence`. The sealer and qualifier schema tests pass, but using
   synthetic fixtures as physical proof is forbidden.
6. UI verification remains blocked by missing pre-existing dependencies; no install
   was permitted.
7. Simultaneous loss of both sidecars does not durably preserve delivery/replay
   history.

## Feature-lane handoff

Next physical lane should replace the local SQLite seam with an authenticated
reservation-owner protocol, then execute the preflight-authorized two-device run.
Capture signed load identities, remote capacity ownership, generation/topology and
path-attempt rotation, stale-frame rejection, recovery-prefill continuity, negative
runs, and final cleanup into one create-new evidence tree. Only the qualifier may
then issue `route_ready=true`.

No merge to `main`, push, fetch, pull, PR, remote-host action, dependency install,
credential access, phone action, or Observatory mutation was performed.
