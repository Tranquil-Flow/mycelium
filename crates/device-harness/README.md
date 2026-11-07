<!-- SPDX-License-Identifier: AGPL-3.0-only -->
# device-harness — L12 work in progress

AGPL-3.0-only. Rust device-runner policy, deterministic state machines, and bounded
I/O seams. **Fake qualification and offline replay are not physical-device or
release evidence. Nothing here authorizes a device run.**

## Implemented boundary

- `admission` / `preflight`: battery and AC start waiver, conservative network
  classification and consent, clean current/previous state, exact owned marker,
  disk/memory floors, candidate/install decisions, fresh listener/status identity.
- `effects` / `process`: one bounded SSH connection-rc255 retry; remote command
  rc255 is distinguished by an explicit remote-exit marker rather than mistaken for a
  connection failure; finite helper
  supervision, first failure plus cleanup diagnostics. Raw stdout/stderr is not
  diagnostic evidence. `effects::execute_reported` emits only syntax-checked
  command basenames and observed rc/timeout diagnostics, never argv, hostnames,
  stdout, or stderr.
- `observe`: full-tree credential redaction, independent two-host failure
  snapshots, load/top-five CPU and both database/WAL/SHM sizes.
- `host_observer`: additive paired local/SSH command adapter for `Observer`
  metrics, composed with separately injected status/session/mailbox authority.
  Fixed `sysctl -n vm.loadavg` and `ps -axo pid=,pcpu=,comm=` recipes avoid
  argument disclosure; malformed/nonfinite/duplicate top-five observations and
  oversized output refuse. Reuses bounded command execution and its single
  connection-only SSH retry. Owner failures do not suppress member observations.
  Output-size checks are post-read; a real transport must also bound capture.
  Tests use recording fakes only, not OS/SSH/device qualification.
- `ownership`: exact owned PID + birth + executable stop; reread late awake
  binding, verify marker removal; Apple Event-only product Quit, no escalation.
- `host_state`: non-destructive state/cache moves and APFS clone command policy.
  The state capability has no delete operation. A joined member's cache root is
  distinct from the owner's, and authority/peer identity is never cloned.
- `http`: GUI consent bodies and bounded guarded chat polling, exact
  `chat_switching_runs` recognition, completed nonempty output, client-poll
  latency/token metrics without token text. Refused submissions retain integer
  `submit_http` and syntax-checked `submit_code`, including switching-on-send.
- `stranger`: package/admission/install/set-aside/fresh-launch/configure/pair/
  member-cache/serve/three-chat state machine. Host operations are injected
  capabilities; there is no implicit live adapter. Explicit quick/demo relay
  selection shares the exact demo body with owner restart. `run_owned` persists
  the result and releases its marker after failure snapshots and bounded cleanup.
- `schedule` / `soak`: issued/completed independent jobs, rollover lower/upper
  bounds, full two-host metric samples, explicit restart expectations and pure
  verdicts. Missing lifecycle steps, observations, policy or content cannot pass.
  Actual restart issue/completion times bound the whole chat: only recovered
  windows excuse HTTP 503 with `chat_not_ready`, `chat_warming_up`, or
  `chat_switching_runs`. These count as `owner_restart_unavailable_chats`;
  existing rollover-switching exemptions take precedence. Twenty-minute policy
  replay covers the owner-ruled floor without weakening longer requested runs.
  This is an injected fixture, not a new operational default (L1 remains open).
- `private_fs`: no-follow 0600 single-link owned private reads, exclusive writes,
  first-snapshot preservation, inode-owned marker release. The private parent
  directory must be single-writer; pathname unlink is not an atomic compare and
  unlink against an adversary with the same UID.
- `attempt` / `lifecycle` / `member`: owned attempt records, private-session
  rewrites, paired bounded awake leases, power-watch samples, private-link
  transfer cleanup and bounded member publication after a consumed join. Safe
  initial power-probe failure causes survive in the final soak receipt.
- `resume`: owner-only automatic/user restart, quick/demo bodies, strict restart
  option parsing, no member repair. This library parser does not enable live CLI.
- `package`: actual in-memory ZIP creation/readback over inert fixture bytes,
  symlink confinement, source/executable/bundle identity and installation-metadata
  checksum validation. Same-bytes comparison requires READY and a passed device
  result; it explicitly does not certify the package or release.
- `package_driver`: fake-qualified Rust finalizer-helper commands with rc-only
  failure diagnostics; unique leased per-attempt/per-host staging; archive
  read-back hash before extraction and extracted-manifest read-back.
  `stranger::run_device_owned` persists package/source/archive binding even on
  later admission/execution failure. No native staging/finalizer adapter is
  supplied, and the candidate app is never a packaging helper.
- `release` / `audit`: durable-intent/readback seams for the preflight/package/
  cert/device/soak/proof/publish/download/promotion graph, failure preservation,
  candidate-pinned collected test IDs/obligations/incident jobs/removal evidence,
  explicit publication/promotion authorization, and unfired-only candidate
  set-aside. Every progression/promotion receipt remains `fake_only` and
  `release_qualified: false`. Supplied audit evidence is validated, not authenticated.
- `release_github`: `ReadbackIo` composes authenticated GitHub readback with
  `release::advance`, preserving durable intent and preventing effect replay on
  ambiguous readback. Drafts require exactly one exact-tag match across the
  complete authenticated paginated release listing; published releases retain
  the tags endpoint. Both enforce uploaded ZIP/sidecar server digests and explicit
  draft/prerelease flags, under one injected total budget and a page bound.
  Tests model the draft tags-endpoint 404, not a live GitHub service. No network
  client, release creation/upload, nonlatest/latest mutation or Git push is
  implemented or authorized by this read-only adapter.
- `retention`: pure dependency-closure plans protect offline/native/npm/model,
  rollback, continuity and active-lease roots and resolved aliases. Restore
  evidence is supplied; the planner has no filesystem or deletion capability and
  never authorizes deletion.

## Executable

```text
device-harness --help
device-harness evaluate <receipt.json>
device-harness stop --attempt <private-directory> --budgets <bounds.json>
```

`evaluate` accepts a tagged `scenario: "soak" | "stranger"`, `schema_version: 1`
receipt with the corresponding policy/candidate and evidence. It returns JSON
with `evidence_boundary: "offline_replay_not_physical"` and
`release_qualified: false`. Exit 0 means the supplied scenario evidence passes;
exit 1 means it does not; exit 2 means input/observation/cleanup refusal. Arbitrary
imported error prose is not echoed.

`stop` is an effectful owner command, **not exercised on a real process in this
lane**. It accepts no arbitrary PID or executable. It reads a 0600 single-link
`harness-pid.json` in an owned private attempt directory, requires this exact
executable, and checks PID/birth/executable immediately before SIGINT. It never
signals the product app and never escalates a harness stop. Its bounds JSON has
explicit nonzero `command_ms`, `grace_ms`, `poll_ms`, `max_output_bytes`; there are
no hidden operational time defaults. If a paired/remote awake record remains,
the local adapter refuses with `paired_awake_adapter_required`, rather than
claiming cleanup it cannot prove. The generic stop policy supports a qualified
paired adapter, which is still to be supplied.

There are deliberately **no live `stranger` or `soak` CLI commands** in this
foundation-gated WIP. The `NativeTransport` leaf is compiled but its real
process-launch/signal path is not physically qualified; tests use fake transports
and process handles. Product app Quit remains an injected Apple Event operation.

## Keeper and native-session continuation

The fake-qualified `native_session` adapter binds candidate/process/resource
identity, native re-open and browser-session readback to one deadline. The
`keeper::NativeKeeper` composition permits at most one renewal per whole check,
including recovery rechecks. Bounded auto-Start is restricted to a failed
`rollover_members_changed` rollover with proven cleanup and durable reservation;
member silence only alerts. `member_watch` parses real member clock fields, and
`site` refuses all client CF-* and credential headers before transport.
`harness_budgets`/`soak_driver::drive_registered` enforce the semantic L1 floor
without a numeric fallback. Requested L1 entries remain unregistered in this
preview. See [S14-SESSION-SEAM.md](S14-SESSION-SEAM.md) for exact owner handoffs,
resource-manifest reconciliation, tested boundaries and remaining native IO.
No live device or release acceptance is implied or enabled.

## Integration obligations before live use

1. L1: register device command, retry-delay, poll, phase, stop, clone, watch and
   scenario budget IDs. Current APIs accept `budgets::Span`; trace milliseconds
   and test fixture durations are not production defaults.
2. Supply the authorized paired-device adapter: native private member helper,
   status/session HTTP, clean/read-only host probes, exact launch/install/package
   identity, Apple Events, lease ownership and secret-free transfer/cleanup.
3. Qualify the native composition of attempt lifecycle, periodic power watch,
   signal cancellation, first snapshots, paired awake ownership and both-host
   cleanup. Owned soak/stranger fake compositions and scratch-file lifecycle
   tests do not prove real-device or signal-driven cleanup.
4. Owner review must choose the restart expectation. `IdleUntilExplicitStart`
   requires an explicit approval reference and records endpoint changes; it is
   never promoted to automatic F3 recovery. Diagnostic-only cannot qualify.
5. Supply native package staging/finalization, restore/rollback and release-state
   persistence/publication adapters; bind the pure inventory to the real mandatory
   CI collection and approved obligation register. Authenticate reviewer/retirement
   authority and restore evidence outside these fakes. Published-download execution,
   install/uninstall behavior, physical qualification and release acceptance remain
   open. No Python runtime or cross-language digest equivalence is introduced.
6. No merge into main until foundation exit; no test retirement approval or
   physical/release acceptance is implied by this crate's green fake suite.
7. Qualify and bound native helper termination/reaping before live use. The
   current timeout cleanup retains an owned-child `kill`/`wait` leaf; fake
   supervision tests do not prove a bounded OS reap under a stuck kernel wait.

## Tests and provenance

Tests replace SSH, processes, HTTP and host operations with recording fakes.
Private-file, attempt and owned-run tests also use real temporary files under an
explicit `TMPDIR` containing `.hermes/cache/scratch` and remove their fixtures.
Package tests create/read real ZIP containers containing inert bytes, never apps.
No test starts an app, SSH session, model or remote device.

The external lane report is
`runs/v2/v02/L12-HARNESS.md` in the Mycelium core-program documents. It carries the
complete Python assertion/threshold transcription, source hashes, Rust test
mapping (including partial/open rows), exact receipts and integration gaps.
No executable Python is stored in or run by this crate.
