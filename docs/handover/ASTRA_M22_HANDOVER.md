# Astra current handover

Mycelium now serves Qwen2.5-3B-Instruct through a qualified, three-host physical
pipeline over EndpointID-authenticated Iroh. Layers are contiguous: macOS/MLX
`[0,22)`, macOS/MLX `[22,35)`, and Linux/NumPy `[35,36)`. Browser prompts stream
real model output; every stage advances counters. Incremental decode retains
stage-local KV state and reduces measured token latency by 88.64% versus complete
context replay. Prompt/output history survives navigation and refresh in its tab.

The live demo is registry-driven rather than fixture-pinned. Its model selector is
populated from qualified deployments (currently Qwen2.5 0.5B and 1.5B), and a
selection changes the active route atomically. The Nodes workspace reads durable
seed membership, mints signed short-lived enrollment bundles for arbitrary native
devices, and permits safe revocation of standby members. Joining runs on the target
device so its identity and capabilities cannot be forged by the browser.

Start here:

- Current and planned architecture: `docs/handover/CURRENT_AND_PLANNED_ARCHITECTURE.md`
- Governing product architecture: `docs/superpowers/specs/2026-08-09-mycelium-astra-architecture-product-design.md`
- Latest physical/KV evidence: `docs/handover/M23_PROGRESS_2026-08-11.md`
- Operator runbook: `docs/live-mvp-operator-runbook.md`
- External reviewer procedure: `docs/release/astras-macbook-reviewer.md`
- Release UI contract: `docs/release/m22-ui-requirements.v1.json`

The UI exposes membership, directed topology, contiguous allocation, runtime
admission, local-model feasibility, recovery/replication/speculation evidence,
release gates, and live per-stage KV state in product language. Qwen3-8B is locally
complete and its dense adapter passes MLX/NumPy, int8, parity, and stage-local-KV
tests, but selection remains correctly blocked by current safe contiguous capacity.
No model download is authorized implicitly.

Release remains withheld for one honest reason: the reviewer route passed from a
separately enrolled Mac on the same hotspot, not a genuinely different network.
Repeat the reviewer procedure externally before changing that gate.

Future decisions remain separately scoped: continuous batching, autoscaling,
tensor/hybrid parallelism, and quantized Qwen3 qualification.
