# API boundary (L10)

This crate is a **partial, service-backed HTTP port boundary**, not a runnable
Mycelium application and not L10 acceptance. It never instantiates a planner,
model runtime, authority, directory database, or fake product service. Test fakes
live only under `tests/`.

## Boundaries

- `http::serve_connection`: accepted-stream HTTP/1 adapter. The host owns the
  loopback listener, thread/admission bounds, shutdown and registered I/O budget.
  Host/auth/Origin preflight precedes slow body reads. This adapter is one request
  per connection; native directory chunked ingress is **not yet implemented**.
- `desktop::Desktop`: desktop Host/session/Origin/action-header and body guards,
  one-use form launch, JSON action seam, immutable assets and status. An action
  service must implement exact route allowlisting, action schema/authority,
  error mapping, and publication-before-success. `Session::native` ports native
  reauthorization, one-use form exchange and registry-derived exact expiry using
  an injectable monotonic clock. Native authorization is distinct from browser
  cookies; `/native-session` rejects Origin and requires the native credential.
  `NativeLauncher` delegates private launch-file publication/consumption, secure
  token entropy and closed-state ownership to the host. These host effects,
  pairing and other delegated listeners are **not yet integrated**. The legacy
  `Session::new` is for controlled callers/tests, not a production TTL override.
- `directory::Directory`: signed-route transport checks, duplicate-key JSON,
  GET/HEAD and static assets. Signed inputs remain untrusted until the service
  enforces the exact document shape, signature, freshness, ownership, replay,
  sequence, rate and persistence rules. `schemas.json` freezes source field sets;
  it does not replace that validator. No unsigned-success implementation exists.
  `preflight` refuses credentials before reading a posted body; `handle_from`
  passes a peer/proxy-trust-derived rate key to the service. `link_svg` dispatches
  GET/HEAD to an injected signed-record lookup/QR renderer; `swarm::service::NativeQr`
  (`qr`, a port of the vendored Nayuki encoder) is byte-identical to v0.1.4's `qr_svg`.
  Legacy `handle` has unknown client identity and is not a rate-authority adapter.
- `actions::Control`: typed run-model, fast-mode, demo/network and serving routes,
  shape guards and L1-derived relay bounds. Serialized backend prepare/execute/
  publish preserves authority ordering without holding the status mutex. Role,
  nested consent, rollback and real effects remain backend work. `persistent::PersistentBackend`
  persists fast mode, run-model choice, owner-creation consent (`resource-policy.json`) and
  standing-consent withdrawal exactly as v0.1.4 and delegates everything else; the desktop host
  supplies `persistent::Host` facts/effects (authority, relay, acquisition).
- `pairing::SignedBackend`: verifies `/api/pair/preview` with real Ed25519,
  closed fields, exact source refusal ordering/lifetime and a minimal public
  projection. Its wrapped host still fences closed/owner state and publication;
  verified identity is not membership, consent or an issued invite.
- `chat::Chat` / `WithChat`: public owner-chat assets and JSON polling routes,
  custom-header guard, error/retry JSON, relay IPv6 /64 and mapped-IPv4 identity.
  Chat events are **not SSE**. Only trusted listener configuration enables relay
  identity. Admission, generation/history and accounting remain service-owned.
- `requests::Gateway` with `http::serve_response_connection`: isolated bearer
  request gateway, submit/cancel/status and streaming HTTP SSE. Owner token and
  Last-Event-ID parsing precede subscription; frames bind request/generation and
  resume cursor, flush before ack, and close subscriptions on all return/error
  paths after dispatch. Cleanup errors retain a simultaneous transport failure.
  Host owns bounded/cancellable subscription polling, disconnect detection,
  entropy and L9 terminal authority. The gateway now always applies the shared
  L5 event validator before optional host checks. This does
  not implement the browser BFF or compatibility stream translators.
- `signed_requests::SignedService`: concrete single-admission L5 grant → L9
  session → HTTP service adapter. Real signed receipts precede token decoding;
  native success is not terminal proof. Signed router cleanup plus actual host
  residue observations settle the terminal. Completion cannot be reversed by
  late cancel; accepted cancellation wins over racing unsettled completion.
  v2 lifecycle/token/terminal order and v1 field differences are tested. Prompt
  and bearer binding use digests; the owner token is consumed by acceptance.
  Hosts supply currentness/clock, tokenizer/native dispatch, authenticated
  cleanup and entropy. Multi-request registry/retention, signed generation
  rebinding/replay, public qualification and recovery remain OPEN; unsupported
  generation reuse refuses rather than relabeling an old signed grant.
- `status::PublishedStatus`: build snapshots before publishing; the lock only
  swaps/clones an `Arc`. Serialization and old-snapshot destruction are outside
  the lock. `StatusOwner` binds tickets to one owner and lifecycle generation;
  collection-start revisions are NOT runtime generations. `refresh` mints the
  ticket before reading the source outside the status lock, retains both source
  and settlement failures, and fences older completions/failures. `advance`
  fences old collectors even before replacement collection succeeds. Legacy
  publishing cannot bypass an owned target. Retain the last good observation
  on failure: display state is never admission authority. L6/SLICE-1 must sample
  `(generation, snapshot)` atomically and advance the owner on lifecycle replace;
  nested projections and real refresh/action composition remain open.
- `sse::EventStream`: immediate-flush request-event serialization, per-connection
  identity/sequence and terminal fencing, explicit write errors, credential-byte
  refusal. The embedding service must supply an L5-backed `EventValidator` and
  enforce frame/queue limits, replay, request ownership and terminal arbitration.
  This is **not** the browser SSE proxy, compatibility stream translators or a
  cancellation service. Test `FakeProtocol` is deliberately not an L5 validator.

## Evidence and scope

`contracts/routes.json` is a provenance-backed **216-route discovery inventory**,
not exhaustive shipped/semantic closure. Its per-route `semantic_review` and
`coverage` fields are authoritative; unresolved transitive schemas/auth/error
sets must not be interpreted as complete contracts. All 129 previously pending
entries now have dispatch reviews (216 reviewed total), with explicit nested
closure gaps, source spans and privacy hazards. None claims complete transitive
closure or independently established packaged reachability. All source references pin
Python commit `f4e366e98a1f705dfaf182897044500d290dbd7f`.

`assets/` contains 11 byte-identical desktop/chat/directory files; hashes and
source paths are in `contracts/assets.json`. This does not establish DOM/browser
qualification. `contracts/schemas.json` freezes the 30 desktop status fields,
directory signed-document key sets, and browser bootstrap paths/fields from the
same source. Nested schema validators are intentionally not invented here.

Tests use real loopback HTTP, unchanged L3 comparator/scalar golden assertions
for eleven observations, a held fake backend for F5 and actual HTTP SSE framing.
The signed HTTP submit → SSE → cleanup-completed → late-cancel flow is a local
composition test with controlled native effects, not an unchanged L3 journey.
Signed preview contributes two golden observations, not two whole journeys.
Concurrent authenticated status requests finish while both fake backend and
collector phases remain held; a lock-across-collection MODEL fails this test.
Four copied-fault controls reject disabled chat role/typed shape guards,
ack-before-write and wrong QR content type. They are retrospective discrimination
checks, not evidence of real authority, generation or persistence. Round 3 adds
generation-fence and expiry-equality mutation controls; fake-clock native renewal
also crosses real TCP HTTP. JSON ingress retains duplicate rejection, scalar
types and forged-private-number objects under workspace arbitrary-precision
feature union. The added onboarding observation checks unchanged asset defaults,
not browser interactions or a full onboarding journey.
No test launches Python or a model. Full L3 journeys remain unrun against this
partial crate. The external report/evidence directory is:

`/Users/mycelium-dev/Documents/mycelium-core-program/runs/v2/v02/L10-API.md`

## Integration request

Preview 2 is merged; gates run directly in this worktree with
`--offline --locked` (no mirror/private lock). Under the rev-3 edge-only rule,
API dependencies on already-locked `protocol`, `runtime`, `router` and
`base64 0.22.1` were admitted and verified in separate commits; no package,
version or checksum changed and no download occurred. Adopt the additive signed
adapters in the host without bypassing shared lifecycle authority. Signed
directory storage/replay/rate authority is still unimplemented, not blocked on
those now-resolved lock edges. See the round-4 report for remaining seams and
separate observation/journey counts. Do not merge into main yet.

## Round 8: wire bytes, join/pair routes, headless launcher

* `api::wire` reproduces `json.dumps(v, separators=(',',':'))` byte for byte: an
  insertion-ordered `Json`, `ensure_ascii` escapes (surrogate pairs, lowercase hex), CPython
  float `repr`. Desktop action responses, `/api/status` (host order, `status_snapshot`
  last) and the chat page use it; the directory (`ensure_ascii=False`, own `J`) and the
  request gateway (sorted, raw UTF-8) already matched. Proven against 4,994 executed
  `json.dumps` cases and the released handler's bytes for every ported route.
* `resource-policy.json` is read through `json.detect_encoding` (utf-8/-sig, utf-16/32 with or
  without BOM); `run_model::{choice,load_choice,save_choice}` return the collapsed
  `action_not_completed` like the HTTP handler.
* `/api/join/{request,window,window/poll,window/cancel}`, `/api/pair/{request,approve,accept,
  window/*}`, the owner role gate, `/api/revoke*`, `/api/serving/status`, `/api/quit` and the
  handler's `last-action-error.private.json` live in `persistent::PersistentBackend`; relay,
  join transport, authority and window-store effects are `Host` seams that fail closed.
* `cargo build -p api --example headless` builds a minimal desktop host (not a product
  binary) that the unchanged L3 baseline corpus launches with `--headless --offline`. It owns
  one identity, the owner's one-member swarm record and process lifetime; everything else is
  a seam (see `runs/v2/v02/L10-evidence/round8-seams.md`).

## Shared owner-chain composition

The headless host now constructs `SharedHostChain<HeadlessChainPorts>` over
`owner_run::owner_chain::OwnerChain`. The shared chain owns all step dispatch and
partial run state; the API wrapper only preserves historical missing-port advice
and the pre-run optional state view. The original `HostChain::new` constructor
remains available through a compatibility adapter, including its accepted-model
notification. Compatibility capability accessors must remain available throughout
one step; the actual headless host uses directly borrowed independent fields.

The host shares its existing local agent between contributor/report adapters,
without retaining a core lock while ticking it. Its Core holds a non-owning
RunnerView, not an owning Runner. Observe/compile remain absent; slices/work
retain the shared default-None ports. A source manifest, installation `Source`
enum, or signed receipt is not a compiled workflow or a complete source plan.
There is no new native-session factory, serving success, or cleanup claim.

Acquisition repository count, path-part count, stream chunks and additive close
slack use their own registered origins. Local-manifest character/part policies
use the separate physical-deployment origins even where values match. The close
allowance adds to the caller's transport timeout; it does not cap an override.
HTTP redirect/framing bounds still need separate provenance adjudication.

This source checkpoint requires fresh guarded compilation, API/shared-chain
regressions, actual headless-process tests and strict scoped lint qualification.
It is not a qualified runnable-workflow claim.
