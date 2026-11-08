# Native dense stage-shard preparation

`prepare_shard_v1` is an additive private worker command. Existing `prepare`,
`step`, `resident_proof_v1` and `close` bytes remain compatible. Its versioned
contract and refusal codes live in `stage-contract::native_shard`.

## Trust and load boundary

1. The compiler's B2 producer writes exact source-derived safetensors through the
   B3 storage reservation ledger; partial or failed outputs retain their charge.
2. The compiler independently reopens source/shards and recompiles the slice
   recipes before worker admission. Transfer rows and node recipes are retained.
3. Each child receives only its stage filename, transfer row and pinned recipe.
   CPU admission checks range/node/revision binding, recipe digest, transfer size
   and hash, exact BF16 tensor names/shapes/coverage, inline header bytes, complete
   segment coverage/hashes and bounded source references. Source recipe paths are
   metadata, never opened by the worker.
4. A pre-exec macOS sandbox denies the full source root, Hugging Face cache and
   other safetensors; only the assigned stage file is excepted. This isolates the
   local loader's model inputs, not arbitrary hostile native code or the host OS.
5. Verification precedes Context creation and repeats around native loading.
   Resident proofs still hash evaluated native arrays, not disk bytes. The same
   independent Qwen3 golden workload checks the four greedy challenge tokens.
6. The signed native-local operator-plan v2 binds per-stage transfer/recipe pins;
   the retained handoff checks those pins and holds the shard files as well as
   the original source-provenance and binary handles. The owner may still hold
   the source checkpoint; the individual stage worker does not read it.

Tamper identities are `native_shard_transfer_hash_mismatch`,
`native_shard_length_mismatch`, `native_shard_recipe_mismatch`,
`native_shard_binding_mismatch` and `native_shard_tensor_mismatch`.
A physical swap with unequal lengths is a length mismatch; swapping an entire
stage contract is a binding mismatch. These precede native allocation.

## Qualification and remaining work

The model-free `backend-mlx::stage_shard` fixture uses labelled zero-filled
layout bytes (no native model proof). `api::native_shard_sandbox` tests the exact
production pre-exec profile with real subprocess reads. The ignored headless
model tests use the cached Qwen3-0.6B checkpoint and actual native workers.
`MYCELIUM_B6_EVIDENCE_ROOT` retains the whole-source resident map; the shard test
requires exact equality of every row against that independently executed path.
Both tests reopen `CompiledHandoff` after `BuildPoll::Done(Ok)` and check cleanup.

This is one physical host. `route_ready=false` remains mandatory. Two-host
serving still needs authenticated shard transfer and retention, remote membership/
consent/resource admission, execution ownership, serving activation, routed token
and cancellation/cleanup qualification on both Macs. A stored compilation packet
is not fresh remote authorization. The whole-source path remains supported.
