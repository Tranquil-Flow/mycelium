# A5 design gap: dispatcher rotation and per-window benchmark mode (needs Evi decision)

**Discovered 2026-08-20 (evening session), fleet-free analysis. Blocks Phase 6.1/6.2
as currently inherited. NOT covered by the four locked decisions.**

## Facts (verified in code, not speculation)

1. `ReplicaTrackSessionBackend._choose_track_id` rotates ONLY over replica
   qualification docs. With ONE physical replica (node-3-r2, locked decision 1),
   every request selects the same replica track. The incumbent path
   (plain A4 backend) is reached only when NO replica track is selectable.
2. Therefore the positive gate's required proof — "distinct requests use
   distinct complete tracks" (spec §1) — is physically impossible today:
   two concurrent requests both land on the one replica track.
3. The frozen benchmark protocol requires 12 measured windows (ABBA×3,
   baseline + candidate modes) with IDENTICAL bindings across all windows
   (`identical_binding_fields`) and declares `server_restarted` an
   invalidation rule. Two serves (baseline-only + candidate) therefore
   cannot produce an honest run — the checker turns the declared
   invalidation into `inconclusive`.
4. There is no product-path mechanism today to make ONE serve deliver
   baseline windows (incumbent only) and candidate windows (track rotation)
   on demand. `ReplicaTrackDispatcher.select()` already accepts
   `requested_track_id` — the seam exists — but nothing in the request path
   plums it (ASGI body → `InferenceSubmission` → `backend.run`).
5. `InferenceSubmission` (mycelium_request_gateway/contracts.py) is a frozen
   A4 contract with strict closed validation; adding a field is an A4-module
   edit and must follow the same post-rebase discipline as the item-1
   plumbing.

## Proposed design (recommendation)

A. **Rotation includes the incumbent.** `ReplicaTrackSessionBackend` rotates
   over `[incumbent(plain), replica_track_1, …]` instead of replica tracks
   only. Mints nothing: the incumbent track is exactly "the A4 default path",
   already authorized. Two concurrent requests → distinct tracks → positive
   gate provable via admission-status `placement_ids` per request.
B. **Per-request track hint through the ordinary product path.** Add one
   OPTIONAL field to the v2 request body (A4 `contracts.py` +
   `InferenceSubmission`, post-rebase edit): `requested_track` ∈
   {absent, `"incumbent"`, `<qualified track_id>`}. Absent → rotation
   (candidate/default); `"incumbent"` → plain A4 path (baseline windows).
   Validated closed; an unknown track_id fails admission with the existing
   `AdmissionError` vocabulary (no new terminal status).
C. **Benchmark driver binds the mode per window** in the run fixture:
   `qualified_track_policy_digest` = `"incumbent_only"` for baseline windows,
   `"round_robin"` for candidate windows (both are string values in the
   allowed per-mode binding field, so the frozen protocol accepts them).
   One serve, no restarts, identical bindings, honest ABBA.

Alternative considered and rejected: two serves with a declared
`server_restarted` invalidation (guaranteed inconclusive); a serve-level
mode flag (static at start — cannot switch mid-ABBA); a new HTTP endpoint to
flip modes (new operator surface, not the ordinary request path).

## What I need from Evi

RESOLVED 2026-08-20 (Evi decision): (A) approved and landed (rotation includes
the incumbent — `_choose_track_id` rotates over `[None(incumbent), tracks…]`,
tests green). (B) NOT approved as proposed — no `requested_track` in
`InferenceSubmission`. The runtime-mutation alternative was implemented and
verified instead. (C) corrected with quoted fields and real harness output.

### (B) Runtime-mutation mechanism (implemented, no contract change)

- `set_replica_track_qualification` (mycelium_live/route.py:3789) existed with
  exactly ONE caller: the startup path in `run_physical_server`
  (supervisor.py:2197). No HTTP handler reached it at runtime — verified by
  grep (real output). The runtime-mutation capability existed at the route
  object; the product surface did not.
- Added the A5-owned operator surface that calls that EXISTING method:
  `POST /__mycelium/replica-qualification/install` with body
  `{"documents": [<validated replica_qualification.v1>…]}`. Empty list clears
  the set — rotation degenerates to `[None]`, every request runs the incumbent
  A4 default path (baseline windows). Non-empty restores candidate tracks.
  Origin-checked and body-bounded exactly like the existing operator POST
  endpoints (deployments/select et al.). InferenceSubmission and the A4
  request-path contracts are UNTOUCHED; placement stays non-client-steerable.
- Deterministic tests: tests/live/test_supervisor.py (install/clear, origin
  403, invalid-payload 400, unavailable 404) — 27 passed / 3 skipped real run.
- Why the identical-binding rule survives (analysis): all 15 binding fields
  are FROZEN constants the driver writes from the workload manifest — none
  read live route state — and the route method touches only
  `_replica_track_qualifications`. `route_generation` comes from the operator
  plan and is never mutated by the swap. No restart, no `server_restarted`,
  no `route_changed` (the mode is the controlled variable, modeled by the
  protocol's own `mode` field).

### (C) The per-window mode field, quoted and harness-proven

- The frozen SESSION protocol (`mycelium.a5_product_benchmark_session.v1`,
  workload_manifest.v1.json `product_benchmark_session.fields`) binds exactly
  the 15 fields Evi listed. It does NOT contain `qualified_track_policy_digest`
  — correct; the session digest is mode-independent.
- The per-window varying field lives in the RUN FIXTURE bindings:
  `benchmark_protocol.v1.json` → `"allowed_mode_specific_binding_fields":
  ["mode", "qualified_track_policy_digest"]`, and the harness REQUIRES it per
  record — verbatim line from materiality_harness.py `_validate_bindings`:
  `expected_fields = set(identical_fields) | {"qualified_track_policy_digest"}`
  with per-mode consistency enforced (`policy_by_mode`). So each window's
  `bindings` = the 15 identical fields + `qualified_track_policy_digest`
  (`"incumbent_only"` for baseline, `"round_robin"` for candidate).
- Real harness output (synthetic test-only fixture, no physical claim):
  decision `material`, reasons `[]`, no `binding_mismatch`, no
  `workload_or_session_changed`. The harness accepts per-mode policy digests.
- Conclusion: the frozen protocol CAN express per-window modes; no protocol
  change needed. The (B) runtime swap is what makes the serve actually behave
  per mode.
