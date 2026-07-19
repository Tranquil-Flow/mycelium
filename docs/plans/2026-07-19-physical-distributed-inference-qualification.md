# Physical Distributed-Inference Qualification Implementation Plan

> **For Hermes:** Use subagent-driven-development skill for independent read-only reviews, but keep physical orchestration and commits in the primary integration driver.

**Goal:** Produce one immutable, qualifier-accepted physical route proof in which the production Router, MLXRuntimePort, and IrohTransport execute a deterministic GPT-2 route across the M4 Pro and Evis MacBook, including exact parity, cancellation, peer loss, process restart, state recovery, and cleanup.

**Architecture:** Adapt the already verified local route challenge instead of inventing a second runtime. A controller prepares one deterministic tiny-model deployment, starts one assignment-bound node service on each Mac, exchanges authenticated Iroh endpoint bindings, and drives production Router calls through the local entry node. Each node emits canonical, signed, process-bound observations; a separate fail-closed sealer assembles those observations into the ten authority documents and invokes `mycelium_qualification.qualifier.qualify_route` with real Ed25519 verification. Recovery replaces only the failed remote stage process and placement, rotates the Iroh peer generation, publishes a higher topology version, and replays through Router `RECOVERY_PREFILL`; it never loads the full model on either route host.

**Tech Stack:** Python 3.14, MLX, `cryptography` Ed25519, production Mycelium Router/relay/live ports, Rust `iroh-sidecar`, SSH only as the physical process-control channel, pytest.

---

## Scope and claim boundary

In scope:

- Physical route members: local M4 Pro (`node-a`) and Evis MacBook (`node-b`).
- Production classes on both route members: `Router`, `RelayEngine`, `MLXRuntimePort`, `IrohTransport`, native `iroh-sidecar`.
- One deterministic two-stage GPT-2 deployment, one stage per physical host.
- Exact token parity plus bounded hidden-state numeric parity against an independent monolithic reference.
- Separate lifecycle probes under the same run identity: normal completion, cancellation, remote disconnect, remote process restart, Router recovery, stale-generation rejection, lease/KV/capacity cleanup.
- Real, ephemeral, per-run Ed25519 node keys. Private key bytes never leave the generating process or enter evidence.
- Immutable canonical evidence and sole promotion authority `mycelium_qualification.qualifier:RouteQualificationV1`.

Not in scope:

- Main-branch merge, push, PR, package installation, model download, credentials, or release readiness.
- Observatory writes; it remains read-only.
- Pixel 8 as a route stage: the phone lacks the production MLX stage runtime. Its existing service may supply an explicitly auxiliary health/control observation, but no qualifier field may count it as a stage, Router, runtime, or transport peer.
- Relay-scale, NAT-scale, Internet-scale, heterogeneous-runtime, or production-capacity claims.
- `route_ready=true` from a harness flag, hand-written JSON, fixture verifier, or synthetic evidence. Until the real qualifier returns an accepted record, all harness summaries must say `route_ready=false`.

## Definition of done

All conditions must hold:

1. Production Router sends real activation frames over native Iroh between two distinct physical Macs.
2. Stage processes have distinct host IDs, PIDs, endpoint IDs, local stage ranges, and valid signed load proofs.
3. No route member loads all model layers or both stage assignments.
4. Normal run and recovered run match the independent reference; hidden-state error remains within the frozen tolerance.
5. Cancellation releases remote and local KV, reservations, pending deliveries, and worker state.
6. Peer loss is observed, the old process exits, a different process and endpoint start, stale generation is rejected, topology version increases, Router runs `RECOVERY_PREFILL`, and token sequence continues without loss or duplication.
7. Every required negative run contains real mutation/probe evidence and remains `route_ready=false`.
8. Evidence is canonical, path-safe, hash-pinned, immutable after sealing, and signatures verify with public keys embedded in provenance.
9. `qualify_route(...)` returns `evidence_class=physical_qualification`, `route_ready=true`, no reason codes, and the sole authority name.
10. Complete Python/Rust/UI/security/claim gates pass after implementation.

## Safety invariants

- Use only `/Users/evinova-self/Projects/mycelium-wt-distributed-proof`; never mutate canonical or prior worktrees.
- Stage explicit paths only; never `git add -A` or `git add .`.
- Use existing Python, Rust, Node dependencies and lockfiles; never install.
- Use short run/socket roots below `.myc-phys/` or `/tmp` to stay below macOS Unix-socket limits.
- Controller transfers only tracked source, built sidecar, deterministic tiny-model artifacts, and run configuration; no environment files, SSH keys, tokens, caches, or unrelated repository contents.
- Node services reject unknown commands, duplicate commands, mismatched run/deployment IDs, noncanonical JSON, path escapes, stale generations, and unsigned control records.
- Evidence stores hashes, IDs, sizes, timing, and lifecycle state only; never tensor values, private keys, bootstrap secrets, credentials, prompts beyond deterministic public test tokens, or shell environment.
- Every process and sidecar is timeout-bounded and reaped on success or failure.

---

### Task 1: Freeze canonical signing and node-observation contracts

**Objective:** Define one production-safe schema for public keys, signed statements, host/process identity, and immutable node observations.

**Files:**
- Create: `mycelium_qualification/signing.py`
- Create: `tests/qualification/test_signing.py`
- Modify: `mycelium_qualification/__init__.py`

**RED:** Add tests requiring Ed25519 key generation, canonical statement bytes, signature/public-key encoding, verification, tamper rejection, wrong-key rejection, algorithm rejection, exact-field rejection, and absence of private key material from serialized output.

**Run RED:**

```bash
python3.14 -m pytest -q tests/qualification/test_signing.py
```

Expected: collection/import failure because `mycelium_qualification.signing` does not exist.

**GREEN:** Implement minimal `generate_signer()`, `sign_statement()`, and `verify_statement()` wrappers around `cryptography.hazmat.primitives.asymmetric.ed25519`. Bind `run_id`, `deployment_id`, `node_id`, `host_id`, `process_id`, `endpoint_id`, `statement_kind`, and statement digest.

**Verify:** Focused tests pass; `python3.14 -m compileall -q mycelium_qualification tests/qualification` exits 0.

**Commit:** Stage only the three listed files; commit `feat: add physical evidence signing contracts`.

### Task 2: Freeze lifecycle/recovery evidence in qualification authority

**Objective:** Make cancellation, disconnect, restart, generation rotation, recovery replay, and cleanup mandatory for physical route promotion.

**Files:**
- Modify: `mycelium_qualification/contracts.py`
- Modify: `mycelium_qualification/qualifier.py`
- Modify: `tests/qualification/conftest.py`
- Modify: `tests/qualification/test_contracts.py`
- Modify: `tests/qualification/test_qualifier.py`
- Modify: `docs/contracts/route-qualification-v1.md` if present; otherwise create it.

**RED:** Extend `RouteQualificationV1` with `lifecycle_evidence_digest`. Require route challenge `lifecycle_evidence` with exact subdocuments for cancellation and disconnect/restart/recovery. Mutation tests must independently reject same PID, same endpoint, non-increasing generation/topology version, absent `RECOVERY_PREFILL`, duplicate/missing token indexes, unreleased KV/capacity, stale frame acceptance, peer-drop omission, and full-model fallback.

**Run RED:**

```bash
python3.14 -m pytest -q tests/qualification/test_contracts.py tests/qualification/test_qualifier.py
```

Expected: new lifecycle tests fail because authority currently has no lifecycle field or validator.

**GREEN:** Add strict validator and bind its digest into qualification ID/output. Keep synthetic fixtures unmistakably unqualified and update all exact-field tests.

**Verify:** Focused tests pass; existing qualifier mutation corpus remains green.

**Commit:** Stage only listed contract, tests, and contract documentation; commit `feat: require lifecycle recovery evidence`.

### Task 3: Freeze evidence-sealing input and fail-closed filesystem behavior

**Objective:** Define a sealer that accepts node observations, assembles authority documents, pins bytes, and never promotes incomplete or mutable evidence.

**Files:**
- Create: `mycelium_qualification/sealer.py`
- Create: `tests/qualification/test_sealer.py`
- Modify: `mycelium_qualification/__init__.py`

**RED:** Tests require exact ten authority paths, optional extra lifecycle/raw-observation pins, canonical UTF-8 JSON, bounded depth/nodes/size, relative normalized paths, no symlinks, create-new writes, fsync-before-manifest, no post-seal mutation, real signature verification, and rejection of fixture/synthetic timing/simulator/fixture-port claims. Verify malformed or incomplete inputs cannot emit `route_ready=true`.

**Run RED:**

```bash
python3.14 -m pytest -q tests/qualification/test_sealer.py
```

Expected: import failure because sealer does not exist.

**GREEN:** Implement `seal_physical_evidence(...)` and `qualify_sealed_evidence(...)` using shared canonical JSON/evidence helpers and `qualify_route`. Do not duplicate qualifier policy.

**Verify:** Focused tests pass; tampering any sealed byte fails before signature/authority acceptance.

**Commit:** Stage only sealer, tests, and package export; commit `feat: seal immutable physical evidence`.

### Task 4: Extract reusable deterministic deployment preparation

**Objective:** Reuse the proven tiny GPT-2 model, assignment, graph, load, and reference logic without importing a CLI script as production API.

**Files:**
- Create: `mycelium_qualification/physical_deployment.py`
- Create: `tests/qualification/test_physical_deployment.py`
- Modify: `two_process_inference_qualification.py`
- Modify: `tests/test_two_process_inference_qualification.py`

**RED:** Tests freeze deterministic artifact digests, two non-overlapping assignments, no full-model host assignment, identical source bundle rendering, graph construction, stage load proofs, and monolithic-reference isolation.

**Run RED:**

```bash
python3.14 -m pytest -q tests/qualification/test_physical_deployment.py tests/test_two_process_inference_qualification.py
```

Expected: new API unavailable.

**GREEN:** Move only shared preparation/reference helpers; preserve CLI output and byte-level local qualification behavior.

**Verify:** Focused tests pass and the existing local qualification command still exits 0 with `route_ready=false`.

**Commit:** Stage only the four listed files; commit `refactor: share physical deployment preparation`.

### Task 5: Build a command-bounded physical node service

**Objective:** Run one local assignment, Router, MLX runtime, signer, and native Iroh sidecar per physical host.

**Files:**
- Create: `physical_inference_node.py`
- Create: `tests/physical_qualification/test_node_service.py`

**RED:** Subprocess tests require hello/configure/start/snapshot/cancel/rotate/stop commands, exact command schemas, run/deployment identity binding, peer endpoint validation, stage-local load enforcement, signed observations, timeout cleanup, and no secret/private-key serialization.

**Run RED:**

```bash
python3.14 -m pytest -q tests/physical_qualification/test_node_service.py
```

Expected: node script missing.

**GREEN:** Implement JSON-lines control over stdin/stdout. Build `MLXRuntimePort`, `Router`, `PublishedTopologyProvider`, `PublishedDeviceStateProvider`, `InProcessLeaseCapacityPort`, `IrohTransport`, and existing native sidecar wrapper. Emit structured records only on stdout; diagnostics to stderr.

**Verify:** Local two-node subprocess smoke test starts, exchanges authenticated Iroh frames, snapshots, and shuts down with no orphan processes.

**Commit:** Stage only node script and tests; commit `feat: add physical route node service`.

### Task 6: Build controller and safe transfer manifest

**Objective:** Orchestrate the two physical Macs while proving exactly which bytes and commands crossed the SSH control boundary.

**Files:**
- Create: `physical_inference_qualification.py`
- Create: `tests/physical_qualification/test_controller.py`

**RED:** Fake-process tests require source-manifest allowlist, digest verification on both hosts, no env/credential paths, Python 3.14 path override, short socket roots, endpoint exchange, bounded command correlation, stdout/stderr separation, cleanup on every injected failure, and default `route_ready=false`.

**Run RED:**

```bash
python3.14 -m pytest -q tests/physical_qualification/test_controller.py
```

Expected: controller missing.

**GREEN:** Implement prepare/run/seal commands. Use archive bytes generated from an explicit allowlist and transmit through SSH stdin; do not copy the whole worktree. Require remote hash acknowledgment before launch.

**Verify:** Dry-run prints only public file paths/digests/commands and performs no SSH; local fake-host mode completes but remains unqualified.

**Commit:** Stage only controller and tests; commit `feat: orchestrate physical route qualification`.

### Task 7: Prove normal physical route and parity

**Objective:** Execute prefill and decode across M4 Pro stage 0 and Evis MacBook stage 1 using production Iroh transport.

**Files:**
- Runtime evidence only under an ignored `.myc-phys/<run-id>/` directory; no source change unless a real defect is exposed.

**Execute:** Build existing sidecar with `cargo build --release --locked` if needed, prepare artifacts, launch both node services, run prefill plus deterministic decode, and run independent monolithic reference on the controller only.

**Required observations:** Distinct host/PID/endpoint IDs; Iroh mutual authentication; Router path lock; two committed reservations; real activation frame sequences and receiver monotonic timings; exact tokens; hidden-state error within tolerance; no full-model route host; normal completion cleanup.

**Failure handling:** If RED exposes a production defect, add a minimal failing repository test first, implement minimal repair, run focused tests, and commit separately. Never edit evidence to fit the qualifier.

### Task 8: Prove physical cancellation

**Objective:** Cancel while remote decode is in flight and prove distributed cleanup.

**Execute:** Start a fresh request, pause at an explicit remote-runtime test interlock in the harness (not a synthetic transport), issue production Router cancellation, release interlock, and await both hosts.

**Required observations:** PathCancellation traverses Iroh; entry and remote relays remove path; local and remote MLX runtimes release KV; reservations release; pending transport deliveries become zero; request emits no post-cancel token; all processes remain healthy for the next probe.

**Negative boundary:** Interlock controls timing only. Evidence must label it and qualifier must never interpret interlock duration as production latency.

### Task 9: Prove peer loss, restart, and Router recovery

**Objective:** Continue one request after real remote node-process loss without local full-model fallback.

**Execute:** Decode at least one token, terminate the remote node process, observe old PID exit and transport failure, start a different remote process with a new endpoint/generation and replacement placement ID, verify identical stage-only artifact digest, rotate local peer, publish a higher topology version, submit the production failure report, and let Router execute `RECOVERY_PREFILL` before continuing decode.

**Required observations:** Old/new PID and endpoint differ; peer generation and topology version increase; stale-generation frame is rejected; old placement excluded; replacement placement selected; recovery replay contains prompt plus already generated tokens; token indexes remain contiguous and unique; final output matches monolithic reference; both hosts release recovered KV/capacity; no stage 1 weights ever load on node-a.

**Failure handling:** If current recovery ordering cannot safely publish/rotate before `receive_failure_report`, freeze the missing control contract with RED tests rather than bypassing Router recovery.

### Task 10: Execute all ten real negative runs

**Objective:** Bind concrete fail-closed observations to each required authority negative-run kind.

**Files:**
- Evidence only unless authority defect found.

**Runs:** `stale_proof`, `wrong_revision`, `wrong_endpoint`, `missing_tensor`, `expired_reservation`, `sequence_replay`, `dropped_peer`, `full_model_fallback`, `simulator_participation`, `synthetic_timing`.

Each run must execute or validate the forbidden mutation against production validation seams, record the exact stable rejection code and evidence digest, and state `route_ready=false`. A hand-authored reason code without observed rejection is insufficient.

### Task 11: Seal and invoke sole qualification authority

**Objective:** Assemble immutable evidence and attempt physical promotion exactly once from sealed bytes.

**Execute:** Stop writers; canonicalize node observations; verify real signatures; write required documents create-new; fsync; build manifest; reopen and rehash every pin; call `qualify_sealed_evidence` with current wall-clock time and real Ed25519 verifiers.

**Pass condition:** Qualifier returns `physical_qualification`, `route_ready=true`, no reason codes, and authority `mycelium_qualification.qualifier:RouteQualificationV1`. Serialize that returned record separately; never copy `route_ready=true` from input evidence.

**Fail condition:** Preserve rejected evidence, output stable reason, keep route_ready false, and do not weaken or deselect any gate.

### Task 12: Full verification, independent review, and handover

**Objective:** Prove repository health and document exact local claim boundary.

**Run:**

```bash
python3.14 -m pytest -q
python3.14 scripts/contract_audit.py
python3.14 -m compileall -q .
/opt/homebrew/bin/ruff check .
git diff --check
python3.14 scripts/security_audit.py
python3.14 scripts/claim_audit.py
(cd native/iroh_transport && cargo fmt --check)
(cd native/iroh_transport && cargo clippy --all-targets --all-features -- -D warnings)
(cd native/iroh_transport && cargo test)
(cd ui/web && npm run check)
```

Use existing dependencies only. For UI, temporarily link existing `node_modules` if required, then remove the link immediately.

Request two independent read-only reviews: (1) production Router/runtime/recovery semantics; (2) evidence/signature/qualifier claim boundary. Repair confirmed defects with RED tests and rerun all gates.

Create exact handover under `docs/automation/` containing branch/head, commit list, commands/exits/counts, evidence archive path and digest, physical host identities, parity/recovery metrics, qualifier output, residual gaps, and explicit no-merge/no-push statement.

Stage explicit files only. Commit source/tests first and handover last. Do not merge or push.

---

## Expected residual boundary after success

Even if the authority returns `route_ready=true`, that means only this pinned deterministic two-Mac deployment and its validity window passed RouteQualificationV1. It does not imply release readiness, arbitrary model support, Pixel route participation, Internet relay behavior, WAN/NAT resilience, long-duration stability, production throughput, or safety under untested host/runtime combinations. `release_ready` remains false.
