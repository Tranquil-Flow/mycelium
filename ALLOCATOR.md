# Mycelium Layer Planner / Allocator v1

Goal: turn the broadcast layer's `GET /nodes` output into an initial contiguous layer route.

This is a heuristic bootstrap allocator, not final runtime scheduling.

Product V1 is now implemented side-by-side in `mycelium_layer_planner/` and
emits deterministic `mycelium.route_plan.v2` placement intent. Its architecture
and implementation record are `LAYER_PLANNER_PRODUCT_V1.md` and
`docs/plans/2026-07-15-layer-planner-product-v1.md`. The legacy
`layer_planner.py` and `mycelium.route_plan.v1` contract remain unchanged.

Product V1 CLI:

```bash
python3 -m mycelium_layer_planner \
  --snapshot scenarios/product-v1-replicated.json \
  --output outputs/product-v1-route-plan.json
```

The Product V1 package reads activation/KV dimensions from machine-readable
model configuration, scores prefill and decode separately, freezes primary
layer order before replication, and emits independent/cross-connected legal
decode tracks. Primary search is bounded by a deterministic candidate-evaluation
budget recorded in provenance; exhausted searches cannot claim global exactness.
It remains placement intent: no weights-loaded or runtime-ready claim is made.

## Files

- `layer_planner.py` — stdlib-only allocator module + CLI.
- `test_layer_planner.py` — allocator regression tests.
- `outputs/layer-plan-llama32-3b.json` — current live route artifact.

## Inputs

Allocator consumes Mycelium broadcast JSON:

```bash
curl http://<seed>:8788/nodes > outputs/m4pro-router-nodes.json
```

Then plan:

```bash
python3 layer_planner.py \
  --nodes-file outputs/m4pro-router-nodes.json \
  --model-id llama-3.2-3b-ish \
  --num-layers 28 \
  --hidden-size 3072 \
  --context-length 2048 \
  --batch-size 1 \
  --out outputs/layer-plan-llama32-3b.json
```

Can also read directly from router:

```bash
python3 layer_planner.py \
  --nodes-url http://100.84.252.4:8788/nodes \
  --model-id llama-3.2-3b-ish \
  --num-layers 28 \
  --hidden-size 3072
```

## ModelSpec fields

- `model_id`
- `num_layers`
- `hidden_size`
- `weight_bytes` default `2`
- `activation_bytes` default `2`
- `context_length` default `2048`
- `batch_size` default `1`
- `compression_ratio` default `1.0`
- `min_memory_reserve_gb` default `2.0`
- optional `layer_weight_gb`
- optional `kv_cache_gb_per_layer`
- `required_backends`

If exact layer memory is unknown, planner estimates:

- layer weights ≈ `12 * hidden_size^2 * weight_bytes`
- per-layer KV cache ≈ `2 * batch * context * hidden_size * activation_bytes`
- per-layer memory = layer weights + KV cache

For real model support, pass exact `--layer-weight-gb` and/or `--kv-cache-gb-per-layer` once known from model config.

## Eligibility rules

A node is layer-eligible when:

1. it is on AC power, unless `--allow-battery` is set
2. it exposes at least one required backend
3. it has enough available memory after reserve for at least one layer

Phones are eligible by default. Pass `--disable-phone-layers` to exclude them.
The former `--allow-phone-layers` spelling remains as a hidden compatibility
alias but is no longer necessary.

Default required backend set:

- `mlx`
- `torch_mps`
- `cuda`
- `torch_cuda`
- `llama_cpp`
- `mlc_llm`
- `onnxruntime`

Current ineligible reasons include:

- `not_on_ac_power`
- `phone_layers_disabled`
- `no_required_backend`
- `insufficient_memory`

## Allocation policy

v1 policy:

1. Convert each node into a `CandidateNode`.
2. Estimate per-layer compute time from memory bandwidth × backend efficiency × device-class efficiency.
3. Estimate max layer count from available RAM minus reserve.
4. Choose widest feasible set up to `--max-nodes`.
5. Dynamic-program contiguous layer counts across that set to minimize stage bottleneck.
6. Test permutations of node order and add estimated transfer penalty between adjacent stages.
7. Emit contiguous ranges `[start_layer, end_layer]`.

Important: v1 intentionally behaves like an allocator. It prefers using eligible nodes up to `max_nodes` when feasible instead of collapsing to one fast node. Later network-aware pruning can disable bad links after real pairwise measurements exist.

## Output shape

`mycelium.route_plan.v1`:

```json
{
  "ok": true,
  "protocol": "mycelium.route_plan.v1",
  "model": {...},
  "node_order": ["m4pro"],
  "route": [
    {
      "node_id": "m4pro",
      "layers": [0, 27],
      "layer_count": 28,
      "backend": "metal",
      "estimated_compute_ms": 28.2805,
      "memory_bandwidth_gbps": 273.0,
      "ram_available_gb": 22.73
    }
  ],
  "estimated_decode_ms_per_token": 28.2805,
  "estimated_pipeline_compute_ms": 28.2805,
  "estimated_transfer_ms_per_token": 0,
  "ineligible": {
    "4b982606bdf4": ["no_required_backend"],
    "android-47031FDJG000RD": ["phone_layers_disabled", "no_required_backend"]
  },
  "diagnostics": {...},
  "claim_boundary": "heuristic_initial_layer_allocation_from_tier1_profiles; not benchmarked real inference"
}
```

## Current live result

Against current M4 seed registry:

- `m4pro`: eligible, receives layers `[0, 27]`
- Linux container: ineligible, no required backend
- Pixel 8 Pro ADB profile: phone-eligible, but still requires a detected runtime
  backend and must satisfy the normal power and memory rules

Optional backend bootstrap:

```bash
python3 install_serving_backend.py --profile-file outputs/pixel8-adb-profile.json
```

See `SERVING_BACKEND.md`. Installation is a dry run unless `--execute` is used
on the target device.

Artifact:

- `outputs/layer-plan-llama32-3b.json`

## Verification

```bash
python3 -m py_compile probe.py probe_android_adb.py mycelium_broadcast.py layer_planner.py test_broadcast.py test_layer_planner.py
python3 -m unittest -v
```

Current result:

```text
Ran 7 focused allocator tests
OK
```

## Claim boundary / next steps

Implemented:

- stdlib allocator
- CLI
- model spec estimation
- eligibility filtering
- memory capacity checks
- contiguous layer range allocation
- multi-node balancing tests
- live route artifact from M4 seed

Not implemented yet:

- real per-node layer microbenchmarks
- real pairwise latency / jitter / bandwidth probes
- backend-specific model support checks
- runtime route executor
- dynamic rebalancing after memory/network drift
- privacy-preserving/signed capability claims

Next best improvement: add pairwise probe service and feed measured link matrix into allocator instead of using location/upload heuristics.
