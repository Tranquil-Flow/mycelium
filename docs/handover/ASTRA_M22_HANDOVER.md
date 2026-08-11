# Astra M22 handover

Mycelium now runs qualified contiguous pipeline inference over three physical hosts
and two runtime classes using EndpointID-authenticated Iroh. The measured M22 route
allocates Qwen2.5-3B-Instruct layers `[0,22)`, `[22,35)`, and `[35,36)`; a browser
request returned a useful seven-token answer and advanced every stage’s counters.
Prompt/output history survived workspace navigation, refresh, and supervisor restart.

- Governing architecture: `docs/superpowers/specs/2026-08-09-mycelium-astra-architecture-product-design.md`
- M22 closure spec: `docs/superpowers/specs/2026-08-11-mycelium-m22-release-closure.md`
- Operator runbook: `docs/live-mvp-operator-runbook.md`
- Reviewer entry point: `docs/release/astras-macbook-reviewer.md`
- Release UI audit: `docs/release/m22-ui-requirements.v1.json`
- Milestone commits: M12 `27a3478`, M13 `d6249c1`, M14 `2ff804d`, M15 `7d3b420`, M16 `b565e0d`, M17 `e739f54`, M18 `0761a97`, M19 `a6294aa`, M20 `26d863a`, M21 `b7f01e2`, M22 baseline `4b8be84`; managed closure is this commit.
- Private operator evidence: `m22-release-20260811/m22-release.json`, SBOM, transport matrix, bounded plan, service packages, and reviewer bundle.

Qwen3-8B is locally complete and its dense `qwen3` adapter is verified for manifest
ownership, MLX/NumPy parity, int8 weight-only execution, and stage-local KV. It remains
correctly blocked because the measured swarm cannot fit a safe exact contiguous
allocation. Managed launchd/systemd child recovery, persistent restart budgets,
coordinator restart, three-member renewal, and post-restart inference are digest-bound
and verified. The release gate remains withheld only until an external-network
surrogate reviewer passes.

Future decisions remain separately scoped: stronger incremental KV, continuous
batching, autoscaling, tensor/hybrid parallelism, and quantized Qwen3 qualification.
