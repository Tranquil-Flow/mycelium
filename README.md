# Mycelium

Mycelium is a privacy-first distributed-inference system for assigning model stages to heterogeneous peers, provisioning assignment-specific artifacts, routing activations, and qualifying physical execution with explicit evidence.

## Current claim boundary

The repository contains working Planner, provisioning, Gossip, Router, batching, transport-spike integration surfaces, tests, and a read-only Network Observatory UI.

The strongest completed physical claim is two-peer, assignment-specific artifact provisioning. Existing evidence retains `route_ready: false`. Real assignment-bound tensor loading, Layer Builder integration, physical distributed prefill/decode, and route qualification remain under construction.

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

Network Observatory:

```bash
cd ui/web
npm ci
npm run check
```

The MVP is not complete until a physical multi-peer qualification demonstrates real stage computation and deterministic token parity with a monolithic reference.
