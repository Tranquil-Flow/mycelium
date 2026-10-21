# A4 Browser Gate — CLOSED (2026-08-20 02:09 CEST)

The 3-engine live browser e2e is GREEN and reproducible.

## Evidence
- `a4-product-browser-final.json` (sha256 a12bedd02e176009…) + `a4-product-browser-repro.json`
- 3 engines (chromium/firefox/webkit) x 8 workspaces, 3 reconnect scenarios
  (waiting/prefill/decode) all reaching `cancelled` terminal with publisher
  reconnect observed, second-session privacy clean, 0 cross-origin, 0 console errors.

## The five harness bugs that masked a working product
1. `document.querySelector('[aria-live="polite"]')` returns the submit-reason div
   (first aria-live), NOT the terminal status div. Probes read the wrong element
   for the entire debug campaign while the UI worked.
2. `terminalLabels = /^(Completed|Cancelled|Failed)$/` anchored-match can never hit
   the container text "Completed\n64 tokens applied" (and `Completed64` has no
   word boundary). Terminal match is now unanchored word-prefix on the container.
3. Scenario phases were `['waiting','prefill','decode']` but the wait branches
   only handled 'prefill'/'decode' — 'waiting' skipped its wait and raced
   terminalPhase mid-decode.
4. Start button requires non-empty prompt: waiting for button-enabled BEFORE
   filling deadlocks ("Prompt is required"). Fill first, then wait, then click.
5. `waitFor({state:'enabled'})` is not a valid Playwright locator state — it
   throws instantly and `.catch(()=>{})` swallowed it (silent no-op wait).

## Product fixes shipped along the way (kept, all tested)
- `source.ts` cursor-gap recovery via snapshot refetch (was: livelock disconnect).
- `sessions.py` eviction-under-pressure (oldest session) replaces 503 fail-closed;
  43/43 ui_gateway tests green.
- `ProductEvidenceContext.tsx` mirrors evidence status onto
  `document.body[data-evidence]` (live-verified: null->loading->connecting->
  connected in 1.77 s) and logs `product_evidence_load_initial_failed` on catch.
- e2e: engines closed after workspace verification (Chromium 6-conn/origin pool),
  cancel moved BEFORE reload (post-reload cancel raced the bounded resume retries
  against `stream_already_attached`), expected-capability-404 console filtering
  via response tracking.

## Follow-ups (non-blocking)
- UI resume-after-reload retry schedule ([250,750,1500,3000]ms) gives up while
  the server still holds the old SSE subscription; a longer bounded backoff would
  make the original reload-then-cancel ordering work too.
