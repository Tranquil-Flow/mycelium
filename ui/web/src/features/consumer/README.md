# Consumer demo surface

Export: `ConsumerDemoPage` (also `ConsumerDemoRoute`) from `./ConsumerDemoPage`.
The actual App/main entrypoint now mounts `ConsumerLiveRoute` at `/`. It owns the
same-origin live bootstrap and ProductEvidenceProvider; a missing or non-live
bootstrap keeps the page blocked, with an explicit Retry connection action.
An explicit fixture build setting applies only to `/operator`, never the consumer.
Operator workspaces retain their hash navigation and A6 RecoveryStatusBar under
`/operator`; legacy root workspace hashes migrate there. The departing operator
listener ignores consumer history events, preventing stale duplicate-shell routing.
Device Lab capabilities are consumed and scrubbed by the original helper before
path migration; no capability is placed in storage or a server-bound URL.
UI routing grants no backend privilege. Existing protected control-plane APIs and
authentication are unchanged. No deployment or public-origin routing is performed.

This is a consumer presentation over the exact `useInferenceSession` controller
used by `InferenceWorkspace`, with the same `ProductInferenceClient`, bounds,
qualification checks, stream cursor handling, cancellation and binding review.
It deliberately does not nest the entire operator-oriented InferenceWorkspace:
that would expose model administration and create an additional inference session.
`InferenceWorkspaceProps` supplies the shared injection contract. The existing
`RouteExplorerWorkspace` is composed directly for the live read-only route panel.
No transport, backend, fallback engine or new runtime field is introduced.

The consumer uses in-memory state only. Navigation away loses its prompt/output;
this is stated in the interface. Retry submits the current editable prompt as a new
request, after the canonical engine revalidates qualification. Reconnect resumes
an interrupted accepted request at its last applied event, without resubmission.
A cancellation acknowledgement is not displayed as a confirmed terminal.

TTFT/TPOT are measured in the browser from the real request event stream
(`clientTiming.ts`, a pass-through client over the same `InferenceClient`) and every
figure is labelled client-side; the origin's snapshots carry no TTFT/TPOT
(`has_ttft: false` / `has_tpot: false` in the physical transcript). Stage, layer and
node placement is read from the product snapshot's `stage`/`device` entities and their
`placed_on` relations; when those are not published the panel says so and no split is
inferred. Still unavailable: cold-restart status/ETA, Mac display names and OS identity,
and any layer-to-node mapping the documents do not carry. Do not reinterpret RTT as TTFT,
segment count as layer count, a transport endpoint as a Mac, a device id as a hostname,
or evidence-feed reconnecting as worker recovery. `route_explorer` offers endpoint/member
peers, retained segments, transport measurements and freshness, not those fields. Source fixture/replay/degraded
and frozen status remain explicit. No two-Mac success, in-place recovery or soak
claim is made. All operator/evidence links begin with `/operator`.

Design: quiet forest-green canvas, pale leaf accent, editorial serif headline,
primary prompt/response card, narrow evidence sidebar, and progressive disclosure
for the dense normative route explorer. Local system fonts; no external assets.
Responsive single-column layout below 1000px; keyboard focus, skip navigation,
stream live region and reduced-motion support.

Reference inspected (design only):
`~/Documents/playground/mycelium-final-mvp-u9zrsk7f/committed-workbench/packages/access/viewer/index.html`.
Borrowed explicit prompt/bounds and separation of claims; copied no code or economic
integrations. This retained reference is an older numbered Connect/Choose/Request/
Claims viewer, not the later literal “Try it / How it works” version from the plan.

Tests: `npm test -- src/features/consumer`.
Neighbors: `npm test -- src/features/consumer src/features/inference src/features/routeExplorer`.
Types: `npm run typecheck`.
Route/entrypoint regressions: `npm test -- src/App.consumer.test.tsx src/main.consumer.test.tsx src/app/navigation.test.ts`.
Fixtures in ConsumerDemoPage.test.tsx are test-only protocol inputs, not physical
qualification evidence. Route-unit browser evidence loads the actual production
build on ephemeral loopback. Root/missing-backend checks use no API interception;
operator-shell checks label their test-only bootstrap response and keep all actual
evidence endpoints unavailable. This establishes local entrypoint routing, not live
inference, public origin integration or a delivered mycelium.now site.
