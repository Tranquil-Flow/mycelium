# Lane B7 — loopback two-node serving qualified (e/f); grant renewal and LAN open

## B7e/f continuation (branch lane/b7e-stream from a73a52b)

Commits: 436d7d9 (serving + Stop/stall/death/leave + two RED-tested fixes), a471d71
(typed test budgets; `record_stage_exit` rename for the TM002 substring heuristic),
then this docs commit. Gates at a471d71 all PASS (merge 1093, quick 1966, headless 28,
clippy/Metal clippy/fmt/lint clean). Real ignored run on a471d71, one Mac (Metal, local Qwen3-0.6B, two member processes):
a chat POSTed to the product gateway was admitted by the live M16 factory, dispatched
over the signed `mycelium.joined_route.v1` route by `m16_route::RouteExecutor` (owner
hub, Router v1 HopHeader/TokenEvent/PathCancellation over LocalOnly Iroh) and streamed
over SSE: 32 tokens == recorded Python/MLX oracle tokens[0..32]; tokenizer prompt ids
== oracle prompt ids. TTFT 624.4 ms, 24.61 tok/s decode (loopback, one Mac).
Public DELETE reached both workers in 60.8 ms (ACK after kill+reap of the interrupted
worker, graceful close of the idle one). SIGSTOP stall -> `joined_route_hop_deadline`
at 10019.5 ms; SIGKILL death settled 589.5 ms via exact-process evidence
(`joined_route_stage_died`); product MemberLeave 202.7 ms with member-signed withdrawal,
consent withdrawn, renewal stopped, shard retired/refunded, durable reopen `left`;
route refuses 409 `readiness_revoked`. Reservations return to baseline after each.

Class A (new, versioned): `mycelium.joined_route.v1` route + entry token payload,
`mycelium.joined_shard.withdrawal.v1`. Existing native prepare/step/close bytes and
joined-native proof v1 unchanged. Ruling 10(b) additive API:
`requests::Subscription::publisher_generation` (default None keeps prior behavior);
the gateway binds a resumed stream to the NEW subscription generation (registry bumps
it per subscribe; previously every resume failed `stream_order_invalid`). RED:
`joined_route::sse_client_resumes_across_bounded_stream_polls`.
Worker fix: step cancel flag scoped per exchange (RED
`native_compiler::worker::tests::step_cancel_flag_does_not_cancel_later_close_exchange`);
failed graceful close keeps the worker for kill+reap. node-service gains a private
`mycelium-node tokenize <tokenizer.json>` helper over its existing `tokenizers` dep.
No lockfile/dependency, reserved status, packaged chat or network-cancel edits.

Owner remains a test driver (pairing/emission/prepare harness, carrier_address,
waitpid death evidence, withdrawal delivery over operator IPC). Physical
route_ready=false; scope `loopback_two_node_qualified`.

Not done: artifact-grant renewal (source chunk verification
`runtime/src/iroh_artifact_source.rs:159`, journal fence `runtime/src/artifact_journal.rs:225`
— changes shared source-journal behavior, needs a ruling) and a reviewed direct-LAN
carrier (executor refuses non-loopback peers). Journey risk recorded: M16 streams end
after each idle OwnerPoll; browser EventSource clients must resume with an advanced
generation after an empty accepted stream (Python keeps the stream open).

## Earlier: Lane B7 — native received-shard preparation qualified; serving incomplete

## Current continuation from 52ae99e (m4/main 0c171da merged)

Phase (c) qualified in the real two-member loopback run in scratch/b7d-4:
member PIDs 90313/90312 owned native workers 90702/91131, received source-derived
751648856/751651104-byte shards, and returned 155/156 actual resident rows.
Owner verified enrolled signatures, offer/nonce bindings and the independent
resident-weight maps. Final native test PASS 1/1, 481.38 s tests / 491.752 s command.
Wrong-identity, wrong-nonce and discard-while-live controls passed.
Phase (c) checkpoint: 34851cc. Phase (d) now qualified in the same run: the
owner independently verified both enrolled native proofs and drove the two live
member processes to [151667,198,32313,11], exactly the pinned Python reference.
An actual member resume advanced generation; the stale signed native proof AND
stale worker step were refused with joined_shard_member_changed. Native close,
reap and inert refund measured 109.460 / 92.784 ms, with both ledgers empty and
all four exact PIDs independently absent. These are NOT public Stop/leave timings.
The fixture establishes one durable source journal before grant issuance, gates
connect on source verification/accept readiness, and removes emitted source links
only after the received shard is verified. It retains the producer charge (no
retained BuildOutput/refund claim). Minimum sampled free disk was 28.173 GiB.
Source snapshot uses APFS clone; object publication uses hardlinks, not copies.
Physical route_ready=false. Owner remains a test driver; no M16 chat/Stop/leave.

Ruling 10 / class C: independent private-stdin cancellation ingress requires a
monotonically increasing operation_id; wrong/old IDs cannot cancel another job.
A stalled real QUIC receive cancels in 502.114 ms, retaining reservation debt
until explicit inert discard. Its source connection closes while member stays
alive. A separate current-thread driver previously stopped pumping during stdin,
hash/native work; the member now owns one background Tokio I/O worker. Existing
wire/signature formats and native prepare/step/close commands remain unchanged.
Class A: existing joined-native v1 proof contract is unchanged; private operator
commands are additive. No reserved S1, packaged chat, status or network-cancel
implementation changed; no external dependency or lockfile edits.

Native prepare now checks cancellation before/through custody hashing and shares
one operation deadline through prepare and resident proof. Native controlled step
is bounded by both its operation deadline and the original member consent wall.
Receive uses one operation deadline, checks cancellation during pending I/O and
bounded verification, and clones read-only Session snapshots rather than holding
the renewal mutex across network/hash work. Existing membership renewal remains.
The legacy ChunkSession::fetch remains unchanged; responsive fetch is additive.
No claims of interrupting a kernel-blocked filesystem call or native graceful
cleanup after cancellation: failed native work retains debt fail-closed.
Artifact-grant renewal is STILL MISSING (membership renewal is not grant renewal).

Behavioral REDs: pre-cancel prepare returned member_shard_length_mismatch instead
of compiler_stopped; source-release-while-member-live timed out; serialized stdin
returned artifact_source_timeout instead of responding to cancellation. GREEN:
pre-cancel test, ordinary transfer/release/refund and real stalled-cancel tests.
The first cancellation fixture retained a Connection past runtime destruction;
explicitly dropping that owner fixed its socket-rebind assertion (fixture bug).
Expired-prepare coverage is added characterization, not a claimed pre-fix RED.

Current evidence: scratch/b7d-{1,2,4}. Two failed model attempts are retained:
first second-member connect Expired; second correctly refused a grant predating
journal genesis. Neither reached native load. No deadline/guard was weakened.
Current-code gates at acd194d: six-package merge 1088/1088 PASS (52 skipped;
728.841 s command / 340.891 s tests), quick --base m4/main 1961/1961 PASS
(136 skipped; 818.228 s command / 96.373 s tests), headless 28 PASS / 4 ignored
(20.256 s), default/Metal clippy PASS (40.762 / 14.231 s), fmt PASS (4.113 s),
lint PASS (1293 files, zero violations; 14.340 s). No LEAK diagnostic recurred.
Both new pre-cancel and expired-prepare controls executed in merge and passed.
No owned runner/member/native PIDs, lane-target processes or model fixtures remain.
Evidence: ../mycelium-m4-program/qualification/laneB7d-acd194d/ (71 raw files,
hash manifest and exact cleanup receipt). Full B7 merge-ready NO. Remaining:
genuine M16 carrier/graph/tokenizer composition, streamed chat, public bounded
Stop and leave, death-midstream/stall settlement, artifact-grant renewal. The
CONTINUATION.md describes the actual missing producer and cleanup-ownership seams;
these are unfinished implementation, not fabricated proof or a permission blocker.

## B7b review repair (supersedes precursor cleanup claims below)

Branch `lane/b7b-serving`, base `8c9b00f`. REVIEW-laneB7-precursor R1/R2
were reproduced as deterministic failures and repaired before serving work.
Missing shard/file paths no longer authorize cleanup or refund after restart.
A v2 durable cleanup intent binds the original file, stage directory and B3 row.
Cleanup uses pinned directory descriptors, no-follow relative opens and exclusive
capture. It verifies the captured inode and truncates ONLY that descriptor;
there is no pathname unlink of a data file. The zero-byte original inode remains
as `member-shard-retired.bin` audit evidence, plus intent/refund metadata. Only
empty-directory removal uses unlinkat(AT_REMOVEDIR). Foreign swapped bytes are
restored without overwrite when possible, otherwise preserved with full debt.
The v2 refund requires the real zero-byte tombstone, matching intent, original
owner and absent stage directory; v1 missing-path refunds fail closed.
Tests cover moved file/directory on restart, swaps at the last cleanup checkpoint,
four interruption phases, stale replacement-ledger recovery, and a replaced
retirement inode. Focused storage + member tests and api clippy passed;
full-gate outcomes are recorded below.
Ruling 10: existing rustix dependency enables its fs feature; no new package,
lockfile change, reserved behavior or artifact-v1 wire change. Private cleanup
records are explicitly v2. This is inert cleanup, NOT serving-worker leave.

## B7b pairing-bound receiver

`Session::shard_identity` is an additive live-lease/enrolled-key check. New
`mycelium.joined_shard.{signed,registration,offer}.v1` signed envelopes bind the
actual accepted node, endpoint, incarnation and generation to its Iroh endpoint,
compiler recipe and unchanged artifact-v1 grant. The seed checks the current
member row; `JoinedSource` repeats that check around durable source I/O, while
`JoinedShard` repeats session/standing-consent checks around receiving/hashing.
`api --example shard_member --state-root PATH` is a standalone member process:
private stdin/stdout controls real pairing/accept/renew, consent, LocalOnly QUIC
receipt, B3 verification/retirement and bounded endpoint close. It does NOT run a
native worker or provide a serving route. The owner side remains a test driver.
Real two-process test (explicit B7_MEMBER_BINARY, ignored in routine gates) passed:
actual join generation, signed transfer, exact corpus bytes, refund, child reap
and source socket rebind. An actual member resume invalidates the old offer;
wrong signer, signed-content tamper and withdrawn consent refuse before custody.
These are corpus byte-custody tests, NOT model/proof/token evidence. Recipe node
metadata is rebound to the actual joined identity; no native plan is claimed.
Ruling 10 permits these additive APIs/launcher/versioned envelopes; no reserved
S1/chat/network-cancellation behavior changed. Full native-serving phases c–f,
model transport renewal/bounded-source hashing, and physical launch remain TODO.

## B7b qualification on code 9d16574

- Member custody 11 PASS/1 ignored; compiler storage 8 PASS; paired member process
  4 PASS including the explicitly invoked ignored process test (3.17 s).
- Six-package merge 1081 PASS/1 FAIL/49 skipped, 341.145 s tests (910.020 s wall).
  Failure: known network_start window-reopen 409; exact binary 5/5 isolated PASS.
  Root cause remains unestablished; original full failure is not relabelled green.
- Quick --base m4/main: 1931/1931 PASS, 132 skipped, 121.186 s tests (995.927 s wall),
  exact 16 affected crates/no exclusions. RU-053 inventory not selected or re-pinned.
- Headless 28 PASS/4 ignored; default and Metal clippy, fmt, lint PASS.
  Wall seconds respectively: 35.035, 66.496, 39.209, 8.152, 29.221.
- Initial Metal setup refusal (missing MLXC env) and Quick disk-guard abort retained.
  Authorized stale duplicate-executable sweep touched only this lane's debug/deps;
  no active/open files, newest executable per stem and all libraries preserved.
  454 removals reclaimed 3.820 GiB; final Quick minimum 29.077 GiB. No <25 GiB sample.
- Exact final process inventory is empty. No native two-node model test exists yet;
  no token/reference or Stop/leave timing. Phases c–f remain implementation work,
  NOT permission-blocked. Full B7 is NOT merge-ready; no readiness promotion.
- Durable raw evidence: ../mycelium-m4-program/qualification/laneB7b-9d16574/.
  REPORT-laneB7.md and TWO-MAC-TEST.md name executable commands and explicit gaps.

## Historical precursor (claims qualified by the repair above)

Base: `1bb931978f08799c97e09bb6da012145e2058a9f`; branch `lane/b7-two-node-serving`.
Disk blocker cleared (39 GiB at resume). This is NOT the requested serving journey.

## Additive boundary implemented

- `api::compiler_inputs::member_shard::MemberShard` receives one independently
  pinned transfer row from a node recipe without needing the source checkpoint.
- Reuses B3's exact ledger rows, consent cap, headroom and reservation-before-write
  semantics. `reserve_output` is extracted without changing old compiler behavior.
- Exclusive per-run custody, bounded append, byte-identical duplicate retry,
  no sparse/overlapping writes, fsync before acknowledgment, retained debt on Drop.
- Transfer hash/length and contiguous recipe range hashes/inline bytes checked;
  caller checkpoint before each bounded hash read. Not a native tensor proof.
- Explicit inert-byte discard validates retained file and directory identities,
  unlinks, fsyncs, writes a versioned refund marker, then removes the ledger charge.
  Restart resumes partial custody; refund retries survive pre-rename interruption.
- Existing ledger temporary recovery additively accepts an omitted row ONLY with
  its matching durable refund marker, unchanged run identity and absent output
  directory. It discards the temporary; never adopts it or refunds by inference.
- `fetch_chunk` consumes the existing signed `ArtifactChunk` QUIC carrier and
  rechecks a host-supplied admission checkpoint before/after network I/O.

## Compatibility and ownership

Class B additions: `mycelium.member_shard_custody.v1` and
`mycelium.member_shard_refund.v1`, private local records only. Existing B3 ledger
rows and all wire/signature bytes unchanged. No dependencies, lockfile, reserved
S1/chat/status/network-cancellation edits. Ruling 10 authorizes the additive API.
No API-to-backend edge. No changed ready level; `route_ready=false` remains.

## Executed proof and remaining gap

The focused carrier test uses the product `LoopbackHost`, `ArtifactService`,
`DurableObjects` and `ChunkSession` in two exact-owned OS processes, separate
roots and endpoint/signing identities, relay/discovery disabled. It transfers
actual bytes from the independent source-slice corpus, verifies signed receipts,
checks retries/revocation-after-read, refunds and reaps/rebinds sockets. Its grant
issuer and admission checkpoint are CONTROLLED fixtures, not real app pairing.
No models, resident proofs, challenge tokens or plans were synthesized.

This is a verified sub-boundary WITHIN phase 2, not phase 2 completion:
1. Wire real pairing/current member generation/consent to the checkpoint and pins.
   Artifact grant v1 itself does not bind membership generation; do not relabel it.
2. Owner source publication still uses DurableObjects' whole-object `verified_size`
   read; qualify/replace with bounded scanning before large model transport claims.
3. Compose a node-owned receiver with headless `slices_ports` / `work_ports` and
   source-derived compile output; no public serving launcher exists here.
4. Bind member native proof to identity; cross-node admission, M16 executor,
   real chat, Stop, worker death/stall and leave remain unimplemented.
5. Discard is for inert bytes only. Future serving ownership must prove worker reap
   BEFORE calling it; path possession is not execution or cleanup authority.
6. Interrupted initial creation before the file-identity record commits fails closed
   with retained debt; automatic reconciliation of that narrow window is not supplied.
7. Physical no-public-relay LAN configuration and TWO-MAC-TEST launch/action commands
   remain blocked on the actual serving composition. Never use N0 as a shortcut.

No demonstrated need to change Evis-reserved behavior; these are implementation
follow-ups, not a request for wider permission.

## Final precursor gates (code f9677be)

- Six-package merge: 1075/1075 passed, 48 skipped (347.810 s tests; 713 s command).
- `cargo xtask quick --base m4/main`: 1924/1924 passed, 131 skipped; 16 affected
  crates, no exclusions (138.705 s tests; 949 s command). No RU-053 re-pin.
- Headless 28 passed/4 ignored. Default and Metal all-target clippy -D warnings,
  fmt and lint passed; lint 1280 files, zero violations.
- Final logs contain no failed/leaked/timed-out test diagnostic. Exact lane-target
  process inventory and B7 fixture-root inventory are both empty; final disk 35 GiB.
- Durable raw logs, commands and parsed receipt live outside the repo at
  `../mycelium-m4-program/qualification/laneB7-f9677be/`.
- This precursor is gate-qualified for review. Requested B7 serving is INCOMPLETE;
  no model two-node gate, reference comparison or native Stop/leave was executed.
  TWO-MAC-TEST.md has runnable precursor commands, but physical serving commands
  remain unavailable. Do not promote these gates into the missing journey proof.
