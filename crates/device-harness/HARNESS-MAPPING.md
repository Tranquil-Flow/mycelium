<!-- SPDX-License-Identifier: AGPL-3.0-only -->
# Harness migration map — L12 round 5

Authoritative machine-readable inventory and receipts live outside the Rust tree
in the program root, `runs/v2/v02/l12-receipts/`:

- `round5-device-map.json`: the **same 99 original device test methods**,
  preserving verbatim assertion transcriptions and individual Rust references.
  **94 MAPPED / 5 PARTIAL / 0 OPEN**; registration-dependent default assertions
  are not silently promoted by fake passage.
- `round5-demo-map.json`: **15 supplemental methods**, source bodies and explicit
  coverage: **6 MAPPED / 2 PARTIAL / 7 OPEN**. The open rows cover unported demo
  request/duplicate-retirement/visitor-parser behavior, not this keeper slice.
- `round5-lessons-map.json`: additional BACKLOG 4/9 and candidate-adapter controls.
- `round5-mapping-summary.json`: exact external-source SHA-256s, source-ID set
  check and **111 unique resolved Rust test references**. `round5-map.rb` is a
  read-only text inventory, never a Python loader or test runner.
- `round5-budget-requests.json`: exact L1 policy requests with provenance.

`MAPPED` means the named behavioral assertion is represented in Rust fake/inert
fixture tests, **not** physical parity, live native IO, default registration,
mandatory CI inclusion, retirement approval or release qualification.

## Current lesson coverage

| Requirement | Rust regression anchors |
|---|---|
| Owner-ruled 20-minute minimum without operational literals | `harness_budgets::owner_ruled_floor_is_registry_bound_even_when_requested_or_injected_floor_is_shorter`; `soak_driver::registered_driver_enforces_owner_floor_and_blocks_missing_registry_before_effects` |
| Strict recovered restart containment; exact HTTP 503 + one of three codes | Existing `soak::owner_ruled_restart_unavailable_requires_exact_code_and_recovered_containment`, `restart_window_edges_counter_precedence_and_cli_receipt_are_exact`, scheduler actual-window test retained unchanged |
| Exact `chat_switching_runs`; submission HTTP/code preservation | Existing `http::owner_ruled_send_refusal_keeps_http_and_safe_code_including_switching`, `send_503_switching_and_event_switching_are_explicit_not_generic_503` retained unchanged |
| Expired owner session: one native re-open, one total deadline | `native_session::expired_owner_session_renews_via_native_reopen_exact_requests_and_receipt`, `second_refusal_is_not_a_renewal_loop_and_other_failures_never_reopen`, `keeper_check_shares_one_renewal_allowance_and_one_observation_deadline` |
| No client CF-* headers | Both `site` integration tests; rejection occurs before transport, including casing/CRLF/credential cases |
| Member silence alerts, no member restart | `keeper::member_silence_is_alerted_while_owner_serves_never_restarts_member`, `member_observation_continues_when_owner_check_fails`; both `member_watch` tests parse actual authority clock fields |
| Bounded Start only for failed rollover + exact code + proven cleanup | `keeper::failed_rollover_exact_code_and_explicit_cleanup_proof_only`, `recovery_rechecks_swarm_rollover_and_cleanup_before_reservation`, `durable_reservation_readback_required_and_ambiguous_start_never_replayed`, `auto_start_never_overrides_an_operator_stop_even_after_qualifying_failure` |
| Candidate/process/resource binding and private session readback | Native tests reject candidate/process/hash/port/stale-control changes, changed observation after HTTP, bad form/cookie/redirect/store; composition combines renewal, member alerts and durable recovery |

The pure Python-style `stopped` policy classification remains tested, but it does
not authorize automatic Start: the executable keeper separately requires the
owner-approved failed-rollover case on both initial and fresh observations.
S14 resource ownership and still-open native requirements are recorded in
[S14-SESSION-SEAM.md](S14-SESSION-SEAM.md). S15's original regression is not
weakened, deleted or bypassed.
