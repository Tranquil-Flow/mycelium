# A4 requalification at HEAD 7d0ffb6 — 2026-09-20

**Gate:** A4 — concurrent dispatch and scoped liveness
**Requalification worktree:** `/private/tmp/a4-requal-20260920` (branch `requal/a4-20260920`)
**Base sha = head sha:** `7d0ffb6f52b24bc00353aeed170a29acdabc49e2`
**Frozen tree:** `/Users/evinova-self/Documents/playground/mycelium-core-pilot` (read-only here; `git status --porcelain` empty before and after this run)
**What this document is:** a re-measurement and re-binding audit of the recorded `complete` claim. Nothing was fixed, edited, re-labelled or re-bound. The record, the register and the spec are byte-identical to `7d0ffb6`.

Artifacts read:

- `docs/handover/a4-completion-record.v1.json` (2154 B) — the recorded `complete` claim.
- `docs/handover/a4-product-concurrency-liveness-qualification.json` (11848 B) — the sealed evidence the record points at.
- `docs/handover/mycelium-completion-checklist.v1.json` — the A4 register row (state `complete`, 13/13 `completed_requirements`, `blockers: []`).
- `mycelium-core-plan-review-20260915/{PROGRESS_MATRIX.md,REVIEW.md,FRESH_CHECKS.md}` — the independent review.
- Retained run artifacts outside the repo: `~/mycelium-physical-run/a4-concurrency-a4closure-20260820/evidence/`, `~/mycelium-a4-concurrency-node0-v5|v6/`, `~/mycelium-physical-run/a2-physical-gen7-20260814/`.

The review's A4 verdict is the input hypothesis: PROGRESS_MATRIX row A4 — integrated (`599e393` is an ancestor), "hist ✔; qualification JSON hash matches [R] audit", and the residual item **"High retained; requalification needed after A5/A6/A13 lifecycle edits"**. `FRESH_CHECKS.md` carries no A4 item.

---

## 1. Per-binding table (the core deliverable)

Every binding the record carries, recomputed at `7d0ffb6`. "Live-derived" means the sealer
(`scripts/run_a4_qualification_gate.py`) computes that value from an HTTP call to a **running** route
serve (`/__mycelium/live-status`, `/__mycelium/runtime/admission-status`), so it cannot be recomputed
from the tree at all — only from a live session or a retained snapshot of one.

| # | binding (where) | sealed value | value at / from `7d0ffb6` | verdict |
|---|---|---|---|---|
| B1 | `bindings.contract_digest` | `sha256:adfed097…86e6d` | `sha256:e635436f…c3a6f` (fixture bytes at HEAD) | **MISMATCH — stale** |
| B2 | `bindings.model_digest` | `sha256:2729a427…6d305` | `sha256:2729a427…6d305` (recomputed from the retained model manifest) | **MATCH** |
| B3 | `bindings.representation_digest` | `sha256:7a46fda9…81cda` | `sha256:7a46fda9…81cda` (retained representation decision, protocol v1) | **MATCH** |
| B4 | `bindings.environment_digest` | `sha256:40426e7d…b5f5` | live-derived (platform/python/peers/route_alive/fatal/topology/decode_mode) | **NOT RECOMPUTABLE OFFLINE** — internally consistent |
| B5 | `bindings.runtime_digest` | `sha256:e67321fa…302ee` | live-derived (frozen runtime doc from admission-status) | **NOT RECOMPUTABLE OFFLINE** — internally consistent |
| B6 | `bindings.authority_generation` | `1787317421072` | live: `qualification.issued_at_unix_ms` | **NOT RECOMPUTABLE OFFLINE** — internally consistent |
| B7 | `sealed_evidence.digest` (record → evidence file) | `sha256:022bc93d…4968d` | `sha256:022bc93d…4968d` | **MATCH** |
| B8 | record artifact digest (register, 10 of 13 requirement rows) | `sha256:1423354a…5a28bd` | `sha256:1423354a…5a28bd`; every copy on disk agrees | **MATCH** |
| B9 | `evidence_digest` (self-seal of the evidence document) | `sha256:b2a32483…123be8` | `sha256:b2a32483…123be8` | **MATCH** |
| B10 | `specification.digest` | `sha256:e4a0037d…8d32df` | `sha256:e4a0037d…8d32df` | **MATCH** |
| B11 | `atomic_commit` `599e393…` | — | `git merge-base --is-ancestor` → exit 0 (and it is the commit that added the record) | **MATCH** |
| B12 | `evidence.source.repository_commit` `487a1b4…` | — | `git merge-base --is-ancestor` → exit 0 | **MATCH** |
| B13 | `selected_deployment_qualification.resolved_commit` `7ae5576…` | — | not an object in this repository (model revision, not a repo commit) | **N/A — out of repo scope** |
| B14 | embedded contract bound `installed_a4_qualification.cancellation_and_cleanup_bound_ms` | `2000` | current registry value `8000`; current validator **rejects** the sealed document | **MISMATCH — stale** |
| B15 | temporal binding `sealed_evidence.fresh_until` (also on 10 register rows) | `2026-08-21T14:06:29.214Z` | now 2026-09-20 → window closed ~30 days ago | **MISMATCH — expired** |

### Literal commands and outputs

```
[B1 contract_digest]
  $ git show 599e393:contracts/compatibility-fixtures/product-concurrency-liveness-qualification-v1.json | shasum -a 256
  adfed0971cabb10b1dd10d37c2444684e69ea7f9e3dc623f06a6aff11d286e6d  (commit 599e393 == record's atomic_commit)
  $ git show 487a1b4:contracts/compatibility-fixtures/product-concurrency-liveness-qualification-v1.json | shasum -a 256
  adfed0971cabb10b1dd10d37c2444684e69ea7f9e3dc623f06a6aff11d286e6d  (487a1b4 == evidence source.repository_commit)
  $ shasum -a 256 contracts/compatibility-fixtures/product-concurrency-liveness-qualification-v1.json   [HEAD 7d0ffb6]
  e635436fed13e9e4789468606b9db670ddf3d6ae68c3dc6cb3d10a2cdbfc3a6f  contracts/compatibility-fixtures/product-concurrency-liveness-qualification-v1.json
  record binds: sha256:adfed0971cabb10b1dd10d37c2444684e69ea7f9e3dc623f06a6aff11d286e6d

[B2 model_digest]
  $ python3 -c 'from model_manifest import manifest_digest_ref; manifest_digest_ref(json.load(open("/Users/evinova-self/mycelium-a4-concurrency-node0-v6/control/model-manifest.json")))'
  sha256:2729a427933ffaf01af4739ef70206023f2e7aa1061c2791283d3356f686d305
  # verify_manifest_digest(manifest) -> True in both retained A4 run roots (node0-v5, node0-v6)
  record binds: sha256:2729a427933ffaf01af4739ef70206023f2e7aa1061c2791283d3356f686d305

[B3 representation_digest]
  $ python3 -c 'print(json.load(open("/Users/evinova-self/mycelium-physical-run/a2-physical-gen7-20260814/model-representation-decision-existing.json"))["representation_digest"])'
  sha256:7a46fda9efd7811523e5ea9e7d03157e74cf7c48df8062a6b75db45f18281cda   protocol=mycelium.model_representation_decision.v1 model_id=Qwen/Qwen2.5-0.5B-Instruct
  record binds: sha256:7a46fda9efd7811523e5ea9e7d03157e74cf7c48df8062a6b75db45f18281cda

[B4 environment_digest] live-derived from GET /__mycelium/live-status by evaluate()
  fields: platform, python, peers[{node_id,architecture,decode_mode}], route_alive, fatal, topology_version, decode_mode
  record binds: sha256:40426e7d59c4c9a6ad3ee9071b5c48e87f4a412c889a844242eae9a7d369b5f5
  sealed evidence carries the same value: sha256:40426e7d59c4c9a6ad3ee9071b5c48e87f4a412c889a844242eae9a7d369b5f5

[B5 runtime_digest] live-derived from GET /__mycelium/runtime/admission-status (frozen runtime doc)
  record binds: sha256:e67321fa924a3444fa520ef13eea300ef29b1f1872c720fe4be3bda9629302ee
  reconstruction attempt from the only retained admission-status on disk (a different deployment): sha256:b0601bc6f30080d63903d589751c856915898923444f0f1e29a1359f41510760
  (that snapshot's deployment_id=72868ce5-02e9-40ed-aff1-40eee0bd5f39 != A4 route e1d1a2fa-f68d-5b38-a507-34b367c8d855)

[B6 authority_generation] live: route qualification issued_at_unix_ms
  record binds: 1787317421072
  nearest retained route qualification for the same deployment (a4-concurrency-20260818/qualifications/live-v61.json): issued_at_unix_ms=1787159781973

[B7 sealed_evidence.digest]
  $ shasum -a 256 docs/handover/a4-product-concurrency-liveness-qualification.json
  022bc93d512e012feb0541873e397e184129a10cdaf61689dda6f04d1234968d  docs/handover/a4-product-concurrency-liveness-qualification.json
  record binds: sha256:022bc93d512e012feb0541873e397e184129a10cdaf61689dda6f04d1234968d

[B8 record artifact digest]
  $ shasum -a 256 docs/handover/a4-completion-record.v1.json
  1423354a43a4d85680a7c9a319649709d9c42c2f8a8856311deafb52785a28bd  docs/handover/a4-completion-record.v1.json
  register binds: sha256:1423354a43a4d85680a7c9a319649709d9c42c2f8a8856311deafb52785a28bd (10 of 13 requirement rows)
  # sweep: mdfind "kMDItemFSName == 'a4-completion-record.v1.json'" -> 30+ copies on disk, all 2154 B, all 1423354a…

[B9 evidence_digest self-consistency]
  $ python3 -c 'canonical_json(doc minus evidence_digest) -> sha256'
  sha256:b2a324831929eae1f6c41d3d8b053e47b63748b78c3892d26f158de7ec123be8
  document declares: sha256:b2a324831929eae1f6c41d3d8b053e47b63748b78c3892d26f158de7ec123be8

[B10 specification.digest]
  $ shasum -a 256 docs/superpowers/specs/2026-08-17-mycelium-a4-product-concurrency-liveness.md
  e4a0037d01b810e53e84df488a0b94a7577e8ca58e18e6c8be6b4863ab8d32df  docs/superpowers/specs/2026-08-17-mycelium-a4-product-concurrency-liveness.md
  document declares: sha256:e4a0037d01b810e53e84df488a0b94a7577e8ca58e18e6c8be6b4863ab8d32df

[B11/B12 commit bindings]
  $ git merge-base --is-ancestor 599e3936db48d8b167a65e53dcdc94db2b4bc20c HEAD   # record.atomic_commit
  exit=0 ANCESTOR-OF-HEAD
  $ git merge-base --is-ancestor 487a1b43039d61510100afeba2ed48a6253eae61 HEAD   # evidence source.repository_commit
  exit=0 ANCESTOR-OF-HEAD
  $ git cat-file -e 7ae557604adf67be50417f59c2c2f167def9a775^{commit}   # resolved_commit
  exit=128 fatal: Not a valid object name 7ae557604adf67be50417f59c2c2f167def9a775^{commit}

[B14 embedded contract bound]
  $ python3 -c 'print(mycelium_deadlines.COMMAND_INTERRUPTION_AND_CLEANUP_MAX_MS)'
  8000
  sealed installed_a4_qualification.cancellation_and_cleanup_bound_ms = 2000
  $ validate_product_qualification(sealed doc) -> ValueError: invalid_product_concurrency_liveness_qualification
  $ validate_product_qualification(same doc, bound patched to the current registry value) -> ACCEPTED

[B15 freshness]
  sealed_evidence.observed_at = 2026-08-21T13:06:29.214Z
  sealed_evidence.fresh_until = 2026-08-21T14:06:29.214Z
  register A4 rows carrying fresh_until: ['2026-08-21T14:06:29.214Z']
```

### Binding notes

- **B1** is the load-bearing mismatch. The sealed `contract_digest` is exactly the bytes of the A4
  contract fixture at the seal commit `599e393` and at the evidence's `487a1b4`; at HEAD the same path
  hashes to `e635436f…`. Cause: `555945d` (2026-09-19) `fix(protocol): carry the re-derived cleanup budget
  through the node protocol and contracts`.
- **B14** shows the B1 drift is *semantic*, not cosmetic: the A4 contract's cleanup bound moved
  `2_000` → `mycelium_deadlines.COMMAND_INTERRUPTION_AND_CLEANUP_MAX_MS` (= `8000`), and the current
  validator now **refuses** the sealed qualification document verbatim (`ValueError:
  invalid_product_concurrency_liveness_qualification`); patching only the bound to the current registry
  value makes it acceptable. The `2000` values in the record's `dispatcher_and_locks` and
  `installed_a4_qualification` are therefore bindings to a superseded contract revision.
- **B4–B6** cannot be re-measured at all: the A4 route serve (`deployment_id
  e1d1a2fa-f68d-5b38-a507-34b367c8d855`) no longer exists, and no retained snapshot of that deployment's
  `live-status` / `admission-status` / route qualification was found on this host. The one retained
  admission-status snapshot on disk belongs to a different deployment (`72868ce5-…`), so reconstructing
  `runtime_digest` from it yields a different digest — as it must. These three are therefore
  *internally consistent* (the record and the sealed evidence agree) but **unverifiable at HEAD**.
- **B13** is a route-model revision, not a repo commit; it is correctly *not* a git-ancestry binding and
  is recorded as N/A rather than a mismatch.
- **B7–B12** establish that the record and its evidence are intact, unedited, and integrated: the
  evidence document re-seals to its own `evidence_digest`, the specification bytes never moved, and both
  the seal commit and the evidence source commit are ancestors of HEAD.

---

## 2. Acceptance suites re-run at `7d0ffb6`

Exact toolchain (env scrubbed, bytecode off, cache off), per file, run in
`/private/tmp/a4-requal-20260920`:

```
env -u PYTHONPATH -u PYTHONHOME -u VIRTUAL_ENV PYTHONDONTWRITEBYTECODE=1 \
  /opt/homebrew/bin/python3 -B -m pytest -q -p no:cacheprovider <path>
```

| suite (A4's own + the A4 families) | literal result | exit |
|---|---|---|
| `tests/a4_acceptance` | `30 passed in 0.29s` | 0 |
| `tests/live/test_a4_commands.py` | `10 passed in 0.04s` | 0 |
| `tests/live/test_a4_contracts.py` | `8 passed in 0.02s` | 0 |
| `tests/live/test_a4_ownership.py` | `37 passed in 3.98s` | 0 |
| `tests/live/test_a4_runtime_interruption.py` | 17 collected; rerun alone: `17 passed in 341.55s (0:05:41)` (first attempt hit my own 420 s per-file cap mid-run on the MLX path, 13/17 then killed — no failure) | 0 |
| `tests/live/test_a4_liveness.py` | `20 passed in 0.12s` | 0 |
| `tests/live/test_a4_concurrency.py` | `21 passed in 1.64s` | 0 |
| `tests/request_gateway/test_a4_abandoned_subscriber.py` | `6 passed in 5.25s` | 0 |
| `tests/request_gateway/test_a4_cancellation.py` | `8 passed in 0.62s` | 0 |
| `tests/request_gateway/test_a4_reconnect.py` | `2 passed in 0.46s` | 0 |
| `tests/request_gateway/test_a4_silent_subscriber.py` | `1 passed in 0.71s` | 0 |
| `tests/evidence/a4` | `1 passed in 0.02s` | 0 |

**Zero failures, zero errors** across every suite that completed. `tests/a13_macos` was deliberately
not run (owned by another child on this host).

**Not re-run, and why:**

- The record's *whole-suite* deterministic step (`deterministic.command` = `python3.14 -m pytest -q`,
  recorded `4325 passed / 13 skipped / 121 subtests / 304.49 s`) — a full-repo run on a host already
  carrying three other heavy suites; the A4 families above are the A4 surface.
- The record's frontend regression step (`cd ui/web && npm run check`) — `ui/web/node_modules` is absent
  in this worktree, so it is not runnable offline here.

---

## 3. A4's qualification gate — attempted, could not run, wrote nothing

`scripts/run_a4_qualification_gate.py --help` shows the gate has **no read-only/dry-run mode**;
`--output` and `--completion-record` are both required, so any invocation writes a gate document and a
completion record. It also requires a live route serve (`--base-url`, default `http://127.0.0.1:8791`)
plus 11 executed-evidence files.

I ran it with the retained evidence set from
`~/mycelium-physical-run/a4-concurrency-a4closure-20260820/evidence/`, the retained representation
decision, and **scratch output paths outside the repo**:
`--output /private/tmp/a4-scratch/gate-output.json --completion-record /private/tmp/a4-scratch/completion-record.json`.

Literal result:

```
  File ".../scripts/run_a4_product_gate.py", line 68, in request
    raise GateError(f"http_{method.lower()}_unavailable") from error
scripts.run_a4_product_gate.GateError: http_get_unavailable
EXIT=1
```

The gate consumed the retained inputs through `evaluate()` (all evidence-document protocol checks
passed) and failed exactly where it needs the live route: `_collect_live()` → `GET
/api/v1/bootstrap`. **Nothing was written**: `/private/tmp/a4-scratch` is empty, and the record in both
the frozen tree and this worktree still hashes to `sha256:1423354a…` (B8). So: the gate **cannot be
re-run offline**, and re-running it as designed would require the physical fleet *and* would overwrite
the record — which I did not do.

---

## 4. What changed under A4's surface since the record was written

`git log --oneline 599e393..HEAD -- <A4 paths>` → **33 commits**, 2026-08-26 … 2026-09-20 (HEAD itself is
`925a73d`'s descendant set; 155 commits separate the seal commit from HEAD in total).

```
925a73d 2026-09-20 fix(a13): retry the boot-id collection inside one bounded budget, and prefer the live hello at the builder's substitution site
90381bc 2026-09-19 feat(live-route): a withheld terminal now records its CAUSE, not only its refusal
15af3c8 2026-09-19 feat(live-route): persist which terminal was withheld, and why, so the next round reads one file
c5ee01a 2026-09-19 chore(contracts): refresh the manifest owner-source pins for routeStatus.ts and iroh.py
555945d 2026-09-19 fix(protocol): carry the re-derived cleanup budget through the node protocol and contracts
185b17f 2026-09-19 fix(cleanup): mint the owner's cancellation window from the registry
db80812 2026-09-19 fix(deadlines): re-derive the cleanup budget from the MEASURED two-Mac cleanup latency
8824f63 2026-09-19 fix(contracts): refresh the service.py owner-source pins after the terminal change
1704ac2 2026-09-19 fix(gateway): publish a failure-to-prove terminal for a blocked request
07408a3 2026-09-16 Retry retryable reconnect window rejections; make seed time rejections self-describing
0f0d796 2026-09-16 chore(contracts): refresh the supervisor owner-source pin
5b76cb4 2026-09-16 fix(registry): record why a deployment is unavailable, and emit the stage diagnostic
bbc8871 2026-09-16 chore(contracts): refresh the build_qwen_live_route owner-source pin
617b055 2026-09-16 fix(route): wire the peer cleanup-bound refusal, retire a stale binding, and hold the poll for an expired ack
1a022c9 2026-09-16 integration: fan in the A13 macOS demo lane
e72d2b0 2026-09-16 feat(a13): the macOS demo adapter, its execution-path fixes and the C4/C5 terminal
827b2ad 2026-09-16 refactor(deadlines): bind the remaining consumers to the registry
0ed89fc 2026-09-16 fix(deadlines): derive the advertised cleanup bound from the route fanout window
ae20480 2026-09-15 feat(iroh): bound a declared peer connect and publish a live peer address
35f6f95 2026-09-15 feat: single-source request-path deadlines and assert their composition
435b857 2026-09-15 integration: fan in A14a route explorer candidate
aa87470 2026-09-08 chore(a5): refresh repaired contract owner-source pins
f35ab91 2026-09-08 Build A14a production route explorer and local browser conformance
4c64ae2 2026-09-08 feat(a5): attribute applied runtime work and KV to exact placements
217033e 2026-09-04 fix: preserve cancellation client pool capacity
0281498 2026-09-04 fix: prevent cancellation client starvation
e522832 2026-09-04 fix: retry production sidecar unknown-message races
b54ebac 2026-09-03 fix: release obsolete cancellation clients
49c2d3f 2026-09-03 fix: align cancellation fanout budget
81e71ed 2026-09-03 fix: preserve cancellation receipt seal budget
c17f523 2026-09-03 fix: retry asynchronous cancellation cleanup
0a83a2d 2026-09-03 integration: fan in A5 candidate
efcdf44 2026-08-26 integration: fan in completed A8 gate
```

Commits that touch **A4's subject matter** (concurrency / cancellation / cleanup bounds / scoped
liveness / representation authority), i.e. the ones a requalification has to answer for:

1. **The cleanup-bound registry replaces the sealed 2000 ms constant.** `mycelium_deadlines.py` **did not
   exist at `599e393`** (`git ls-tree 599e393 mycelium_deadlines.py` → empty). It arrives at `35f6f95`
   ("single-source request-path deadlines"), the bound is then *re-derived from a measured two-Mac
   cleanup latency* (`db80812`), derived again from the route fanout window (`0ed89fc`), bound into the
   remaining consumers (`827b2ad`, `185b17f`), and carried into the node protocol **and the contracts**
   (`555945d`) — which is the commit that moved the A4 contract fixture and hence `contract_digest`.
   `mycelium_live/a4_contracts.py` now validates against the registry instead of the literal `2_000`.
2. **Cancellation / cleanup liveness hardening:** `49c2d3f` (fanout budget), `81e71ed` (receipt seal
   budget), `c17f523` (retry async cleanup), `b54ebac` (release obsolete clients), `0281498` /
   `217033e` (cancellation client starvation and pool capacity), `e522832` (sidecar races).
3. **Route/registry/live-route lifecycle:** `617b055` (peer cleanup-bound refusal, retire a stale
   binding, hold the poll for an expired ack), `5b76cb4` (record why a deployment is unavailable),
   `15af3c8` / `90381bc` (persist and explain a withheld terminal), `1704ac2` (failure-to-prove
   terminal), `07408a3` (retry retryable reconnect-window rejections).
4. **Transport:** `ae20480` (bound a declared peer connect) — a peer-liveness path.
5. **A13 lane fan-in:** `e72d2b0`, `1a022c9`, then `925a73d` at HEAD.
6. **Contract-pin churn:** `8824f63`, `0f0d796`, `bbc8871`, `c5ee01a`, `aa87470`, plus `f35ab91` /
   `435b857` (A14a) and `efcdf44` / `0a83a2d` / `4c64ae2` (A8/A5 fan-in).

Unchanged: the A4 **specification** (`e4a0037d…`, B10) and the A4 acceptance/`live` test files' own
behaviour (all green, §2).

---

## 5. Verdict

### (b) Still true in substance, but the record's bindings are stale.

**Stale bindings (named):**

| binding | stale because |
|---|---|
| `bindings.contract_digest` = `adfed097…` | the bound fixture bytes at HEAD are `e635436f…` (B1). |
| embedded contract revision `cancellation_and_cleanup_bound_ms = 2000` (record's `dispatcher_and_locks`; evidence's `installed_a4_qualification`; every `physical` check derived from it) | the current A4 contract requires `8000`, and the current validator **rejects the sealed document** verbatim (B14). |
| `bindings.environment_digest`, `bindings.runtime_digest`, `bindings.authority_generation` | live-session bindings; the named deployment's serve is gone and no retained snapshot of it exists, so they cannot be re-confirmed at HEAD (B4–B6). |
| `sealed_evidence.fresh_until` = `2026-08-21T14:06:29.214Z` (repeated on 10 register rows) | the freshness window closed ~30 days ago (B15). |

**Still standing (checked, not assumed):** the record and its sealed evidence are byte-intact
(`1423354a…`, `022bc93d…`), the evidence self-seals (`b2a32483…`), the specification is unchanged
(`e4a0037d…`), the seal commit `599e393` and the evidence source commit `487a1b4` are both ancestors of
HEAD, and **every A4 acceptance suite that ran at HEAD is green** (`30 + 10 + 8 + 37 + 17 + 20 + 21 + 6 + 8 + 2 + 1 = 160` passed, 0 failed).

**Why this is (b) and not (c):** the disqualifying evidence is *drift of the bound artifact*, not an
observed behavioural failure. Nothing I ran at `7d0ffb6` failed: the A4 suites are green, the evidence
document is intact and self-consistent, and the recorded physical observations (≈108 ms cancellation
latency, ≤0.58 ms disconnect visibility, 60 accepted / 365 rejected with 0 leaks) are all still *inside*
the widened 8000 ms budget. What has moved is what the record is bound to — the contract bytes, the bound
value the contract now demands, and the freshness/authority window. A `complete` claim bound to a digest
that no longer exists at HEAD is exactly the staleness class this program keeps paying for, and the
record's own `claim_boundary` disclaims "current route qualification, authority freshness": so the honest
status is **requalification owed**, not "still true, bindings match" (a) and not "no longer
demonstrated" (c).

**What requalification would require (not performed here):** a fresh physical/live A4 run at the current
contract revision, re-sealing the evidence and re-binding every value — i.e. re-measuring and re-binding,
never re-labelling. I edited nothing: the record, the register and the spec are untouched; `git status`
in both trees is clean.

---

## 6. What this does NOT prove

- **No physical fleet run.** No node was provisioned, no route serve was started, no request was
  dispatched. Nothing here re-executes A4's concurrency/liveness behaviour on hardware; the physical
  observations in the record remain historical.
- **No re-binding was performed.** I recomputed and compared; I did not write a new binding, re-seal the
  evidence, edit the record, edit the register, or touch the spec. A4 is **not** requalified and **not**
  closed by this document.
- **A MATCH on a digest proves the bytes are unchanged, not that the behaviour still holds.** B2/B3/B7–B12
  show that artifacts are intact and integrated; they say nothing about whether the code path they
  describe still produces the recorded result.
- **`MISMATCH` on B1/B14/B15 is a binding verdict, not a behaviour verdict.** It shows the record binds a
  superseded contract revision and an expired window; it does not show that A4's cancellation, cleanup
  or liveness behaviour is broken — the suites in §2 argue the opposite for the code they cover.
- **B4–B6 were not verified at all.** They are live-session bindings; only internal consistency between
  the record and the sealed evidence was checked. The `runtime_digest` reconstruction shown is from a
  *different* deployment's retained snapshot and is reported as a failed reproduction, not as a match.
- **The retired evidence inputs were not re-validated against the current code beyond protocol checks.**
  The gate consumed them up to its live-serve step; I did not re-run the physical gate scripts
  (`run_a4_physical_*`, `run_a4_negative_*`) that produced them.
- **Coverage gaps in §2:** the whole-suite deterministic run (4325 tests) and the frontend
  `npm run check` step were not re-run here (reasons in §2). `tests/a4_runtime_interruption.py` needed a
  second, uncontended attempt (341.55 s alone) because the MLX compute path is starved by the three other
  heavy suites running on this host; its row is the slowest, not the weakest — it ends `17 passed`.
- **Environment:** this audit ran on one macOS host (the same arm64 machine family the A4 run used);
  it says nothing about other platforms, other model revisions, or the A13 macOS lane.
