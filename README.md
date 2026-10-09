# Mycelium

Mycelium is a distributed-inference system for assigning model stages to heterogeneous peers, provisioning assignment-specific artifacts, routing activations, and qualifying physical execution with explicit evidence. It is the infer## Current status (updated 2026-10-09)

Mycelium is a research prototype. It currently assumes trusted, operator-controlled devices. It is not a public network, and it does not hide prompts or activations from the devices that run model stages.

`main` contains the Planner, provisioning, Gossip, Router, batching, transport, and read-only Network Observatory surfaces. The physical multi-device results below were produced on integration branches that are not yet merged into `main`. The canonical status record is `docs/handover/CURRENT_AND_PLANNED_ARCHITECTURE.md` on branch `w6-27b-native-adapter`.

Physical results (all models int8 weight-only, pipeline-split across the operator's own devices):

- **Qwen2.5-0.5B-Instruct:** distributed requests served through the browser product across two physical hosts (Apple M4 Pro / MLX -> Microsoft Surface / NumPy), and across three hosts with stage replicas. An earlier three-device route measured about 0.75-0.82 tokens/s.
- **Qwen2.5-3B-Instruct:** a three-host physical route completed browser inference.
- **Qwen2.5-7B:** qualified over the M4 Pro / MLX -> Surface / NumPy route, streaming a real browser answer; retained as a qualified standby.
- **Across networks:** internet-native control and activation transport completed ordinary inference over direct and forced-relay paths with an external peer on an unrelated network, without Tailscale (`docs/handover/A8_STATUS_2026-08-21.md` on branch `codex/a8-internet-native`).

Limitations:

- The runs above are retained, evidence-sealed observations, not current live health. Deployments must be freshly qualified before admitting new requests.
- Pipeline (contiguous stage) parallelism only; no tensor parallelism.
- Model adapters cover GPT-2, Qwen2, Qwen3, and Qwen3.5 architectures, and each model must be qualified before use. Qwen2.5-1.5B timed out during decode and must be requalified.
- Request-level stage replicas worked end to end, but the speed-up benchmark was inconclusive (+16.7% point estimate, 95% lower bound -42%), so the feature was not promoted.
- Throughput is early and has not been optimised.

Next: running a ~27B-class model, starting with Qwen3.8-27B. A native 4-bit route for the Qwen3.5 27B architecture is on branch `w6-27b-native-adapter`; it is unit-tested but not yet run on physical hardware.

nd route qualification remain under construction.

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

## License

Mycelium is licensed under the GNU Affero General Public License v3.0 or later (AGPL-3.0-or-later). See [LICENSE](LICENSE).
