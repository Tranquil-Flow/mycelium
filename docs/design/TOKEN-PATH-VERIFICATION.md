# Token-path desk verification

## Current continuation: admission, relay and TLS mailbox components

**PARTIAL; no usable flag-ON chat/device route.** Behavior commits `e44f9a6d`,
`2a991f79`, `e33e4ef3` add pinned native admission, default-absent relay execution
fences and a registered-owner TLS receipt/ACK channel. Python guard: `b313069a`.
The full resume contract is at the top of [TOKEN-PATH.md](TOKEN-PATH.md).

Evidence root: `~/Documents/mycelium-core-program/runs/v2/tokenpath-phase3-admission/`.
Each label retains command, raw pytest log, XML and machine-parsed summary.

| Label | Passed | Failed | Errors | Interpretation |
|---|---:|---:|---:|---|
| python-guard | 34 | 0 | 0 | Interpreter guard and native progress |
| admission-green | 283 | 7 | 0 | Admission and adjacent tests; not wholly green |
| baseline-prefill | 0 | 7 | 0 | Same seven prefill-wait failing IDs on wave4 |
| hops-final | 56 | 0 | 0 | Real relay with fake runtime, admission and adjacent checks |
| mailbox-green | 58 | 0 | 0 | Actual pinned TLS, grammar and control/concurrency regressions |

The relay's initial failure was fixture setup, not behavioral RED. Retrospective
fence-removal mutation retained 16 failures/6 passes. Final coverage strengthened
queued post-compute revocation after that mutation. Mailbox missing-API RED had
10 failures; a later sealing-race RED had 2 failures/10 passes before the fix.
A separate relay-pump selection had one MLX-fence collection error (56 passed).
No MLX/GPU/model workload ran.

These are separate component proofs: private native child → verified pipe, real
relay → fake runtime, and signed receipt → pinned TLS → verified owner inbox/ACK.
They are NOT one joined worker/chat/two-Mac test. The TLS evidence sealer is a
recording test callback, not the production full-execution evidence writer.
Native ACK forwarding, durable pump progress replay, production origin mounting,
trusted participant policy provisioning, owner/chat selection, interruptible
compute waits, A6/A7 continuation and PV34 evidence reconciliation remain open.

Full eleven-suite comparison against current `rel2/wave4-staging` (`9bb62f79`)
was NOT run: A1 still held the device/first-chat lease. Focused runs completed
while that interval was active are retained and not claimed as exclusive-load
qualification. Further runs are held. Canonical manifests are NOT re-pinned;
there is no new full no-regression or release-readiness claim. Re-pin last only
after the remaining composition and fresh comparison.

## Historical phase 3: native receipt transport slice (partial)

**NOT READY for device use; the requested flag-ON chat route is not implemented.**
Behavior commit `9df4bbf2` adds the native POSIX progress frame, bounded verified
inbox, shared-reader demultiplexing and native worker facade. It also fixes
phase-2 rejection of the valid initial `path_attempt=0`. No placement code,
managed command field set, owner/chat dispatch or public capability was changed.

### Exercised boundary

A real private CPU subprocess executes the existing signed entry adapter and
native `_infer_decode` over fake two-stage compute. Its real `_ResponseEmitter`
writes signed batches through an OS pipe to `PhysicalNodeProcess`; the parent's
pinned `ReceiptCursor` verifies six tokens before the held start response is
released. An independent cleanup-lane **test response** remains deliverable.
The child has a test dispatcher, not production grant-bearing `_infer_start`;
this proves native pipe composition, **not** managed admission, actual cleanup
receipts, model execution, iroh, chat or a two-Mac route. Test authority uses fresh
private keys and owned scratch files; no fleet signer or membership is loaded.

- Initial seam-absence RED: **9 executed failures**, missing new progress module.
- Initial native transport GREEN: **108 passed**, including inherited authority
  and local-driver tests.
- Initial-path regression: **1 failed, 19 passed**; `path_attempt=0` was rejected
  as `token_path_grant_invalid`, then fixed to match native control validation.
- Worker facade absence RED: **1 failed, 119 passed**, then forwarding added.
- Cleanup-lane behavioral RED: **1 failed**; progress on the cleanup prefix was
  accepted, then explicitly rejected without returning tokens.
- Final focused candidate: **121 passed**. Covers real private pipe composition,
  signature tamper, gap/duplicate refusal, bounded overflow, active/lifetime
  capacity, identity binding, cancellation before command write and during
  blocked validation, absolute read/write deadlines, full pipe backpressure,
  partial-write poisoning, EOF/terminal distinction and independent cleanup.

Retained evidence and reproducible guard/runner:

`~/Documents/mycelium-core-program/runs/v2/tokenpath-phase3-native-progress/`

### Full requested desk comparison

The desk guard and transitive MLX fixture exclusion rule are unchanged from
phase 2. All eleven requested suites ran serially against the existing wave4
worktree (`f230c809`) and the lane's unchanged behavior commit (`9df4bbf2`), with
the same **50 excluded MLX-fixture modules**. The requested `tests/node_agent`
and `tests/testbed` are included alongside the prior nine suites. Both overrides
(`PYTHONPATH`, `MYCELIUM_PHYSICAL_SERVICE_PYTHON`) are unset. All pytest basetemps
use owned `tokp3-*` scratch paths and are removed after parsing.

| Candidate | Passed | Failed | Collection errors | Skipped |
|---|---:|---:|---:|---:|
| Wave4 `f230c809` | 5174 | 113 | 12 | 41 |
| Native transport slice | 5295 | 113 | 12 | 41 |

Each run additionally reported nine passing subtests. The baseline took
1133.84 s and the lane 998.95 s; these are suite durations, not token-path
performance measurements. Both pytest runs exited 1; the wrapper's zero status
only records successful receipt retention and scratch cleanup.

Programmatic testcase-ID comparison found **no new failing/error IDs, no removed
tests, identical exclusions**, and all **121 token-path tests added since wave4
passed**. This is a clean regression delta against a failing fenced baseline,
not a green full qualification. Twelve dynamic MLX-import collection errors and
the remaining native/environment/baseline failures were retained, not waived.
Full raw logs/XML, command manifests and `comparison.json` are in the evidence
root. Documentation and canonical pins were finalized after the behavior run;
the subsequent focused/contract/governance/claim check is retained as
`postpin.xml`, with exact committed hashes and manifest checks in `FINAL.json`.

### Exact unfinished phase-3 work

1. Owner grant construction from admitted request/route/membership state;
   grant-bearing grammar across managed control, worker and work authorization;
   native validation **before prefill**, actual process/lease clocks and remote
   grant installation/enforcement before every autonomous hop.
2. ManagedWorker receipt forwarding and authenticated mailbox transport with
   evidence sealing, signed ACKs, bounded replay/reconnect, prompt admission and
   no reissued start. The new native facade is available but dispatch does not
   register it. It is not an implemented managed stream.
3. `PhysicalLiveRoute`/chat negotiation and selection, exactly one grant-bearing
   start plus receipt consumption when fully supported, unchanged per-token
   fallback only before autonomous admission, and owner cancellation fanout.
4. Grant-bound hop/prefill interruption, unchanged terminal mapping, A6 fresh
   attempts, committed A7 continuation/cursor authority, and PV34 credit only for
   independently verified stage execution. Token receipts alone grant none of
   those claims; generation changes still fail closed.
5. Full composition review/qualification, then explicit device authorization.

`lane/rel2-fastroute` (`760a76d6`) placement/speed/locality files are untouched.
Only the shared final source manifest is expected to overlap; regenerate it at
integration rather than choosing one lane's digest. Device trials must hold the
actual selected entry fixed across OFF/ON, not force owner-as-entry. The detailed
0.5B/7B interleaved measurement plan remains in [TOKEN-PATH.md](TOKEN-PATH.md).

---

## Historical phase 2: signed authority/receipt slice

**Partial / NOT READY for device use.** Behavior commit `36080eb9` adds
`mycelium_router/token_path_authority.py`, the internal
`PhysicalNodeService.drive_granted_entry_token_path` adapter and
`tests/live/test_token_path_authority.py`. The flag stays OFF; neither
`infer_start` nor the owner/managed/chat grammar is extended. With the internal
flag ON, the desk composition verifies a pinned-owner grant, drives native
`_infer_decode` over fake two-stage compute, signs bounded batches, and verifies
the receipt cursor before exposing tokens. **There is no real two-Mac or web-chat
streaming path yet.** See the security delta and exact remaining implementation
steps in [TOKEN-PATH.md](TOKEN-PATH.md).

### Focused evidence

* Initial API-absence RED: 42 executed failures, all due to the missing authority
  module. This is seam-absence evidence, not a cryptographic or wire-behavior RED.
* Initial implementation: 71 focused tests passed (42 new + 29 inherited).
* Additional lost-ACK behavioral regression: 1 failed, 55 passed; retrying an
  acknowledged receipt raised `token_path_receipt_ack`. The fix preserves only
  the latest acknowledged digest, allowing its exact retry while rejecting a
  conflicting digest. No unbounded ACK history was added.
* Final focused run: **99 passed**, including 70 phase-2 and 29 inherited tests.
  Real CPU Ed25519 signatures; fake native two-stage compute; no MLX/iroh/device
  execution. Covers every existing control field, requester/route binding,
  expiry and process/lease clamps, tamper/unknown fields/boolean substitution,
  bounded replay/retention, output cursor/terminal checks, lost ACK, mutation
  isolation, publication expiry, native overflow/cancel/stage-loss composition.
* Removing only the new adapter method from the current native AST produces an
  AST identical to `29e47be01f20acbd4a26f266570547f08f721448`. This is an additive
  default-path source check, not a live equivalence measurement.

### Battery environment and evidence

The same prior desk guard and transitive MLX exclusion algorithm were reused,
with only the scratch prefix changed to `tokp2-`. Runs use
`env -u PYTHONPATH nice -n 19` and the specified ladder venv, pytest
`-p no:cacheprovider -n 1`; the broad baseline and lane runs are serial. No
quarantine attribute was removed. All temporary test state is in owned scratch;
retained receipts are under:

`~/Documents/mycelium-core-program/runs/v2/tokenpath-phase2-36080eb9/`

The first baseline was a `git archive` of exact wave4 `f230c809`, which omitted
Git metadata: five Git-dependent tests failed for that setup reason and three
Git-dependent cases skipped. All seven affected modules were rerun on an
isolated **detached wave4 worktree**, with the same guard: **52 passed**, recovering
all eight cases. The raw archive results remain retained, not rewritten. No
other branch was checked out or changed. The programmatic comparison records
raw results separately from the explicitly overlaid Git-setup recovery.

All nine requested suites were selected. The exact same **50 modules** were
excluded on both sides by the prior MLX fixture fence.

| Run | Passed | Failed | Collection errors | Skipped |
|---|---:|---:|---:|---:|
| Raw wave4 archive | 4694 | 111 | 12 | 12 |
| Wave4 after explicit Git-setup recovery overlay | 4702 | 106 | 12 | 9 |
| Phase-2 lane | 4801 | 106 | 12 | 9 |

Each broad run also reported 9 passing subtests. The baseline broad run took
1111.95 seconds; the lane took 1220.30 seconds. Wrapper exit zero records the
run, **not** pytest success: both broad pytest invocations returned failure.

Programmatic testcase-ID comparison: **no new failures or collection errors**,
no removed tests, no changed exclusions; all **99 added tests** passed. After
Git-setup recovery, the failing/error ID sets are identical. This is a clean
regression delta inside a fenced, failing baseline, not a green qualification.
The native/build/MLX-blocked coverage remains unqualified. Raw XML/logs,
`tokp2-baseline-git.xml`, both command manifests, guard/runner/comparison script,
focused RED/GREEN receipts and default-path AST evidence are retained above.

The final focused and contract/governance/claim rerun is retained as
`tokp2-postpin.xml`; `FINAL.json` records the final manifest checks and head.
Manifest-only pins are the last commit. The full battery above tested the same
behavior-bearing source; final edits were documentation and canonical pins.
This phase does not claim A6/A7 streaming parity, PV34 receipt liveness,
interruptible publication or full evidence sealing; those remain integration
gates, not failures hidden by the desk fence.

---

## Historical phase-1 verification

**Partial / NOT READY for device use.** The public demo still uses its existing
per-token path. See [TOKEN-PATH.md](TOKEN-PATH.md) for the security delta and
ordered remaining implementation. No device or MLX/GPU inference was run.

## Candidates and scope

- Original baseline: `9c6c7260` (`rel2/wave23-staging`).
- Integration: `f230c809d2de545e7a519a33e766535170ddb997`
  (`rel2/wave4-staging`): no-ff merges of PV34 `f1d40a73` and PV35 `c5d83e39`,
  followed by canonical contract/A6 repinning. Both merges were clean.
- Token driver behavior: `1fdc223c` on `lane/rel2-tokenpath` (plus this final
  verification documentation and canonical manifest pins). The internal adapter
  is OFF by default and is NOT in the managed command grammar.
- Implemented: entry-local loop, existing native decode/signing adapter,
  bounded unsigned local batches, immediate prefill output, stop/cancel checks,
  local deadline/ceiling checks, append-only output validation and exception
  propagation. Wire grant admission, signed streaming, interruptible deadline
  enforcement and owner A6/A7/A10 evidence reconciliation remain unimplemented.

## Reproducible environment and exclusions

All pytest runs used the specified ladder venv, `nice -n 19`, `-n 1`,
`-p no:cacheprovider`, and `--basetemp` under `~/.hermes/cache/scratch/tokp-*`.
The accepted battery runs started with `env -u PYTHONPATH`; pytest was 9.0.2.
A pytest plugin loaded after normal Python site initialization changed cwd to
its explicit `TOKP_REPO`, fenced MLX imports, and rejected quarantined child
executables/scripts. It used macOS libc `getxattr`, never subprocesses inside
its audit hook. A non-executable plain-data `setxattr`/`getxattr` self-test passed;
the quarantined file was detected, never executed, then removed.

The safety fence is intentionally conservative. Static transitive test-fixture
analysis excluded **49 modules** at the original baseline and **50 modules** at
wave4/lane; the additional module is PV35's MLX-fixture-backed
`test_snapshot_bound.py`. Remaining dynamic MLX imports caused 12 collection
errors and 74 explicit fence failures in the original baseline, plus downstream
capability-discovery failures. Thus these results are **not** the historical
25-native/package-failure vanilla baseline and **not** a full qualification.
No fence or assertion was weakened to make the counts green.

Earlier missing-pytest, invalid `os.listxattr`, and interrupted setup attempts
are excluded from the comparison. The wrapper's exit zero means it finished
recording pytest, not that pytest passed; each failing battery's pytest status
and raw summary are retained.

The exact invocations, ignored paths, plugin, runner, XML, logs, benchmark
samples and programmatic comparison are retained outside scratch at:

`~/Documents/mycelium-core-program/runs/v2/tokenpath-desk-1fdc223c/`

To replay the exact runner, copy its `guards/tokp_desk_guard.py` into
`~/.hermes/cache/scratch/tokp-guards/` (do not create a `sitecustomize.py`), then
invoke `executed-runner.py <repo-path> <unique-label>` with the stated venv and
`env -u PYTHONPATH nice -n 19`. The runner invokes `python -m pytest` with
`-p tokp_desk_guard`; the plugin supplies the repo import path without replacing
Homebrew's normal site setup. Every output goes to an owned scratch namespace.

## Requested battery

All nine requested directories were selected: `tests/live`,
`tests/a6_acceptance`, `tests/a7_acceptance`, `tests/a10_deterministic`,
`tests/a13_macos`, `tests/a13_operator`, `tests/claims`, `tests/governance`,
`tests/contracts`. The original-to-wave4 comparison therefore also includes
all five required integration directories.

| Candidate | Passed | Failed | Collection errors | Skipped |
|---|---:|---:|---:|---:|
| `9c6c7260` | 4678 | 106 | 12 | 9 |
| wave4 `f230c809` | 4702 | 106 | 12 | 9 |
| token-path lane | 4730 | 107 | 12 | 9 |

Each battery additionally reports 9 passing subtests and one warning. Do not
add JUnit's subtest-inclusive declared total to the ordinary testcase counts.

- **Wave4:** exactly 24 new passing tests, no removed tests, identical failure
  and collection-error IDs. Failure messages match after root normalization
  except dynamic browser locator state in the same existing failed assertion.
- **Lane:** exactly 29 new tests, all passing. One extra failure in unchanged
  `tests/a13_macos/test_demo_target.py:147` expected `downloading_slice` but
  observed `slice_ready`. The inline slice worker can finish before that
  observation. Neither this test nor `demo_target.py` differs from wave4.
- Forced completion immediately after `Thread.start` reproduced the **same
  assertion failure on both wave4 and lane** (`tokp-race-w4.xml`,
  `tokp-race-lane.xml`, retained `tokp_schedule.py`). Normal whole-module reruns
  passed **28/28 on both**. This attributes the extra raw failure to an existing
  scheduling-sensitive test; it does not erase it or turn the full run green.
- No new code regression was identified within the runnable desk scope. Blocked
  MLX/native fixture coverage remains unqualified.

## Focused RED/GREEN and measurement

The first API-absence run produced 19 failures because the node adapter did not
exist: seam-absence evidence, not proof of wire behavior. Replacing an overly
simple signing fake with the real CPU signer exposed 8 response-shape failures;
the adapter now reads `observation.details`. A publication-deadline regression
failed with DID NOT RAISE before its fix. The clean-env first-token regression
failed with decode round 2 instead of 1 before the prefill-flush fix. The final
focused suite passed **29 tests**, exercising the real native decode method
with fake two-stage compute and real local observation signing.

Desk/fake microbenchmark (`python -m scripts.bench_entry_token_path`): five
interleaved trials per path, 128 tokens, **10 ms simulated control delay**,
**two 1 ms fake hops per token**, trivial fake compute, batch limit 8/50 ms.
These are wall-clock measurements of fake components, not iroh/model/device
performance or a near-local-speed qualification.

| Median metric | Per-token control | Entry-local driver |
|---|---:|---:|
| End-to-end tokens/s | 67.9 | 369.8 |
| Observed steady tokens/s | 67.9 | 383.7 |
| TTFT (ms) | 14.3 | 15.1 |
| Inference commands per trial | 128 | 1 |

The benchmark does not include production signing, mailbox delivery, live
compute, cleanup or receipt verification. Command counts exclude bind/health/
cancel/cleanup controls. The driver's unsigned callbacks are not wire receipts;
fake stage-trace equality does not establish complete A10 evidence parity.

## Remaining gate

Implement the signed per-request grant and participant binding, bounded signed
progress channel, owner route integration and continuation cursors, then prove
cancel/deadline/ceiling, stage loss/successor and evidence parity on that actual
composition. Keep the feature OFF. After explicit device approval, compare
flag OFF/ON on the 0.5B and 7B two-Mac routes, fixed owner-entry/split/prompts/
seed/token budget, at least five interleaved trials each: warm/cold TTFT,
steady and end-to-end tok/s, inter-token gaps, control counts, receipt lag,
local baseline, and failure/cleanup controls. No device measurement was attempted.
