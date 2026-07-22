# Mycelium

Mycelium is a privacy-first distributed-inference system: it assigns model layers
to heterogeneous peer devices by capacity, provisions each device only the weights
it needs, routes activations device-to-device, and streams tokens back.

## Current state

Working today:

- Capacity-aware layer planning across arbitrary device topologies
- Request and inter-layer routing with attempt fencing and recovery
- Peer discovery and evidence gossip
- Native iroh transport between independent processes
- Sharded MLX inference matching a single-process reference (same host)

Not working yet:

- Inference across two physically separate machines
- Devices that are not Apple Silicon holding real layers
- Peers on different networks
- A coherent pretrained model end-to-end (currently a tiny local fixture)

Component designs are authoritative in [`ALLOCATOR.md`](ALLOCATOR.md),
[`GOSSIP_PROTOCOL.md`](GOSSIP_PROTOCOL.md),
[`BROADCAST_PROTOCOL.md`](BROADCAST_PROTOCOL.md),
[`REQUEST_AND_INTER_LAYER_ROUTER_DESIGN.md`](REQUEST_AND_INTER_LAYER_ROUTER_DESIGN.md),
[`FAULT_TOLERANT_LAYER_REPLANNER.md`](FAULT_TOLERANT_LAYER_REPLANNER.md), and
[`LAYER_PLANNER_PRODUCT_V1.md`](LAYER_PLANNER_PRODUCT_V1.md).

`route_ready` is `false`. Nothing in this repository claims qualified physical
execution.

Active development plan (local, not tracked): `~/Desktop/mycelium-demo-plan.md`

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

Interactive browser swarm:

```bash
python3.14 scripts/interactive_swarm_server.py
```

Open the emitted `operator_url`, create one join link, and open that link in the contributing browser. Non-loopback use requires HTTPS (direct TLS or a reverse proxy) and an explicit `--public-origin`; see the linked interactive guide. This remains local evidence with `route_ready=false`.

The MVP remains incomplete until a physical multi-peer production-Router qualification demonstrates route-ready stage computation, token continuity through peer failure, and qualified device authority.
