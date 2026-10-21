# Real-site local browser evidence

This directory is generated from the shipped directory/entry HTTP services on ephemeral loopback ports, **not the clickable prototype or public website**. Generation and run telemetry are scripted test fixtures; the visible timing/model/Mac values are not public-swarm benchmarks.

## Screenshots

80 candidate screenshots: 32 route views, 44 access-state views and 4 owner-inbox views. The matrix is **1440×900 / 390×844 × dark / light**. Full-page captures retain the actual viewport-fixed mobile tab bar; content below it belongs to the scrollable page. Chat conversation/gate content scrolls separately from the composer.

| View | Desktop dark | Mobile light |
|---|---|---|
| Browse | [image](browse-1440-dark.png) | [image](browse-390-light.png) |
| Chat list | [image](chat-1440-dark.png) | [image](chat-390-light.png) |
| Contributing / Helping | [image](contributing-1440-dark.png) | [image](contributing-390-light.png) |
| Owned empty | [image](owned-1440-dark.png) | [image](owned-390-light.png) |
| Owner inbox | [image](owner-inbox-1440-dark.png) | [image](owner-inbox-390-light.png) |
| Create | [image](new-1440-dark.png) | [image](new-390-light.png) |
| Detail | [image](swarm-detail-1440-dark.png) | [image](swarm-detail-390-light.png) |
| Privacy | [image](notice-1440-dark.png) | [image](notice-390-light.png) |
| No access | [image](access-needs-request-1440-dark.png) | [image](access-needs-request-390-light.png) |
| Pending after reload | [image](access-pending-1440-dark.png) | [image](access-pending-390-light.png) |
| Approved, consent required | [image](access-approved-1440-dark.png) | [image](access-approved-390-light.png) |
| Ready | [image](access-ready-1440-dark.png) | [image](access-ready-390-light.png) |
| Offline with draft retained | [image](access-offline-1440-dark.png) | [image](access-offline-390-light.png) |

Other `access-*` images cover declined, expired, quota, invite-only, warming and owner-stop states. Screenshots were visually inspected; this is not a Safari/Firefox or screen-reader qualification.

## CLS

The same observer runs in light and dark cases, with an unchanged **< 0.01** requirement, no retries and no dark-case skip. Attribution includes DOM selectors and previous/current rectangles. The 80 samples include the pending-state reload, so they are not 80 unique routes.

| Matrix | Max route CLS | Max access-flow CLS |
|---|---:|---:|
| Desktop dark | 0.0006766069 | 0.0004175922 |
| Desktop light | 0.0006766069 | 0.0004175922 |
| Mobile dark | 0.0003976928 | 0.0008564557 |
| Mobile light | 0.0003976928 | 0.0008564557 |

Overall maximum: **0.0008564557383140713**, below 0.01. Browse, Chat list, Contributing, Owned, Create and Privacy measured 0 in this capture. Remaining tiny movements are text/metric updates; do not call this mathematically zero CLS on every screen.

The browser test file passed **three consecutive complete runs**, 11/11 each. The third run produced this matrix. Full-suite and baseline JUnit/log receipts live in `test-results/` and are summarized in `test-results/summary.json`.

## Deliberately broken controls

`negative-controls/` is separate from candidate evidence. The diagnostic restores old markup/CSS only in intercepted **local** responses and delays script/config responses asynchronously. It reproduces both historical full-suite failures exactly:

- Mobile Create: **0.013019805543904951**, page-heading controls wrap/move when Browse flashes before the selected route.
- Desktop Privacy: **0.016977921850887347**, footer then its paragraph move due to the route/anchor transition and late text.
- Current assets under the same delays: **0.0** for both.

The controls are supposed to exceed the budget. Their JSON includes the source rectangles and their screenshots are not candidate screenshots. Reproduce with the project test environment:

```sh
nice -n 19 /Users/evinova-self/Documents/playground/mycelium-v2-m0-test-ladder/.venv/bin/python tests/swarm_directory/site_layout_shift_probe.py
```

See [the build handoff](../SITE-V2-BUILD.md) for scope, phase boundaries, test command and the **unexecuted, owner-approved-only** deployment procedure.
