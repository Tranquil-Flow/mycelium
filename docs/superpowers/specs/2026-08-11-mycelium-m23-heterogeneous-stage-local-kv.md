# Mycelium M23 Heterogeneous Stage-Local KV Specification

**Status:** implementation specification
**Milestone:** M23
**Parent architecture:** `2026-08-09-mycelium-astra-architecture-product-design.md`

## 1. Outcome and boundary

M23 makes dense Qwen2 and Qwen3 decoding genuinely incremental on qualified routes
that mix MLX and NumPy placements. Every decoder placement owns only its assigned
layers' key/value state. After prefill, decode transports one token or one-token
activation at a time; it does not replay the full context through a NumPy stage.

M23 does not add tensor parallelism, continuous batching, KV migration between
backends, or mobile activation eligibility. Pixel remains `complete_context_replay`
unless a later device-class qualification proves otherwise. A backend or architecture
without an exact incremental implementation keeps the whole candidate route in replay
mode or rejects the candidate; it is never silently labelled stage-local KV.

## 2. Decode-mode authority

Decode capability is bound to architecture, backend, representation, and runtime
build. Dense `qwen2` and `qwen3` may advertise `stage_local_kv` for MLX and NumPy only
after their focused parity gates pass. Flat backend capability must not imply support
for every architecture.

One execution graph has one qualified decode mode. Every active placement must support
that exact mode. Runtime construction derives the mode from the loaded, assignment-
bound architecture and active backend set. An unknown architecture/backend, Pixel
placement, mixed declared mode, or missing capability fails closed before Router work.

The qualification and product projections distinguish:

- `complete_context_replay`: every decode operation carries the committed context;
- `stage_local_kv`: prefill creates local state and each decode operation consumes one
  new position;
- unsupported or unqualified: route admission is withheld with an exact reason.

## 3. NumPy KV execution

The NumPy RuntimePort implements Qwen rotary position offsets, grouped-query KV heads,
Qwen3 query/key normalization, causal attention over cached plus current positions,
and assignment-local layer ownership. Cache identity binds request, path and attempt,
placement and assignment, deployment epoch, manifest, layer range, position, sequence,
lease, architecture, dtype, and quantization.

Prefill creates exactly one state per path and placement. Decode requires the next
position and sequence, a one-token payload, the same lease, and the same bound
identity. Duplicate idempotency keys replay the prior result without advancing state;
conflicting replays fail. Missing, stale, expired, released, or cross-path state fails
closed.

Normal completion, cancellation, timeout, lease expiry, route release, and worker
shutdown remove all KV arrays and replay entries. Public snapshots expose only mode,
identity digests, positions, layer count, aggregate bytes, counts, and release reason;
they never expose tensors, activations, token arrays, prompts, or output.

## 4. Correctness and performance gates

Deterministic tests cover Qwen2 and Qwen3, tied and untied heads, float and int8
weight-only representations, entry/intermediate/final stages, prefill plus multi-token
decode, idempotent replay, bad position/sequence/lease/identity, cancellation, expiry,
normal completion, and close. Incremental logits and greedy tokens must match complete-
context NumPy and the existing MLX implementation within the representation's declared
tolerance.

A physical A/B run uses the same model, prompt set, stage allocation, hosts, and
transport. It records prefill, TTFT, TPOT, total time, activation bytes, frame counts,
per-placement operations, peak KV bytes, cleanup, and exact output. Promotion requires:

1. exact greedy-token parity for every prompt;
2. one-token decode payloads on every stage after prefill;
3. nonzero stage-local KV on the NumPy placement while active and zero after release;
4. no increase in fatal, cancellation, or cleanup failures;
5. a measured TPOT or decode-work reduction over replay, otherwise the mode remains
   implemented but not performance-qualified.

## 5. Product and UI contract

Inference shows the qualified decode mode, prefill-to-decode transition, current
position, and waiting activity without exposing cache contents. Nodes and Readiness
show per-placement backend, architecture, mode support, active state count, aggregate
KV bytes, watermark, and release state. Plans explains why a candidate selected KV or
replay and names any incompatible placement. Incidents distinguishes missing state,
lease expiry, incompatible successor, replay fallback, and cleanup failure.

Refresh, section switching, reconnect, and terminal history preserve the qualified
mode and terminal cleanup result. The UI must revoke `stage_local_kv` when the current
qualification or runtime evidence is stale, missing, mixed, or failed.

## 6. Completion gate

M23 is complete only when local contract/adversarial/parity suites pass, the three-host
MLX/MLX/NumPy Qwen route completes a real browser prompt in `stage_local_kv`, every
stage advances physical counters, the UI shows the live per-placement mode and cleanup,
and the sealed A/B evidence reports parity and the measured performance result. A
synthetic runtime, all-MLX route, replay-only heterogeneous route, or modeled speedup
cannot close the milestone.
