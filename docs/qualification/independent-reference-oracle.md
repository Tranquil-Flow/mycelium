# Independent reference oracle for the tiny GPT-2 qualification fixture

## Scope and claim boundary

This tranche implements a local numerical reference for the existing generated tiny GPT-2 fixture. It reads `config.json`, `model.safetensors.index.json`, and two local Safetensors artifacts directly and evaluates the model with an implementation owned entirely by `mycelium_reference_oracle/`.

It makes one bounded claim:

> local independent numerical oracle only; no transport, no distributed execution, no physical-host qualification, and no `route_ready` claim

It does not load assignments, partitions, routes, gateways, sidecars, transport code, or either two-process qualification harness. It is not a general GPT-2 loader.

## Closed fixture contract

The parser accepts only the current fixture shape and configuration:

- GPT-2 LM head model
- vocabulary size: 7
- embedding width: 4
- positions: 8
- decoder layers: 2
- attention heads: 2
- MLP inner width: 8
- FP32 tensors
- `gelu_new`
- scaled causal self-attention
- untied LM head
- exactly two Safetensors artifacts

Config keys, tensor names, shapes, dtypes, finiteness, index mapping, declared total size, and artifact count are closed and validated. JSON duplicate keys and non-finite JSON numbers are rejected. Small file-size limits are applied before JSON parsing or tensor loading.

Artifact identity includes:

- raw config digest
- raw checkpoint-index digest
- ordered tensor-artifact digests
- combined tensor-set digest
- semantic tensor-value digest framed by tensor name, dtype, and shape
- model digest binding model ID, resolved commit, config, tensor set, and semantic tensor values

The loader hashes each tensor artifact again after MLX has loaded and evaluated all arrays. A changed artifact is rejected rather than reported under stale evidence.

## Independent numerical path

`gpt2.py` independently implements:

1. token embedding plus positional embedding
2. layer normalization from mean, centered variance, and reciprocal square root
3. fused Q/K/V projection and independent head reshaping
4. scaled dot-product attention with an upper-triangular causal mask
5. attention output projection and residual addition
6. second pre-normalization
7. two-layer MLP with GPT-2 `gelu_new`
8. MLP residual addition
9. final layer normalization
10. untied LM-head projection
11. full-context greedy decoding

No neural-network layer objects or production execution helpers are used.

## Shared primitives

The following primitives remain shared because replacing a tensor backend and Safetensors decoder would turn this bounded numerical oracle into a separate framework project:

- `mlx.core` array allocation, Safetensors parsing, indexing, reshape/transpose, matrix multiplication, reductions, `rsqrt`, `tanh`, `softmax`, and `argmax`
- Python standard-library JSON parsing, canonical JSON encoding, path access, and SHA-256

The oracle shares primitive operations with the runtime environment, but not Mycelium model execution, partitioning, routing, or qualification functions. Source tests enforce an explicit implementation import allowlist.

## Frozen numerical challenge

The GPU-backed challenge uses:

- first-token prompt token IDs: `(1, 2, 3)`
- eight-step prompt token IDs: `(1,)`
- exact eight-token greedy result: `(6, 6, 6, 2, 0, 0, 0, 0)`
- absolute tolerance: `2e-6`
- relative tolerance: `2e-5`

The frozen last-position first-token logits are:

```text
(0.030795037746429443,
 0.030794939026236534,
 0.030794847756624222,
 0.030794737860560417,
 0.03079465590417385,
 0.030794579535722733,
 0.030794454738497734)
```

Tests compare both decoder-layer hidden tensors, including exact expected shapes `(1, 3, 4)`, under the declared tolerances. Separate mutations to the consumed token embedding, attention value projection, and MLP projection must change activation and logit digests. Prompt, config, tensor-set, and model identity mismatches must be rejected.

### Fixture limitation

The current fixture uses affine ramp weights and has extremely narrow final-logit margins. Its exact greedy sequence differs between the MLX GPU and CPU backends even though hidden-state numerics remain close. Qualification report generation therefore requires the MLX GPU backend and records the exact device and MLX package version.

This limitation belongs to the current fixture, which this tranche is intentionally forbidden to redesign. In particular, the fixture is weak at distinguishing causal-mask or attention-score-scaling mutants because normalized rows are nearly collinear. This oracle still provides independent coverage of artifact parsing, embeddings, layer norms, attention value flow, projections, residuals, MLP/GELU, final norm, LM head, full decode, and identity binding. A future nondegenerate fixture should be a separate tranche, not silently substituted here.

## Canonical report

A qualification report requires all of the following external bindings:

- frozen prompt digest
- frozen config digest
- frozen model digest
- exact expected eight-token sequence
- exactly eight decode steps

`qualified: true` is emitted only after all bindings and exact token IDs match. CPU report generation is rejected for this fixture. The report uses an exact allowlisted schema, and canonical serialization rejects added fields.

Report contents include:

- protocol and claim boundary
- implementation name/version, source digest, and implementation identity
- MLX backend, version, device, and FP32 dtype
- config, checkpoint index, tensor artifact, tensor set, semantic tensor-value, and model digests
- prompt digest and token count
- each generated token ID
- each step's complete-logit digest and two decoder-layer activation digests
- declared absolute and relative tolerances

It contains no prompt text, model ID, resolved commit, credentials, filesystem paths, tensor values, logits, activations, or model weights.

The prompt digest is a deterministic binding required by the qualification protocol, not an anonymity mechanism. With this seven-token vocabulary and a short prompt, dictionary recovery is practical. Treat the report as disclosure-minimized evidence, not as proof that the prompt cannot be inferred.

Example:

```python
from mycelium_reference_oracle.init import (
    build_report,
    canonical_report_json,
    load_gpt2_fixture,
)

oracle = load_gpt2_fixture(
    local_model_root,
    model_id="local/tiny-gpt2-qualification",
    resolved_commit="0123456789abcdef0123456789abcdef01234567",
    expected_config_digest=(
        "sha256:05bb0ae803af9010a31517c3e20b3b1d958a5edcc4a866d1189a2cc00bb02310"
    ),
    expected_tensor_set_digest=(
        "sha256:6dd2fc5f64df4eaeeb515ecdaaab859c94d3ca4a91490d5643d138adcb442bb0"
    ),
    expected_model_digest=(
        "sha256:694a1f6113bc2c6fbb312f42a114b7c56ff7708ef70d68ce437031ba21756729"
    ),
)
report = build_report(
    oracle,
    (1,),
    steps=8,
    expected_token_ids=(6, 6, 6, 2, 0, 0, 0, 0),
    expected_prompt_digest=(
        "sha256:9ffdd78c7d7eb75b7c2e088fc79e845d4dae354824e2bd0be3e2e0be4eaf7325"
    ),
    expected_config_digest=oracle.identity.config_digest,
    expected_model_digest=oracle.identity.model_digest,
)
print(canonical_report_json(report), end="")
```

## Verification

```bash
python3.14 -m pytest -q tests/reference_oracle
python3.14 -m compileall -q mycelium_reference_oracle tests/reference_oracle
python3.14 -m pytest -q
python3.14 scripts/contract_audit.py
git diff --check
```

The focused suite records the TDD surface: deterministic parsing, frozen first-token logits and greedy token, both per-layer hidden states, exact eight-step decode, three numerical mutation classes, identity mismatch rejection, report schema/privacy constraints, and source-level production independence.
