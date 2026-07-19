# Mycelium

Mycelium is a privacy-first distributed-inference system for assigning model stages to heterogeneous peers, provisioning assignment-specific artifacts, routing activations, and qualifying physical execution with explicit evidence.

## Current claim boundary

The repository contains working Planner, provisioning, Gossip, Router, batching, transport-spike integration surfaces, tests, and a read-only Network Observatory UI.

The strongest production-path inference claim remains the same-host native-iroh harness: two independent Python Router processes and two independent native iroh sidecars load disjoint, assignment-bound MLX stages, exchange canonical Router frames over authenticated iroh, and match a single-process reference. The harness additionally verifies distributed cancellation cleanup, fail-closed peer loss, and successful inference after full process and endpoint restart.

A separate live physical harness now runs a three-stage tiny-GPT-2-shaped split across two independent local MLX subprocesses and a Pixel 8 Pro Termux process. The phone executes a decoder substage derived from a deterministic, locally generated fixture; its dependency-free worker binds the exact tensor subset and supported attention/activation configuration to parent-assignment, parent-load-proof, pack, and worker-source digests. Across two four-token lifecycles, every phone hidden-state element and final logit matches the separate MLX reference within `1e-6`; round-to-even `1e-5`-quantized token selection also matches. The harness rejects bad authentication and a mismatched request assignment, proves the old phone PID exited and its endpoint became unreachable, then passes after worker restart. This proves physical Mac -> Pixel -> Mac stage execution, not production Router routing: transport is `stdin/http/stdin`, the Mac derives and provisions the phone subpack, the generic bootstrap bridge credential is RCE-equivalent, live ADB identity was unavailable for the final run, device authority evidence has not passed the production qualifier, and `route_ready` remains `false`.

Both claims remain bounded. The production Router proof shares one host and a local SQLite lease coordinator. Generation/topology rotation, multi-host native-iroh routing, token-continuity recovery, and production authority qualification remain open.

## Architecture direction

```text
Gossip -> Planner -> assignments -> provisioning -> runtime load proofs
       -> Layer Builder -> Router -> physical stage execution -> qualification
```

Control-plane records never carry model tensors or protected implementation edits. Peers download assignment-specific artifacts directly when possible. Runtime readiness and route readiness remain distinct.

## Repository boundaries

- Generated model caches, run directories, local evidence, internal handovers, and planning files remain untracked.
- Curated evidence must be redacted, immutable, and manifest-addressed before entering version control.
- Secrets and device tokens must never be committed.
- The Network Observatory remains read-only; request submission is a separate control surface.

## Verification

Python baseline:

```bash
python3 -m pytest -q
```

Offline two-process MLX runtime-load qualification:

```bash
python3 two_process_runtime_qualification.py --json
```

The command generates all model artifacts locally, provisions through an injected local-only fetcher, uses `multiprocessing` with the `spawn` start method, and emits JSON-safe child load proofs. A Python audit guard rejects socket/DNS events from immediately before each assignment load until child exit; this is not an OS-level network sandbox. It does not claim distributed inference, route readiness, activation transfer, or simultaneous/post-exit device-memory residency.

Network Observatory:

```bash
cd ui/web
npm ci
npm run check
```

The MVP remains incomplete until a physical multi-peer production-Router qualification demonstrates route-ready stage computation, token continuity through peer failure, and qualified device authority.
