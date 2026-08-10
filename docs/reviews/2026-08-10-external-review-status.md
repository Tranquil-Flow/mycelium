# Mycelium external review status — 2026-08-10

## Reviewers

- An independent Codex reviewer inspected the staged diff, tests, plans, code paths,
  and private evidence references read-only.
- Hermes Agent using `deepseek-v4-pro` with `xhigh` reasoning inspected the same
  repository read-only. Claude Code was attempted first, but Anthropic rejected the
  run because the local session limit was exhausted; Claude produced no review.

Hermes began before the first review fixes were applied and completed while those
files were changing. Its raw conditional no-go was therefore useful for discovery but
is not itself a post-fix release verdict. This file records the independently checked
disposition.

A second Hermes `deepseek-v4-pro` read-only pass inspected the post-fix code and found
no additional unpatched security defect in the reviewed request path. It independently
confirmed the cancellation chain, terminal request cleanup, bounded body handling,
owner-only evidence writer, and current-versus-planned capability labels. That pass ran
concurrently with the fresh physical proof and therefore reported the earlier
stable-peer/test state; the owner-only status digest and primary local gate results below
supersede those stale environmental observations.

## Accepted findings and disposition

| Finding | Disposition |
| --- | --- |
| Port 8791 was not serving a qualified route | Confirmed. Both deployments were restaged and requalified. Evi then went offline during the next request, exposing the still-open M18 liveness gap. Never call the current route healthy while that peer is absent. |
| Browser cancellation did not reach physical inference | Confirmed in the reviewed version. Fixed: cancellation is now request-signalled, sends `infer_cancel` between bounded node commands, verifies physical cleanup, and returns `CANCELLED`. A fresh browser request emitted one token, was cancelled, advanced both physical-stage counters, incremented cancellation release on both peers, left both KV counts at zero, and kept the route non-fatal. The owner-only status is `m8-qwen2.5-int8-two-host-mlx-v4/review-live-status-20260810.json` with digest `sha256:9bb18fe80465503dadd3752c3d072a827881dc3fcc8027284f88d7b6ad2b1429`. |
| Completed requests leaked adapter sinks and prompt/output token arrays | Confirmed. Fixed with deferred terminal release through registry, router adapter, and physical route. |
| Loopback wrapper read an unbounded declared request body | Confirmed. Fixed with pre-ASGI `Content-Length` validation and a 262,144-byte bound; regression test passes. |
| Quality evidence files were world-readable by default | Confirmed. Fixed writer uses owner-only mode `0600`, including overwrites. Existing private evidence should also remain owner-only. |
| M11 has no coherent sealed release manifest | Confirmed and still open. Task 0 of the post-M11 plan is the release gate before M12. |
| Live-clock membership uses an ephemeral seed signer | Confirmed and still open. It prevents frozen expiry but is not durable seed identity. Unified persistent membership remains M20 scope unless separately pulled forward. |
| Quality refusal was described as model behavior | Confirmed documentation issue. The refusal is deterministic gateway policy; three other cases are model output. Canonical architecture text now says so explicitly. |
| `ASTRA_CURRENT.md` was dangerously stale | Confirmed. It now points to `CURRENT_AND_PLANNED_ARCHITECTURE.md` as the canonical review entry point. |
| Live UI has split special status/registry projections | Confirmed, accepted MVP limitation. M12 unifies the evidence spine. |

## Rejected or stale Hermes findings

| Finding | Resolution |
| --- | --- |
| M8 uses `complete_context_replay` by default | Rejected for the physical product route. `FakeLiveRoute` intentionally reports replay mode because it is a simulated test double; the qualified physical status reports `decode_mode=stage_local_kv` on both peers. The cited plan prose described the pre-M8 baseline. |
| `LiveRoute` lacks cancellation/release contract | Stale because Hermes read during the patch. The protocol and both implementations now include the cancellation callback and request release. |
| Cancellation does not send `infer_cancel` | Stale because Hermes read during the patch. The physical route now sends `infer_cancel` whenever caller cancellation is observed while status is `DECODING`. |
| M7 is open because an earlier progress paragraph says so | Documentation ambiguity, not contrary runtime evidence. That paragraph was an intermediate checkpoint; later accepted progress records closure. It has been relabelled to prevent misreading. |
| Python count is 3,449 rather than 3,489 | Rejected. The primary clean post-fix run produced 3,492 passed, 12 skipped, and 121 subtests in 258.72 seconds: the prior 3,489 plus exactly three new review regressions. Hermes' result was not reproducible. |

## Remaining no-go conditions for sealing M11

1. Rebuild the 1.5B deployment after its final-review `decode_completion_timeout` and
   preserve a fresh completed browser request with both stage deltas and zero KV state.
2. Create one manifest that binds each milestone claim to its source state, deployment,
   physical run, evidence digest, and qualification result. Do not splice facts from
   different deployments into one claim.
3. Decide explicitly whether durable seed identity is required to call M11 sealed or
   remains an acknowledged M20 successor feature.

The stable remote peer, normal browser inference, browser cancellation, zero-KV
cleanup, and post-review Python/UI/Rust/contract/security gates have now been rerun.

Until these close, the correct verdict is **M11 review candidate, conditional no-go
for sealing, suitable for Astra architecture review**.
