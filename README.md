# Mycelium

Mycelium is a privacy-first distributed-inference system for assigning model stages to heterogeneous peers, provisioning assignment-specific artifacts, routing activations, and qualifying physical execution with explicit evidence.

## Current claim boundary

The repository contains working Planner, provisioning, Gossip, Router, batching, transport-spike integration surfaces, tests, and a read-only Network Observatory UI.

The strongest completed multi-host claim is two-peer, assignment-specific artifact provisioning. An offline local qualification now also demonstrates two independent spawned Python processes loading and probing disjoint, assignment-bound MLX stages from a generated sharded GPT-2 checkpoint. All of this evidence retains `route_ready: false`; Layer Builder integration, activation transfer, physical distributed prefill/decode, and route qualification remain under construction.

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

The MVP is not complete until a physical multi-peer qualification demonstrates real stage computation and deterministic token parity with a monolithic reference.
