# Foundation exit — NOT MET

> **RU-053 note (owner-approved 2026-10-31):** `budget-model --example inventory_gate` is now a report. It prints the inventory counts and a closure status line and exits 0 whenever the report was produced. The `inventory_gate` results recorded below (exit 1; 1337 and 1,845 closure violations) were gate results when they were taken and are kept as recorded.

## CURRENT STATE
- **Round 3 (resumed after provider limit) tested at product/data HEAD `6e3ae8d6`, rerun on `edee89e6` (docs-only delta):** released-package disposition gate `cargo run -p obligations --example package_gate` exit0: 1,552 ZIP entries = 182 directories + 1,370 dispositioned paths, 2,631 embedded members, 431 families, **0 unexplained paths**. `cargo test -p obligations` exit0 (45 tests incl. package_dispositions/package_evidence controls); `cargo fmt -p obligations --check` exit0. Receipts `R3/final-*.{log,json}`; independent verification `R3/independent-verification-final.log` (739 accepted source objects rehashed, corrupt-ZIP-digest rejected, package not executed).
- Round3 workspace gates (guarded, same HEAD): strict all-target Clippy PASS; **fmt RED (L10 only: crates/api/tests/event_validation.rs, 6 hunks)**; tests **952 pass / 2 fail / 1 pre-existing ignore** (949 unit+integration, 3 doctests); **xtask lint RED: 12 TM002 / 518 sources, all in inherited L12 crates/device-harness/tests/{keeper,native_session}.rs**. No lane file (test-support/obligations/**, evidence/foundation-exit/**) appears in any fmt/lint/Clippy finding.
- The 2 test failures are inherited, not lane code: L1 `budget-model inventory_bindings` (reviewed-composed-ceiling category, unchanged since round2) and L3 `journey-tests process_safety::early_child_exit_fails_readiness_and_removes_its_scratch` = `BLOCKED:host_busy_marker` because the device lane's `DEVICE.busy` (job .../device-calib/attempt3, appeared 00:41) is present; journeys' process.rs blocks on any busy marker while the resume guard exempts device-calib. Environmental, rerun alone (`R3/final-process-safety-rerun.log`) fails identically while the marker exists; passed in round2 with no marker. Not loosened, not edited.
- Clause tally after round3 re-audit: **unchanged 14 MET / 15 PARTIAL / 1 OPEN / 5 BLOCKED across 35 clauses**. L0.1 and L0.2 evidence is stronger (package-path and embedded-member inventory closed, zero unexplained) but each stays PARTIAL: endpoint/profile/old-suite-history and final contract-owner/A6 digest rebinding to f4e366e9 are still open. Foundation exit NOT MET.
- Round 2 baseline retained below: merged preview 3 `e3773ea3` as `14aa6a27` (main `8a2c0f0d` untouched, no promotion); round2 evidence/data HEAD `0b26d0bc`.
- Fresh historical MODEL controls: exactly ten intended assertion REDs (exit101), same ten corrected GREENs (exit0). Not historical Python/product executions.
- BACKLOG 10 data delivered: committed `evidence/foundation-exit/{oracle-provenance,oracle-target,native-inputs,seal}.json`; 96 unique referenced inputs rehashed; corrupt-digest control rejected.
- Exact preflight transition: native-adapters and oracle-journeys NOT_RUN(BLOCKED/unset) -> RUN; all six selections RUN, exit0. Metadata presence only; no execution/authentication implied.
- Native input all-target Metal check PASS with actual L8 header tree + libmlxc/libmlx/libjaccl/metallib. No GPU/model/native test execution; current Rust compiler 1.95.0 recorded separately from unknown historical L8 compiler.
- Frozen-preview L1 inventory: **1337 violations**. No qualified phase p99; prior 24-attempt/360-base/672-synthetic profiles retained, not remeasured. BACKLOG 11 seven semantic budget requests remain open in this preview.
- Prior released Python replay remains 20+33 observations; rehashed provenance, not rerun. Full joined-member/two-host HTTPS/model-backed HTTP corpus and unchanged full Rust replay remain BLOCKED.
- Round2 L0 census (superseded by round3 above): 1,552 entries = 182 directories + 733 mapped + 637 then-unexplained; the 637 are now individually dispositioned in `test-support/obligations/data/package-dispositions.json`.
- Lock/API requests: none. Round3 changed only test-support/obligations (data, example, tests) and evidence/foundation-exit; no root Cargo/lock change, Python addition, download, installed-app/demo/port/MacBook operation or remote/main write.
- Next: owners close the eight remaining-work rows below (row 1 narrowed by round3); integrator takes a new frozen preview and reruns gates/full authorized corpus. Keep main blocked. Private target retained.

## Authority and evidence conventions

Authority: `/Users/mycelium-dev/Documents/mycelium-core-program/status/V02-PLAN.md` §§5,7,9 and `runs/v2/v02/LANE-COMMON-V02.md` revision3. Preview3 was committed at intake; its narrative report was not yet published. Frozen input pins are in `INTEG-PREVIEW-3-evidence/pins.json`; no credit is borrowed from later moving lane heads.

`E/` = program `runs/v2/v02/`; `R2/` = `E/FOUNDATION-EXIT-round2/`; `R3/` = `E/FOUNDATION-EXIT-round3/`; `D/` = repository `evidence/foundation-exit/`; `FX/` = prior `E/FOUNDATION-EXIT-evidence/`. `R2/gate-summary.json` contains independently counted completed logs, commands, exits and current compiler metadata. `D/seal.json` binds genuine manifests/provenance; `runs/v2/v02/FOUNDATION-EXIT-round2-evidence-manifest.json` binds this round's report/status and receipts. MET credits only each named bounded clause, never overall product/release qualification.

## Every L0–L4 Done-when clause — re-audited on preview3

| ID / plan line | Clause | Status | Evidence / exact remaining boundary |
|---|---|---|---|
| L0.1 /113 | Every shipped module/resource/public endpoint/profile and old test group has a Rust destination or reviewed retirement | PARTIAL | Round3 package inventory CLOSED (R3/final-package-gate: unexplained_paths 0; R3/final-focused-tests exit0): test-support/obligations/data/package-dispositions.json explicitly maps all 1,370 non-directory ZIP paths and 2,631 embedded members in 431 reviewed families, including all 637 former gaps. D/package-verification.json rehashes actual ZIP bytes and 739 accepted source objects; D/package-disposition-review.md records the decisions. Remaining: endpoint/profile/old-suite-history rebinding to accepted f4e366e9 and final contract/A6 crosswalk; the whole clause stays PARTIAL. |
| L0.2 /113 | Compare final package, static closure, contract owners and A6; zero unexplained paths | PARTIAL | Round3 exact census (R3/final-package-gate exit0, 0 unexplained) covers 1,552 ZIP entries (182 directories) plus CArchive13/PYZ2463/base_library155, with zero unexplained package paths. All 637 old gaps are individually required; same-shape new header/dependency/native paths fail instead of inheriting coverage. D/package-verification.json and Rust package controls bind every exact member. The 725 loose runtime path set still equals provisional static closure; final contract-owner/A6 digests differ and final crosswalk rebinding remains OPEN. |
| L0.3 /113 | Pin existing fixtures; preserve external old suite/history | PARTIAL | D/oracle-provenance.json and seal.json bind accepted source/tree, executable, published ZIP, READY/release receipts and immutable historical fixture identities; prior accepted-successor replays remain separate. Full retained old suite/history and final registry rebind still need L0 review; no golden recapture. |
| L0.4 /113 | Supported device/model/profile cells and explicit unknowns | MET | R2/tests.log: obligations supported-cells inventory controls pass; test-support/obligations/data/supported-cells.json retains explicit unknowns. Inventory credit only, not device/model qualification. |
| L0.5 /114 | Inventory all ten mandatory IDs and historical/model recipes | MET | R2/tests.log obligations/incident matrix controls; data/escapes.json and sim/INCIDENTS.md preserve all ten IDs. R2 historical-model-red/corrected-controls logs additionally execute each named model assertion. |
| L1.1 /120 | Property/stateful valid/impossible caps, checked arithmetic, jumps and late joins | MET | R2/tests.log: checked_arithmetic_never_wraps, explicit_overflows_units_and_clock_domains, stateful_reservations_atomic_and_cleanup_bound and f15_absolute_member_deadline_red_green_and_late_join pass. Model-contract scope, not whole-product admission. |
| L1.2 /120 | Renewal ≤ TTL/3; rollover reserve ≥ stop+start+safety | MET | R2/tests.log: renewal_valid_and_impossible, individual_origins_not_smallest_relative_cap, f4_worker_not_extended_rollover_precedes_worker_expiry pass. Configured/model relations only; tail calibration remains separate. |
| L1.3 /120 | Every applicable operational budget ≥2× measured phase p99 | BLOCKED | FX/device-v2-final.json still has zero qualified p99; no new physical measurements authorized. R2/inventory.log has 1337 violations. R2/tests.log also exposes inherited L1 binding_categories_reference_real_rows_without_turning_defaults_into_values failure; current owner repairs are outside the frozen preview. |
| L1.4 /120 | Consent is upper bound; refuse early/use authorized plan, never enlarge grant | PARTIAL | R2/tests.log preserves consent_fit_and_impossible_work and consent_never_expands_to_fit_work. New planner/node-service component tests do not replace genuine joined-member public assignment/deadline observations. |
| L1.5 /120 | Independent expected values do not call implementation budget function | PARTIAL | Independent model/literal controls remain in R2/tests.log. Complete call-site expected-value independence review is absent; inherited binding-category failure and unclosed source inventory are not waived. |
| L1.6 /121 | F1/F4/F13/F14/F15/F15b/F16 RED/GREEN, plus F2 retention accounting | MET | R2/historical-model-red.log: ten actual intended assertion failures, exit101. R2/corrected-controls.log: identical ten names PASS, exit0. All required IDs including F2 are retained; no historical Python execution or final product-GREEN claim. |
| L2.1 /127 | Synchronous driver controls clock/RNG/order/persistence/restart/I/O | MET | R2/tests.log driver_contract tests pass with clock/RNG/order/restart/I/O control. Original L2C producer provenance remains at 2ee2ae0d; no new determinism claim for native adapters. |
| L2.2 /127 | Pinned turmoil partition/drop/duplicate/reorder/disk integration and comparison | MET | R2/tests.log pinned_turmoil_network_matches_minimal_driver_and_independent_oracle and pinned_turmoil_disk_failure_matches_minimal_driver pass at the typed application seam. This does not claim deterministic Iroh/QUIC/Metal. |
| L2.3 /128 | Raw samples/count/method/model/build/device/load and p50/p99 for compile/publish/start/stop/close/relay RTT/TTFT; sparse calibration BLOCKED | PARTIAL | R2/tests.log reruns V2 importer controls. Preserved FX/device-v2-final.json contains 24 attempts/360 base profiles plus 672 synthetic variants, not new measurements. Native close/relay RTT, comparable repeated tail samples and complete hardware/checkpoint/load identity remain missing; p99 stays BLOCKED. |
| L2.4 /128 | Explicit synthetic 2×/3× loaded-host variants, never called measured | MET | R2/tests.log V2 provenance/scaling controls pass. 2x/3x millisecond variants remain explicitly SYNTHETIC_NOT_MEASURED; throughput is not multiplied and sparse/censored data cannot invent p99. |
| L2.5 /129 | Replay pins build/lock/simulator/PRNG/config/profile; concrete minimized traces survive generator changes | MET | R2/tests.log budget_reads, trace_migration and both persisted corpora pass on merged code. Concrete v2 traces retain their original build/profile identities and migration metadata; no seed-only substitution. |
| L2.6 /129 | Same fake/real storage/transport/worker corpus incl. failures/cleanup | PARTIAL | R2/tests.log storage corpus and integrated runtime worker-adapter controls pass. Same-corpus production transport/Metal-worker parity is not proved by default-feature tests; this round does not execute real Iroh/GPU/model jobs. |
| L2.7 /130 | Ten actual historical-model assertion REDs; corrected controls with authorized progress and reachability | MET | R2/historical-model-red.log and corrected-controls.log freshly prove the same ten historical assertion REDs and ten corrected GREENs; R2/tests.log retains authorized-progress/reachability checks. Setup failures are excluded from RED counts. |
| L2.8 /130 | Replace model SUT with actual production transitions after L5+ | OPEN | Preview3 introduces more production node/worker transitions, but L2 incident SUTs remain models. L2 with L5/L6/L8 must replace those seams and kill the same faults against actual production transitions before release qualification. |
| L3.1 /136 | Same full corpus passes accepted Python first, then unchanged assertions against Rust | PARTIAL | D/oracle-provenance.json rehashes prior f4e366e9 baseline20/boundary33 observation receipts and original golden bytes. R2/tests.log tests runner mechanics, not external app journeys. Full accepted-Python corpus and unchanged Rust-target replay remain unrun. |
| L3.2 /136 | Create/join/consent/model/plan/serve/chat/cancel/status/stats/restart/leave; signed refusals, persisted normal/fast and cleanup | PARTIAL | Preserved FX released receipts cover create/model choices/normal-fast persistence/status/signed refusals/unjoined restart/leave/teardown. Successful bound join, public plan/serve/chat/active cancel/SSE/directory stats remain blocked; native manifest readiness does not execute them. |
| L3.3 /137 | Fresh state, genuine constructors/defaults/test keys; owner no standing member grant | MET | Prior released receipts retain genuine constructors/defaults/test keys, fresh isolated HOME/state and exact-owned cleanup; R2/tests.log reruns process containment controls. Owner standing grant is not fabricated. No external package is launched in round2. |
| L3.4 /137 | Member explicitly chooses 10min/2GB; separate fresh4GB and saved2GB upgrade | PARTIAL | Prior observations include fresh4GB and saved unjoined2GB restart. Genuine bound member10min/2GB and actual saved2GB cross-version upgrade require separately authorized non-demo HTTPS/two-host resources and are not substituted by helper fixtures. |
| L3.5 /137 | Public actual stage assignment/reservation and worker/rollover deadlines | BLOCKED | Actual public assignment/reservation and worker/rollover deadline observations need joined consent, distinct hosts and prepared models. New planner/component tests cannot discharge this HTTP boundary; no fake second host or grant enlargement. |
| L3.6 /137 | Preserve production unknown-member semantics | PARTIAL | Unchanged historical refusal fixtures plus accepted-successor receipts preserve observed unknown-member rules. Successful real-worker public observations remain absent; no unapproved stricter strategy-draft policy introduced. |
| L3.7 /138 | Direct F3/F5/F14/F15/F15b, compose other lifecycle escapes with L2 | BLOCKED | Direct F3/F5/F14/F15/F15b joined-member journey coverage remains unrun. Fresh model RED/GREEN evidence is separate; idle status is not compile responsiveness, unjoined restart is not leave/re-report, and helper staging is not public actual placement. |
| L3.8 /138 | Combined real-default regression target ≤5min | BLOCKED | Prior 12,419ms baseline and 9,474ms boundary runs omit real-default worker cases. No complete real-default corpus duration exists; short partial runs cannot prove the <=5min target. |
| L4.1 /144 | Compiler/Clippy + tested policy reject ignored fallible results, detached failures, false cleanup success and literal durations | PARTIAL | R2/clippy.log: default workspace/all-targets -D warnings PASS; former L6 and SLICE-1 Clippy diagnostics are closed. R3/final-lint.log is RED: 518 sources, 12 TM002 violations, all in inherited L12 keeper/native_session tests (none in lane files). R2/tests.log policy controls pass; this lane neither suppresses violations nor edits the owner crate. |
| L4.2 /144 | Safe specific error code/kind/source/phase; best effort records failure; no secrets | PARTIAL | R2/tests.log retains sanitized importer/journey/xtask diagnostics and explicit failure controls. No exhaustive product error-kind/source/phase and no-secret diagnostics audit is claimed. |
| L4.3 /145 | Positive/negative fixtures; narrow owned expiring exceptions; beyond must_use | MET | R2/tests.log xtask syntax/semantic positive and negative fixtures pass; measurements remain distinct from enforcement. Existing owned expiring exception rules remain enforced; no exception added or weakened. |
| L4.4 /145,147 | F16 swallowed-stop and literal30 negative controls; C1/C2 harness regressions | MET | R2/tests.log: F16 swallowed-stop/literal30 negative controls and C1/C2 harness regressions pass. R3/final-lint.log still rejects 12 inherited L12 literals; controls passing does not waive actual violations. Real native-close performance/physical cleanup remain separate qualifications. |
| L4.5 /146 | Fresh-worktree admitted native/tool/fixture discovery/build; absent prerequisites BLOCKED | PARTIAL | D/native-inputs.json, oracle-provenance.json and content-verification receipt bind genuine inputs; R2/preflight-final.log changes both blocked selections to RUN. R2/native-input-check.log passes Metal all-target compile/check with actual selected header/libs, no GPU execution. Automatic fresh setup, shared content authenticator and packaged relocation/minimum-OS/IO receipts remain open; preflight is file presence only. |
| L4.6 /146 | Quarantine owner/reason/expiry/replacement; expired quarantine fails | MET | R2/tests.log expired_quarantine_fails_even_with_present_prerequisite and valid_quarantine_is_visible_and_does_not_admit_mandatory_group pass. No new quarantine; mandatory absent inputs stay NOT_RUN/BLOCKED. |
| L4.7 /146 | Per-change/transitive selection includes doctests and custom jobs | PARTIAL | R2/tests.log transitive selection/blocked dependency controls pass; R2/preflight-final.log admits workspace doctest and custom lint/native/journey groups. Per-change execution automation and descriptive command repair remain L4/L12 work; RUN is not executed coverage. |
| EXIT /147 | L1–L4 contracts/lints/control tests work, all ten historical/model RED recipes have real assertion-failure evidence, corrected controls pass, and HTTP corpus has passed Python | BLOCKED | Ten fresh historical/model assertion REDs and corrected GREENs, strict Clippy and genuine input discovery are proven. Full Python HTTP corpus, measured calibration, complete disposition/budget closure and inherited L1 test/L10 fmt/L12 literal-lint failures remain; round3 also shows L3 process_safety blocked by the device lane's busy marker (environmental). No main promotion or product-port GREEN. |

## Remaining exit work / exact owner boundary

1. **L0 (with L8/L11 for package resources)** (L0.1, L0.2, L0.3): Rebind final static/contract-owner/A6/test-history inventories and endpoint/profile coverage to accepted f4e366e9. Round3 closes all 637 package gaps and enumerates/dispositions every embedded PYZ/base_library/CArchive member; these are no longer unexplained. The 725 loose runtime path set matches, but final contract/A6 digests still differ. L8/L11 retain candidate-native/JIT placement, minimum-OS/relocation and replacement-parity obligations; inventory disposition does not qualify those behaviors.
2. **L1 coordinator/review partitions** (L1.3, L1.5): Close 1337 frozen-preview inventory violations; repair inventory_bindings.rs:49 exact-category assertion without loosening it (new reviewed-composed-ceiling category). Independently review all expected values. Integrate later owner fixes only in a new preview, not as untested credit.
3. **L1 + L12 (BACKLOG 11)** (L1.3, L4.5): Adjudicate/register all seven exact semantic requests: HarnessMinimumSoak 1200000ms, KeeperRecoveryWindow 21600000ms, KeeperRecoveryAttempts 3, KeeperRolloverGrace 300000ms, KeeperStartupGrace 900000ms, proposed KeeperMemberSilence 300000ms, HarnessOwnerStatusCheck 20000ms; preserve source/owner-decision provenance, no fallback constants. Names are absent from frozen budgets source.
4. **Owner/fleet + L2 + L1** (L1.3, L1.4, L2.3): Supply authorized exact-boundary repeated comparable compile/publish/start/stop/native-close/relay-RTT/TTFT trials with source/checkpoint/device/macOS/load identity and reviewed independence/tail method; then enforce applicable >=2x measured phase p99 without enlarging consent. Existing sparse/censored observations stay BLOCKED.
5. **Owner/fleet + L3 + composition owners L6/L7/L8/L10** (L1.4, L3.1, L3.2, L3.4, L3.5, L3.6, L3.7, L3.8): Authorize separate non-demo HTTPS and actual two-host/model-backed resources; execute genuine member10min/2GB, fresh4GB, saved2GB upgrade, join/leave/re-report, actual plan/staging/reservations/deadlines, serve/chat/active-cancel/SSE/status/stats and clean teardown. Complete direct F3/F5/F14/F15/F15b plus lifecycle compositions on accepted Python, then unchanged Rust assertions; measure full real-default <=5min target. Refusals/model helpers are not substitutes.
6. **L2 + L5/L6/L8** (L2.6, L2.8): Run the same behavioral failure/cleanup corpus over actual transport and worker adapters, replace incident model SUT seams with production transitions and kill the same faults. Current default/component tests do not qualify real Iroh/Metal or the full supported native model matrix.
7. **L10 + L12 + integrator** (L4.1, EXIT): Repair inherited L10 formatting in crates/api/tests/event_validation.rs. Repair 12 inherited L12 TM002 calls: keeper.rs lines137,144,153,211,229,245,256,262,276 and native_session.rs lines427,459,483, with typed registry/fake-policy seams, not suppressions. Then rerun fmt, strict Clippy, full workspace tests and lint on the next preview. Also L3/L12: decide whether journeys `process.rs` host_busy_marker should honour the same DEVICE.busy `/device-calib/` exemption the resume guard grants (journey-tests process_safety fails while the marker exists); do not weaken the guard without that owner ruling.
8. **L4/L12 + L8/L11 + integrator** (L4.2, L4.5, L4.7): Agree/enforce content-authenticated manifest setup and reconcile descriptive preflight commands with real journey/Metal invocations before scheduling. Complete safe product diagnostics and per-change/transitive execution automation. Bind candidate executable/resources/toolchain to relocation/minimum-OS/install-upgrade-rollback/native-IO receipts. Round2 discovery RUN and compile-only Metal check are not package/physical acceptance.

## 2026-10-28 — round3 package disposition gates (guarded, HEAD 6e3ae8d6 / rerun edee89e6)

All commands via R3/cargo-guard.rb (busy-marker policy with DEVICE.busy `/device-calib/` exemption, parent-cert hold, disk>=12GiB, pressure1, nice19, offline/locked, j2). Receipts `R3/final-*.json`. Product/test source unchanged between 6e3ae8d and edee89e.

| Command | Actual exit/result | R3 evidence |
|---|---|---|
| fmt -p obligations --check | 0 | final-focused-fmt |
| test -p obligations --no-fail-fast | 0; 45 PASS (7+20+2+16) | final-focused-tests |
| run -p obligations --example package_gate | 0; 0 unexplained paths (1,552/182/1,370/2,631/431) | final-package-gate |
| clippy --workspace --all-targets -- -D warnings | 0 | final-clippy |
| fmt --all --check | 1; event_validation.rs only (L10) | final-workspace-fmt |
| run -p xtask -- lint | 1; 518 sources, 12 L12 TM002 | final-lint |
| test --workspace --no-fail-fast | 101; 952 pass, 2 fail (L1 inventory_bindings:49; L3 process_safety host_busy_marker), 1 ignored | final-workspace-tests |
| test -p journey-tests --test process_safety (rerun) | 101; same host_busy_marker while DEVICE.busy exists | final-process-safety-rerun |

## 2026-10-27 — round2 committed evidence and direct gates

No-ff merge: `14aa6a2`; genuine manifest/census increment: `0b26d0b`. Author/committer Tranquil-Flow, no trailers. All Cargo commands used R2/cargo-guard.rb: read-only busy/parent-cert admission polling120s, free disk>=12GiB, pressure1, nice19, offline/locked, j2, retained private target. Default suite is feature-off; Metal check compiles/verifies input pins but does not execute native tests or use the GPU. Source code remained unchanged through data commit. No native/oracle app was launched.

| Guarded command (Cargo --offline --locked -j2 where applicable) | Actual exit/result | R2 evidence |
|---|---|---|
| fmt --all --check | 1; inherited L10 event_validation.rs formatting | fmt.log/json |
| clippy --workspace --all-targets -- -D warnings | 0; PASS | clippy.log/json |
| test --workspace --no-fail-fast | 101; 928 unit/integration + 3 doctests pass; 1 failure, 1 existing ignore | tests.log/json, gate-summary.json |
| run -p xtask -- lint | 1; 514 sources / 12 inherited L12 literal violations | lint.log/json |
| run -p budget-model --example inventory_gate | 1; 1337 closure violations | inventory.log/json |
| run -p xtask -- preflight, unset inputs | 1; native/oracle selections BLOCKED | preflight-before.log/json |
| same preflight, genuine env manifests | 0; all six selections RUN (not tests PASS) | preflight-final.log/json, D/preflight-readiness.json |
| SIM_MODEL=historical test -p sim --tests section7_ --no-fail-fast -- --nocapture | 101; exactly10 intended assertion panics | historical-model-red.log/json |
| same section7 selection, corrected | 0; same10 tests PASS | corrected-controls.log/json |
| check -p backend-mlx --all-targets --features metal | 0; actual selected inputs accepted by build.rs, compile-only | native-input-check.log/json |
| external read-only hash/census verifier | 0; 96 referenced inputs; corrupt digest rejected; exact ZIP census | content-verification.json |

_RU-053 note: the `inventory_gate` row above is a result recorded when that example was a gate (owner-approved 2026-10-31). It is a report now and exits 0; the row is kept as recorded._

The full-suite failure is `binding_categories_reference_real_rows_without_turning_defaults_into_values`, `test-support/budget_model/tests/inventory_bindings.rs:49`: actual category set includes `reviewed-composed-ceiling`, frozen expected set does not. Same failure is independently present in preview3 `tests-counts.json`; no test was loosened. The existing ignored test is `artifacts::real_cached_qwen2_offline_reopen_keeps_verified_handle`, requiring a separately admitted cached model. It is not counted as PASS.

Preflight's first post-manifest attempt failed exit101 because the initial data directory matched workspace `test-support/*` and had no Cargo.toml. Moving only the data to D/ fixed this; root workspace/lock were untouched. Failed attempt is retained in preflight-after.log/json. No empty/synthetic evidence file was used. `env_file` currently has **no content parser**; `oracle-target.json` remains the strict four-field runner target while richer provenance is the preflight manifest. L12 schema/authentication/command-setup seam remains open; no L12 crate change.

D/package-reconciliation.json enumerates every unresolved path, including packaged dependencies, metadata, symlinks and opaque executable/archive containers. There is **no zero-unexplained claim**, no blanket third-party retirement and no claim that path equality proves final contract/A6 closure. All source comparison used read-only ZIP bytes and git objects at f4e366e9. The certified package and published ZIP were not executed.

## Archived round1 evidence (historical; current states above supersede)

### Mandatory incident evidence (§7)


All rows are faithful Rust MODEL executions, not modified historical Python. `FX/historical-model-red.log` has actual assertion panics; `FX/corrected-controls.log` reruns the same names without `SIM_MODEL=historical`. `test-support/sim/traces/<ID>-v2.json` and `test-support/sim/evidence/l2c-trace-migration.json` retain their original build/profile identities from L2C commit `2ee2ae0d1b88ad7cbcc22992867b838bc6b9b0c3`; no golden re-recording in this lane.

| ID | Historical source/fault basis | Exact test suffix (prefix `section7_`, suffix `_authorized_progress_contract`) | Result |
|---|---|---|---|
| F1 | ff8299ac before a015f2eb; stale pin/no renewal | f1 | MET MODEL RED/GREEN |
| F2 | ff8299ac before f8061466; unbounded retention | f2 | MET MODEL RED/GREEN |
| F3 | ff8299ac before c8aa0528; missing capability re-report | f3 | MET MODEL RED/GREEN; journey BLOCKED |
| F4 | ff8299ac; expiry-driven rollover | f4 | MET MODEL RED/GREEN |
| F5 | ff8299ac before68119358; lock across slow work | f5 | MET MODEL RED/GREEN; journey BLOCKED |
| F13 | ce4ca16df2c9f495043f6e8030229a88fa1ba141; quick relay expires | f13 | MET MODEL RED/GREEN |
| F14 | d42e4100^; storage-blind placement | f14 | MET MODEL RED/GREEN; public placement BLOCKED |
| F15 | 57e8d356^; owner-only rollover | f15 | MET MODEL RED/GREEN; member deadline BLOCKED |
| F15b | 076ffaa4; owner gets unknown-member60s | f15b | MET MODEL RED/GREEN; owner worker BLOCKED |
| F16 | 52d1b69f;30s stop vs ~33s close and swallowed failure | f16 | MET MODEL RED/GREEN; ~33s is NOT p99 |

## 2026-10-27 — recorded profiles and released external replay

Commits: checklist `cc44da312ab66296216459184aee11a937778b54`; importer `1df0e29ab1e56619d7cea30c733ea0300d49e2b4`; released-oracle replay/pressure gate `e80e8a96ef508f093fa644d517356fde55e61e38`; importer hardening `87596050600c85a9a23d6538a3ff02b21066ae47`. Identity is Tranquil-Flow, no trailers.

Importer: `latency-profiles --device-v2 SAVED_DEVICE_ROOT NEW_OUTPUT.json`; `FX/device-v2-final.json` is schema2 / `recorded-device-v2.2`. It reads only selected recorded files, deduplicates event/stdout/summary copies and repeated run/rollover observations, hashes exact bytes, and emits no unknown input fields or raw failure payloads. All 134 hashes were independently rechecked. Missing expected filenames:106; invalid historical Quit JSON:3, explicitly blocked. These are dispositions, not fabricated samples. Source SHA/macOS/model/host role/load are retained where recorded; unknown hardware/checkpoint identity stays null. V1 API/data/traces remain unchanged; V2 is NOT automatically admitted to L1 or simulator calibration.

| Recorded phase | Raw samples across separate cohorts | p99 |
|---|---:|---|
| compile | 23 | BLOCKED |
| publish / start | 19 / 17 | BLOCKED; polling-censored |
| stop / native close | 5 / 0 | BLOCKED; stop is not native-close proof |
| relay RTT | 0 | BLOCKED; status latency is not relay RTT |
| TTFT / decode tokens per second | 78 / 78 | BLOCKED |
| rollover / restart | 4 / 5 | BLOCKED; observed transitions |
| Quit | 25 | BLOCKED; AppleEvent-to-observed-exit |

Counts across attempts are NOT pooled into comparable tail trials. Descriptive nearest-rank p50 exists for exact recorded metrics with at least two samples in a cohort. Repeated observations, missing provenance and unsupported independence never manufacture p99. Synthetic 2×/3× millisecond variants are explicitly not measurements. Cleanup-unproven stops remain failed outcomes; malformed Quit receipts retain only safe disposition/digest metadata. TDD logs include actual initial importer, memory-admission and four later hardening assertion REDs, then GREEN. No source bytes were modified.

External target: read-only certified local package at `E/../l2-final-f4e366e98a1f705dfaf182897044500d290dbd7f/package/out/0.1.0-preview.2/Mycelium A13 Demo.app/Contents/MacOS/Mycelium A13 Demo`; actual absolute launch path is in `FX/target.json`. Source `f4e366e98a1f705dfaf182897044500d290dbd7f`, executable SHA256 `8fff41823d357949ffa55c00b3fb8dfb0b4cc541f66343a72f21e3d8dd89186f`. Historical golden identities remain ef9ae61d/bc06dc66, independently validated rather than rewritten to claim new captures. The released target matches their unchanged behavioral assertions. Superseded launch identities still refuse.

All Cargo commands used the external `v02-fx-guard.sh`: demo busy/parent-cert polling every120s, disk≥12GiB, memory pressure1, nice19, offline/locked, j3, retained `~/.hermes/cache/scratch/v02-fx-target`. Exact argv appear in logs. No installed app/state, public demo, ports8787/4385, cloudflared, MacBook or oracle worktree was modified or launched. No Python added to repository/test/release paths. Cleanup remains available if memory admission subsequently refuses.

| Command after guard (Cargo flags `--offline --locked -j 3`) | Actual outcome | FX log/receipt |
|---|---|---|
| `test -p budgets -p budget-model -p sim -p sim-adapters -p latency-profiles -p journey-tests -p obligations -p xtask --no-fail-fast` | exit0;287 PASS before four added hardening tests | `foundation-tests.log` |
| `test -p latency-profiles` after hardening | exit0;15 PASS;291 distinct combined target/test IDs | `importer-hardening-green.log` |
| `test -p latency-profiles --features serde_json/arbitrary_precision` | exit0;15 PASS | `profiles-precision.log` |
| `test -p journey-tests` | exit0;24 PASS | `journeys-tests-green.log` |
| `SIM_MODEL=historical ... test -p sim --tests section7_ --no-fail-fast -- --nocapture` | exit101; exactly10 intended assertion failures | `historical-model-red.log` |
| Same section7 selection corrected | exit0;10 PASS | `corrected-controls.log` |
| `fmt --all --check` | exit0 | `fmt-final.log` |
| foundation-scope `clippy --all-targets -- -D warnings`; final profile Clippy | exit0, exit0 | `foundation-clippy.log`, `profiles-clippy-2.log` |
| `clippy --workspace --all-targets -- -D warnings` | exit101; inherited L6 type_complexity | `workspace-clippy.log` |
| `run -p xtask -- lint` | exit0;zero violations | `lint-final.log` |
| `run -p budget-model --example inventory_gate` | exit1;1,845 closure violations | `inventory-gate.log` |
| `run -p xtask -- preflight` | exit1;mandatory inputs/path BLOCKED | `preflight.log` |
| `run -p latency-profiles -- --device-v2 <saved device root> <new output>` | exit0;1032 profiles,240 source dispositions,p990 | `import-final.log`, `device-v2-final.json` |
| `run -p journey-tests -- --target <target.json> --receipt <new receipt> --golden <unchanged golden>`; second adds `--scenario loopback-boundaries` | both exit0, PASS;20+33 observations | `baseline-released.{log,json}`, `boundaries-released.{log,json}` |

_RU-053 note: the `inventory_gate` row above is a result recorded when that example was a gate (owner-approved 2026-10-31). It is a report now and exits 0; the row is kept as recorded._
