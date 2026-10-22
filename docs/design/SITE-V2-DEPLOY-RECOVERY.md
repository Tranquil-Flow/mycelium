# Site deployment recovery (2026-10-22)

This is a tooling-only follow-up to `a54e5297` on `lane/rel2-siteux-p3`. No product code, contract, live database, live job, relay, tunnel, demo or keeper change is authorized by this work. The interrupted changes were read and preserved in WIP commit `9303cf51` before continuing.

## Failure mechanism and evidence limits

The preserved launchd history records old directory PID 58421 being signalled at 02:19:15.399; bootstrap was attempted at 02:19:15.404–405 and rejected with daemon error 37, “Operation already in progress.” The old job completed removal at 02:19:15.405. `switch.py` previously bootstrapped immediately after bootout and slept only after bootstrap. A successful bootout is not a synchronous removal barrier.

The original automatic rollback then aborted because it tolerated only the text “Could not find specified service.” Real isolated probes captured absent-job bootout rc 3 with “Boot-out failed: 3: No such process”, and `print` rc 113 with “Could not find service”. Exited and crash-looping registrations themselves were successfully booted out (rc 0); their subsequent absent-job bootouts returned rc 3. A slow-exit probe reproduced immediate bootstrap rc 5 (“Input/output error”). The original production wrapper discarded the precise bootout rc/stderr; these probe results must not be presented as its missing historical receipt.

The candidate directory was not started in the failed promotion. A 502 therefore demonstrated loss of availability, not a cookie leak or opened Profile A route. The old verifier's generic privacy error hid that distinction. The operator command also piped failures through `tail` without `pipefail`, masking failures and skipping its outer rollback branch.

Historical raw evidence is under `~/.hermes/cache/scratch/sitefix-investigation/` and `~/mycelium-hosted-demo/state/site-deploy-a54e5297-20261022/`. The new prepared state directory retains copies of relevant evidence. The final RCA and SHA-specific operator commands are written to `/Users/evinova-self/Documents/mycelium-core-program/runs/v2/overnight-20261022/SITE-DEPLOY-RCA.md`.

## Changed safety behavior

- Wait for observed service removal before bootstrap; reconcile bootout errors with registration state instead of matching a single error string.
- Restore both original configurations independently, including after bootstrap/readiness/HTTP failure, timeout or an already-absent job. Retry boundedly, verify the actual registered working directory and loopback HTTP, and explicitly fail if recovery cannot be established. An OS denial, damaged original, disk failure or forced process kill cannot honestly be promised recoverable.
- Hold a single writer lock. Preserve rc/stdout/stderr in atomic operation receipts. Journal failures abort promotion but cannot short-circuit recovery of either job.
- Authenticate rollback inputs independently of candidate files or installed plist health. Deploy still refuses candidate/source/target drift, missing pins, quarantine and busy leases. Emergency rollback is not blocked by a newly acquired device/certification lease.
- Run loopback HTTP verification inside the promotion transaction. `--live` adds the public verifier to that same recovery transaction. Neither flag authorizes deployment by itself; application still requires `--apply` and separate owner approval.
- Distinguish HTTP availability failures from surface-contract failures. Availability failure means privacy has not been established, not that a privacy breach was observed.
- Prepare only from the exact clean detached release. Keep byte-exact original plists, normalized rollback plists and hashes. Only the two approved WorkingDirectory values and explicit apex root-redirect opt-out can differ for promotion.

## Rehearsal and tests

`rehearse.py` uses unique `now.mycelium.test-*` labels, a private synthetic home, copied plists, separate loopback ports and a fresh DB/WAL byte snapshot. SQLite opens only the copy. Missing databases/schema fail closed; DB/WAL hashes, integrity, row counts and a logical content hash are recorded. The content hash must remain unchanged through the read-only cycles. No model, relay, tunnel or public entry is contacted. Safety assertions remain active under optimized Python. Cleanup attempts every owned label even after a command timeout.

The rehearsal executes original startup → candidate promotion with real HTTP verification → original rollback with real HTTP verification → injected candidate-verification failure with verified automatic rollback. It independently reads back both final runtime bindings and plist bytes, then removes and verifies absence of both test jobs.

The original `a54e5297` candidate passed this real launchd/copied-DB control on resume: 118 KV rows, integrity OK and identical logical content before/after; all four cycles and cleanup passed. This rules out a required PV33/schema/model-whitelist migration for the exercised startup/list/static paths. It is not a test of every possible write or migration, nor a live deployment.

Focused fault coverage includes asynchronous removal, absent jobs, failed bootstrap, HTTP failure, busy rollback, damaged candidate/installed plists, post-bootout journal failure, retained candidate versus original registration, timeout, missing hash pins, sandbox-label refusal and a WAL-only canary. The fake snapshots configuration at bootstrap rather than incorrectly reading the mutable plist on every `print`. The resume RED observed seven concrete failures before fixes; the cleanup helper initially failed as an absent interface and is not claimed as a behavioral RED. The final focused run passed 31 tests. Preserve all raw RED/GREEN receipts externally.

Final qualification is the five requested suites against the exact final detached SHA, using the a54e5297 stage's `full.xml` baseline. Require zero failures/errors, no removed or duplicate case IDs, no new skips and the identical inherited skip. Final receipts live outside the candidate checkout. Generate contract pins, then the A6 manifest with its existing base `53d1222b`, and make the pin commit last. Do not treat the focused tests or control rehearsal as the final full-scope result.

## Future operator procedure

The exact release/stage path and commands belong to the final RCA, after final-head qualification and sandbox rehearsal. Do not use the old stage or apply anything during preparation. Future approved commands use `switch.py deploy --apply --live` and `switch.py rollback --apply --live`, with no output pipeline that can mask exit status. A nonzero deployment result stays nonzero even if automatic rollback succeeds. Inspect its operation receipt for `rollback_verified`; do not infer success from the presence of a rewritten plist.
