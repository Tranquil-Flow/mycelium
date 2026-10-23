# Entry-driven token path (v0.1.2 optional fast mode)

## Option A release candidate: explicit opt-in, default OFF

This reversible desk candidate is not certified or approved for publication.
With `MYCELIUM_TOKEN_PATH` unset, or explicitly `off`, the production serving
factory retains the V1-compatible `per_layer` materialization and legacy
peer-evidence path. Fast mode is selected explicitly by setting
`MYCELIUM_TOKEN_PATH=on` in the owner/application process environment before
starting a new serving route. ON selects token control and `per_stage`
materialization. Stop serving before changing the selection; this is not a
mid-request toggle. To return to the V1 path, restart the owner/application
with the variable unset or set to `off`, then start a new route.

There is no new GUI switch in this port. The existing operator environment
setting is the opt-in; values other than the exact strings `on` and `off` are
rejected. The application supplies authenticated owner/member trust and signed
native grants; the environment setting grants no authority and cannot bypass
missing trust, membership, lease, signature, or capability checks.

Fast mode does NOT support mid-request failover or recovery (A6 replay or A7
KV continuation). If a participating Mac or remote stage drops during a
request, the request must end with an honest, bounded failed terminal, not hang
or claim completion. Already verified output is not a promise of continuation.
A separately restarted route may accept a new request; that is not recovery of
the interrupted request. Use default OFF when V1 recovery behavior is required.
The desk guard is `tests/live/test_a5_remote_stage_loss_terminal.py`, run with
`MYCELIUM_TOKEN_PATH=on` as well as OFF; it checks remote loss during prefill
and decode, including failure publication while a replacement is held. Its
bound is the existing detection plus cleanup budgets and scheduling margin,
not an unlimited wait. Real two-Mac/app qualification remains a parent gate.

Historical design and development evidence below are not release speed claims.

## Coalesced receipts

The managed native start path permits one unacknowledged signed receipt per
request. While its authenticated owner ACK travels through the pinned TLS pump
and native command lane, generation accumulates unsigned contiguous tokens.
The next delivery signs the entire accumulated range; one ACK of that exact
statement digest retires the range through its exclusive `end_index`. No
per-token ACK or second producer thread is introduced. A terminal drain waits
only inside the original grant/cancellation fences, never a renewed budget.

Each receipt binds its grant, sequence, exact `[start_index, end_index)` token
range and predecessor statement digest (the first predecessor is the grant
digest). Both producer ranges and received ranges are checked: gaps, overlaps,
wrong digests, bool-as-int values, changed grants and budget overruns fail closed.
The signed statement covers every token. Default mailbox buffering is bounded
by the admitted request token budget, not 64 receipt objects; explicit smaller
capacities remain available for overflow fault-injection tests. Receipt and
private-sealer bounds accommodate the maximum admitted 65,536-token range.
Durable pump records store independently bounded canonical receipt text, so
multiple ranges do not exhaust the aggregate JSON-node limit. Pending receipt
state is capped at 4 MiB and the concurrent pump's single outbound state at
2 MiB, on both write and reload; other private-state defaults are unchanged.

Slow valid delivery therefore increases range size instead of exhausting the
native receipt queue. Revocation, expiry, malformed delivery and genuine resource
failure still stop the request; owner reconciliation retains the last verified
prefix and uses the existing signed cleanup path. Default activation remains
OFF. The standalone low-level driver retains its non-coalesced diagnostic mode;
production managed native dispatch selects coalescing. This change does not
qualify A6/A7 continuation, physical devices or a packaged application.

## Phase 8 checkpoint (in progress; default OFF)

The native progress bridge now distinguishes ordinary command exhaustion from
corrupt/invalid receipt failure. After every verified prefix and its native ACK
has drained, an exact durable native rejection can use the existing signed
managed failed-command ACK. It does not invent a receipt terminal or successful
completion. Successful commands without a verified terminal and malformed
receipts remain fail-closed. Independent control remains usable after rejection.

Evidence: `runs/v2/tokenpath-phase8/refusal-red` (2 behavioral failures), then
`refusal-final` (36 passed). These are real CPU-child/worker/TLS/pump tests with
fake compute/load, not A6/A7 continuation or device evidence. Startup fixture
probe hit the existing MLX import guard via an unrelated plan-binding test import;
its three failures are fixture setup failures, not token-path behavioral RED.

The joined two-native CPU/Iroh/TLS/governed-chat fixture now injects a tail-stage
runtime loss after the first token. OFF established `decode_completion_timeout`;
ON initially masked it as `token_progress_terminal_missing`. The route now runs
ordinary native rejection validation before resolving its start future. Both
arms retain one visible token, the same failed chat code, signed cleanup on both
participants and zero active runtime state, with no legacy fallback. Evidence:
`stage-loss-parity-red` (OFF passed/ON failed), `stage-loss-green` (39 passed).
Earlier attempts retained an incorrect timeout-handling fixture and an unmeasured
expected code; they are setup/oracle diagnosis, not credited as behavioral RED.
This tests loss without recovery authority, not A6 restart/A7 successor parity.

Startup now separates signed participant installation from per-request grant
creation. `_qualified_runtime` installs policy after qualification/stop-token
binding and before publishing ready runtime. Partial installation closes the
route, cannot retry, and cannot mint a grant. The pure plan fixture was extracted
from its MLX-importing test module; the existing three workflow startup cases now
run under the unchanged MLX fence. Evidence: `startup-install-red` (2 API-absence
failures), `startup-caller-red` (2 behavioral failures/9 passed), `startup-final`
(31 passed). The caller test fakes qualification/session responses. The later
`joined-startup-final` (68 passed) also composes `_qualified_runtime` → real
`route.open`/hello/configure/start → exact legacy startup challenge → live
qualification issuer → signed installs on BOTH participants → governed chat.
Both modes complete; ON issues no request grant until both installs succeed.
Startup's one start/one decode are counted separately from the chat request.
This is installed-workflow CODE on two CPU children, not an installed app run.
Model load/compute, discovery host labels, resource metadata, plan/codec and
projection loading remain explicit synthetic fixtures. Both processes run on
ONE actual host; their synthetic host labels are NOT physical qualification.
Real per-process SQLite capacity synchronization is used for the startup path.
Earlier native-startup failures were fixture defects (identity/configure schema,
missing cross-node capacity synchronization, host distinctness and digest shape),
NOT behavioral RED. All logs are retained; no production checks were weakened.

HTTP/UI attribution is now joined to the CPU-native route. The real request ASGI
gateway, same-origin auth/CSRF gateway, loopback HTTP/SSE, RegistryClient, ChatService
and shipped chat JavaScript run together. Both OFF/ON preserve `served_by` on
state, acceptance, replayed events and the Chromium-rendered node/layer labels.
HTTP emits six visible tokens; the browser arm uses its real 128-token selector
and suppresses the sixth generated EOS, yielding five identical visible tokens.
Both still execute six rounds per stage, ON one start/zero decodes, OFF one
start/five decodes, with signed cleanup and zero active runtime state.
`http-page-final`: 36 passed. Earlier browser failures were fixture-selector,
premature metadata-wait and EOS-oracle errors, NOT product behavioral RED.
No production change was needed for this characterization. Owner status,
qualification/model metadata and compute remain explicit private desk fixtures;
no installed app/public relay/device was used.

Recovery remains BLOCKED beyond the proven no-successor loss terminal parity.
`tests/live/test_token_recovery_boundaries.py` pins three passing refusal cases:
a fresh signed A6 attempt cannot reuse an already-installed process gate, and
direct/queued A7 successor hops cannot use the old grant. A never-used participant
can admit the fresh grant, but that is not end-to-end restart qualification.
The joined final batch includes these cases. They are characterization, NOT RED
or evidence of continuation. A6's recovery challenge and A7's committed successor
need separately admitted authority/cursor reconciliation; no path, generation or
replay fence may be weakened to make them pass.

### Resumed phase 8: executable recovery blocker (2026-10-22)

The resumed worktree was clean at `094fc8b2`; no inherited work was discarded.
`resume-focused-corrected` freshly passed 116 tests, including the two-native
startup, stage-loss, HTTP/browser attribution, mailbox and native progress paths.
The preceding `resume-focused` invocation collected zero tests because two file
selectors were wrong; exit 5 is a setup failure, not a passing test receipt.

The pending acceptance probe retained at
`runs/v2/tokenpath-phase8/recovery-acceptance/test_a6_installed_probe.py` calls the
REAL `LiveA6RecoveryQualifier._run_physical_challenge` immediately after the
real signed-startup desk composition. Both modes are required to pass. In
`resume-a6-probe-red`, OFF passes and ON fails with
`token_path_activation_unsupported`: the qualifier calls ungoverned `infer`,
which required ON refuses before dispatching any inference command. Both native
processes have zero active runtime state after the probe. This is a behavioral
RED that remains UNFIXED, not a green refusal characterization or recovery proof.
The pending acceptance probe is outside the eleven canonical regression suites;
a future no-new-failing-IDs regression result does NOT close this separate RED.

Do not repair this by merely adding the startup exemption: after installation,
the relay requires independently admitted grant authority. A fresh A6 attempt
also encounters retained native cleanup/control state and the per-request hop
replay fence. A complete repair must admit the exact qualification probe and
reserved successor separately, preserve predecessor tombstones and original
ceilings, and retain the existing qualification/commit/publication ordering.

A7 similarly needs authorization tied to committed publication, not merely its
preparatory import/rekey receipt. Its new hop state must bind the imported-KV
position; a normal grant starts before prefill. Generated native tokens, verified
receipt prefixes and gateway delivery-event cursors must be reconciled rather
than treated as interchangeable. Old-path direct/queued refusal tests remain.
No authority check, replay fence, cleanup check or default-OFF selection was
weakened during this continuation.

The first resumed wide launch was refused before pytest by `DEVICE.busy`. The
second launched after a 180-second wait with both flags clear, then was stopped
via SIGINT when `DEVICE.busy` reappeared. Its partial JUnit is explicitly
INCOMPLETE, not a qualified comparison; all recorded owned processes were gone
and its basetemp was removed. Its partial failures did expose two test-contract
regressions: the newly imported governed fixture left a discoverable open route
behind, and an old supervisor assertion omitted the intentional authenticated
startup marker. A deterministic teardown regression plus the old assertion were
RED (`resume-fixture-red`: 4 failed/2 passed, including downstream diagnostics).
The fixture now retires its open/closed state before shutting down its executor;
the supervisor expectation requires the protected marker. No diagnostics or
startup production guard was changed. `resume-fixture-green` passed all 66
focused route/supervisor/diagnostics/startup tests. The third wide attempt was
also interrupted by a reappearing busy flag; neither interruption is comparison
qualification. Further focused tracing found additional pre-existing A6/A7 test
fixtures retaining open routes. Without diagnostic instrumentation,
`resume-diag-normal-baseline` reproduces all four diagnostics failing IDs at the
unchanged wave4 baseline (55 passed/4 failed); the candidate reproduces three
(56 passed/3 failed). Those fixture sources are byte-identical across branches.
This supplemental baseline reproduction does not replace a complete wide
comparison or rewrite the original baseline's failure set. Read the final
phase-8 report for exact completed comparison and manifest results, not
interrupted attempts.

A separate read-only review found an owner-consumer race: an empty receipt read
could time out, then the producer could enqueue/ACK its prefix and complete the
ordinary command before the consumer checked `result.done()`. Raising that result
immediately cleared verified queued tokens. Deterministic barriers with real
signatures and the real mailbox stream reproduce lost prefixes and a false
missing-terminal error. The owner now performs an authority-fenced nonblocking
queue read after observing command completion, before surfacing its result.
`resume-prefix-race-red` has four failures (three exact prefix/completion REDs;
the revocation case also had an incorrect expected exception class). The initial
green attempt preserved that oracle error (62 passed/1 failed); correcting it to
the existing `TokenPathAuthorityError` left production checks unchanged.
`resume-prefix-race-final` passed 63 owner/mailbox/pump/route/joined-native tests,
including refusal, successful completion, missing terminal and revocation at the
controlled timeout boundary. Revoked queued tokens remain undeliverable. This
unit race proof is not a new physical or A6/A7 recovery qualification.

Remaining: A6 fresh attempts/A7 committed successors and packaged-app/device
qualification. The joined startup is installed-workflow CODE with fake compute,
not an installed-app launch. Final regression comparison/manifest state is
recorded in `runs/v2/tokenpath-phase8/REPORT.md`; neither can close recovery RED.

### Coworking continuation: signed startup refusal coverage

Resumed clean at `eee450c5` (2026-10-22 10:51 CEST). Fresh `coworking-focused`
passed 145 desk tests with no skips. `coworking-a6-acceptance` again passed OFF
and failed ON with `token_path_activation_unsupported`; recovery remains open.
The first coworking wide run was interrupted by a newly appearing DEVICE.busy;
all recorded owned processes exited and its basetemp was parsed/removed. Its
empty partial failing-ID difference is NOT a completed comparison.

New `test_token_startup_native_refusal.py` corrupts one well-formed policy
signature at each participant position through the real signed-startup joined
composition. It requires no ready route, no request grant, no retry, and a closed
route after refusal. The managed target preserves its actual pre-dispatch
`token_path_policy_signature` refusal behind `managed_control_reconciliation_required`;
the local native participant reports `token_path_policy_signature` directly.
The initial one-failure result expected the native code at both transports;
that was an oracle error, not a product behavioral RED. A labeled in-memory
retrospective mutation removing only startup's failure-close produced two
failures at the exact `_closed` assertion. Source was never mutated on disk;
the outer fixture still closed both private children. The unchanged production
path then passed all 29 startup/native/owner cases in `coworking-startup-refusal-final`.
This is stronger desk refusal coverage, not new A6/A7 authority or a device claim.

After both busy flags cleared, the fresh baseline completed uninterrupted with
5,189 passed, 106 failed, 12 collection errors and 41 skipped. The first complete
candidate preseal run had 5,552 passed, 96 failed, the same 12 errors and 41 skips.
`coworking-preseal-comparison.json` preserves two new failing IDs, not a green
comparison: the signal-stop fixture omitted the new `token_policy_trust_file`
parser field, and the LAN refusal observer raced its server callback after the
HTTP response was already readable. The focused pre-fix run reproduced the
namespace setup failure (29 passed/1 failed); the LAN scheduling failure remains
in the full-run receipt. The namespace now matches the real parser's default and
asserts its forwarding. The LAN test waits for the post-send observation while
retaining the exact status/error, zero-byte and no-dispatch assertions. No
production code or verifier changed. `coworking-wide-fixtures-final` passed all
30 cases. The one missing old managed-route test ID is replaced by its explicit
OFF/ON parametrizations, not deleted or skipped. Final sealed wide results and
manifest checks are recorded in the phase-8 report; these fixture repairs do not
close A6/A7 acceptance.

## Phase 7 checkpoint (work in progress; no installed activation claim)

A signed route-policy bootstrap now exists in `mycelium_node/token_provisioning.py`.
It is independent of the request grant: the local host pins the owner key/endpoint,
run/deployment, its own member and the original process/lease ceilings. The owner
signs the graph/model, participant endpoint/generation/key records and bounded
policy. Native `install_token_path` verifies it against the running graph and
live transport peers before installing the existing participant gate. A separate
process-lifetime admission registry supplies pinned target receipt cursors.
Unknown fields, tampering, wrong keys, stale members/peers, replay and expiry fail
closed. It is not load consent and cannot reset a process replay fence.

First focused evidence: `runs/v2/tokenpath-phase7/native-install-green/summary.json`
(66 passed). API-absence RED: 23 failed; native-dispatch RED: 2 failed / 23 passed.
A test expected the wrong exception base during the first green attempt (43
passed / 1 failed); corrected to the authority's existing ValueError contract.

**Target-side continuation:** the enrolled target now carries its accepted join's
owner endpoint/key into a private local host pin. ManagedWorker passes only the
pin file path to the native process, observes successful configure/start, verifies
`install_token_path`, and derives the receipt cursor automatically. The concurrent
pump uses the current worker's cursor after replacement. Original process/lease
ceilings are retained; an expired optional pin does not break OFF startup, but
cannot admit autonomous work. A changed installed local member fences the worker.

The joined test now has host-API and signed-wire bootstrap arms: four passes,
including ON signed installation over managed TLS into a real native process,
with the existing two-process local-Iroh token/command-count proof unchanged.
`target-final` passed 69 focused tests, including actual enrolled TargetApplication
→ runtime → CPU native hello and cursor registration. Compute/configure remain
fake in the joined token proof; no physical models/devices were used.

**Owner continuation:** installed workflow session factories now construct a
`ManagedRouteTokenOwner` from the enrolled coordinator signer, live member lookup,
verified native keys and owner mailbox. The route attaches it; first governed
request signs and installs policy on every graph participant before any request
grant. Partial/uncertain installation cannot retry or fall back. Scope, expired
leases and changed members fail closed; receipt bytes are private durable files.
`owner-wiring-green`: 29 passed. The full serving-workflow fixture is blocked by
the existing desk MLX import guard, not credited as behavioral RED or proof.

**Joined governed/chat continuation:** `joined-chat-lock-green` passed 25 tests,
including eight host/signed/direct/governed/chat OFF/ON joined cases. The chat
arms traverse real native CPU processes → local Iroh → managed TLS → installed
owner policy builder → PhysicalLiveRoute → M16 → LiveRouterPort →
RouterSessionBackend → RequestGatewayService. Both emit the same six ordered chat
tokens and one `completed` terminal (code null); ON uses one start/zero decodes,
OFF one start/five decodes. Both native processes remain alive with zero active
fake-runtime state after signed cleanup and six observed executions per stage.
Compute, model loading, resource metadata and deployment qualification are
explicit desk fixtures; no physical/device/HTTP-browser attribution claim.

Verified nonempty current-grant receipts now defer redundant PV34 health probes,
just like legacy decode progress; empty terminals create no progress or cleanup
authority. The exact controller-owned startup challenge remains legacy without
minting grants; arbitrary ungoverned ON requests still refuse. Progress had a
behavioral RED; startup's initial test had a signature setup error, then a labeled
retrospective mutation proved the missing exemption before the final four passes.

The eight-case run exposed a real mailbox/member lock inversion: owner result
reads held mailbox→member, while receipt validation held member→mailbox. The
interrupted 240-second run is NOT green. A deterministic lock-order audit failed
before the repair; receipt transactions now use the established order before
and after sealing, without holding either global lock during sealing. The final
25-test batch passed with all membership/claim rechecks preserved.

**Remaining:** actual installed workflow startup, served_by HTTP/UI attribution,
and A6/A7 stage-loss/continuation parity remain unproven. No device-readiness claim.
Unprovisioned installed owner routes still refuse. Two early mixed test invocations hit the
180-second tool timeout without complete JUnit; retained logs are not green
receipts. A verbose repeat and the final 69-test batch completed. Canonical
manifests will be regenerated only after the source/docs freeze.

## Phase 6 checkpoint (2026-10-22; supersedes historical notes below)

The inherited changes are preserved in `c623554b` (explicit two-failure WIP).
`3d66688f` fixes the joined two-process test. Both OFF and ON now execute the
shipped native service, router, local-only Iroh and managed TLS mailbox using
private fixture authority and fake CPU compute. OFF produces six tokens using
one start and five decode commands; ON produces the same tokens and six stage
executions on each process using one grant-bearing start and zero decode commands.
The focused joined/ACK/mailbox/wait/governed-route batch passed 36 tests.

### Root cause, not a larger timeout

The fixture wrote `time.monotonic()+50` into `reservation_expires_at`, but native
Router uses `_DistributedProtocolClock`: Unix-aligned time advanced monotonically.
Thus both arms' leases were already expired by an epoch-sized amount. OFF exposed
`reservation_expired`; ON finished its native command without a terminal receipt,
which the forwarder reported as `token_progress_read_failed`. The fix changes only
the fixture lease clock to `time.time()+50`; all wait budgets remain unchanged.
`inherited-red` retains 32 passed / 2 failed; `lease-domain` retains 2 passed.
No production lease validation was weakened.

The inherited native wait fences and separately pinned managed-control versus
Iroh endpoint names are committed too. Endpoint aliases do not relax the exact
key digest, membership generation, owner signature or command binding checks.

### Operator activation option (default OFF; trusted composition required)

`MYCELIUM_TOKEN_PATH=off` (or unset) is OFF; `MYCELIUM_TOKEN_PATH=on` is ON.
Other values are errors, including `1`, `true`, uppercase and empty strings.
`OwnerTokenPath` reads this option when `enabled` is omitted. Explicit Boolean
configuration remains supported. Operator ON is strict: unsupported participants
raise `token_path_activation_unsupported` before a grant or binding, rather than
silently counting a legacy request as the ON arm. Speculative or ungoverned route
requests are likewise refused in required mode.

`PhysicalLiveRoute` and `PhysicalLiveRoute.from_operator_plan` accept
`token_path_owner=owner_path`. ON with an absent/disabled owner refuses with
`token_path_activation_unprovisioned` before plan I/O or route/device startup.
The flag is selection, NOT authority: it never derives expected scope from an
incoming grant, installs native policy, invents membership, or waives load consent.

**Remaining device blocker:** the installed app/workflow does not yet build and
supply this trusted owner object, install `ParticipantAdmissionPolicy` in every
native process, and attach the independently pinned target cursor automatically.
These are explicit host APIs exercised in the joined desk harness, not a shipped
cross-process bootstrap. Therefore this is a runnable opt-in for a provisioned
host composition, NOT a claim that setting the variable on today's app launches
a device A/B. Unprovisioned ON must refuse. Do not deploy it to the public demo.

Exact private-host composition order:

1. Derive owner key, selected entry/key, all participant endpoint/generation pins,
   model, request/control scope and process-origin/lease ceilings independently
   from the admitted private route. Do not use a received grant as its own pins.
2. After each native service starts, call
   `service.provision_token_path(participant_policy)` once for that process;
   its membership/current-peer callbacks must be live authenticated host state.
3. Construct `ConcurrentTargetControlPump(..., token_progress_cursor=cursor_factory)`;
   the factory independently admits the claimed grant and returns a pinned
   `ReceiptCursor`. Register progress before native start and seal before ACK.
4. Construct `OwnerTokenPath(...)` with no `enabled` override, the trusted pins,
   current/capability callbacks and mandatory evidence sealer. Pass it as
   `token_path_owner` to `PhysicalLiveRoute.from_operator_plan(...)`.
5. Relaunch the private host composition with the exact selection prefix
   `env MYCELIUM_TOKEN_PATH=off` for A and `env MYCELIUM_TOKEN_PATH=on` for B.
   Do not hot-toggle an in-flight grant. Missing wiring is a failed prerequisite,
   not permission to substitute the desk child or fake runtime on devices.

### Exact A/B acceptance procedure (device execution still NOT RUN)

Acquire P1's sole device lease and use a separate private swarm. Never touch r7,
port 8787, the public demo or quarantined executables. Require at least the
standing device disk/memory/AC preflight; the continuation's desk host had less
than 20 GiB free, so it was not device-admissible.

For Qwen2.5-0.5B and 7B separately, pin the candidate SHA, generated manifests,
model revision/quantization, tokenizer/sampling config, layer split, actual
selected entry and participant generations, prompt bytes, seed and 128-token
budget. Record one cold OFF/ON pair separately; then run five warm pairs in
OFF/ON, ON/OFF, OFF/ON, ON/OFF, OFF/ON order with the option above. Keep route,
locality and all generation settings identical. Reject silent fallback.

For every arm retain private exact signed receipts and verify: ON has one
`infer_start` plus zero `infer_decode`; OFF has ordinary per-round decode calls;
binding, receipt/ACK and cleanup calls are counted separately. Compare tokens and
indexes, terminal codes, cleanup authority, per-stage scoped evidence and A10
intervals. Record TTFT, steady and end-to-end tok/s, p50/p95 token gaps, receipt
lag, command turnaround, direct/relay status and machine load. Token receipts
alone cannot credit PV34 stage liveness. Repeat cancel-before-start, midstream
cancel, expired prefill/decode/publication deadlines, withheld progress, stage
loss, stale peers, lost ACK, duplicate receipts and the process-origin ceiling.
A6 fresh-attempt/A7 committed-successor parity remains required, not inferred
from these happy-path desk tests. Leave OFF on any failed or absent gate.

### Resume and evidence

Evidence root: `~/Documents/mycelium-core-program/runs/v2/tokenpath-phase6/`.
The runner uses the same desk guard and eleven requested suite directories for
both arms, strips PYTHONPATH/MYCELIUM_PHYSICAL_SERVICE_PYTHON, uses nice 19 and
xdist 2, blocks MLX and quarantined execution, and parses JUnit before removing
owned `tokp6-*` basetemps. Its process exit is not pytest's exit: read summary.json.
The wide baseline is pinned to `9bb62f79`: 5,189 passed, 106 failed,
12 collection errors, 41 skipped. The first lane run had 5,446 passed,
123 failed, the same 12 errors and 41 skips. Its effective command/exclusions
matched baseline (50 conservative MLX exclusions). Of 17 new failing IDs,
16 were contract/governance manifest drift; the remaining deadline-registry
failure exposed that this lane had not ported wave4's `bc90fcd6` binding. The
identical binding and mutation-based tests are now ported (41 focused passes);
no deadline value changed. The final sealed-run comparison lives in
`candidate-sealed-comparison.json`; do not treat the pre-seal run as qualified.
Freeze source/docs before generating contract and A6 manifests LAST (A6's
recorded base is `53d1222b`, not the wave4 comparison base).

Not yet proven: one joined real native → PhysicalLiveRoute → completed chat
terminal test (the governed chat test uses scripted nodes), automatic installed
host provisioning, device/model performance, or A6/A7/PV34 end-to-end parity.

## Historical phase-3 checkpoint: admission, relay fences, mailbox transport

**PARTIAL — NOT READY for flag-ON chat or devices.** On `lane/rel2-tokenpath`,
`e44f9a6d` adds grant-bearing managed grammar and independently pinned native
admission before prefill; `2a991f79` adds relay ingress/runtime fences;
`e33e4ef3` adds an opt-in authenticated TLS mailbox receipt channel. The Python
minor-version guard is ported as `b313069a`. No public activation flag exists;
all new authority/stream registrations default absent. Legacy dispatch remains
selected when no grant is supplied. No models, devices or installed services
were changed.

### What is actually exercised

- The shipped native `infer_start` accepts exactly request/control/path_manifest/
  token_grant, checks an independently provisioned `EntryAdmissionPolicy` and
  signer identity before real prefill, then runs the existing decode primitive.
  A real private child emits receipts before the final start response with fake
  compute. Completion returns bounded metadata, not a duplicated token array.
- `GrantedHopGate` requires independently admitted authority and exact
  request/model/path/participant pins. The real relay checks it at direct and
  queued ingress, immediately before runtime execution, and after execution
  before forwarding. Deadline, revocation, peer endpoint/generation, token budget,
  order and recomputation are fenced. Progressive/recovery/VERIFY frames are
  refused; no successor authority is invented. Installation is still trusted
  process-local provisioning, NOT an implemented managed install command.
- `ManagedControlMailbox.open_token_progress` registers an independently pinned
  owner cursor and mandatory exact-byte evidence sealer. The new target-signed
  TLS endpoint binds receipts to the existing claimed command and current
  member; verifies entry signatures; seals before issuing an owner-signed
  sequence/digest ACK; and rechecks membership/claim state after sealing. Bounded
  queues poison on overflow. Exact latest receipt retries do not redeliver.
  Registration is process-local and is not restored by mailbox restart.
  Tests use the real pinned TLS server/client, private authority and a fake
  sealer. They do NOT connect ManagedWorker/native ACKs or chat.

### Exact resume order and blockers

1. **Trusted policy installation:** derive owner/entry/participant/model pins from
   independently authenticated membership and the actual selected route. Carry
   grant authority in signed participant binding before any prefill, instantiate
   one replay registry per process, clamp real process-origin/lease deadlines,
   and connect `GrantedHopGate` peers/current callbacks to live transport/control
   generations. Do not use the incoming grant as its own expected scope.
2. **Worker/native ↔ mailbox bridge:** register `PhysicalNodeProcess.open_progress`
   before start using its existing shared reader; consume verified receipt bytes
   through the actual ManagedWorker/concurrent pump; serialize progress with that
   pump's durable signing sequence, not a second signer stream; drain progress
   before command-terminal ACK. Connect authenticated mailbox sequence/digest ACKs
   back to the retained native `ReceiptJournal`. Add durable replay support to
   `durable_requests.py` and the production `control_origin.py` route allowlist.
   Close streams on all exits. The current TLS API is only a component, and its
   trusted sealing callback is not an interruptible worker/cancel protocol.
3. **Owner/chat:** negotiate support on every participant, issue exactly one
   grant-bearing `infer_start`, consume verified receipts through the existing
   chat sink, preserve actual speed/locality-selected entry, and use legacy only
   when OFF/unsupported BEFORE autonomous admission. Never retry legacy after an
   admitted start. No usable flag or exact flag-toggle command exists yet.
4. **Waits/reconciliation:** bind prefill/hop/decode waits to the grant deadline;
   connect independent chat cancellation; preserve existing terminal/cleanup
   codes; authorize A6 fresh attempts and committed A7 successors with delivered
   cursor and unchanged original budget/deadline. Validate per-stage evidence
   before PV34 credit. Entry receipts alone do not prove every stage is alive.
5. Run the full eleven-suite lane/baseline comparison under the identical desk
   fence after the device lease is released. Re-pin canonical manifests LAST,
   then run final contracts/governance/claims and independent review. Current
   manifests are intentionally not re-pinned or qualified for these increments.

### Evidence and scheduling

Evidence: `~/Documents/mycelium-core-program/runs/v2/tokenpath-phase3-admission/`.
Read `FINAL.json` for exact head/status and individual `summary.json` files for
actual pytest exit codes. The runner itself exits zero even when pytest fails.

- Interpreter/progress check: 34 passed.
- Admission/adjacent check: 283 passed, 7 failed. All seven prefill-wait failing
  IDs reproduce in the focused wave4 baseline; this is not a full comparison.
- Final relay/admission/adjacent check: 56 passed.
- Final mailbox/grammar/managed-control/concurrency check: 58 passed.
- Hop tests initially had a fixture failure, NOT a behavioral RED. A retained
  retrospective no-fence mutation produced 16 behavioral failures; the later
  final check additionally strengthens the queued late-revocation assertion.
- Mailbox API-absence RED: 10 failures. Sealing-race RED: 2 failures, 10 passes;
  both fixed before the final 58-pass check.
- An exploratory relay-pump selection hit the explicit MLX import fence
  (56 passed, one collection error); no model execution occurred.

The A1 status board at 22:31 CEST still held the device/first-chat lease. Further
runs and broad qualification are held. Focused runs already completed during
this interval are retained, not represented as an exclusive-load qualification.
The previous native-transport full comparison remains historical evidence only:
its baseline was `f230c809`, not current wave4 `9bb62f79`. Do not carry its
no-new-failing-IDs conclusion over these new commits.

## Previous native transport checkpoint (historical)

The original native transport behavior commit was
`9df4bbf2c8174df7c895ace3087e150f3b91eafc`; evidence lives under
`runs/v2/tokenpath-phase3-native-progress/`. Its fenced comparison had 5295
passed, 113 failed, 12 collection errors and 41 skipped versus wave4's 5174
passed with identical failures/errors/skips. These numbers do not qualify the
current continuation. Preserve the earlier evidence; do not rewrite it.

## Current path and intended change

`PhysicalLiveRoute.infer` sends `infer_start`, then one signed managed-control
`infer_decode(count=1)` per round. `PhysicalNodeService._infer_decode` already
calls `router.decode_one_distributed` (or the A11 speculative equivalent),
which uses the existing authenticated iroh hop/token frames and stage-local KV.
There is no reason to reimplement sampling, forwarding or KV ownership.

Target: ONE owner-authorized inference command per REQUEST, `infer_start`,
carrying an immutable request grant. The ENTRY runs the same decode primitive
locally until the grant terminates. This counts inference commands, NOT initial
per-participant `bind_request_control`, cancellation or cleanup commands. An N
round legacy request costs 1 start + N decode commands; entry-driven costs 1
start + 0 decode commands. Do not claim all control traffic becomes one command.

## Authority and security delta

### Implemented in the phase-2 desk slice

* `GrantRegistry.admit` verifies a domain-versioned Ed25519 statement using an
  **independently pinned owner key and endpoint**, not an envelope-supplied key.
  Its closed scope binds run/request/requester, owner and entry endpoint/key,
  participant node/endpoint generations, all existing request-control fields
  (including deployment/qualification/path/publisher/cancel generations), model
  digest and full-request digest. The trusted caller must derive that scope from
  admitted route/request state; echoing the envelope as expected scope is unsafe.
* The signed body also carries max_new_tokens (1–65536, further capped by local
  policy), exact stop IDs (at most 128) and UTC expiry. Scope admits at most 64
  distinct participants; each encoded grant envelope is capped at 16 KiB.
  Validation rejects booleans-as-integers, unknown fields, duplicate participants
  or stop IDs, changed scope, budgets over local policy, invalid signatures and
  expiry. Principal refusal codes are `token_path_grant_signature`,
  `token_path_grant_invalid`, `token_path_grant_mismatch`,
  `token_path_grant_over_budget`, `token_path_grant_expired`,
  `token_path_grant_stale`, `token_path_grant_replay` and
  `token_path_grant_capacity`. Canonical scope comparison distinguishes `true`
  from `1` even though Python equality does not.
* Admission snapshots canonical bytes, subtracts the trusted clock-uncertainty
  allowance, and translates UTC expiry once to a local monotonic deadline,
  clamped by caller-supplied **process-origin** and lease deadlines. The helper
  cannot establish those clocks or verify a lease by itself. Owner-monotonic
  `absolute_deadline_ms` remains an identity field, never a remote wall clock.
* A process-lifetime registry admits each (run, request, attempt) once and never
  evicts replay fences (4096 maximum; fail closed when full). An admitted object
  permits one execution claim. It is a trusted process-local object, not a bearer
  capability or a Python sandbox. A new registry must not be created per command.
* `ReceiptJournal` signs contiguous bounded delta/terminal statements, tied to
  the full grant digest, using the pinned entry endpoint/key. It retains at most
  64 receipts, each at most 128 tokens and 16 KiB encoded; overflow refuses without
  advancing its cursor. Resume reads never recompute. Cumulative ACKs require the
  exact retained receipt digest; exact retry of the latest ACK is idempotent.
  Reads before the acknowledged prefix fail rather than conceal lost tokens.
  **ACK authentication is still the future channel's responsibility.**
* `ReceiptCursor` verifies signature, grant subject, sequence, interval, budget,
  stop exclusion, terminal ordering, deadline and a caller-supplied current-
  authority predicate before returning token IDs. Duplicate/gapped/conflicting
  receipts cannot advance it. Mutable input/readback cannot rewrite retained
  signed data. FAILED/CANCELLED receipts carry no invented tokens. Bounded stage
  receipt references are only references: they do **not** credit PV34 liveness
  or prove execution without independent validation of the referenced evidence.
* The default-OFF native adapter checks run/entry identity, the live control
  generation and the grant deadline around publication, and drives the same
  `_infer_decode` primitive after prefill. Failed publication/overflow stops the
  loop; retry cannot reuse its claimed grant. Native loss exceptions propagate
  unchanged. No cleanup receipt is minted, no participant binding is changed,
  no A10/PV35 snapshot behavior is changed, and no managed/network surface gains
  authority. The original unsigned adapter remains an internal trusted seam.

### Implemented in the phase-3 native transport slice

* `mycelium_node/token_progress.py` defines a closed private native frame,
  `mycelium.physical_node_progress.v1`, with command/node identity and an unchanged
  signed receipt (16 KiB maximum). It is not a new managed command or trust root.
* `PhysicalNodeProcess.open_progress` binds an independently pinned
  `ReceiptCursor` to this native node/run/deployment before start. The existing
  stdout reader demultiplexes progress without filling ordinary one-slot response
  waiters. There are at most eight open inboxes, 64 receipts per inbox and 4096
  lifetime stream IDs. Closed IDs are not reused or silently evicted. Callers
  must close their inbox explicitly; registration is not compute authorization.
* `ReceiptInbox.read` verifies signature, grant, token order and current
  authority before returning immutable tokens plus exact bytes for evidence
  sealing. Overflow poisons that request instead of blocking the shared reader
  or losing a prefix. Signature verification and authority predicates execute
  on the consuming thread, never under the producer lock. No implicit ACK,
  stage-liveness update or cleanup authority is created.
* Reads are bounded by the smaller local wait/grant deadline. Closing or
  interrupting a registered stream wakes its reader and rejects late tokens.
  EOF or a command response without a signed terminal is not empty success.
* `_ResponseEmitter.emit_progress` provides POSIX pipe writes with absolute
  deadline and cooperative cancellation checks, including while waiting for
  stdout ownership. A partial write poisons stdout; cleanup stderr remains
  independent. Progress is forbidden on the cleanup channel. Non-POSIX writers
  explicitly refuse; no unsupported Windows streaming capability is advertised.
* `_ConcurrentNativeSession` forwards registration/closure to that same process
  reader. **ManagedWorker dispatch and mailbox delivery do not yet call it.**
* Grant validation now admits `path_attempt=0`, matching the native initial-path
  contract; negative values and booleans remain invalid. The entry identity is
  bound to the actual selected node, never implicitly to the owner.

### Not implemented / required before managed exposure

The grant is now carried by `infer_start` and validated before real prefill
under trusted test provisioning. Remote gate and authenticated mailbox components
exist, but no owner-issued grant builder derives scope from `PhysicalLiveRoute`,
no managed participant installer wires the gate, and no worker-to-mailbox-to-chat
composition exists. Native child and TLS mailbox proofs are separate tests. Only the new
native pipe publication/read waits are interruptible; existing hop/prefill waits
and arbitrary publication callbacks have not been reconciled with the grant.
Cancellation that advances the actual control generation fails closed at the
fence; successor reconciliation and owner terminal mapping are still required.
There is no new claim of A6/A7, A10 receipt parity, PV34 streaming liveness, device
readiness, or Byzantine compute correctness. The following paragraphs specify
the target, not an already-deployed boundary.

Today the owner's signature separately authorizes every decode invocation. The
new owner-signed grant authorizes a bounded sequence instead. Bind it to run,
deployment/epoch, qualification digest, request/attempt, path/digest/attempt,
topology generation, publisher generation, cancellation generation, entry and
participant identities, model/config digest, max_new_tokens, stop IDs, deadline
and the grant protocol version. Reuse the existing identity fields, not an
unbound bearer token. Freeze the grant at admission and never widen it on retry.

Remote stages must receive and validate this grant (or its verified digest via
existing signed bind control) before executing any autonomous hop. iroh peer
identity/authentication is NOT owner authorization by itself. Every hop still
checks the exact bound path, peer generation and increasing token index; reject
replay, wrong request, changed budgets, stale publisher/cancel generation and
unapproved peers before compute. Completion ACK under PV5 is receipt-only and
must NEVER be treated as computation or token-production evidence.

This trades per-token owner intervention for bounded delegation. Safety comes
from exact grant admission, per-hop authenticated identities plus locally
installed authority, non-extendable limits, replay fences, interruptible waits
and signed evidence. It does not claim Byzantine compute correctness. The
initial local driver and adapter assume a previously authenticated start and
are NOT signature validators or remotely accessible authority issuers.

## Entry loop and stream

The driver consumes cumulative snapshots from the existing decode primitive;
it checks append-only output, token bounds and terminal state, batches deltas
(max 8 tokens or 50 ms checked at each completed round), flushes prefill's
first output immediately to avoid adding a decode-batch wait to TTFT, and
publishes a final receipt even for zero-token completion. There is never an unbounded output queue:
a synchronous publisher applies backpressure; production publication needs a
bounded queue and deadline-aware writes. A stalled hop must be interrupted by
the existing transport deadline; the desk loop cannot preempt a blocking callback.
Slow prefill/decode needs independent non-token progress heartbeats, not invented
tokens and not a claim that the 50 ms target is guaranteed during blocked compute.

Each production receipt needs exact grant digest + subject, monotonic receipt
sequence, token-index interval, token IDs, terminal/error code, and bounded
references to per-stage execution receipts. Sign once per batch, verify at the
owner, reject gaps/duplicates/conflicts before publishing tokens. Resume reads
by receipt cursor, not by re-running start. Lost receipt acknowledgement must
not cause duplicate compute. On bounded retention overflow, fail closed; never
pretend a missing prefix was delivered. Tokens are private request data, not
public diagnostics.

Transport gap: native receipt framing is implemented, but command admission and
the managed mailbox still return one terminal response per command. A long
synchronous `infer_start` alone would hit command deadlines. Complete the
authenticated unsolicited channel from the worker -> owner, including bounded
replay/ACK and evidence sealing, before exposing the driver. Merely polling
signed `snapshot` per token would reintroduce the bottleneck and is not this design.

## Cancellation, deadlines, loss and evidence

* Check cancellation before start, each round and each publication. Owner
  cancellation advances the existing generation and uses the independent
  `infer_cancel_wait`/teardown lane. No tokens from an overtaken round are
  emitted. Driver completion is NOT a cleanup receipt. Keep the owner's cleanup
  authority, fanout, signature checks and terminal CAS unchanged.
* Translate the signed remaining deadline to a local monotonic deadline ONCE
  at verified admission, accounting for transit/clock uncertainty; reject
  expired grants. Clamp to process-start + 1800 s and the existing worker lease.
  Do not restart the 1800 s clock on each request or on retries. Bound every
  transport wait and publication by remaining time. No new work/publication
  after expiry; retain the existing terminal mapping at the owner.
* Stage loss uses the same router/iroh exceptions and terminal codes; do not
  turn FAILED or a missing token into empty success. A6 restart remains an
  owner-authorized new attempt. A7 successor remains the existing fenced,
  committed KV/path authority transition, never a local grant rewrite. Suspend
  the driver until that authority is reconciled. Late old-path receipts cannot
  update liveness, publish tokens or authorize cleanup.
* Verified progress batches feed PV34 `_note_progress_receipts` only for stages
  actually proven by those receipts. Silence resumes ordinary health/loss
  detection within the existing 2 s budget. Entry heartbeat alone is not proof
  that every stage is alive.
* The same decode primitive records A10 intervals/runtime observations and
  iroh scoped evidence. Batching only changes delivery, not execution accounting.
  Compare normalized token indexes, terminal/failure codes, scoped receipts,
  cleanup authority and A10 intervals; command count/signature count necessarily
  differs. PV35 still bounds snapshots (64 intervals, 16 trace, 12+6 scoped
  events and 56 KiB details). Full evidence must be sealed before eviction;
  never restore cumulative per-token command responses to claim rows.

## Initial coherent slice and remaining implementation order

Slice limits: the supplied run deadline is trusted local input, not derived by
the helper; the existing blocking decode wait is only checked before/after, not
preempted. The adapter starts publication at index/sequence zero, so it cannot
resume a partially published request or an A7 successor yet. A generation change
fails closed through the native fence. Continuation needs a verified successor
authority record plus the already-delivered cursor, preserving the original
budget/deadline. New internal `token_path_*` validation errors still need owner
terminal-code mapping. Desk tests compare fake per-stage execution traces and
native exception codes, NOT complete A6/A7/A10 signed evidence parity.

1. **Implemented (desk only):** `mycelium_router/token_path.py` covers local
   deadline/ceiling checks, append-only output, stop, cancellation, no-progress
   refusal, bounded batching and exact raised-exception propagation; OFF by
   default. Native adapter invokes the shipped
   `_infer_decode` locally, without sending control commands. This is an internal
   composition seam, not a registered command or a claim of wire integration.
2. **Implemented in phase 2 (desk only):** closed signed grant admission,
   process-lifetime bounded replay registry, signed bounded receipt journal,
   cumulative exact-digest ACK/replay, owner receipt cursor, and internal native
   composition. `tests/live/test_token_path_authority.py` exercises these with
   real CPU Ed25519 signing and fake two-stage compute. The continuation adds
   managed grammar and a separate mailbox component; this phase-2 helper alone
   is not wire integration.
3. **Partial: authority plumbing.** Grant-bearing field sets and native validation
   BEFORE prefill are implemented, as are default-absent relay gates. Still add
   owner grant construction from admitted request/route state in
   `mycelium_live/route.py` and trusted participant installation. Instantiate one registry at native
   process start, pass actual process/lease deadlines, pin owner identity from
   verified membership, and propagate the verified grant digest through signed
   `bind_request_control` to every stage. Enforce budget/token index and grant
   identity on incoming hops; peer authentication alone is insufficient.
   Test tamper/replay/stale generation and unknown fields without changing any
   legacy field set. Negotiate feature support on owner AND every participant,
   default OFF; do not activate this independently of the channel below.
4. **Partial: progress channel.** `_ResponseEmitter`, native-session readers and
   the worker's native-session facade now support bounded signed receipt frames.
   Connect actual `ManagedWorker` dispatch to the authenticated mailbox component;
   no production dispatcher currently bridges them. Connect `ReceiptJournal` to signed
   owner ACKs, `ReceiptCursor` to owner consumption. Return admission promptly;
   never queue cancel behind decode. Test disconnect/resume without a second
   start, lost ACK retries, gaps/conflicts, deadline on write, overflow, terminal
   ordering and independently progressing cleanup. Preserve full execution
   evidence before ACK/eviction; token receipts alone cannot replace it.
5. **Next: owner and chat composition.** In `PhysicalLiveRoute.infer`, select one
   grant-bearing start ONLY when the route and all nodes support it and the flag
   is ON; otherwise leave today's per-token path unchanged. Verify receipts
   before the existing sink; feed PV34 only after validating the referenced
   per-stage execution evidence. Test the actual route with fake native sessions
   and two stages: 1 start + 0 decode versus today's path, same output and cleanup,
   stop/cancel-before-start and midstream, deadline at every boundary, unchanged
   A10 intervals and PV35 limits. Do not silently retry legacy after admitted
   autonomous compute; reconcile the original request first.
6. **Next: waits and A6/A7.** Share one immutable local absolute deadline across
   prefill, decode, publication and reads. Connect chat cancellation to the
   independent existing owner cancellation fanout. Reconcile actual generation
   changes and signed FAILED terminals at the owner. A6 restart is a fresh
   authorized attempt; A7 requires committed successor authority and delivered
   cursor preserving original budget/deadline, never a local grant rewrite.
   Test blocked hop/publication interruption, stage loss with/without successor,
   late old-path receipts, withheld progress, process ceiling and lease expiry.
7. Repeat all requested desk suites vs wave4, canonical manifests LAST commit,
   then independent review. Only then request device authorization. Do NOT
   expose a flag that silently runs the legacy path or enables an unsafe slice.

## Desk and device measurement plan

### Device A/B execution contract — BLOCKED, not a runnable gate yet

There is no implemented activation option, worker bridge or end-to-end driver to
invoke on this candidate. Do not invent an environment variable or deploy it for
A/B. Once the missing desk gates are complete, the device operator must:

1. Acquire the sole device lease from P1; use a separate private test swarm, not
   r7 or the public demo. Apply the standing clock/load/disk/memory preflight and
   quarantine checks. Record candidate SHA, pinned manifests and actual toggle
   command from the completed implementation.
2. For each Qwen2.5-0.5B and 7B two-Mac route, pin model revision, quantization,
   tokenizer/generation config, layer split, actual selected entry/participant
   generations, seed, exact prompt bytes and a 128-token budget. Keep these
   identical across A/B; reject a run that changes route or locality.
3. Run one cold pair separately; then five warm pairs in OFF/ON, ON/OFF,
   OFF/ON, ON/OFF, OFF/ON order. Seal all exact request and execution receipts
   privately; record transport direct/relay status, CPU and memory pressure.
4. Prove OFF used the legacy start-plus-decodes path, and ON used exactly one
   grant-bearing infer_start and zero infer_decode commands. Count binding,
   receipt/ACK, cancellation and cleanup traffic separately. Verify receipt
   tokens arrived before the final start response and match the chat sink.
5. Compare normalized token indexes/output, terminal code, signed cleanup
   disposition, scoped stage receipts and A10 intervals. Record TTFT, steady
   decode rate, end-to-end rate, p50/p95 token gap, receipt lag and command
   claim/run/turnaround; do not turn fake-compute rates into device predictions.
6. On both modes repeat: cancel before start, cancel after first verified token,
   withheld progress, deadline during prefill/decode/publication, stage loss
   with and without an authorized successor, stale peer generation, lost ACK,
   duplicate receipt and process-origin 1800-second expiry. Require no post-fence
   tokens, no duplicate compute, identical terminal semantics and proven cleanup.
7. Verify A6 new-attempt authority and A7 committed continuation separately;
   demonstrate PV34 credits only independently verified stage execution and
   resumes existing loss detection when progress stops. Leave the flag OFF on
   any missing/failed case. No default flip without owner acceptance.

The blocked prerequisites above are part of the result, not waived by this
procedure. No device A/B was executed in this continuation.

Desk microbenchmark uses fake compute and a fake two-stage 1 ms hop, reports
wall-clock tokens/s and TTFT separately for per-token control vs local driver,
with simulated mailbox latency explicit. It is neither iroh nor MLX performance
and cannot establish near-local speed.

After explicit device approval: on Qwen2.5-0.5B and 7B two-Mac routes, record and
fix the actual speed/locality-selected entry (not necessarily the owner), model
revision, split, warm state, prompt, sampling seed and 128-token
budget; interleave flag OFF/ON trials (at least five each). Record cold and warm
TTFT separately; steady decode tok/s excludes prefill, also report end-to-end
rate, p50/p95 inter-token gaps, command count, receipt lag, CPU, control
claim/run/turnaround and iroh direct/relay state. Compare a local baseline with
the same generation settings. Repeat cancellation, deadline, stage loss and
1800 s ceiling controls without changing public demo deployment. Store exact
candidate/config digests and bounded evidence; no default flip until owner
accepts throughput/TTFT AND unchanged failure/cleanup semantics on both models.
