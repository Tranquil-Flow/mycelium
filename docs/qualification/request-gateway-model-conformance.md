# Request-gateway model conformance

Date: 2026-07-18

Exact base: `25c8b1b1e9dc8025f81789bee7d62627bd19adea`

Branch: `test/request-lifecycle-conformance`

Readiness claim: `route_ready=false`

## Scope and oracle

`mycelium_request_conformance` is an independent, pure-stdlib, immutable state
machine. It imports no request-gateway, Router, qualifier-transition, transport,
or runtime implementation. Its phases are `NEW`, `ADMITTED`, `STREAMING`, and
exactly one of `COMPLETED`, `CANCELLED`, or `FAILED`. Admission and backend start
are separate transitions.

The model covers exact-current admission; deployment, epoch, path, evidence,
qualification, and readiness revalidation; ordered bounded tokens; exact and
conflicting replay; terminal-once behavior; at-least-once event delivery until
ACK; disconnect/reconnect/resume; acknowledged replay trimming; bounded
backpressure; resource side-effect counters; and reachable session-capacity
transitions. Request identity, prompt, and token material are retained only as
SHA-256 digests where plaintext is not required for an authenticated stream
event.

The deterministic corpus contains 91 unique reachable bounded traces and 45
unique reachable serial race linearizations. Inapplicable symbolic replay,
backend callbacks after worker termination, and disconnect without an attached
public stream are excluded instead of being counted as production coverage.
All 136 traces are replayed twice against the model and driven through
`RequestGatewayService`, `EventSubscription`, `QualificationSource`,
`InferenceBackend`, and metrics public interfaces. Comparison includes safe
event projections, stable error codes, runtime/cancel/capacity/KV counters,
buffer maxima, terminal counts, cleanup, and metrics.

A separate simultaneous race test releases token, cancellation, qualification
revocation, disconnect, and completion operations from one barrier for 25 fresh
sessions. Every final event/counter/phase projection must belong to the model's
reachable serial linearizations; operation returns are checked and operation
exceptions are forbidden.

Synthetic qualification fixtures establish local contract behavior only. They
do not establish physical qualification.

## Preserved RED counterexamples

Tests were committed before corresponding production repairs:

- `1738bc4`: initial model/minimizer/production cross-check. The focused
  comparison observed `1 passed, 7 failed`. Minimal conflicting-token trace:
  `admit -> token(0, alpha) -> token(0, different)`. Production completed where
  the model failed closed. Completion also failed to detect current-authority
  changes.
- `49b8e9f`: `admit -> paused revalidation -> cancel -> release` entered backend
  runtime after cancellation. The named counterexample failed before the
  cancellation/start repair.
- `65b930b`: lifecycle-review counterexamples. A clean checkout of that commit
  runs the then-current full conformance directory as `23 failed, 14 passed`.
  Failures include pre-start backend cancellation, non-UTF-8 token divergence,
  and model/capacity expectations intentionally awaiting repair. This replaces
  the earlier unsupported claim that the full directory had exactly six
  failures.
- `22a3d69`: bounded-token counterexamples. The targeted command observed
  `2 failed, 2 passed`: model and production accepted a 1,048,577-byte backend
  token instead of failing before token-buffer growth; sticky start-gap tests
  already passed.

Deletion-1 minimization retains side-effect counters in its predicate. The
minimal conflicting-token counterexample is
`admit -> start -> token(0, alpha) -> token(0, different)`; deleting any action
removes the specified failed state.

## Narrow production repairs

Only `mycelium_request_gateway/service.py` changed:

1. revalidate captured qualification across start, token, and completion;
2. retain at most `max_new_tokens` SHA-256 token digests while a worker runs;
3. make exact token replay side-effect-free under unchanged authority;
4. reject conflicting, future, or over-limit token indices as
   `token_order_violation`;
5. reject invalid UTF-8 or UTF-8 token text above 1 MiB as
   `invalid_backend_token`;
6. encode and hash a token once, outside the session lock and backpressure loop;
7. linearize logical backend start against cancellation under the session
   condition;
8. require backend cancellation to be idempotent and sticky across the narrow
   logical-start-to-`run` entry window; the production Router backend and a
   deterministic gap backend both satisfy this requirement;
9. avoid backend cancellation when cancellation or revalidation wins before
   logical start;
10. emit terminal exactly once and clear captured request state and token
    digests after worker release.

No contract schema, qualifier internal, Router/MLX/KV implementation, native or
iroh transport, Observatory, or other production file changed.

## Trace and counter evidence

Exact token replay under unchanged authority produces no delta in runtime,
buffer, capacity, event, cleanup, terminal, failure, or completion counters.
Before and after replay:

```text
runtime_starts=1, backend_cancels=0
capacity_acquires=1, capacity_releases=0, active_capacity=1
kv_acquires=1, kv_cleanups=0, active_kv=1
buffered_events=2, maximum_buffered=2
terminal_events=0, token_events=1, failures=0, completed=0
```

Its terminal trace is `[accepted, token, completed]` with one runtime, token,
terminal, capacity release, and KV cleanup. Conflicting replay produces
`[accepted, token, failed(token_order_violation)]`, one backend cancellation,
one failure terminal, and exactly one capacity release and KV cleanup.

Cancel-before-start produces `[accepted, cancelled]` with
`runtime=0, backend_cancel=0, capacity=0/0, KV=0/0, active=0/0`. Revocation
between admission and start produces `[accepted, failed(readiness_revoked)]`
with the same zero backend/resource counters. Cancel-after-start produces one
backend cancellation and exactly one capacity/KV cleanup. The explicit
logical-start/physical-entry gap test confirms sticky cancellation prevents
runtime/resource acquisition when cancellation lands in that gap.

Generated request-ID collisions, with equal and conflicting prompts, fail as
`duplicate_request_id` with zero runtime, event, terminal, failure, capacity,
KV, buffer, or metrics delta. Capacity-model admission now makes room before
duplicate detection, matching production `submit` ordering.

## Verification

Final gate results after the last repair:

- request conformance: `53 passed`;
- existing request gateway: `40 passed`;
- full Python: `1018 passed, 2 skipped, 117 subtests passed`;
- contract audit: `14 contracts`, passed;
- compileall: passed;
- `git diff --check`: passed;
- release-security audit: `365 tracked files`, passed;
- claim-boundary audit: `141 source files`, passed.

## Frozen semantic gap

True duplicate HTTP request replay remains unqualified. The frozen
`InferenceSubmission`/POST contract has no caller-supplied request identity or
idempotency key; request IDs are generated only after qualification capture.
An exact or conflicting network retry therefore cannot be correlated through
the public interface. Generated-ID collision fails closed but is not an
idempotent retry mechanism. Repair requires a frozen cross-lane contract change,
so this lane did not modify schemas or claim request-submission idempotency.
Token replay and acknowledged stream replay are covered.

## Claim boundary

Evidence is deterministic and local. It does not prove authenticated network
transport, iroh/native behavior, physical route qualification, real Router
MLX/KV execution, distributed timing, scheduler fairness under physical load,
or exhaustive thread schedules. Race evidence covers every generated reachable
serial linearization plus 25 simultaneous barrier releases, not all possible OS
schedules.

The batch handover explicitly assigned this lane's new model, tests, narrow
request-gateway repairs, and report. The older active-lanes manifest does not
name this later batch branch and was not edited because governance metadata is
outside assigned ownership.

No credential, network, installation, remote host, push, PR, physical
qualification, contract-schema change, qualifier-internal change, Observatory
change, native-transport change, or Router execution change was used.
