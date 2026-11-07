# L8 MLX compute slice — committed WIP, not a release backend

The `metal` feature ports the Qwen2.5-0.5B explicit FP32 and shipped weight-only
packed-int8 arithmetic through official mlx-c. The packed cell has a separate
recorded v0.1.4 oracle: all 290 weight images and 3,317 embedding/layer/stage/logit/KV
arrays match bit-exactly, including 64 greedy selections, complete-context replay
and an unsplit prefill control. This is **not completion of L8**: tokenizer, wire,
L5/L6 process/transport composition and broader supported-profile integration remain release blockers. Do
not merge this lane before the foundation exit and integrator review.

Packed storage uses streamed BF16 conversion, half-away signed rounding, offset-
binary uint32 codes, row scales and affine 128-element groups (1/-128). There is
no resident full signed-code or FP32 matrix. The 32-row floor and per-shape seeded
row-independence admission are retained. MLX 0.32.4 introduced a batch-one split-K
reduction that fails this admission; the native compatibility adapter executes
identical two-batch qmm and selects the first to force the old non-split-K kernel.
This changes dispatch/work, not the weight image or accepted numerical tolerance.
Its cost must be measured separately; no performance improvement is claimed.

Qwen3-0.6B is also qualified in separate FP32 and packed-int8 cells: each
compares 3,837 independently recorded state/output arrays and all 311 checkpoint
weight records (including the verified duplicate tied head, not twice resident),
64 greedy selections, replay and unsplit prefill. Its Q/K RMSNorm, unbiased
projections, 128-wide heads, and 28-layer geometry are explicitly selected by
pinned descriptor, not inferred from the Qwen2 constants. Invalid identities and
one-byte-under weight/state/scratch caps fail before artifact IO. Qwen3-8B/14B
and tokenizer/thinking-template behavior are not covered by this stage fixture.

Qwen3.5-0.8B text-only hybrid is qualified in separate FP32 and packed-int8
cells, each with 320 recorded weight images and 3,317 complete arrays, 64 greedy
selections, replay and unsplit prefill. Zero-centered norms, 64-dimension partial
RoPE, gated full attention, four-tap depthwise convolution and ordered DeltaNet
match their own recorded oracles bit-exactly. Each step compares all convolution,
recurrent and attention states; recurrent state is never reconstructed by slicing
terminal KV. The fixed convolution tail is copied to avoid retaining prefill
backing storage. State caps depend on actual layer kinds, with an explicit bound
for lazy recurrent scratch. Rewind, padded batches and KV-only hybrid snapshots
refuse. Vision, other hybrid geometries and MoE remain unsupported.

`dense_oracles` requires `MYCELIUM_QWEN2_CHECKPOINT`,
`MYCELIUM_QWEN3_CHECKPOINT` and `MYCELIUM_QWEN35_CHECKPOINT` pointing to the cached pinned safetensors. It runs
serially (`--test-threads=1`) with a 6GiB native ceiling, except Qwen3.5 FP32's
explicit 8GiB aggregate grant. Both hybrid siblings reserve their complete logical
scratch concurrently; a separate test proves 6GiB refuses. Qwen3 unsplit FP32 needs
an explicit 2,048,000,000-byte scratch grant for its BF16 loading image. An earlier
1,024,000,000-byte test grant correctly refused, rather than silently expanding.

The external oracle generator is archived with the lane report, never invoked by
Cargo/test/release. `tests/fixtures/qwen2-packed.json` seals the recorded arrays,
source pin `941765e882ed3eea9a5576cc53e525c4caa20300`, checkpoint, generator and
runtime versions. Its raw arrays remain in the external evidence archive.
`examples/verify_recorded.rs` checks every NPY payload/shape/dtype, candidate
fixture identity, generator and source hashes in Rust, without invoking Python.
All five new captures passed this verifier. The read-only oracle checkout later
advanced; the hybrid FP32 capture used a hash-sealed `git archive` of the accepted
pin, verified every archived regular file before importing it, and did not reset
or modify the owner's checkout. See the lane report for that archive and receipts.

## Accepted oracle successor

The accepted v0.1.4 oracle is now `f4e366e98a1f705dfaf182897044500d290dbd7f`.
`tests/fixtures/oracle-successor.json` binds it to the immutable captures at
`941765e882ed3eea9a5576cc53e525c4caa20300`: all six recorded compute modules
are byte-identical. The intervening ten-file diff changes control/token retirement
and tests, not these compute paths. Captures retain their real generation commit;
no fixture has been relabelled as freshly captured at the successor.

## Ownership and contract

- `stage-contract`: immutable-by-ownership descriptor/binding values, typed errors,
  activation metadata, limits, `BackendFactory` preparation and `StageBackend`
  lifecycle. No MLX pointer, implicit wire conversion or backend dependency.
- `model`: closed pinned Qwen2, Qwen3 and Qwen3.5-0.8B text configurations, assigned
  weight geometry and fallible explicit dense/hybrid arithmetic. It preserves the two-row
  single-row projection rule; it does not substitute fused kernels.
- `backend-mlx`: a thread-affine factory and stage worker. Only the `native`
  module (including its private MoE submodule and generated bindings) allows unsafe code. Arrays/maps/iterators/
  streams use RAII. A nonallocating, non-unwinding C callback replaces native exit;
  errors report stable code/operation/status/callback presence, not secret-bearing
  native messages. Native context ownership is exclusive per process.
- Prepare binds exact checkpoint, sorted-JSON tensor-manifest identity, revision,
  semantics, representation, profile, layer range and component ownership. It
  verifies checkpoint/header hashes before and after loading. L7 must provide
  immutable artifacts; these checks are not an atomic filesystem snapshot.
- Only assigned tensors are expanded/materialized. Safetensors I/O uses a CPU
  stream; casts/compute use Metal. No CPU inference fallback.
- A stage admits one session. Request/assignment/load/state generation, expected
  position and payload digest must match. New KV is committed only after successful
  synchronous hidden/logit export. Duplicate steps refuse rather than append again.
- Cancel is cooperative **between synchronous calls**, not native-kernel preemption.
  Reset invalidates the prior state generation. Close refuses active sessions.
  Native cleanup errors are sticky and fence a successor in the same process.
  `Context::close` proves final stream release after all stage/array owners drop.
- `Activation.bytes` is a typed **in-process** FP32 snapshot. `export_activation`
  deliberately returns `Unsupported` until L5's existing versioned codec and
  conversion policy are wired. This is not a new network format.

## Qualified fixture cell

`Qwen/Qwen2.5-0.5B-Instruct` revision
`7ae557604adf67be50417f59c2c2f167def9a775`; original BF16 weights expanded to FP32,
FP32 activations and attention KV; stages `[0,8)` / `[8,24)` in one process.
A 35-token prefill and 63 one-token decodes produce 64 greedy selections.

The Rust integration test compares complete FP32-word SHA-256 digests and shapes
for both stage outputs, full logits and every layer's K/V at **every** step:
3,264 tensor comparisons, no tolerance or text-only substitution. The intermediate
KV fixtures are explicitly derived by taking each head's sequence prefix of the
recorded terminal KV, justified by the oracle's append-only cache semantics. They
are not newly captured intermediate states or independent author-reference data.

Provenance: recorded spike `2e0dd01349f886ecce4c68a51870adb900c7565a`, generated
against oracle `52d1b69fb98e85529537500133ce6301c612d093`. The actual compute module
has identical SHA-256 `b248c09612e0bb10b238f0a00f84b1eaa8e1fd3c30f5864e77b82ee88d4b30c5`
at accepted oracle `27fdd49de7e18b58affa85b8a31e9a734d7b4d1e`.
`examples/seal_oracle.rs` verifies the committed source seal and all original NPY
hashes before writing digest fixtures. It never invokes an interpreter. The older
oracle used MLX 0.31.1; this backend uses 0.32.4. Equality here does not establish
universal cross-version/device determinism.

## Native prerequisites and tests

No automatic download or CMake bootstrap is hidden in Cargo. A mandatory Metal
job fails when its pinned headers, libraries or cached checkpoint are unavailable;
feature-off tests are not native qualification. Use the admitted host guard before
**every** Cargo/CMake command, `nice -n 19`, `-j 2`, and an owned target directory.

```text
MLXC_INCLUDE_DIR=<official mlx-c 57731b00 source root>
MLXC_LIB_DIR=<installed pinned libmlxc/libmlx/libjaccl/mlx.metallib directory>
MYCELIUM_QWEN2_CHECKPOINT=<existing pinned cached model.safetensors>
MYCELIUM_QWEN3_CHECKPOINT=<existing pinned cached model.safetensors>
MYCELIUM_QWEN35_CHECKPOINT=<existing pinned cached model.safetensors-00001-of-00001.safetensors>
MYCELIUM_MOE_CHECKPOINT=<cached Qwen3.6-35B-A3B snapshot 995ad96eacd98c81ed38be0c5b274b04031597b0>
CARGO_TARGET_DIR=<owned scratch target>

cargo test --locked --offline -j 2 -p stage-contract -p model -p backend-mlx --features backend-mlx/metal --lib -- --test-threads=1
cargo test --locked --offline -j 2 -p backend-mlx --features metal --test qwen2_oracle -- --nocapture --test-threads=1
cargo test --release --locked --offline -j 2 -p backend-mlx --features metal --test dense_oracles -- --nocapture --test-threads=1
cargo test --release --locked --offline -j 2 -p backend-mlx --features metal --test resource_lifecycle --test isolated_worker --test moe_component -- --nocapture --test-threads=1
cargo test --locked --offline -j 2 -p stage-contract -p model -p backend-mlx --features backend-mlx/metal --doc
cargo clippy --locked --offline -j 2 -p stage-contract -p model -p backend-mlx --all-targets --features backend-mlx/metal -- -D warnings
cargo run --locked --offline -j 2 -p xtask -- lint
```

`build.rs` hash-pins all 44 C headers and the installed native dylibs/metallib against the table in
`src/pins.rs` (`PIN_SET`, included by `build.rs`; the selected set name is exported as
`MYCELIUM_MLX_NATIVE_PIN_SET`). Pins: mlx-c `57731b00e8bed710e0750e7356da9f18f8eb3e20` (C API 0.7.0),
MLX `36969c094354086e15d2a327a9c50feb20b9cf11` (0.32.4), bindgen 0.72.1.
- `candidate-minos-14.0` is the **only** accepted set: offline rebuild from the vendored sources with
  `MACOSX_DEPLOYMENT_TARGET=14.0` (MLX 0.32.4's own minimum, and v0.1.4's libmlx floor),
  `@rpath` install names plus `LC_RPATH @loader_path`, metallib `air64_v26-apple-macosx14.0.0`
  (no NAX kernels: Metal 4 + deployment >= 26.2 only). The name is kept from L8's evidence. Recipe, hashes
  and exactness evidence: `runs/v2/v02/L8-evidence/` (`build-native-macos14.sh`,
  `native-inputs-candidate-minos14.json`, `suite-minos14/`, `MINOS-LOAD-PROOF.md`); the foundation-exit
  re-verification is `runs/v2/v02/FOUNDATION-EXIT-round5/`. Built with AppleClang 21.0.0.21000101,
  Xcode 26.5 (17F42) on macOS 26.5.1. L8 loaded and ran it on a real macOS 15.6.1 host with a std-only probe;
  macOS 14.x and the packaged v0.2 executable are untested.
- The earlier `foundation-exit-minos-26.5.1` set (minos 26.5.1; cannot load on macOS 15) was removed in
  FOUNDATION-EXIT round 5. `pins::tests` prove its libraries are refused, singly and mixed.

Relocation: `build.rs` emits `-Wl,-rpath,@executable_path/../Frameworks` first, then the
development `MLXC_LIB_DIR`, for this package's own test/example binaries. Cargo does not
forward `rustc-link-arg` to dependants, so a crate that links `backend-mlx` into a shipped
executable must emit `rpath::BUNDLE_RPATH` from its own `build.rs`.

Worker children go through `node::process::confine_descriptors` (stdin/stdout/stderr only,
fail closed), as v0.1.4's `close_fds=True` did; see `tests/descriptor_confinement.rs`.

Direct Rust dependencies are exact-pinned `sha2=0.10.9`, `serde_json=1.0.154`,
`bindgen=0.72.1` plus the three local crates; transitive pins are in Cargo.lock.
MLX/mlx-c/JACCL, nlohmann/json 3.11.3 and fmt 12.1.0 use MIT; metal-cpp_26
uses Apache-2.0; bindgen uses BSD-3-Clause; sha2 and serde_json are
MIT OR Apache-2.0. L11 must bundle the original notices with native artifacts.

## Resource and claim boundaries

Admission checks exact expanded-weight bytes, KV bytes, context length and a
logical scratch reserve. Scratch includes the transient assigned BF16 image,
replacement KV, padded head and an explicit-op estimate. The measured loader
peak exposed and now refuses an insufficient scratch allowance. MLX also receives
an explicit whole-process memory limit. A shared typed reservation pool now
admits the sum of resident/scratch/KV before artifact I/O, rolls back failed loads,
releases on proven close, and fences reuse after unproven cleanup. Session limits
cannot enlarge a loaded stage's grant; generations advance across successor sessions.
These checks do **not** complete L7/L6 signed-cap composition, physical
allocation enforcement over all workloads or an F14 release gate.
Residency tests distinguish logical bytes from physical allocation. Their sealed-
component upper bounds include 16KiB VM rounding and the pinned allocator's
strictly-less-than-two-extra-page cache reuse rule, remain below a second complete
resident image, and require zero active bytes after close. They are host/version-
labelled bounds, not portable allocator or independent per-worker physical caps.

Timings include synchronous host export and (for stage A) embedding / (for stage B)
head. They exclude fixture hashing, KV diagnostic copies and guard checks. Keep
all samples, process-cold kernel effects and hash-verified load time separate;
never compare these directly to the spike's differently scoped stage timers.

## Owned leaf-process lifecycle

`process::OwnedWorker` reserves before spawn and owns a trusted **non-forking leaf**
through normal exit, crash, graceful EOF close, cancellation, immutable monotonic
deadline and bounded forced termination/reap. Its caller supplies typed close,
reap and poll allowances; no local timeout defaults or deadline extension exist.
One-slot coalesced notifications plus an atomic cancellation flag bound control
state and preserve cancellation priority. Cleanup uncertainty fences the shared
pool, and a reaped process cannot falsely release staged-file reservations.

The custom Rust `isolated_worker` harness includes real Metal stage children:
successful close with zero active/reserved bytes, abort, cancellation during a
bounded compute loop, hung close, sticky native cleanup fencing, and a fresh
successor. Every terminal checks OS disappearance and accounting rollback.
Deterministic leaf faults cover kill/poll/reap/panic failures. This is **not** a
sandbox, descendant supervisor, signed-authority adapter, activation protocol or
physical per-process RSS enforcement. L6 must compose the actual worker executable
and L5 transport; no inference RPC is introduced here.

## Cached MoE component, not a seventh model cell

All 26 BF16 safetensors shard files are cached (71,903,776,776 bytes total)
for `Qwen/Qwen3.6-35B-A3B` revision
`995ad96eacd98c81ed38be0c5b274b04031597b0`. `model::moe::qwen36_first_block`
qualifies **only its first-layer MoE block**, in separate FP32 and packed-int8
profiles. Each recorded successor-oracle fixture binds seven actual checkpoint
tensors, source bytes/headers/config/index, resident weight images and 25 complete
input/router/score/index/output arrays over 1/2/8/33/65 rows. No weights are downloaded.

Routing preserves threshold selection, low-index ties, **descending probability
rank** and ranked summation. The expert kernels use `gather_mm` / `gather_qmm`;
3-D packed loading is streamed, with per-shape seeded call-independence refusal.
Router and shared-expert gate stay FP32. The 3-D expert kernel does not reuse the
2-D qmm dispatch adapter. Public expert indices are checked before native gathers.
The native tests consume JSON fixtures only; Python was used externally solely
to record the pinned product oracle and is absent from the Rust tree/path.

`DenseConfig`/stage admission still **refuse MoE**. This is not full decoder,
recurrent-state, split-stage, tokenizer/head, greedy decode, independent author
reference or physical distributed qualification. Those remain explicit work;
the cache discovery must not be reported as a missing-model blocker.

## Streamed MoE decoder component

`model::moe_decoder::DecoderLayer` composes the indexed MoE block with the exact
Qwen3.6 layer norms, 32-value-head recurrent path and 16-query-head full attention.
Its checked cursor rejects duplicate/rewound positions, missing or malformed
weights, invalid shapes and layer indices; reset drops both state tensors. State
is committed only after output and replacement state evaluation succeeds. This
closed component is **not** a `StageBackend`, session-generation authority or
model registry entry.

The `moe_decoder` target streams all experts of one of the 40 layers at a time.
Separate FP32 and packed fixtures bind 690 selected checkpoint tensors and 964
complete input/output/router/ranked-index/score/mixed-state records per profile.
The workload is three-row prefill, two one-row decodes, then five-row reset/replay.
Both contiguous composition and host-export/import boundaries at 1/3/20/39 are
compared to the same independently recorded oracle. It keeps a 6 GiB allocator
limit and 128 MiB cache limit; run under the owning lane's pressure/RSS watchdog.
Loading and full resident-image hashing dominate this test on removable storage.
The continuation's FP32 decoder capture is separate from native qualification:
its full Rust comparison is **NOT RUN / timebox-blocked** this round. The mandatory
FP32 test remains in the target; it is not skipped or reclassified as admitted.

```sh
cargo test --release --locked --offline -j 2 -p backend-mlx --features metal --test moe_decoder -- --nocapture --test-threads=1
```

Layer-major streaming with fixed hidden inputs is not resident production-stage
split/unsplit or autoregressive model generation. **Full MoE admission remains
refused** pending 64 independently selected greedy tokens, tokenizer/template identity,
production-session (StageBackend) reset/no-rewind, real multi-process stage splits/transport, and the
remaining integration/release gates (untied embedding/head, logits and in-process stage handoff are now
qualified; see below). Do not infer those from decoder component parity.

## Qwen3.6 untied embedding/head, resident stages and stage-split resources

Qwen3.6-35B-A3B is **untied** (`tie_word_embeddings=false`; `lm_head.weight` and
`model.language_model.embed_tokens.weight` are different tensors on different shards).
`model::moe_decoder` owns `embedding_weights`/`head_weights`/`stage_weights` (exact stage-only
inventories), `embed`, `final_norm`, `head` (last row unless `all_positions`; **never** falls back to the
embedding) and `select_greedy` (first maximum, non-finite refused). `model::moe_stage::MoeStage` owns a
contiguous layer range plus optional embedding (entry) / norm + head (final), refuses any missing, extra or
misshapen tensor, and commits layer states and the position only after every layer and the head evaluated.

Oracle evidence (pinned v0.1.4 `f4e366e9`, same module bytes as the decoder fixtures; recorders
`L8-evidence/round7/capture_head.py` / `capture_entry.py` are external and never in the Cargo path; the Rust
`verify_recorded` example independently re-hashes every NPY): `moe-head-{f32,packed}.json` (embedding rows,
final norm, all-position and last-row logits, selected tokens, bound to the existing layer-39 decoder outputs),
`moe-entry-{f32,packed}.json` (token ids -> embedding -> layers 0/1). Exactness rule is unchanged: SHA-256 of
the f32/uint32 payload equals the oracle (bit exact), no tolerance.

```sh
export MYCELIUM_MOE_CHECKPOINT=<cached snapshot> \
  MYCELIUM_MOE_DECODER_CAPTURES=<L8-COMPUTE-evidence/moe-decoder> MYCELIUM_MOE_ENTRY_CAPTURES=<round7/entry-capture>
cargo test --release --locked --offline -j 2 -p backend-mlx --features metal --test moe_head --test moe_stage -- --nocapture --test-threads=1
cargo test --locked --offline -p backend-mlx --test moe_stage_resources   # no native lib, no weights
```

`moe_stage` runs an entry stage (tokens -> embedding -> layer 0) whose exported `Activation` bytes feed a
second stage; a mid stage (layers 19-20, or 19 in FP32) and a final stage (layers 38-39, or 39, -> last-row norm
-> untied head -> greedy token) are fed the recorded boundary outputs. FP32 stages own one layer (a resident
FP32 layer is ~3.46 GB and the allocator cap is 6 GiB); packed stages own two. Handoff is an in-process host copy
over the L5 `Activation` contract, **not** a real wire/QUIC transport. `moe_stage_resources` binds the
resident-byte model to every oracle-recorded resident image (693 tensors, both profiles) and shows the whole
model is 37.0 GB packed / 138.6 GB FP32, so a 6 GiB worker needs >= 6 packed stages (40 FP32).

Still **QUIET-HOST-PENDING**, not loosened: full-model 64 greedy tokens (needs the whole model resident or one
~57-minute weight pass per token) and a multi-process resident stage pipeline. See
`L8-evidence/round7/QUIET-HOST-PENDING.md`. Full MoE admission (`DenseConfig`/`StageBackend`) remains refused.

## Remaining cells and exact next work

The five additional numerical cells above are qualified; remaining obligations below
are **BLOCKED / unimplemented**, not retired supported behavior.

| Cell | Next implementation and evidence |
|---|---|
| Starter packed-int8, `persistent_packed_int8` | Port `weight_quantization.py` signed-code rounding/scales and uint32 offset-binary packing, group size 128/bits 8, affine group scales 1 and bias -128. Port `_packed_int8_product` with the **32-row** floor and row-independence refusal, not FP32's two-row rule. The separate packed oracle, streamed packing, embedding, first layer, every-step KV, replay and split/unsplit prefill are now bit-exact. Still integrate VERIFY `auto` row-attention vs sequential-rows selection and the external lifecycle/transport seam. |
| Starter per-call materialization; FP16/BF16 unquantized profiles | Inventory reachable dtype/profile combinations from the accepted validators. Preserve conversion/rounding and per-call expansion explicitly; generate separate fixtures and exact existing tolerance contracts. Do not label these packed residency. |
| Qwen2.5-7B-Instruct `a09a35458c702b33eeacc393d103063234e8bc28` | Parameterize only the existing Qwen2 shapes/config fields; pin cached tensor/config/tokenizer identities, tied-head ownership and stage sizes. Then run its own packed/unquantized fixture cells and real split checks. Current hard-coded starter geometry must refuse this model. |
| Qwen3-0.6B `c1899de289a04d12100db370d81485cdf75e47ca`, Qwen3-8B `b968826d9c46dd6066d109eabc6255188de91218`, Qwen3-14B `40c069824f4251a91eefaf281ebe4c544efd3e18` | The 0.6B FP32/packed stage cells are qualified. Other geometries/profile cells and tokenizer/thinking templates still need their own pinned artifacts and fixtures. Use cached assets; ledger any required missing-model acquisition under the owner's policy. |
| Qwen3.5 dense hybrid (`qwen3_5`) | The cached 0.8B revision `2fc06364715b967f1860aea9cf38778875588b17` is qualified for text-only FP32/packed stages and actual per-step mixed states. Other geometries, vision and external lifecycle/transport integration remain unqualified. This does not register a new A13 GUI choice. |
| Qwen3.5 MoE hybrid (`qwen3_5_moe`) | The cached Qwen3.6 first-block FP32/packed component is bit-exact. The new layer-indexed decoder target covers streamed hidden composition, routing and mixed-state replay across all layers; it is not full model/stage admission. Still implement production stage ownership/resources, embedding and untied head; qualify greedy tokens, production replay/no-rewind, resident split/unsplit and independent reference controls. MoE stage admission remains refused. |
| Existing CPU int8/f32/libm | Extract the existing native/mobile_core Qwen2 arithmetic unchanged into `backend-cpu`, retaining its author-reference fixtures. It is not an equality oracle for this original-floating-weight MLX profile. |
| Cross-cutting | Port tokenizer/BOS/EOS/chat templates; integrate L5 activation codec and actual transport. Compose the tested leaf lifecycle and aggregate reservations with L6 signed authority/L7 immutable artifacts, preserving physical split and replay obligations. Qualify native relocation, minimum OS, missing/corrupt resources and package signing through L11. |

The L0 census also retains reachable legacy GPT2/other runtime boundaries for an
explicit reachability/retirement decision. This lane does not silently remove them.
Normal/fast are route activation policies, not aliases for FP32/int8 numerics.
