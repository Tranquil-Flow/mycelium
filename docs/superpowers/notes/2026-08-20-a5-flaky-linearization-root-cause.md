# A5 — `test_concurrent_race_linearizations` flake: root cause (2026-08-20)

**Status:** root-caused. Fix dispositioned to land with/after the A4 seal (see end).

## Reproduction (real output, this session)

- Isolated: 30/30 green (0.52–0.60 s each).
- Under full-suite CPU contention (canonical `pytest` run concurrent): 1 failure in
  60 runs; second campaign with a 6-thread hash burn: 1 failure in 35 runs
  (iteration 35, at iteration 20 of the inner 25-iteration loop).
- Verdict: load-dependent race, ~1/35–1/60 per run under contention. NOT a
  regression from A5 changes — the test exercises `RequestGatewayService` with
  a local `ConcurrentRaceBackend`; A5's change set does not touch `service.py`.

## Observed failure (verbatim, iteration 20)

Observed linearization was rejected by the serial model:

```
events:    (0, accepted) → (1, token, index 0, digest 6c02cf20…) → (2, completed)
counters:  (runtime_starts=1, backend_cancels=1, cap_acq=1, cap_rel=1,
            kv_acq=1, kv_clean=1, cap_delta=0, kv_delta=0)
phase:     completed
```

The permitted set (6 outcomes) CONTAINS `accepted → token → completed` with the
identical token digest — but with counters `backend_cancels=0`. The observed
carries `backend_cancels=1`.

## Root cause

A serial-model under-permission window between the backend's internal outcome
commit and the service's terminal publication:

1. `ConcurrentRaceBackend.complete()` sets the backend outcome `completed`
   (internal) and wakes the worker thread.
2. Before the worker returns the outcome and the SERVICE publishes the
   terminal event, the `cancel` operation runs. `RequestGatewayService._cancel_session`
   checks `session.terminal_event is None and not session.cancellation_started`
   (service.py:349) — still true — so it forwards to the backend via
   `_cancel_backend_once`, and the backend's `cancel()` increments
   `backend_cancels` (the real `RouterSessionBackend` increments its internal
   cancel counter on every forwarded cancel).
3. The worker then publishes `completed`. Result: terminal `completed` with a
   cancel counter bump — an outcome the serial model cannot produce, because
   the model's `_apply_complete` transitions the phase to `COMPLETED` at the
   same instant the backend outcome commits, so a later `_apply_cancel` takes
   the `already_terminal` branch (no counter bump).

In other words: the model treats backend-outcome-commit and
service-terminal-publication as one atomic step; the implementation does not.
The implementation behavior is legitimate (the cancel genuinely reached the
backend and lost the race to the already-committed outcome); the MODEL
under-permits. The same window class exists for the failed path
(`_apply_complete` revalidation → `_finish(..., cancel_backend=True)`) and
should be treated together.

## Disposition (lane discipline)

RESOLVED 2026-08-20 (evening): the fix LANDED in the A5 change set rather than
waiting for A4. The serial model and its tests are A4-owned conformance code,
but the change is self-contained (A4's unsealed dirty set does not touch
`mycelium_request_conformance/`), deterministic-tested, and will re-apply
cleanly at the A4 rebase. What landed:

1. `ModelState.publication_pending_kind` — the terminal publication window is
   now an explicit model state. `_finish` marks the terminal kind as pending;
   any action settles it first (the service's publication completes before
   the next operation observes it).
2. The window bump rule, grounded in the real service+double behavior:
   - pending `completed` (worker-delivered outcome) + cancel -> backend
     cancel counter bumps, terminal stays completed, code stays
     `already_terminal`. This is the flake's observed outcome.
   - pending `cancelled` -> a second cancel is absorbed (the service's
     `cancellation_started` path) — no double bump.
   - pending `failed` -> no bump: failed terminals publish synchronously on
     the service's own validation paths; the backend-commit/publication
     window only exists for worker-delivered completions.
3. Publication is now an explicit `publish` model action:
   - `generate_bounded_traces` inserts it after every terminal transition
     (the sequential production harness waits for publication there).
   - `generate_race_traces` emits BOTH serializations per ordering:
     window-open (no publish — cancel may outrun publication) and settled
     (publish after the first terminal). 90 race traces total.
   - `test_generated_production_traces` replays the settled variants
     (sequential replay cannot reproduce an outrunning cancel); the
     concurrent race test's permitted set covers both.
4. Test updates: three terminal-state immutability assertions in
   `tests/request_conformance/test_model.py` now compare the settled state
   (`replace(state, publication_pending_kind=None)`); new deterministic
   tests pin the window bump for completed and the no-bump for failed;
   trace-generator pins updated to the measured counts (bounded 91 / max
   length 6; race 90 / lengths 6-9).
5. Verification: tests/request_conformance 55/55 green; the flake tuple
   (accepted -> token -> completed, backend_cancels=1) is now in the
   permitted set alongside the settled cancels=0 variant (real output).
   LOAD LOOP (23:19): the exact contention recipe that produced the flake
   (6-thread SHA-256 burn + 100 sequential race-test iterations) now passes
   100/100 (0.56-0.64s each). Pre-fix the same shape failed ~1/40. The
   under-permission is empirically closed.

## Verification recipe (for whoever fixes it)

```bash
cd /Users/evinova-self/Documents/playground/mycelium-wave8-g4
# load generator (background): 6 threads sha256 burn for ~7 min
/opt/homebrew/bin/python3.14 -c "
import threading, time, hashlib
stop = time.time() + 420
def burn():
    while time.time() < stop:
        h = hashlib.sha256()
        for i in range(2000): h.update(b'load-' + bytes([i & 255]))
        h.digest()
threads = [threading.Thread(target=burn) for _ in range(6)]
[t.start() for t in threads]; [t.join() for t in threads]"
# in parallel: ≥60 repetitions of the single test
for i in $(seq 1 60); do /opt/homebrew/bin/python3.14 -m pytest \
  tests/request_conformance/test_concurrent_race_linearizations.py \
  -q -p no:randomly --tb=long 2>&1; done
```
