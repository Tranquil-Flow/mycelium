# P3 demo-route copy follow-up

Base: `4c42ba68f12f69cf131a814f21689db54da71e6f`, same private lane. **Preparation only: no live deployment or job rebind.**

## Copy and data boundary

The apex makes its existing single public-list GET, with no identity, storage, entry call or prompt. It selects serving listings whose `entry_origin` equals the demo CTA origin, not a display name, hardcoded swarm ID, or the global Mac sum. The public v1 directory exposes `members` and `status`, not a separate route-mode field. This remains owner-reported snapshot data, not a physical topology measurement.

| Directory observation | Route note |
|---|---|
| One unambiguous serving demo listing, `members = 1` | The demo route runs on one Mac, not a distributed swarm. Reported by its listing; availability may change. |
| One unambiguous serving demo listing, `members = N`, 2–64 | The demo route runs across N Macs. Reported by its listing; availability may change. |
| Offline, warming, absent, zero/invalid count, multiple serving demo listings, API failure, or JavaScript unavailable | The demo is volunteer-hosted and may be unavailable. Check its listing for current serving Macs. |

Offline duplicate listings are ignored; multiple serving matches are deliberately ambiguous rather than summed or picked arbitrarily. The static HTML starts neutral. A three-line reserved note region prevents late response text from changing layout. The status band explicitly calls this a snapshot and tells the visitor to refresh for a new one.

Copy audit: removed the unsupported **fast** claim along with the hardcoded route topology. Also replaced “Swarms still work if you have their entry link” on API failure with “Try an entry link to reach a swarm directly,” which does not imply an offline swarm is working. The rest of the apex describes general swarm behavior (including one-Mac execution), trust boundaries and platform limits; its speed/availability statement is already conditional. No speed, model, topology or availability claim depends on a future route switch.

## Verification and evidence

The regression was written and exercised before the implementation: the two-Mac fixture rendered the old one-Mac claim (`demo-route-two-red.*`). Focused GREEN covers both widths and both themes. The original first RED (against the obsolete “fast” wording) is also retained rather than overwritten.

The new browser test checks eight real signed directory states per width/theme: two, one, maximum 64, offline, warming, zero/unknown, multiple-serving ambiguity and unavailable. Its unrelated same-name serving listing and offline demo duplicate catch incorrect selection and aggregation. Every transition uses a fresh context and checks exact-zero CLS, stable note bounds, unclipped content and no non-directory traffic.

Twelve full-page captures and per-image JSON: [site-v2-p3/demo-route/](site-v2-p3/demo-route/). All were inspected directly with vision; [inspection record](site-v2-p3/demo-route/INSPECTION.md). Historical screenshots are retained separately.

The inherited baseline receipt is `site-v2-p3/test-results/candidate.xml` (570 passed, one skipped); its code revision `aa4d387d` is source/test-identical to `4c42ba68`—only documentation, evidence and the source manifest changed between them. The follow-up comparison preserves IDs without renaming or dropping cases.

Before the pin/preparation boundary: **three consecutive complete browser-file runs, 26 passed each**, and the five requested suites excluding browser files, **535 passed, one skipped**. All 12 deployment-tool tests pass. Browser/route/script receipts are under `site-v2-p3/test-results/demo-route-*`. The broad run waited for the device lease rather than competing with it. Final full-scope qualification is run from the exact detached final checkout, with its JUnit/log/comparison outside the checkout in the prepared state directory. Consult that directory's `DEPLOY-REPORT.md` for the final full-suite result; the pre-pin receipts alone are not a full-scope claim. This separation lets final-head tests run without invalidating the last source/evidence manifest.

## Prepared deployment tooling

Versioned templates: [site-v2-p3/deploy-tools/](site-v2-p3/deploy-tools/).

- `prepare.py`: validates a clean detached exact-SHA checkout, reads the two current site plists and creates a **new** private state directory, byte-exact backups, normalized staged plists, script hashes and a dry-run receipt. Never writes LaunchAgents or invokes launchctl.
- `switch.py`: **dry-run by default**. `deploy --apply` and `rollback --apply` are explicit future operator actions. Both jobs are validated before either is stopped. Only WorkingDirectory and the apex's existing explicit root-redirect flag may change; all ports, DB/capability paths, environment and other arguments are preserved. Dirty/mismatched checkouts, staged/live plist drift, quarantine and device/cert leases refuse application. Promotion failures automatically restore both staged rollback jobs. Runtime/target readbacks are mandatory. Rollback does not require a healthy candidate checkout.
- `http_verify.py`: no network without `--live` or loopback `--base`. Candidate root GET/HEAD must serve the explainer; rollback must restore the 302 `/swarms/`. Checks exact JS/CSS bytes, CSP, cookie-free assets and API, Profile A and additional closed chat routes. It reports demo listing state without treating an honest offline demo as a site failure. No entry calls or prompts.
- `loopback_verify.py`: runs the actual candidate/rollback checkout on ephemeral local ports with an in-memory directory and no model. Both paths passed the HTTP verifier. This is **not** a claim of public deployment or launchd qualification.

`test_site_deploy_tools.py` exercises dry-run non-effects, promote/rollback readbacks, failed-promotion recovery, staged/live drift, busy/quarantine/dirty guards, prohibited argument changes, damaged-candidate rollback and real candidate HTTP success/negative controls. Launchd tests use a recording runner and scratch home; no live service is touched.

Important discovered deployment difference: the current apex job explicitly passes `--redirect-root-to-swarms`. The prepared candidate replaces only that token with `--no-redirect-root-to-swarms`; a WorkingDirectory-only rebind would leave the explainer hidden. Rollback restores the original flag and checkout `790d3caa`.

Re-pin after all source/evidence edits, keeping the existing A6 base. The final detached checkout and new state directory are created from that final pin commit. The state directory's `DEPLOY-REPORT.md` contains exact SHA-specific future promote/verify/rollback commands. No push/fetch, branch switch, live launchd rebind, keeper/relay/demo edit or public request is part of this preparation.
