# Second runtime selection (Task 3B.5, steps 1–3)

Date: 2026-07-22
Host: Apple Silicon macOS 15.6.1, Python 3.14.2

## Scope and claim boundary

This tranche selects and proves one non-MLX runtime for monolithic GPT-2 parity. It does not prove cross-runtime stage transport, distributed decode, Android execution, or route readiness. All emitted NumPy runtime identities keep `route_ready=false`.

## Reproducible candidate probe

No package was installed or built for this selection. The selected dependency was already present. The exact import probe was:

```bash
/opt/homebrew/bin/python3.14 - <<'PY'
import importlib.metadata as m, time
for name, dist in (("numpy", "numpy"), ("torch", "torch"), ("onnxruntime", "onnxruntime")):
    started = time.perf_counter()
    __import__(name)
    elapsed_ms = (time.perf_counter() - started) * 1000
    print(name, m.version(dist), elapsed_ms)
PY
```

Measured results from one cold process:

| Candidate | Version | Import time | Installed package bytes | Result |
|---|---:|---:|---:|---|
| NumPy | 2.4.3 | 139.904 ms | 31,345,080 | Selected |
| ONNX Runtime | 1.24.4 | 139.551 ms | 70,673,037 | Rejected for this tranche: requires a separate graph-export/model-conversion contract |
| Torch | 2.10.0 | 1,855.577 ms | 385,847,187 | Rejected for this tranche: much larger import and package footprint |
| ExecuTorch | absent | n/a | n/a | Blocked locally |
| WebGPU (`wgpu`) | absent | n/a | n/a | Blocked locally |

Exact unavailable-candidate import traces:

```text
ModuleNotFoundError: No module named 'executorch'
ModuleNotFoundError: No module named 'wgpu'
```

NumPy was selected because it can consume the current verified safetensor arrays directly, adds no graph export or build phase, and has the smallest installed footprint among locally runnable candidates. This is a macOS CPU proof, not a deployment recommendation for phones or Linux laptops.

## Implemented interface and parity proof

- `runtime_contracts.py` adds backend-neutral `StageRuntimeBackend` and `MonolithicRuntimePort` protocols plus strict runtime dispatch.
- Existing MLX validation remains backend-specific and unchanged at its public entrypoint.
- `runtime_loader.MLXStageBackend` adapts the existing authenticated MLX stage executor without changing load proofs.
- `numpy_runtime.NumpyGPT2Runtime` implements strict monolithic full-context GPT-2 on CPU. It rejects missing/extra tensors, shape mismatches, unsupported dtypes, non-finite weights, invalid token shapes, and out-of-range token IDs.

Main-host same-seed/same-config/same-token parity result:

```text
max_abs_diff 1.4901161193847656e-08
mean_abs_diff 4.99284791288801e-09
argmax_equal True
```

Full Main-native regression gate after implementation:

```text
1985 passed, 3 skipped, 121 subtests passed
```

## Deferred gates

Before any heterogeneous distributed claim, later work must still:

1. integrate a non-MLX assignment-bound stage loader and signed load proof;
2. transport one activation across MLX→NumPy and NumPy→MLX stage boundaries;
3. prove multi-token parity under the frozen tolerance policy;
4. publish qualification evidence through the sole authority.
