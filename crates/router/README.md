<!-- SPDX-License-Identifier: AGPL-3.0-only -->
# Router lane slice

This is a **partial deterministic core port**, not the full router, a running
owner/member deployment, or L9 acceptance. Oracle: `f4e366e98a1f705dfaf182897044500d290dbd7f`.

- `ReceiptStream`: real L5 admitted grant/cursor/signature checks, exact duplicate
  retry, bounded inbox, sticky overflow/seal failures, authority checks before
  duplicate/read and after sealing, and once-only terminal delivery. `None`
  preserves the oracle's token-budget-bounded inbox default; an explicit message
  capacity is 1..64. Pull composition pauses before reading more input; unsolicited
  overflow still fails closed. No thread, timer or condition-variable driver yet.
- `RegistrationBook` / `OwnerAttempt`: process-local single-use registration,
  explicit-cancellation tombstones, finalizer exception safety and one-shot start.
  The 8-active / 4096-seen bounds are source protocol registration limits, not new
  retention/consent policy. An owner restart intentionally loses tombstones.
- `CleanupRound` / `RequestOwner`: independently pinned participants/processes,
  exact subject and peer-generation checks through L5 signed observations, complete
  participant quorum, fallible local-residue verification, currentness/deadline
  checks through terminal publication, and a terminal latch that cannot reverse.
  Successful completion needs both progress and ordinary native completion.

- `routing`: consumes L5 semantic activation/manual-route/replica validators,
  canonical owner document pins, execution graphs, path manifests and independently
  held exact reservation hops. Activation/model identity and fresh replica
  qualification must bind to the installed route; document self-digests are not authority.
- `Recovery`: full prompt plus committed-token replay, immutable request/total cap,
  phase budgets, source two-attempt cap, excluded failed placements, fresh successor
  command/path/request-attempt identities, ordered bounded output, qualification
  then source fencing, cleanup-gated retry and one-shot current-generation cutover.
  Cancellation and nonretryable failure cannot become a retry after a late result.
  Full signed cleanup subject and expiry are rechecked at final consumption.
  This is a coordinator driven by completed trusted effects, not a checkpoint store,
  a scheduler or proof of live Qwen baseline parity.

## Trust and ownership seams

Host inputs (`AdmittedGrant`, currentness callbacks, cleanup subject, peer pins,
registration metadata, native result and local-residue verifier) come from the
trusted L6 admission/command owner, **not wire claims**. Call methods under one
actor or request-specific lock. No callback here establishes live member locks,
claim/replay transactions, offset-corrected native timestamps or physical residue.
`CleanupProof` has private fields and is not deserializable; it is not a durable
restart capability or a resource-reservation releaser.

`RegistrationBook` exposes a retained retirement tuple for the L6 mailbox to use
with L5 `progress::retirement_subject`, under current membership/claim locks and
outer request authentication. It does not sign responses or infer authority from
an HTTP error. Never persist its tombstones as restart authority.

## Provenance and open boundaries

Sources at the oracle pin:
- `mycelium_a13_macos/token_receipt_mailbox.py:25-155`: inbox and registrations.
- `mycelium_live/token_path_owner.py:96-182`: one start, progress/native distinction,
  cancellation observation and nested finalizer.
- `mycelium_live/router_port.py:3343-3420`: shutdown ownership and unproven cleanup.
- L5's `protocol::observation` and immutable signed fixtures supply the existing
  node cleanup predicate. Local-residue callbacks in these tests are explicitly
  **controlled effects**, not native subprocess inspection.

Still open: real scheduler/standby/KV/batching, durable checkpoint admission and
live qualification/fencing/replay driver; exact source timestamp/authority lifecycle;
service-level signed mailbox transactions, physical worker cleanup and public
gateways. Runtime adapters now compose actual L6 SQLite Core/Supervisor and the
L8 StageBackend trait under controlled effects. That is not live native execution
or the full L2 production trace migration. No old test is retired.
