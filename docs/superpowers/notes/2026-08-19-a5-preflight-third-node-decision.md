# A5 Preflight Decision — 2026-08-19 (Evi)

**Third physical placement for A5 replica: the M4 16GB laptop.**

- Host: `evis-macbook-pro-1` @ Tailscale `100.126.111.123`, remote account `evinova`.
- Constraints from memory: local M4 16GB — no heavy compute without approval; remote-accessed via SSH; queue work by memory budget; Python externally-managed on that host.
- 0.5B incumbent (`Qwen/Qwen2.5-0.5B-Instruct`) is small enough for the 16GB budget for ONE stage range replica (stage-local KV, contiguous subset of layers — never the whole model).
- This satisfies A5 spec §4/§10: "at least one replica of a contiguous stage on another eligible physical placement".
- Same-host/correlated-domain caveats do not apply — laptop is a distinct host AND distinct failure domain from both the M4 Pro 48GB (node-0) and the Surface Book (node-2).
- Pre-A5 work still needed: frozen workload corpus, replica_* v1 contracts, planner/flow-solver leaf module, benchmark session bindings.
- Spec: `docs/superpowers/specs/2026-08-18-mycelium-a5-multistage-replication.md` (status: design_only until physical gates pass).
