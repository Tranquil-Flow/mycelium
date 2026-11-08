# Lane B7 — member native custody; serving still incomplete

## B7c uncommitted continuation (current state)

Branch `lane/b7c-serving` from `ff9a796`. No new phase commit: final native model
qualification is resource-blocked, and phases d–f are not completed. Working source
is retained and snapshotted at `../mycelium-m4-program/qualification/laneB7c-uncommitted/`.
Do not mistake the earlier prepare-only PASS for qualification of the final revision.

Ruling 10 additive changes: `joined_shard::native::NativeShard` composes received B3
custody with the existing B6 socket worker; no api→backend compute dependency.
The new stage-contract `mycelium.joined_native.resident.v1` binds enrolled identity,
current generation/incarnation, offer, prepare, actual resident proof and member PID.
Owner verification uses its held seed member row/offer/nonce and independent resident
map, not the proof's self-declared key. Legacy native and artifact wire bytes unchanged.
Member renewal runs independently of native computation. Consent changes/regrant
invalidate custody; no inert refund before explicit successful native close/reap.
`DurableObjects::verify_bounded` is an opt-in bounded/checkpointed source scan whose
interrupted result never populates the verified cache. Existing callers unchanged.
`emit_loopback_qualification` is explicitly non-ready/versioned, with observed child
PIDs rather than fabricated host identities; normal physical `emit` is unchanged.
No reserved S1/chat/status/network-cancellation behavior, external package or review
pin was changed. Trust remains local-worker hygiene, not hostile-code attestation.

Earlier b7c-2 prepare-only run: 1 PASS (364.39 s tests / 385.692 s command), received
751648856 real source-derived bytes, member PID 38814/native PID 38950, 155 resident
rows, enrolled generation 1. Wrong-identity proof refused; explicit close + inert
retirement/refund took 137.584458 ms, with worker absent and empty member ledger.
This is NOT cross-node challenge, M16 chat, public Stop or member leave evidence.
Later consent guard hardening (behavioral RED→GREEN), contract extraction and extra
wrong-nonce/live-discard model checks were not part of that earlier native PASS.

Final b7c-4 rerun: Metal/member builds PASS; disk guard stopped source production
BEFORE native launch (exit -15, 30.724 s, minimum sampled 26.999 GiB; 26.997 GiB
at cleanup, never below 25). No owned lane process/open fixture handle remained;
only exact disposable `b7c-model-57981` was removed. Its shard-file logical lengths were
751648856 and 751651104 bytes. Full two-node source+received storage needs 2.800114
GiB plus overhead; >=29.800114 GiB free is the arithmetic minimum for the 27 GiB
floor (recommend >=30.5). Full ignored two-member model gate remains NOT RUN.
Authorized stale executable sweeps reclaimed 29336736 + 46921304 bytes, preserving
newest-per-stem and all libraries. No unrelated service/cache/model was touched.

Owner is still a test driver. A two-member operator-IPC reference challenge harness
exists but is unqualified; there is no real M16 carrier executor or Stop/leave flow.
Stale OFFER uses a real resume test; stale native proof/death/stall remain unqualified.
No tokens were produced for B7c comparison. Physical route_ready stays false;
no loopback serving-ready level promoted. Full B7 merge-ready: NO.
Final non-model gates: merge 1086 PASS/51 skipped (645.583 s tests, 1075.031 s
command), exit 0, with one nextest LEAK diagnostic on unchanged pure acquisition
bounds. Focused isolation 5/5 passes without leak; cause UNDETERMINED, original
full diagnostic retained. JUnit has zero failures/errors. Quick --base m4/main:
1959/1959 PASS, 135 skipped, 96.437 s tests / 997.818 s command, 18 affected crates,
no exclusions/re-pin. Headless 28 PASS/4 ignored; default/Metal clippy, fmt, lint
PASS (command seconds 26.703 / 54.817 / 20.417 / 6.052 / 20.821 respectively).
Lint 1292 files/0 violations. Final sweep: 9 stale executables/126463952 bytes;
final disk 28.129 GiB. Exact owned PID and model-fixture-root inventories empty.
No new phase commit because final native qualification remains blocked; no push.

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
