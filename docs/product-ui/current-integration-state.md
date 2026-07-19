# Product UI current integration state

- Repository: `/Users/evinova-self/Projects/mycelium`
- Primary worktree: `/Users/evinova-self/Projects/mycelium-wt-product-ui-primary`
- Primary branch: `integration/mycelium-product-ui`
- Planning commit / primary base: `a2e5527893381c2a87586c061ab292c7dd082c81`
- Code parent under plan: `b447c9423f45fdcf847332ab32d7d74d1921d6a9`
- Canonical `main` was not edited.
- Remote operations: none; no fetch, pull, push, PR, or main merge.

## Preflight

Planning commit parent was verified exactly equal to the intended code base. The primary path and branch were absent before creation, then created from the exact planning commit.

All 54 pre-existing worktrees were inspected without modification. Seven had pre-existing changes and remain untouched:

- `mycelium-wt-iroh-state-conformance`
- `mycelium-wt-p6-local-exec`
- `mycelium-wt-p7-sidecar`
- `mycelium-wt-p9-semantic-ui`
- `mycelium-wt-physical-qualification-preflight`
- `mycelium-wt-request-iroh-e2e`
- `mycelium-wt-router-conformance`

Those paths are isolated from this primary worktree and do not overlap this branch's filesystem.

## Baseline verification

Before product-foundation edits:

- `cd ui/web && npm ci`: exit 0; 147 packages installed; 0 vulnerabilities.
- `cd ui/web && npm run check`: exit 0; 102 Vitest tests and 3 Node contract tests passed; interactive bundle, typecheck, and production build passed.
- `python3.14 -m pytest -q test_observatory_gateway.py tests/observatory_events tests/request_gateway tests/request_conformance tests/interactive`: exit 0; 180 tests and 27 subtests passed.
- `git diff --check`: exit 0.

## Authority and claim boundary

- Observatory authority remains read-only.
- Inference authority remains `mycelium_request_gateway`, composed through the same-origin product gateway.
- Qualification authority remains the qualifier-owned accepted record.
- `route_ready=false` remains the checkout claim until accepted qualifier evidence says otherwise.
- Synthetic browser work is not model inference or route qualification.
- Product behavior must not require Tailscale.

## Frozen product-foundation decisions

- Browser contracts are closed, versioned JSON Schemas under `ui/contracts/product/` with matching strict TypeScript decoders in `ui/web/src/app/contracts.ts`.
- Same-origin product endpoints are fixed under `/api/v1/`; the browser contract exposes no upstream bearer credential.
- Inference submissions preserve the separate request-gateway protocol and require the complete qualifier binding.
- Default product state is `source_mode=fixture`, `route_ready=false`, qualifier authority, with unavailable metrics represented as `null`.
- Stable shell routes are Inference, Network, Nodes, Plans, Readiness, Incidents, and Settings. The former `#evidence` deep link canonicalizes to `#readiness`.
- Feature modules enter through lazy route slots; isolated workers do not edit shared shell, contract, or package files.
- Product fixture factories reject prompt/token payloads, credential aliases, hidden/symbol fields, hostile arrays, endpoint data, and non-redacted private addresses. The synthetic CSRF fixture is permitted only at its exact bootstrap-session path.

## Browser automation decision

- Runner: Playwright Test, pinned exactly to `1.61.1` in the primary-owned package files.
- Engines: Chromium, Firefox, and WebKit.
- Browser smoke uses only fixture data, asserts no inference request, checks console/page errors, checks route navigation, and distinguishes synthetic browser work from model inference.
- Foundation `npm run test:e2e`: exit 0; 3/3 engine projects passed with traces, screenshots, videos, repository-local output, and automatic error-context snapshots disabled.
- Browser automation remains synthetic same-machine evidence, not physical-device, semantic, network-path, or route-readiness qualification.

## Foundation verification

- `npm run check`: exit 0; 152 Vitest tests and 3 Node contract tests passed; interactive bundle, typecheck, and production build passed.
- Focused authority/privacy regression tranche: 50 tests passed in the final independent review, followed by sticky-watermark and hidden-property fixes.
- Python gateway baseline: exit 0; 180 tests and 27 subtests passed.
- Playwright: exit 0; Chromium, Firefox, and WebKit each passed the same-origin, zero-inference, seven-route smoke.
- `git diff --check`: exit 0.
