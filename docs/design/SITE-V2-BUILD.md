# Site V2: real directory app, phases 0–2

## Scope and delivery

Branch `lane/rel2-siteux-build`, based on `9c6c7260`, with the approved design/prototype cherry-picked from `d31fcaed`.
This is the **real** `mycelium_swarm_directory/web` app, not a deployment of the mock prototype.

This document preserves the phase-0–2 build record. The subsequent, undeployed phases 3–4/PV33 candidate is documented in [SITE-V2-P3.md](SITE-V2-P3.md); its root, retention and model-validation changes supersede the corresponding historical phase boundaries below only in that candidate.

- **Phase 0:** shared `site.css`, light/dark themes, fixed-size loading regions, keyed swarm/chat/owner-request nodes, preserved note and quota inputs, public-list session cache, hidden/focused-tab polling pause and error backoff. The conversation scrolls inside its own container; no `scrollIntoView`. Native key dialog retains the explicit destructive confirmation.
- **Phase 1:** hash routes for Browse, Chat, Contributing (mobile Helping), Owned, detail, create and privacy. Browse has search/filters, an offline fold and four stat cells (Macs, availability, run uptime, browser latency). Detail adds status, last seen and browser-measured answer speed. Sources are labelled; no invented aggregate speed, queue depth or 24-hour uptime. Model options are generated from `CHOICES` and `model_shape`, with exact ID/size/layer equality coverage. Owned shows the real pending-request badge.
- **Phase 2:** in-app gate/composer, first-prompt consent, no access, pending (including renewal persistence), approved, declined, expired, quota, invite-only, offline, warming and owner-stop handling. Composer remains in place, disabled with a reason until usable. Busy sends retry only the explicit `chat_busy` refusal, for at most 30 seconds, and waiting/generating can be stopped. Answers show browser-observed throughput/first-word time and reported serving Macs. Invite-only and pending visitors can copy a private nudge including their full **public** key and the owner's inbox link.

No signed document shape, protocol version, server endpoint, database schema, model execution, Mac label or retention policy changed. The only public allowlist addition is `/swarms/site.css`; both the directory and apex proxy name it explicitly. The existing Profile A closed-route/CSP/cookie tests remain. The client additionally refuses an entry URL equal to the directory origin before transmitting a prompt.

## Deliberate phase boundary

Owner defaults A–K remain the target for the later phases, not claims that those phases shipped here:

- Apex `/` **still redirects**. The explainer/removal is phase 4 and should be coordinated in the same release wave per A, not replaced by a blank root or by enabling product/inference routes.
- Model choices are registry-generated in the UI. **Server-side registered-model enforcement is still phase 7**; the existing record validator is unchanged under the no-protocol-change scope.
- The **24-hour decline cooldown, 5-second held-write Undo, grant-by-link, complete issued-grant inbox/revoke endpoint, and app-header inbox count are not implemented** in this phase-0–2 lane. Existing approve/decline writes remain immediate. The UI does not claim a server-enforced cooldown or an Undo it cannot perform.
- No helper download is invented; Contributing says it is not published yet. No claim-to-Mac association, directory expiry, heartbeat v2 or swarm-average speed is fabricated.
- Standalone entry `/chat` is unchanged. A screen-reader/real-user study and Safari/Firefox qualification remain unverified.

## Verification

The test harness runs the shipped directory HTTP server, real browser WebCrypto/IndexedDB, signed requests, real entry/publisher/grant ledger and streaming/cancel paths, all on **ephemeral loopback ports**. Generation and owner-run telemetry use the explicitly labelled scripted fixtures from `test_hosted_ui_browser.py`; this is not real-model performance evidence. No live `mycelium.now` request, deployment, push/fetch, launchd restart or service edit was performed.

- Baseline `9c6c7260`: **536 passed, 1 skipped** in the exact requested five suites.
- Final candidate (`ae821c08`, implementation and tests): **547 passed, 1 skipped** in 435.00 seconds. All 537 baseline cases are retained (one explicit static-allowlist test rename: four assets → five with `site.css`), plus 11 new cases; **no new failures** and the same existing governance skip.
- Stability proof: **three consecutive complete browser-file runs, 11 passed each**, followed by the green full candidate run. Final captured maximum CLS is **0.0008564557383140713**, across 80 page/state samples and 80 candidate screenshots. No threshold changes, test retries or skipped dark cases.
- Exact JUnit/log receipts, original REDs, prior intermittent failures, case-rename mapping and artifact hashes: `site-v2-local/test-results/summary.json`.
- The adapted 20-check main flow is `test_twenty_real_app_behaviours`, with additional parametrized state/geometry/CLS, renewal and busy-cancel checks. Every real legacy browser flow was adapted to the new navigation without dropping its grant/terminal/privacy assertions.
- Screenshots and accessibility snapshots: `docs/design/site-v2-local/` (1440×900 and 390×844, dark and light).
- Per-page and access-flow CLS readings: `site-v2-local/cls-*.json`; every test requires **CLS < 0.01**. Script and list responses are deliberately delayed 700 ms while retaining their actual local-server payloads. Composer bounds are checked before automation can auto-scroll to a control.
- Visual inspection found and fixed: mobile footer padding accidentally applied to card footers, hidden card privacy links, composer placement, a toast covering the composer, asynchronous contributor-copy growth, the privacy/footer slots' layout shift, overlapping Owned empty-state copy, and native hash-anchor jumps caused by container IDs colliding with app routes. Slow-script startup reveals only the synchronously selected route shell, before identity or API work, rather than flashing Browse and reflowing into another page.
- Observed behavioral RED: model picker was an INPUT (not SELECT), absent registry options; renewal incorrectly returned to Ready using an exhausted old grant. Both have exercised GREEN regressions. The two intermittent full-suite failures were reproduced exactly by `tests/swarm_directory/site_layout_shift_probe.py` with async delayed local responses and intentionally restored old asset behavior. Mobile New measured **0.0130198055**: the page-heading `DIV.row` moved from x=175.89/y=95 to x=16/y=118.44 as Browse changed into Start a swarm. Desktop Privacy measured **0.0169779219**: `FOOTER.foot` moved from y=842.02 to y=725, then its paragraph shifted another 20.14px. The current app measured **0.0** in both comparisons. Causes were route-shell flashing/native hash-anchor interaction and unreserved asynchronous copy, not fonts or theme switching (system fonts and CSS `prefers-color-scheme` only). Fixes reveal the synchronously selected route before any identity/API await, avoid route/DOM-ID collisions, and reserve the copy slots. Neither threshold, case selection nor retry policy was relaxed. The observer retains selectors and old/new rectangles in every failure. The deliberately defective controls and their source-attribution receipts are isolated under `site-v2-local/negative-controls/`, not included in the candidate-page CLS totals.

Reproduce from the build worktree:

```sh
PY=/Users/evinova-self/Documents/playground/mycelium-v2-m0-test-ladder/.venv/bin/python
"$PY" -m scripts.generate_site_models --check
SITEB_SHOTS=docs/design/site-v2-local nice -n 19 "$PY" -m pytest -p no:cacheprovider -n 1 \
  tests/swarm_directory tests/demo tests/governance tests/contracts tests/claims \
  --basetemp="$HOME/.hermes/cache/scratch/siteb-check" \
  --junitxml="$HOME/.hermes/cache/scratch/siteb-check.xml"
```

Screenshots are evidence, not source fixtures; rerunning with `SITEB_SHOTS` changes their bytes. Re-pin **after** any such evidence update. The A6 manifest retains its existing base `53d1222b`; contract pins use the existing generator. The final lane commit is reserved for generated pins.

## Later owner-approved deployment — NOT executed

Do not copy only the HTML/CSS: the running apex proxy must also learn the exact new stylesheet allowlist entry. Do not enable product routes.

Read-only inspection of the existing on-disk launchd plists found both jobs rooted at
`/Users/evinova-self/mycelium-hosted-demo/checkout-9599a732`, directory port **4390**, apex proxy **4380**, with the same directory database in `mycelium-hosted-demo/state/directory.sqlite3`. These differ from template ports. No job state was changed or queried through launchctl. Preserve those args/environment/state locations.

After the owner approves deployment (and coordinates the apex explainer decision), the exact safe promotion is a detached checkout of the approved SHA plus a WorkingDirectory-only rebind of **these two jobs**. The snippet below is a future operator instruction, not an installed deploy script. Backups stay in the existing state tree, outside the hash-pinned release checkout:

```sh
set -eu
SRC="$HOME/Projects/mycelium-rel2-siteux-build"
SHA=$(git -C "$SRC" rev-parse HEAD)  # compare with the reviewed handoff SHA first
RELEASE="$HOME/mycelium-hosted-demo/checkout-$SHA"
git -C "$SRC" worktree add --detach "$RELEASE" "$SHA"
DEPLOY_STAGE="$HOME/mycelium-hosted-demo/state/site-deploy-$SHA"
export RELEASE DEPLOY_STAGE
(
  cd "$RELEASE"
  env -u PYTHONPATH -u PYTHONHOME -u VIRTUAL_ENV /opt/homebrew/bin/python3.14 -B -c \
    'from mycelium_swarm_directory.server import ASSETS; from mycelium_demo.public_origin_proxy import _DIRECTORY_ASSET_PATHS; assert "/swarms/site.css" in ASSETS and set(ASSETS) == _DIRECTORY_ASSET_PATHS'
)
# Prepare both plists before stopping either job. No args, database paths or capabilities change.
/opt/homebrew/bin/python3.14 - <<'PY'
import os, pathlib, plistlib, re
home = pathlib.Path.home()
release = pathlib.Path(os.environ['RELEASE']).resolve()
stage = pathlib.Path(os.environ['DEPLOY_STAGE'])
stage.mkdir(mode=0o700)
for label in ('now.mycelium.swarm-directory', 'now.mycelium.apex-proxy'):
    source = home / 'Library/LaunchAgents' / (label + '.plist')
    # Existing comments contain XML-invalid '--'; remove comments in memory only.
    doc = plistlib.loads(re.sub(rb'<!--.*?-->', b'', source.read_bytes(), flags=re.S))
    (stage / (label + '.rollback.plist')).write_bytes(plistlib.dumps(doc))
    doc['WorkingDirectory'] = str(release)
    (stage / (label + '.plist')).write_bytes(plistlib.dumps(doc))
PY
for label in now.mycelium.swarm-directory now.mycelium.apex-proxy; do
  plutil -lint "$DEPLOY_STAGE/$label.plist"
done
for label in now.mycelium.swarm-directory now.mycelium.apex-proxy; do
  launchctl bootout "gui/$(id -u)/$label"
  cp "$DEPLOY_STAGE/$label.plist" "$HOME/Library/LaunchAgents/$label.plist"
  launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/$label.plist"
done
```

Then GET the stylesheet and `/swarms/#browse` via the existing apex, run the closed-surface probe, and check the UI in a fresh browser before claiming public success. Do not post prompts to the apex. Rollback uses the staged `.rollback.plist` files with the same bootout/copy/bootstrap sequence. Leave tunnel, model run, watchdog and other `now.mycelium.*` jobs alone. This procedure is documented but **not deployment-qualified** in this lane; it requires the owner's live approval and post-deploy verification.
