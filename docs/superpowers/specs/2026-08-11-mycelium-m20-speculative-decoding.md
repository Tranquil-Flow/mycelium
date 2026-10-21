# M20 Spec — Qualified Speculative Decoding

## Status and boundary

M20 is an optional optimization over an already-qualified target-only route. The
target model remains the sole token authority. A draft model may propose token IDs,
but no proposal becomes user-visible until the target verifies it. Disabling or
losing the draft must preserve the qualified target-only result.

This milestone does not weaken model, membership, placement, route, or resource
authority established by M12–M19. It does not claim speculative execution merely
because a compatible model is present. Promotion requires closed compatibility,
runtime-capability, parity, cleanup, and measured-gain evidence.

## Closed contracts

M20 owns two privacy-reduced contracts:

- `mycelium.m20_speculative_plan.v1` records the exact target/draft identities,
  tokenizer, position and KV compatibility, proposal width, workload, measurements,
  material-gain threshold, decision and target-only fallback.
- `mycelium.m20_speculative_runtime.v1` records request-level counts and state:
  proposed, target-verified, accepted, rejected, rollback, fallback, cancellation,
  terminal state and bounded cleanup. It never contains prompts, decoded text,
  logits, raw token IDs, tensors, credentials or paths.

Both contracts are closed, canonically digested and bound to deployment ID/epoch,
graph digest, membership generation, model ID/revision and qualification identity.
Unknown or private fields fail validation.

## Compatibility and admission

Before admission the target and draft must have distinct immutable identities and
matching tokenizer, vocabulary, special-token, position-semantics and verification
interfaces. Draft KV is draft-owned and target KV is target-owned; cross-model KV
transfer is forbidden. A target verifier must support one batched verification call
for a bounded proposal. Missing measurements, a non-material predicted/observed gain,
or any incompatibility leaves target-only mode selected.

The default proposal width is four. The planner evaluates a declared workload from
observed target decode, draft decode, batched verification, proposal transfer and
acceptance-distribution measurements. Promotion requires at least a 10% end-to-end
gain over target-only decoding and a lower-confidence result above that threshold.

## Runtime semantics

For greedy decoding the draft proposes up to the admitted width. The target verifies
the proposal positions. The runtime accepts only the common prefix. At the first
mismatch it rolls draft state back to the accepted prefix and emits the target-owned
token for that position. If all proposals match, the verified proposal is accepted.
Sampling configurations must bind the same seed and target sampling policy; otherwise
speculation is rejected.

Draft loss, verification failure, circuit-break opening or policy disablement causes
an explicit target-only fallback from the target-owned committed watermark. Target
loss remains an M19 recovery/abort event and can never be hidden as draft fallback.
Cancellation releases draft proposals plus both model-owned request states before one
terminal result is recorded.

## Physical gate and UI

The physical gate probes the currently bound route and only local model assets. It
must either:

1. prove target-equivalent output, rejection, draft loss, cancellation, cleanup and
   a material observed gain; or
2. publish a closed disabled decision with the measured incompatibility, unavailable
   batch-verifier capability, or insufficient gain.

Plans shows identities, proposal width, acceptance, verification cost, predicted gain,
decision and fallback. Inference shows target plus optional draft overlay and runtime
counts/state. Settings exposes the preference only when the bound plan is qualified
and enabled; otherwise it is disabled with the exact measured reason.
