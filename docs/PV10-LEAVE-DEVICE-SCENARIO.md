<!-- SPDX-License-Identifier: AGPL-3.0-only -->
# PV10 leave / re-join: device scenario and judge clauses (Wave-2 round)

Two Macs: owner (swarm serving a prepared model, relay up) and the laptop member joined at generation `g`. The scenario
is the leave step of the A13 app-flow gate (`runs/v2/lane-a13-appflow-20261021`), widened from "smooth leave with nothing
running" to the three things PV10 changed. Everything is read from the apps' own local APIs; nothing is killed or quit.

## Scenario (physical)

| # | step | action | what the receipt records (raw fields) |
|---|---|---|---|
| L0 | baseline | owner `/api/status` and member `/api/status` | member `node_id`, `generation = g`, `authority.membership_state = current`, acquisition `prepared`, `downloaded_bytes` D0; owner `authority.members[node].leave = null` |
| L1 | smooth leave with a request running | start one long chat (>= 200 new tokens) through the owner; when `owner_run`/chat shows the request decoding, member `POST /api/leave {mode: smooth, confirm: true}` | poll member `leave` until `state = left` (<= 180 s): `mode`, `fallback`, `handoff`, `handoff_decisions` (ids to decisions), `steps[]` (names, ok, codes), seconds; owner `authority.members[node].leave` sampled every 2 s: sequence of `kind` (`leaving` then `left_smoothly`), `drained`, `signed`, `lifecycle_state`, `generation`; the chat's terminal (`completed` or an honest typed terminal) and its token indexes |
| L2 | re-join after the smooth leave | owner opens a window; member `/api/join/window`; owner approves; member poll | `joined`; member `node_id` unchanged; `generation` = G1 > g + 1 (the owner self-revoked g); `membership_state = current`; `leave = null`; standing consent `granted`; acquisition `prepared` at once and `downloaded_bytes` still D0 (no re-download) |
| L3 | chat after the re-join | one short chat | terminal `completed`, route ready |
| L4 | leave now | member `POST /api/leave {mode: instant, confirm: true}` | member `leave.state = left`, `mode = instant`, no `announce_withdrawal` step; owner member row sampled every 5 s up to 120 s: `leave.kind` becomes `left_suddenly` (not before ~60 s of silence), `lifecycle_state` still current, never `left_smoothly`, `signed = false` |
| L5 | the swarm adapts | one chat while the member is gone | honest terminal within the normal budget (completed on the owner alone, or a typed refusal such as `route_not_ready`); no hang, no `request_terminal_blocked_*` |
| L6 | re-join after leave now | as L2 | `joined`, same `node_id`, generation > the one at L4, cache reused (`downloaded_bytes` unchanged) |

Order in the gate: `member_leave_inflight` (L0-L1) -> `member_rejoin` (L2) -> `chat_after_rejoin` (L3) -> `member_leave_now` (L4-L5) ->
`member_rejoin_after_now` (L6), then the existing `demo_stop` onwards. The member must be a **target** with the candidate that
contains `lane/rel2-leave` installed on both Macs.

## Judge clauses (recomputed from raw receipt fields, in the style of `judge.py`)

| clause | PASS when |
|---|---|
| `leave_smooth_signed` | L1: member `leave.state = left`, `mode = smooth`; owner row reached `kind = left_smoothly` with `signed = true`, `lifecycle_state = STOPPED`, `generation = g + 1`; the `announce_withdrawal` step is `ok`; `fallback` is `null` (a `stall`/`cap` fallback with `drained = false` is `PASS_OPEN`, not PASS) |
| `leave_handoff_signal` | L1 had a request running: `handoff_decisions` names it with one of `a7_kv_standby`, `a6_replay`, `not_in_flight`, `loss_detection` (anything else FAILs); the owner row passed through `kind = leaving` before `left_smoothly` (planning was received); if the decision is `a7_kv_standby` or `a6_replay` the chat's tokens are in order with no duplicate index; the chat reached a terminal (never stuck) |
| `leave_bounded` | smooth leave <= 130 s end to end; the `handoff` step took <= 5 s + one poll (owner ack bound) |
| `leave_now_suddenly` | L4: no `announce_withdrawal` step; owner row `kind = left_suddenly`, `signed = false`, within 120 s and not earlier than 55 s; the row never showed `left_smoothly` or `leaving` |
| `no_false_smooth` | across the whole run the owner never showed `left_smoothly` for a member whose `leave.mode` was `instant` (and never `left_suddenly` for the smooth one) |
| `rejoin_same_identity_new_generation` | L2 and L6: same `node_id`, new generation strictly greater than the previous, `membership_state = current`, standing consent `granted`, member `leave = null` |
| `rejoin_cache_reused` | L2 and L6: acquisition `prepared` and `downloaded_bytes` equal to L0 (no re-download), re-join <= the existing `rejoin` budget |
| `swarm_adapts_after_leave_now` | L5: terminal reached within the normal chat budget with a typed outcome; no forbidden codes |
| existing clauses | `flow_no_forbidden_codes`, `logs_clean` (member and owner logs: no traceback, no `owner_notice_failed` on the smooth leave), `leave_rejoin` (now implied by the above) |

## Driver change (describe only; the gate is not edited here)

* `member_leave` becomes `member_leave_inflight`: start the long chat first (reuse the existing chat helper with a higher token cap),
  wait for the request to show as running, then `POST /api/leave {mode: smooth}`. While polling the member `leave` block also
  poll the owner `/api/status` `authority.members[]` (the member's row) and keep the ordered list of distinct `leave.kind` values
  with their timestamps; record `leave.handoff_decisions` and `steps`.
* `member_rejoin` additionally records `node_id`, `generation` before and after, `downloaded_bytes` before and after, and
  asserts `leave == null` afterwards. It still calls `acquire` only when the acquisition is not `prepared` (it now should be).
* new `member_leave_now`: `POST /api/leave {mode: instant}`; poll the owner row every 5 s for up to 120 s for `left_suddenly`;
  then run one chat (L5) and a second `member_rejoin` (L6).
* `--reset-member` keeps using `instant`; its wait on `demo_target.state in (not_joined, idle)` still holds, and the member's
  `authority.membership_state` reads `left` afterwards (it was `revoked`/`current` before).
* `fake_app.py`: add `leave.handoff_decisions`, the `handing_off` state, the owner member row `leave` object (kinds above, with
  the 60 s silence rule on the fake clock), self-revoke on the smooth final (generation + 1, `STOPPED`), and a re-join that keeps
  `node_id` and `downloaded_bytes`. Add faults `leave_no_final` (smooth leave without the signed final: expected `left_suddenly`
  after a planned leave) and `rejoin_new_node` (re-join with a new `node_id`: expected FAIL `rejoin_same_identity_new_generation`).
* `judge.py`: the clauses above; `test_judge.py`: one green receipt plus a negative per clause (false smooth, handoff decision outside
  the closed set, missing owner `leaving` step, cache re-download, same generation).

## What this round does not prove

The owner-side A7/A6 choice for a leaving member is tested deterministically against the real router entry and A7 harness; on the
two Macs it is live only where the owner process has a router attached to the app (`app.leave_handoff`). Until the owner app wires
one, a planning notice is answered `loss_detection` for every request (the member keeps draining within its bound), and the
`leave_handoff_signal` clause records that honestly (`PASS_OPEN` when every decision is `loss_detection`).
