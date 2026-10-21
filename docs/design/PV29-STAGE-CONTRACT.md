# PV29 stage contract (ADR): StageCapabilities, `llama_stage`, quantization ids, wire dtype rules

Status: **frozen at lane L0 (contract) + L2 (wire/dtype hardening); owner answers pending.**
Plan: `status/plans/PV29-LINUX-WINDOWS-PLAN-20261021.md` (sections 0, 1, 2, 4, 7).
Code: `mycelium_platform_neutral/stage_capabilities.py`, `mycelium_router/wire_dtype.py`,
`mycelium_platform_neutral/primitives.py` (registry), `mycelium_a13_macos/serving.py` (negotiation seam).
Tests: `tests/stage_contract/`, `test_router_wire_dtype.py`, `test_router_wire_golden.py`.

## Claim boundary

This freezes a **contract**. It is not Linux or Windows support, not a `llama_stage` implementation,
not qualification, not route readiness, and not performance eligibility. No device ran anything for this
change. `llama_stage` is a registered identity that cannot be selected.

## Owner decisions: defaults recorded, **default pending owner**

| Q | Question | Default used here | Where it is enforced |
|---|---|---|---|
| Q1 | Backend direction | **A: patched llama.cpp `llama_stage`** (pinned patch series, ctypes C ABI, GGUF slices). Own-ggml (B) stays the documented fallback. | Registry id `llama_stage`, fenced. |
| Q2 | Linux/Windows owner mode in PV29? | **Members only on Linux/Windows for V2.** Owner mode (create swarm, relay, invites) is lane L11, optional, not started. | Nothing here changes owner paths. |
| Q3 | Lossy stages (K-quants)? | **Only lossless or Q8_0/int8 stages are admitted.** `gguf-q4_k_m` is refused unless a future explicit owner opt-in flag is passed. Flag name: `allow_lossy_stage_quantization`, keyword-only, strictly boolean, **default off**. | `admit_stage_quantization`, `StageCapabilities.admitted_quantizations` |
| Q4 | Windows thermal unknown | **Keep today's fail-closed Windows thermal refusal.** The MHz-ratio proxy is not accepted. | Not touched by this lane. |

Changing a default is an owner decision plus a one-line change here; none of Q1-Q4 is treated as settled.

## 1. Backend registry

`primitives.RUNTIME_BACKEND_STAGES` gains `llama_stage: "fenced"`. The registry stays closed: an
unregistered id is rejected, never defaulted. `fenced` is deliberately not `implemented`, so
`class_qualification` (requires `implemented`) cannot qualify it.

- `STAGE_BACKENDS = {mlx, numpy, llama_stage, mobile-rust-qwen2}`. `llama_http` is **not** a stage backend: it
  is a whole-model replica path (plan section 1.2) and `require_stage_backend("llama_http")` refuses it.
- Selectable only when present: `stage_backend_selectable("llama_stage")` is true only if
  `mycelium_router.llama_stage_runtime` exists (spec lookup, never imported) or the caller names it in
  `runtime_present`. Today the module does not exist, so it is false. `serving.STAGE_RUNTIME_BACKENDS` is
  unchanged (`mlx`, `numpy`), so no operator plan can name it, and `negotiate_route_wire_dtype` refuses a
  route containing it (`fenced_stage_backend_unavailable`).

## 2. Quantization ids (plan 2.4)

Frozen order and vocabulary: `none`, `int8-weight-only`, `gguf-f16`, `gguf-q8_0`, `gguf-q4_k_m`.
Weight formats map onto them: `safetensors-bf16 -> none`, `int8-rowwise -> int8-weight-only`, `gguf-*` identity.
Quantization is local and declared; it is bound into the stage signature and load proof and never appears on
the wire (R-W4). Mixed `int8-weight-only` and `gguf-q8_0` stages are different numerics by design: they are
judged by the parity gate (lane L1), not by byte equality. `runtime_contracts._SUPPORTED_QUANTIZATIONS` is
unchanged: the new ids are contract vocabulary only until a backend that executes them is registered.

## 3. `StageCapabilities` (`mycelium.stage_capabilities.v1`)

Closed record, 16 fields: `protocol, backend, backend_version, build_digest, device, device_name,
architectures, weight_formats, wire_dtypes_in, wire_dtypes_out, decode_modes, kv_checkpoint_format, batching,
max_ctx, memory_footprint_estimator, deterministic`.

- **Closed**: unknown or missing keys are refused (`stage_capabilities_invalid`); every field has its own
  refusal code. `device` is `cpu | metal | cuda:N | vulkan:N`.
- **Canonical**: list fields must already be strictly ascending and duplicate-free, so
  `from_mapping(m).canonical_bytes() == canonical(m)` with no normalisation step. Bytes are the repository's
  canonical JSON (sorted keys, no whitespace, UTF-8, no NaN). `digest()` is `sha256:` of those bytes.
  A pinned byte vector in the tests freezes field set, names and encoding.
- **Backend rules**: `numpy` and `mobile-rust-qwen2` declare `float32` only on the wire; `kv_checkpoint_format`
  is `null` or the declaring backend's own format (`numpy-kv-v1`, `mlx-kv-v1`, `llama-seq-v1`).
- Not frozen here: the `StageRuntime` protocol and conformance suite (L3), dynamic capabilities in the
  Linux/Windows member classes (L5), the signed capability report wiring.

## 4. Wire dtype rules (plan 2.3, R-W1..R-W3)

`MYAC` v1 is unchanged (`>4sBBBBQ` header, u32 dims, little-endian, codes `float16=1, bfloat16=2,
float32=3`). Existing golden frames are unchanged.

- **R-W1 route-wide dtype.** `negotiate_route_wire_dtype(capabilities, requested=None, enabled=("float32",))`
  returns one dtype that is in every stage's `wire_dtypes_in` and `wire_dtypes_out`. Default and only enabled
  value is `float32`. There is no automatic downgrade: an unsatisfiable request is refused
  (`wire_dtype_negotiation_failed`, `wire_dtype_not_enabled`).
- **Mixed-backend routes use `float32` only** (`mixed_backend_route_requires_float32`). `float16`/`bfloat16`
  remain PV28: own opt-in, own gate.
- **Where it is wired.** `serving._validate_operator_plan` negotiates over the declared backend of every node
  and requires the result to equal the registered model's `runtime_dtype`; a mismatch is
  `operator_plan_wire_dtype_mismatch`. That is the one place a route fixes its hop dtype today.
- **R-W2 no silent coercion.** `mycelium_router/wire_dtype.py` is the only float<->wire conversion:
  non-finite values are refused (`nonfinite_activation`), values beyond the target range are refused, never
  saturated or turned into `inf`: `|x| > 65504` for float16 (`activation_float16_overflow`), `|x| >` the largest
  finite bfloat16 for bfloat16 (`activation_bfloat16_overflow`), `|x| >` the largest finite float32 for a
  float64 source. The plan's looser `6e4` heuristic is superseded by the exact format limit.
- **R-W3 bf16 is raw u16 bits.** Encode is round-to-nearest-even from float32 (bitwise equal to
  `activation_transport.encode_bfloat16`); decode is exact widening. bf16-representable values round-trip
  exactly.
- **NumPy bf16 mapping bug (plan section 1).** `_NUMPY_DTYPES["bfloat16"]` mapped to `np.float32` while the wire
  carries 2 bytes per element. Confirmed behaviour at `99d24e17`: a bf16 payload fails with an uncaught
  `ValueError: cannot reshape` in `_decode_input`, a bf16 output fails with `TypeError` (`np.dtype("bfloat16")`)
  and, had the buffer sizes ever aligned, two bf16 values would be read as one garbage float32. It was
  unreachable through the validated runtime path because `SUPPORTED_NUMPY_DTYPES = {float32}` rejects the
  dtype first, and it never silently corrupted a float32 route. It is fixed so the next lane that lets a
  non-float32 wire reach a NumPy or llama stage does not inherit it.
- **R-W5, R-W6, R-W7** (shared greedy rule, backend-local KV, size budget) are unchanged and unaffected.

`SUPPORTED_NUMPY_DTYPES` stays `{float32}`: fixing the codec does not enable reduced-precision NumPy routes.

## 5. Golden `MYAC` vectors for C

`scripts/generate_router_wire_golden.py` additionally writes `contracts/router-activation-golden/`
(separate directory; the 11 existing router frames, `index.json` and the generator's stdout are byte-for-byte
unchanged):

- valid frames for `float32`, `float16`, `bfloat16` (ordinary values, signed zero, format extremes) and
  rank 1/2/3 shapes; reject frames for each decoder refusal class (magic, version, dtype code, byte order,
  rank, truncation, length mismatch, oversize claim);
- `index.json` (canonical) and `vectors.tsv`, a tab-separated file a C program can read with `fgets`;
- `myac_decode_check.c`, a dependency-free C99 decoder that reads `vectors.tsv`, checks every valid frame's
  element bytes and every reject frame's error code. It is compiled and run by the tests when a C compiler is
  present (skipped, visibly, otherwise).

## 6. Pinned files touched

These are hash-pinned; they were **not** re-pinned by this lane and their pin checks will report drift until the
owning lane re-pins:

- `artifacts/a6/source-manifest-v1.json` pins `mycelium_router/numpy_runtime.py` and
  `mycelium_a13_macos/serving.py` (both edited).
- `contracts/contract-manifest.v1.json` pins `scripts/generate_router_wire_golden.py` (edited).
- Member runtime closure (`mycelium_member_runtime.RUNTIME_PATHS`): none of its files were touched.

## 7. Unverified (carried from plan section 7)

fp16 overflow behaviour on real Qwen2.5 activations (the guard is exact-range and format-level, it says
nothing about whether real activations stay in range); all provisional parity thresholds; llama.cpp
loader-skip and graph-reuse behaviour under the planned patches; `llama_stage` itself does not exist yet.
