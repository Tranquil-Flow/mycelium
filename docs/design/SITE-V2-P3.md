# Site V2 phases 3–4 + PV33 — local candidate

Base: `790d3caa9a3333284df02c6c9e4766d84026ae73`, branch `lane/rel2-siteux-p3`.
**Not deployed.** The live site remains on the reviewed phase-0–2 build. This document describes the scoped overnight P3 additions, not every future proposal in SITE-REDESIGN-V2.md.

## Delivered behavior

- **Phase 4:** `/` is a static explainer (no browser identity, storage, entry traffic or inference endpoint). `/apex.js` reads the existing public directory list once for serving-swarm and online-Mac counts. Warming Macs count as online; offline Macs do not. The count is explicitly a sum across listings, not deduplicated physical Macs. Failure leaves dashes, not false zeroes. Demo / Browse / Download CTAs lead to real destinations; Download uses the existing package availability screen. Copy says MVP, Apple silicon Macs only, the fast demo route is one Mac, volunteer availability, and who can see prompts.
- **Phase 3, requested subset:** local Mac app header polls the existing authenticated publisher inbox at most every 30 seconds while visible. The site already has Owned/mobile badges. Site Approve and Decline now hold the signed write for five seconds with Undo. Undo, page navigation and reload before that deadline send nothing; after sending there is no claim of revocation. Quota defaults are remembered in the owner's browser after a successful approval. A declined requester sees the server retry time and cannot bypass the cooldown with a hand-signed request. An owner can still approve a declined row without asking the visitor to re-request.
- **Model enforcement:** the directory accepts new/updated records only for IDs in `run_model.CHOICES`; access requests to legacy unsupported models are refused. Entry admissions (signed send and the shared relay chat path) reject unsupported policy models before quota charging or backend invocation. Existing signed records remain readable. Static picker equality coverage remains; no inference runtime is imported by the whitelist.
- **PV33:** recent offline swarms stay in the collapsed Offline section. Long-offline swarms are hidden from Browse; after the longer TTL they leave the public list. Records are retained, not tombstoned or erased. Signed owner queries recover retained owned records, direct links still resolve, and a newer owner-signed record via **Republish listing**, or a new serving/warming heartbeat, restores discoverability. Replayed records and unauthorized writes cannot restore ownership. Repeated offline heartbeats do not restart the offline clock.

## Owner assumptions / configuration

| Setting | Default | Meaning |
|---|---:|---|
| `--offline-hide-seconds` / `Limits.offline_hide_seconds` | 86400 (24 h) | Hide from Browse after this much continuous offline time |
| `--offline-delist-seconds` / `Limits.offline_delist_seconds` | 1209600 (14 days) | Omit from the public list; retain recoverable signed record |
| `--decline-cooldown-seconds` / `Limits.decline_cooldown_seconds` | 86400 (24 h) | Refuse a new request by the declined key until the boundary |

Hide must be positive and shorter than delist. Cooldown may be zero for an explicitly configured server. These defaults are assumptions supplied in the overnight brief, not new owner decisions.

No heartbeat or signed-document shape changed. Offline age starts at an explicit offline transition, heartbeat expiry, or creation/republish for a never-online listing. Legacy rows have no historical offline start: use their latest received heartbeat (plus the heartbeat TTL when that report was online), otherwise record update time; subsequent offline reports preserve that start. No fabricated historical uptime. Existing directory storage caps still bound retained records; auto-delisting is not physical garbage collection.

A retained listing is not a secret. Direct links and its signed record remain readable; auto-delisting is reversible discoverability policy, not access revocation. Existing grants remain enforced by the entry. Recovery requires the original owner key or an authorized entry heartbeat: this does not add account/key recovery or transfer a non-extractable browser private key to another device. The existing owner-signed delete path remains available wherever the original owner key is already held.

Out of this scoped overnight change: heartbeat v2/aggregate statistics, browser grant-revoke endpoint, cross-device owner-key migration and a complete historical grants ledger. `owner-inbox.grants` includes the directory's current grant per grantee, not a promise of all historical grants or entry usage.

## Verification

Implementation/test revision: `aa4d387d2ffa934b9d87c29f3730efb3ac2f57eb`. Later commits contain only this delivery record/evidence and final generated pins.

- Baseline `790d3caa`: **547 passed, 1 skipped** in `tests/swarm_directory tests/demo tests/governance tests/contracts tests/claims`.
- Candidate: **570 passed, 1 skipped**, **23 additional cases, no removed cases and no new failing IDs**. Three explicit root/asset-policy test renames are mapped in `site-v2-p3/test-results/summary.json`; the existing skipped case is unchanged.
- **Three consecutive complete browser-file runs: 22 passed each**, followed by the green full five-suite run. All six CLS assertions now require **exactly 0**, stronger than the previous `< 0.01` budget. No retry plugin, skipped themes or threshold loosening.
- **98 final screenshots**, **88 saved page/state CLS samples, maximum 0**. Desktop/mobile and dark/light images, source-attributed pre-fix receipts, and representative direct vision inspection are under `site-v2-p3/`; see `INSPECTION.md`. The browser file separately asserts zero for owner held writes and app-header changes.
- Found and fixed actual shifting elements: chat heading/subtitle container, status-dependent metrics position, intrinsic-width composer reason, initially empty detail owner/model fields, and the centered held-decision text changing from two lines to Saved. Visual review also caught an Undo/Approve overlap and a clipped mobile explanation; geometry regressions cover both.
- Original behavioral REDs remain in `red.*` and `http-red.*`; later visual/layout REDs remain separately documented. The model-picker equality check and `git diff --check` pass. Basetemps were isolated under the requested scratch prefix and removed after each run. Browser/full runs waited for `DEVICE.busy` to clear, using two-minute polling, nice 19 and one worker.

Exact JUnit/log receipts, normalized case comparison, source hashes and evidence hashes: `site-v2-p3/test-results/summary.json`. Browser generation uses explicitly scripted loopback fixtures, never physical-model performance evidence. Mac-header coverage uses shipped HTML/CSS/swarm.js over a loopback test facade backed by the real publisher/directory; unrelated device panels are inert. There is no live-device or deployment qualification claim.

To reproduce after checking executable quarantine attributes and waiting for the device lease (poll every 120 seconds):

```sh
PY=/Users/evinova-self/Documents/playground/mycelium-v2-m0-test-ladder/.venv/bin/python
env -u PYTHONPATH -u MYCELIUM_PHYSICAL_SERVICE_PYTHON nice -n 19 "$PY" -m pytest -p no:cacheprovider -n 1 \
  tests/swarm_directory tests/demo tests/governance tests/contracts tests/claims \
  --basetemp="$HOME/.hermes/cache/scratch/sitep3-recheck"
```

Run `tests/swarm_directory/test_site_v2_browser.py` alone three times for the stability gate. Set `SITEB_SHOTS=docs/design/site-v2-p3` only when deliberately regenerating captures; that changes evidence bytes. Delete only the run's own scratch basetemp afterwards. Re-pin after every source/evidence change, preserving the existing A6 source-manifest base `53d1222b`; the final lane commit is reserved for generated pins.

## Promotion by parent only (not executed)

1. Review the final SHA and create a detached deployment checkout of that SHA. Check executable quarantine attributes before using binaries. Keep a byte-exact backup of the two existing directory/apex job plists outside the release checkout.
2. Preserve actual ports, directory SQLite path, capabilities, environment and arguments. Rebind **only** `now.mycelium.swarm-directory` and `now.mycelium.apex-proxy` to the approved checkout. Do not touch the tunnel, model run, watchdog, production core-pilot jobs or port 8787.
3. Root behavior defaults to the explainer. If an existing apex job explicitly passes `--redirect-root-to-swarms`, replace it with `--no-redirect-root-to-swarms`; do not enable product routes. An explicit `--redirect-root-to-swarms` still selects the legacy redirect.
4. Restart only those two approved jobs using the established parent deployment procedure. Verify GET/HEAD `/` = 200 HTML; exact `/apex.js` and `/swarms/site.css` bytes; Browse and live counts; no cookies; CSP intact. Run the closed-surface probe and separately confirm `/chat`, `/chat/api/send` and apex inference routes remain closed. Prompts must travel directly to the entry, never apex.
5. This also changes the Mac app UI and entry model admission. The static site deployment alone does **not** update an already-running local Mac app. Qualify/install the same reviewed app revision separately under the parent's device lease if those local features are to go live.
6. Roll back the two jobs to the retained `790d3caa` checkout/plists if any public check fails. Additive row metadata is tolerated by the old directory; rollback exposes retained offline rows again. Do not delete the database or retained evidence. No irreversible migration is required.
