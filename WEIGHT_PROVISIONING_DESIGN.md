# Mycelium Weight Provisioning and Layer-Load Contract

## Purpose

Preserve the lessons from the retired `distributed-inference-mvp` codebase before
Mycelium gains a runtime executor.

This is a clean-room design contract, not an instruction to reuse BloomBee code.
It covers the path:

```text
immutable model artifact
  -> layer plan
  -> per-node assignment
  -> file provisioning
  -> integrity verification
  -> runtime layer load
  -> route activation
```

The central rule is:

> A peer is ready only after it proves that it loaded the exact tensor components
> named by an immutable assignment. Download completion and generic process
> startup are not readiness.

## Audit boundary and verification

Reviewed old-project paths:

- `scripts/bootstrap.py`
- `mvp_capabilities/layer_planner.py`
- `mvp_capabilities/join_http_server.py`
- `mvp_capabilities/join_client.py`
- `src/bloombee/cli/run_server.py`
- `src/bloombee/server/from_pretrained.py`
- `src/bloombee/utils/disk_cache.py`
- model-family `config.py` files
- focused deployment/download tests

Executed checks:

```text
python3 -m py_compile ...                                  PASS
pytest --noconftest test_bootstrap_readiness.py
                    test_deploy_pipeline_fixes.py          30 passed
python3 -m unittest (Mycelium)                            36 passed
```

The old repository's normal pytest bootstrap could not run in this container
because `hivemind` is not installed. The focused tests ran with `--noconftest`.
Custom executable reproductions also confirmed the cache-root, tensor-prefix,
checkpoint-format, revision-selection, and `HF_HUB_CACHE` defects below.

## Verified failures and remaining risks in the old design

### P0: bootstrap and runtime use different caches

Evidence:

- `scripts/bootstrap.py:441-446` resolves downloads to the Hugging Face Hub cache.
- `scripts/bootstrap.py:783-910` validates/downloads files there.
- `mvp_capabilities/layer_planner.py:220-255` generates server commands without
  `--cache_dir`.
- `src/bloombee/utils/disk_cache.py:13` defaults runtime block loading to
  `~/.cache/bloombee`.
- `src/bloombee/server/from_pretrained.py:76-80` applies that runtime default.

Executable reproduction:

```text
bootstrap_download_cache: /root/.cache/huggingface/hub
runtime_default_cache:    /root/.cache/bloombee
```

Effect: a peer can finish the advertised per-layer pre-download, then the server
ignores those files and downloads them again into another cache. The dashboard
can show download completion while runtime is blocked on a second download.

Mycelium invariant: an assignment names one absolute artifact cache/staging root.
Downloader, verifier, and runtime receive the same immutable local snapshot path.
Runtime must never independently choose another cache.

### P0: layer-to-shard mapping is hard-coded to one tensor namespace

Evidence:

- `scripts/bootstrap.py:389-438` recognizes only keys containing
  `model.layers.<N>`.
- The actual runtime uses each architecture's `config.block_prefix` in
  `src/bloombee/server/from_pretrained.py:153-176,243-289`.
- Bloom uses `block_prefix = "h"`.
- Falcon uses `block_prefix = "transformer.h"`.
- Gemma uses `block_prefix = "model.language_model.layers"`.

Executable reproduction for layer 3:

```text
org/llama   model.layers                  -> ['shard-3.safetensors']
org/bloom   h                             -> []
org/falcon  transformer.h                 -> []
org/gemma   model.language_model.layers   -> ['shard-3.safetensors']
```

Effect: Bloom and Falcon peers cannot derive their required shards and silently
fall back to broader or incorrect cache logic.

Mycelium invariant: one architecture adapter owns tensor-component discovery.
The manifest compiler and runtime loader consume the same adapter output. Never
parse layer numbers using a globally hard-coded prefix.

### P0: sharded PyTorch checkpoints can pass with the wrong shard

Evidence:

- `scripts/bootstrap.py:413-438,449-460` only discovers
  `model.safetensors.index.json`.
- `scripts/bootstrap.py:603-612` accepts any non-empty `*.bin` when no required
  shard list was derived.
- Runtime supports `pytorch_model.bin.index.json` at
  `src/bloombee/server/from_pretrained.py:294-321`.

Executable reproduction:

```text
binmodel_shards:            []
binmodel_any_weight_cached: True
```

The fixture contained only shard 1 while the assigned layer required shard 2.

Mycelium invariant: format is explicit and fail-closed. Supported initial
formats should include at least:

- sharded safetensors
- single-file safetensors
- sharded PyTorch bin, if intentionally allowed
- single-file PyTorch bin, if intentionally allowed
- backend-specific artifacts such as GGUF or MLX only through separate adapters

Unknown format or empty layer mapping is an assignment-compilation error, never
permission to treat any weight file as sufficient.

### P0: model revision is mutable and cache lookup mixes snapshots

Evidence:

- Coordinator fetches `/resolve/main/model.safetensors.index.json` at
  `mvp_capabilities/join_http_server.py:1945-1950`.
- Peer download commands omit `--revision`.
- `scripts/bootstrap.py:414` and `:592` recursively search every cached snapshot
  and accept the first basename found.
- `run_server.py:117-119` supports `--revision`, but generated commands omit it.

Executable reproduction with `old-commit` and `new-commit` snapshots:

```text
unpinned_shards_selected: ['old.safetensors']
old_named_file_counts_as_cached: True
```

Effect: coordinator, preflight, cache verifier, and runtime can consume different
commits under the same model ID. A branch update during deployment can create a
mixed checkpoint.

Mycelium invariant: resolve branch/tag once to an immutable commit hash before
planning. Put that commit hash and manifest digest in every assignment, event,
cache path, runtime launch, and readiness proof.

### P1: cache verification checks presence, not identity or integrity

Evidence: `scripts/bootstrap.py:587-612` checks only basename and non-zero size.
It does not prove revision, expected byte size, ETag/content hash, safetensors
readability, or required tensor-key coverage.

Mycelium invariant: verify the exact revision-local path, expected size, content
digest, file readability, and expected tensor prefixes before load. Keep
algorithm metadata with each digest. Do not equate “non-empty” with “valid.”

### P1: missed preflight files enter an infinite runtime retry loop

Evidence: `src/bloombee/server/from_pretrained.py:336-375` retries any download or
load exception forever with a fixed delay. Permanent auth, missing-file, disk,
corruption, or incompatible-format failures are not distinguished.

Effect: cache-root mismatch or manifest omission can leave a peer indefinitely
“loading” instead of producing a terminal, actionable assignment failure.

Mycelium invariant: classify errors as permanent or transient. Use bounded retry
with exponential backoff and jitter. Emit a terminal failure reason after the
budget. Never retry authorization, unknown revision, missing manifest entry,
checksum mismatch after redownload, unsupported format, or insufficient disk as
if they were transient network faults.

### P1: stale-lock cleanup targets the wrong location and bypasses ownership

Evidence: `scripts/bootstrap.py:799-809` looks under
`<hub>/<model>/.locks`. The documented HF layout is:

```text
<CACHE_DIR>/.locks/<repo_folder_name>/<blob_hash>.lock
```

Lock files protect concurrent downloaders. Presence alone does not prove an
active or stale owner; deleting a lock used by another process can defeat mutual
exclusion. Current Hub implementations also manage temporary files and atomic
moves internally, so external code should not depend on private cache layout.

Mycelium invariant: use the Hub/library download API and its lock lifecycle.
Never recursively delete lock files. Add a Mycelium assignment lock above the
library layer, keyed by artifact commit plus file digest, if cross-job
coordination is needed.

### P1: range semantics already differ between old runtime and Mycelium planner

Evidence:

- Old runtime parses `--block_indices start:end` as half-open
  `[start, end)` in `src/bloombee/server/server.py:342-349`.
- Mycelium v1 emits inclusive `layers: [start, end]` in
  `layer_planner.py:281-296`; `[0, 27]` means 28 layers.

Effect: directly passing the Mycelium pair as an old-style range would omit the
last assigned layer. Converting in multiple places invites double conversion.

Mycelium invariant: runtime-facing schemas use explicit fields:

```json
{"start_layer": 0, "end_layer_exclusive": 28, "layer_count": 28}
```

Validate `end_layer_exclusive - start_layer == layer_count`. Keep any inclusive
presentation field display-only. Convert legacy output once at the route-plan
boundary, not in downloader and runtime independently.

### P1: generic `Started` proves process readiness, not assignment correctness

Evidence:

- `scripts/bootstrap.py:373-386` accepts a generic runtime `Started` line.
- `scripts/bootstrap.py:1032-1054` then reports serving.

This fixed an earlier false positive, but it still does not prove:

- exact deployment/assignment generation
- exact model commit
- exact loaded layer set
- exact tensor coverage
- exact local artifact path
- successful forward execution through those layers

Mycelium invariant: readiness is structured and assignment-bound. A log regex may
be diagnostic only; it cannot activate a route.

### P2: coordinator and peer duplicate and disagree on component ownership

Evidence:

- Coordinator gives all non-layer shards to both pipeline boundaries in
  `join_http_server.py:1969-2008`.
- Peer bootstrap selectively recognizes embedding and LM-head names in
  `scripts/bootstrap.py:429-436`.
- The old README says the client owns embeddings and LM head.

A test happens to place final norm and LM head in the same shard, so it does not
prove final-norm behavior when those tensors live in different files.

Mycelium invariant: model execution graph explicitly assigns component roles:

- input embedding
- decoder layer range
- final normalization
- LM head
- optional router/shared-expert/vision/audio components

If the client owns boundaries, decoder peers do not fetch them. If a peer owns a
boundary component, the assignment names it explicitly. Never infer ownership
from “first peer” or “last peer” alone.

### P2: `HF_HUB_CACHE` is ignored by custom cache resolution

Official cache precedence includes `HF_HUB_CACHE`, then `HF_HOME/hub`, then the
default. `scripts/bootstrap.py:441-446` only checks `HF_HOME`.

Executable reproduction:

```text
HF_HUB_CACHE_env:        /workspace/.../custom-hub
bootstrap_resolved_cache: /root/.cache/huggingface/hub
```

Mycelium invariant: do not reimplement third-party cache precedence. Resolve the
artifact through the library API, then pass the returned immutable snapshot path
to every downstream step.

## Required protocols

### `mycelium.model_manifest.v1`

One coordinator-side compiler resolves a model source into an immutable,
architecture-aware manifest.

Minimum fields:

```json
{
  "protocol": "mycelium.model_manifest.v1",
  "model_id": "org/model",
  "source": "huggingface",
  "requested_revision": "main",
  "resolved_commit": "40-hex-commit",
  "format": "safetensors_sharded",
  "index_file": "model.safetensors.index.json",
  "architecture": "llama",
  "num_layers": 32,
  "block_prefix_template": "model.layers.{layer}.",
  "components": {
    "input_embedding": ["model.embed_tokens."],
    "decoder": ["model.layers.{layer}."],
    "final_norm": ["model.norm."],
    "lm_head": ["lm_head."]
  },
  "files": [
    {
      "path": "model-00001-of-00004.safetensors",
      "size_bytes": 123,
      "source_etag": "...",
      "content_digest": {"algorithm": "sha256", "value": "..."}
    }
  ],
  "layer_files": {
    "0": ["model-00001-of-00004.safetensors"]
  },
  "manifest_digest": {"algorithm": "sha256", "value": "..."}
}
```

Requirements:

1. Resolve revision before planning.
2. Validate layer coverage for every integer in `[0, num_layers)`.
3. Reject unknown or ambiguous tensor namespace.
4. Reject files referenced by the index but absent from the resolved commit.
5. Preserve upstream shard granularity: a shard may contain several layers.
6. Record exact component ownership separately from layer ownership.
7. Canonically serialize before computing `manifest_digest`.

### `mycelium.layer_assignment.v1`

Minimum fields:

```json
{
  "protocol": "mycelium.layer_assignment.v1",
  "deployment_id": "uuid",
  "deployment_epoch": 7,
  "assignment_id": "uuid",
  "node_id": "stable-node-public-key-or-id",
  "manifest_digest": "sha256:...",
  "model_id": "org/model",
  "resolved_commit": "40-hex-commit",
  "range": {
    "start_layer": 8,
    "end_layer_exclusive": 16,
    "layer_count": 8
  },
  "components": ["decoder"],
  "expected_tensor_prefixes": [
    "model.layers.8.",
    "model.layers.9."
  ],
  "files": [
    {
      "path": "model-00002-of-00004.safetensors",
      "size_bytes": 123,
      "content_digest": "sha256:..."
    }
  ],
  "artifact_cache_root": "/absolute/path",
  "runtime": {
    "backend": "mlx",
    "dtype": "float16",
    "quantization": "none"
  }
}
```

Requirements:

- Stable node ID, not hostname sorting.
- Deployment epoch monotonically changes on replan/redeploy.
- Assignment ID changes whenever range, model commit, backend, precision, or
  component ownership changes.
- Model route identity and actual artifact repo identity cannot be conflated.
- Quantized artifacts use their own manifest; they never inherit a base-model
  manifest merely because tensor names look similar.

### `mycelium.layer_load_proof.v1`

A peer emits this only after verification, local-only load, and a runtime probe.

Minimum fields:

```json
{
  "protocol": "mycelium.layer_load_proof.v1",
  "deployment_id": "uuid",
  "deployment_epoch": 7,
  "assignment_id": "uuid",
  "node_id": "stable-id",
  "manifest_digest": "sha256:...",
  "resolved_commit": "40-hex-commit",
  "loaded_range": {
    "start_layer": 8,
    "end_layer_exclusive": 16,
    "layer_count": 8
  },
  "verified_files": ["sha256:..."],
  "verified_tensor_prefixes": ["model.layers.8."],
  "runtime_backend": "mlx",
  "runtime_probe": {
    "passed": true,
    "finite_output": true,
    "shape_contract_passed": true
  },
  "ready": true
}
```

The proof should eventually be signed by the node identity. Before signed claims
exist, hash and persist it as evidence but label trust as coordinator-local.

## Provisioning lifecycle

1. **Resolve**
   - Resolve branch/tag to immutable commit.
   - Fetch config and checkpoint index at that commit.
   - Select architecture/format adapter.

2. **Compile manifest**
   - Build tensor-component and layer-to-file maps.
   - Validate complete model graph and exact file metadata.
   - Compute canonical manifest digest.

3. **Plan**
   - Planner outputs explicit half-open ranges.
   - Provisioning compiler joins route ranges with manifest mappings.
   - Coordinator rejects gaps, overlaps, bad counts, unsupported backend, or
     insufficient memory/storage before issuing assignments.

4. **Accept assignment**
   - Peer checks node ID, epoch, backend, auth availability, free disk, and
     manifest support.
   - Peer persists assignment before acknowledging it.

5. **Download to immutable staging**
   - Use `snapshot_download`/`hf_hub_download` or backend-equivalent API with
     `revision=resolved_commit` and an exact allowlist.
   - Use one library-managed cache and lock lifecycle.
   - Report real bytes completed against expected missing bytes.
   - Keep discovery/lease heartbeat in a separate task so downloads cannot make
     the node disappear.

6. **Verify**
   - Exact commit-local paths.
   - Exact expected sizes and content digests.
   - Parse every safetensors file or format-equivalent container.
   - Confirm every assigned tensor prefix exists.
   - Confirm no assigned layer lacks required tensors.
   - Confirm required free runtime memory still exists after download.

7. **Activate atomically**
   - Move or point an assignment-specific `current` reference only after full
     verification.
   - Never expose partial staging as a runnable artifact.

8. **Load offline**
   - Runtime receives the verified local snapshot path.
   - Enable local-files-only/offline mode for the load.
   - Any attempted network fetch is a provisioning failure.

9. **Probe runtime**
   - Query actual loaded layer IDs from runtime.
   - Run at least one deterministic tensor through the assigned range.
   - Validate shape, dtype, finiteness, and expected output contract.

10. **Prove and activate route**
    - Peer submits `layer_load_proof` bound to assignment and epoch.
    - Coordinator checks gap-free, overlap-policy-compliant coverage using only
      proofs from the same deployment epoch and manifest digest.
    - Route becomes active only after a full-chain challenge succeeds.

## State machine

```text
assigned
  -> accepted
  -> downloading
  -> downloaded
  -> verifying
  -> verified
  -> loading
  -> probing
  -> ready

Any nonterminal state -> cancelled | failed_transient | failed_permanent
failed_transient -> retrying (within bounded budget)
```

Every event carries `deployment_id`, `deployment_epoch`, `assignment_id`,
`node_id`, and `manifest_digest`. Late events from an older epoch are ignored.
Status must never be copied forward to a new deployment.

## Successor standby and V1 KV replication (planned)

For an active route `P [a,b) -> S [b,c) -> N [c,d)`, direct successor takeover
uses a separately validated candidate route `P [a,c) -> N [c,d)` and a
node-bound candidate assignment for `P [a,c)`. `P` caches the complete immediate
successor stage on disk. It promotes the candidate to RAM only when measured
weight, workspace, primary KV, takeover KV, replication-buffer, and safety
reservations fit. An arbitrary prefix of the next layers is prefetch-only unless
it covers the complete successor stage.

Readiness remains explicit:

- `disk_ready`: candidate artifacts downloaded, verified, and pinned;
- `ram_ready`: merged assignment loaded/materialized, stage-probed, and leased;
- `route_ready`: changed directed links, KV channel, capacity, and full-chain
  candidate challenge verified;
- `active`: Router/Coordinator atomically activates a higher fenced route
  generation, and downstream rejects all stale producers.

V1 runtime planning includes KV replication for in-flight recovery. The successor
replicates chunked prefill snapshots and append-only decode KV deltas to the
predecessor standby. Every update binds deployment/route generation, candidate,
assignments, request/token cursor, exact stage/runtime/KV-layout identity, shape,
dtype, sequence, size, and digest. V1 begins with synchronous commit: the source
does not advance its committed watermark until standby application is
acknowledged. The predecessor retains boundary activations covering bounded
uncommitted lag. If replicated KV is missing, stale, incompatible, or behind the
last committed cursor, recovery falls back to whole-request replay from prompt
plus committed output tokens.

Missed heartbeats never grant local takeover authority. Active forward/connect
failure immediately quarantines the edge; three missed direct probes mean
`suspect`, with indirect/adaptive confirmation required for global death. One
stage/route-epoch state machine coalesces failures across batches. V1 permits one
fenced takeover and fails closed rather than recursively absorbing another
failed stage.

Detailed TDD plan:
`docs/plans/2026-07-16-successor-standby-kv-replication-v1.md`.

## Download and cache policy

- Calculate missing bytes before download using library dry-run/file metadata.
- Require missing bytes plus staging/runtime reserve, not only raw model size.
- Tokens stay local to peers. Coordinator never receives a user's Hub token.
- Distinguish gated/private authorization failure from network failure.
- Use bounded exponential retry with jitter and an explicit total budget.
- Do not infer progress from elapsed time or log chatter.
- Do not manipulate `.locks`, `blobs`, or `.incomplete` internals directly.
- Do not accept a file by basename across arbitrary snapshots.
- Deduplicate identical content by digest, but keep assignment identity separate
  from shared storage identity.
- Keep verified old snapshots until no active assignment references them.

## Stock shards versus real per-layer artifacts

A Hugging Face shard is a packaging unit, not a layer. One shard can contain
weights from several layers; one layer can span several shards. Therefore:

- Mycelium v1 may download the minimal set of upstream shards whose tensor keys
  intersect the assignment.
- Disk bytes will often exceed the exact assigned-layer tensor bytes.
- Runtime must filter loaded tensors by expected prefixes.
- Do not claim “peer downloaded only its layers”; claim “peer downloaded the
  minimal upstream shard set covering its assigned layers.”

A later `mycelium.layer_pack.v1` can repack immutable model revisions into
content-addressed per-layer safetensors chunks. That is the path to truly minimal
peer downloads, but it needs its own reproducible conversion pipeline, manifest,
hashes, licensing checks, and parity tests.

## Required test matrix before runtime integration

### Manifest compiler

- `model.layers.<N>` family
- `h.<N>` family
- `transformer.h.<N>` family
- nested prefix such as `model.language_model.layers.<N>`
- sharded safetensors
- single safetensors
- sharded PyTorch bin
- single PyTorch bin
- layer spans multiple files
- file spans multiple layers
- tied embeddings
- final norm separate from LM-head shard
- unknown prefix fails closed
- missing layer fails closed
- duplicate/ambiguous component fails closed

### Revision and identity

- branch changes after manifest compilation
- two cached revisions with same filenames
- coordinator and peer use same resolved commit
- base and quantized route identities cannot share manifest accidentally
- stale epoch status/proof rejected
- hostname change does not change stable node identity

### Range contract

- one layer: `[0, 1)`
- whole model: `[0, num_layers)`
- adjacent peers meet exactly at boundary
- no gap
- no accidental overlap unless explicit replication policy
- `end_exclusive - start == layer_count`
- legacy inclusive Mycelium route converted exactly once

### Download reliability

- cold cache
- warm exact-revision cache
- warm wrong-revision cache
- partial transfer
- silent network stall
- chatty but too-slow transfer
- insufficient disk before start
- disk fills during transfer
- gated/private repo without token
- invalid/expired token
- concurrent peers/processes requesting same file
- cancellation and redeploy during download
- process crash and restart
- checksum/size mismatch
- corrupted but non-empty safetensors
- library cache on alternate `HF_HUB_CACHE`

### Runtime proof

- runtime cannot access network after verification
- actual loaded layer IDs equal assignment
- actual commit/manifest digest equal assignment
- wrong local snapshot rejected
- missing tensor prefix rejected
- finite deterministic per-stage probe
- full route challenge covers every layer exactly
- generic process-start log alone never marks ready

## Acceptance gate for the future executor

Do not call Mycelium layer provisioning complete until a cold-cache multi-peer test
produces all of these artifacts from one deployment epoch:

1. immutable model manifest
2. route plan with explicit half-open ranges
3. one assignment per peer
4. byte-accurate download events
5. per-peer file and tensor verification reports
6. per-peer layer-load proofs
7. coordinator gap/overlap audit
8. full-chain deterministic inference challenge
9. restart test proving warm-cache reuse of the exact same commit
10. replan test proving stale assignments and proofs cannot reactivate
11. complete-successor candidate assignment and disk-ready proof
12. source/standby KV parity at every committed watermark
13. higher-generation fenced activation and stale-producer rejection
14. injected successor failure proving equivalent continuation or explicit replay

## References

- Hugging Face cache system and checksum verification:
  <https://huggingface.co/docs/huggingface_hub/guides/manage-cache>
- Hugging Face file/snapshot download APIs, immutable revisions, dry-run metadata,
  allowlists, and incomplete-snapshot behavior:
  <https://huggingface.co/docs/huggingface_hub/package_reference/file_download>
- Language-agnostic Hub cache layout and lock location:
  <https://github.com/huggingface/hub-docs/blob/main/docs/hub/local-cache.md>
