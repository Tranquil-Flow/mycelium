# Cross-request prefix KV cache (PV20)

Status: design + first implementation on `lane/rel2-prefix` (from `rel1.1/hardening` 99d24e17). Default **off**.
Backlog: PV20 (`V2-BACKLOG.md`), proposal P2, speed research R1.

## 1. What exists today (read from the code at 99d24e17)

- **No KV is reused across requests, and none across turns of a session, in code.**
  `docs/request-streaming-session-lifecycle.md` states a *contract* ("KV reuse requires ... prefix-digest
  agreement; any mismatch causes ... complete chat prefill"), but there is no implementation behind it:
  `grep session_id` in `mycelium_router/` finds nothing, and the only session string in the live path is
  `request-session:{request_id}` (`mycelium_live/router_port.py`, A6 recovery binding). The "same-session
  prefix-digest reuse" in the PV20 row is therefore a documented intent, not a feature to extend.
- Stage-local KV lives in the runtime ports (`NumpyRuntimePort`, `MLXRuntimePort`): `_KVState.layers`
  `{layer: (K, V)}` with arrays `[1, heads, T, dim]`, keyed by `path_id`, created at `PREFILL` /
  `PREFILL_CHUNK(token_index 0)` (which requires `position == 0`), released at the terminal hop
  (`_release_terminal_state`), lease expiry, cancel or `close()`.
- `mycelium_router/terminal_kv_retention.py` keeps a finished path's KV briefly for an A7 fenced rekey. It is
  keyed by `path_id` and consumed by exactly that request's successor; it is not a content cache.
- A7 (`kv_checkpoint.py`, `mycelium_a7_kv_successor.py`) uses `committed_prefix_digest` over a request's own
  generated tokens for successor recovery; unrelated to cross-request sharing.
- `RequestContext.prompt_token_ids` is known on every node (it rides in `ProgressivePrefillMessage` /
  `ManifestLocked.build.request`), and the relay computes the KV position of a prefill chunk as
  `token_index * prefill_chunk_size` (`relay._kv_position`). Prefill payloads are token ids for the entry
  stage and hidden states for later stages, so a non-entry stage never sees tokens.

## 2. The gap

Every request re-prefills the whole prompt on every stage. A chat with a fixed system prompt, or turn *n+1*
that resends turns 1..n (clients send the full chat each turn), pays the full prefill (TTFT) each time.
Skipping the shared prefix needs three things the code lacks: a content key for a token prefix, a
per-stage store that survives a request, and a way for **all** stages to agree to skip the same tokens (a
later stage can only skip a prefix if the stages before it also do not send hidden states for it).

## 3. Design

### 3.1 Keys: chained block digests
- A prompt is cut into full blocks of `B` tokens (`B` in {16, 32, 64}; default 32). The trailing partial block
  is never cached or matched.
- `d_0 = H("mycelium.prefix.v1" | route_scope | B | tokens[0:B])`,
  `d_k = H("mycelium.prefix.v1" | d_{k-1} | tokens[kB:(k+1)B])`. Changing one token changes that block's digest
  and every later digest, so a match is always a **whole leading run** and a changed token invalidates from its
  block onward.
- `route_scope` = digest of the exact route identity: deployment id and epoch, model id, resolved commit, manifest
  digest, hidden size and activation codec bytes, and for every stage its id, layer range, stage signature and
  runtime backend. Different model, slice, split or codec => different digests => no hit. It is a salt, so
  digests are meaningless outside the route (not comparable between swarms or owners).
- A stage stores a block under `(stage_scope, d_k)` where `stage_scope` additionally binds that placement's
  assignment id, load-proof digest, runtime backend/dtype/quantization/architecture. A stage can only ever hit
  its own entries.

### 3.2 Store: per runtime port, paged by block
- Value of `(stage_scope, d_k)` is that stage's K/V slice for tokens `[kB, (k+1)B)` for its layers (axis -2 of the
  `[1, heads, T, dim]` layout) plus `parent = d_{k-1}`. A prefix of `n` blocks is rebuilt by concatenating the `n`
  slices in chain order. Shared prefixes are stored once.
- Bytes are counted exactly (`array.nbytes`). One cap per port, `max_bytes`. Insert evicts least-recently-used,
  **unpinned, childless** blocks (so a chain is never orphaned mid-way); if it cannot make room the insert is
  skipped. `bytes <= max_bytes` is an invariant, never exceeded, also under pressure:
  `set_prefix_cache_budget(bytes)` shrinks the cap and evicts immediately.
- The cap is **tied to the sharing memory budget**: the owner of the port derives it with
  `prefix_cache_cap_bytes(budget_bytes, fraction)` (default 10% of the stage's KV/memory budget), re-applies it
  when the Adaptive policy changes, and the cache is cleared on `close()` and on any weight unload
  (`clear_prefix_cache(reason)`); the cache is the first thing dropped under pressure, before any live request KV.
- Stored on the port that executed the stage; dies with the port (route teardown / node shutdown). Never written
  to disk.

### 3.3 All-stage agreement (no partial hits)
The entry decides the reusable length, not any single stage:
1. The entry computes the chain `d_0..d_{m-1}` of the prompt (`m = floor((len-1)/B)`, i.e. at least one token is
   always computed so the last stage emits the first token).
2. For every placement on the already-built path it calls `pin(chain, owner=request_id)`; each stage returns how
   many **leading** blocks it holds and pins exactly those against eviction (short TTL).
3. `reuse_blocks = min(over all stages)`. Every stage is told to `settle(owner, keep=reuse_blocks)` (drops the
   extra pins). A stage that lacks block *j* therefore forces `reuse_blocks <= j`. `reuse_blocks == 0` is a normal
   cold run.
4. The request is rewritten with `prefix_reuse_tokens = reuse_blocks * B` and the chain. The entry then prefills only
   `prompt[reuse_tokens:]` at KV position `reuse_tokens` (relay `_kv_position` adds the offset; payloads are the
   suffix tokens, later stages get suffix hidden states). Each stage seeds its `_KVState` from its own pinned
   blocks, then unpins. A missing block at seed time fails closed (`prefix_cache_miss`), never guesses.
5. After the final prefill chunk each stage inserts the blocks it did not already hold, using the chain.
   Decode, recovery (`RECOVERY_PREFILL` always starts at position 0 cold), A6/A7 are unchanged.

Not covered by reuse (fall back to a cold run): `complete_context_replay` mode, progressive prefill whose path is
built hop by hop (the entry does not know the stages up front), hybrid-state architectures (linear-attention state
is not position-sliceable), A11 speculative windows, batched fresh starts (they require `position == 0`).

### 3.4 Exactness
Reuse must give the same tokens as a cold run. A hit replays stored K/V for the prefix and computes only the suffix
with the same attention-with-past code that chunked prefill already uses (the shipped chunked-prefill path). Keys
include everything that determines K/V values (weights identity, dtype/quantization, layer range, tokens by
position). Block-aligned prefix positions are identical by construction because positions are absolute. Tests
require hit tokens == cold tokens on the fixture route and the existing parity tests stay green. Bitwise equality of
the stored prefix K/V against a cold run is asserted on the NumPy fixture; on device the pass criterion is
identical greedy token ids across cold and warm (see section 6).

### 3.5 Privacy
- Only digests and K/V tensors are stored; no token ids and no text. Digests are salted by the route scope.
- The cache is per swarm route and per port: it never crosses owners, routes or nodes, and it is not exported by
  A7 checkpoints, diagnostics or the support bundle (counters only).
- Entries die with the route / port (close, unload), and are never persisted.
- Residual: K/V of a shared system prompt stays in the memory of every Mac that already ran those layers, exactly
  as the activations of a live request do today; a member could infer "this prefix was seen before" only through
  its own cache hits, which it already can observe because it ran the request. Documented in `docs/user/PRIVACY.md`.

### 3.6 Observability
- `prefix_reused_tokens` per request: on the entry `RequestRecord` (`Router.prefix_reused_tokens(request_id)`), in the
  stage `kv_snapshot()["states"][path_id]`, and a stage counter block `kv_snapshot()["prefix_cache"]`
  (`enabled, block_tokens, bytes, max_bytes, blocks, hits, misses, reused_tokens_total, inserted_blocks,
  evicted_blocks, rejected_inserts, pinned_blocks`) that the node status already forwards to diagnostics.

### 3.7 Flag and protocol impact
- Flag: `PrefixCacheConfig(enabled=False)`; the entry only plans when a planner is attached, ports only store/seed
  when enabled. Off => identical behaviour and identical wire bytes.
- Protocol: `RequestContext` gains optional `prefix_reuse_tokens` / `prefix_chain` (emitted on the wire only when
  non-default, so goldens are unchanged) and `HopWorkItem` gains the same two internal fields. A mixed route with an
  old member would not understand a suffix-only prefill, so the planner requires every probe to answer; an
  unknown probe forces a cold run. The cross-process `pin/settle` probe (a managed-control op to member nodes) is
  the remaining live wiring; in-process probes (fixture, same-process stages) are implemented.

## 4. Test plan (RED first)
- Unit: digest chaining, block alignment, one-token change invalidates from its block, route scoping, agreement
  = min over stages, eviction under cap (invariant), pinned never evicted, childless-first, unload/close clears.
- Fixture (NumPy qwen2/qwen3 two-stage): request 2 with the same system prompt reuses N tokens and yields identical
  prefill token and decode tokens to a cold run; a changed prefix token invalidates from its block; a different
  model/stage scope never hits; stage lacking blocks forces the shorter length.
- Entry/relay: second admit prefills only the suffix at the offset and reports `prefix_reused_tokens`.

## 5. Files
`mycelium_router/prefix_cache.py` (new), `contracts.py`, `wire.py`, `relay.py`, `entry.py`, `numpy_runtime.py`,
`mlx_runtime.py`, `docs/user/PRIVACY.md`, tests `test_router_prefix_cache*.py`.

## 6. Device measurement (after tests are green)
0.5B model, two Macs, shared 400-token system prompt + different short user turns: TTFT of request 1 (cold) vs
requests 2..5 (warm), `prefix_reused_tokens` on each (expect 384 with B=32), and identical output tokens for a
fixed seed greedy run with the cache on vs off.

## 7. Status of this lane (what is done, what is not)

Done (code + tests written; test results are recorded in the lane report, not here):
- Core (`prefix_cache.py`): chained digests, scopes, LRU/byte-capped store with pins, planner with all-stage agreement.
- Runtime glue (`prefix_runtime.py`) and both ports (`NumpyRuntimePort`, `MLXRuntimePort`): `prefix_cache=` kwarg
  (off by default), seed on `PREFILL`/`PREFILL_CHUNK(0)`, incremental block store, `prefix_pin`/`prefix_settle` probes,
  `clear_prefix_cache`, `set_prefix_cache_budget`, clear on `close()`, `kv_snapshot()["prefix_cache"]` and per-state
  `prefix_reused_tokens`.
- Entry/relay: `EntryCoordinator.prefix_planner` (off = `None`), suffix-only prefill at the reuse offset
  (`admit`, `start_locked_distributed_prefill`), `Router.prefix_reused_tokens(request_id)`.
- Wire: `RequestContext.prefix_reuse_tokens/prefix_chain`, omitted from the wire and from the iroh identity hash
  when default.

Not done (next steps, all behind the same flag):
1. Cross-process probe: a `prefix_pin`/`prefix_settle` managed-control op to member nodes (and the node-side
   `prefix_cache=` construction in `physical_inference_node`, the Adaptive-budget -> `set_prefix_cache_budget` call,
   and attaching a `PrefixPlanner` to the live entry). Until then only same-process stages (fixture, in-process
   mesh) can reuse; a live route runs cold.
2. `start_distributed_prefill` (progressive path built hop by hop) is not planned: the entry does not know the stages
   up front, so it always runs cold.
3. MLX has no fixture test here (the NumPy fixture is the exactness oracle); run the device measurement in section 6.
4. Surfacing `prefix_reused_tokens` in the gateway receipt / diagnostics bundle (the counter is in `kv_snapshot()`,
   which node status already forwards).
