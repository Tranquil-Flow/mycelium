# Directed-cycle search on synthetic asymmetric costs

## Scope

This note records a deterministic, synthetic, offline qualification of the
three-node directed-cycle search. It uses no device, network probe, transport,
Router process, model artifact, or `link_state.v1` record. The observations
below are fixture inputs, not host measurements.

The bounded result is software behavior only. Physical-host behavior,
multi-device execution, token transport, and any readiness state remain
untested and unchanged.

## Method

The experiment fixes `node-a` as the cycle entry. An edge returning to
`node-a` is therefore the closing edge; every other selected edge is a forward
edge. The target-model shape is GPT-2/DialoGPT-small width 768 at two bytes per
element. Production `ModelIdentity.activation_bytes(1)` returns 1,536 bytes,
while the synthetic token envelope is 9 bytes.

The test delegates scoring and search to production functions:

- `phase_edge_costs` calls `transfer_time_ms` for both payload sizes under the
  default `PlanningPolicy`;
- `cycle_cost` scores the naive and comparison orders, including closure;
- `search_cycle` selects the strategy and exact order;
- `open_cycle` emits the explicit final-to-first loopback.

The test does not reproduce transfer-time or cycle-total arithmetic. Jitter,
loss, and geolocation floor are zero in every fixture edge so the controlled
variables are directed RTT and bandwidth.

## Asymmetric fixture

| Directed edge | Synthetic RTT (ms) | Synthetic bandwidth (B/s) | Activation cost (ms) | Token-envelope cost (ms) |
| --- | ---: | ---: | ---: | ---: |
| `node-a → node-b` | 8 | 2,000,000 | 4.768000 | 4.004500 |
| `node-b → node-a` | 30 | 100,000,000 | 15.015360 | 15.000090 |
| `node-a → node-c` | 12 | 100,000,000 | 6.015360 | 6.000090 |
| `node-c → node-a` | 50 | 1,000,000 | 26.536000 | 25.009000 |
| `node-b → node-c` | 40 | 1,000,000 | 21.536000 | 20.009000 |
| `node-c → node-b` | 4 | 100,000,000 | 2.015360 | 2.000090 |

For the fixed opening, the two forward edges use the 1,536-byte activation
column and the final edge returning to `node-a` uses the 9-byte token-envelope
column.

## Exact result and naive delta

| Scoring model | Selected order | Total cost (ms) |
| --- | --- | ---: |
| Directed, payload-aware exact search | `node-a → node-c → node-b` | 23.030810 |
| Directed, payload-aware naive node-id order | `node-a → node-b → node-c` | 51.313000 |

The exact search beats naive ordering by **28.282190 ms**, or 55.117007% of
the naive total. Input node reordering produces the same result. With three
nodes, production selects `mode="exact_enumeration"`,
`globally_exact=true`, and `explored_candidates=2`; the count is the two
permutations remaining after the canonical entry node is fixed.

Opening the selected cycle at `node-a` yields forward order
`node-a → node-c → node-b` and explicit loopback
`node-b → node-a`. The loopback assertion checks metadata and its 9-byte
scored payload only; no Router execution occurs.

## Symmetric-link simplification

The comparison removes link directionality without removing the distinct
payload roles. For each unordered node pair and each payload size separately,
it averages the two production-scored directional costs and applies that mean
in both directions. The closing edge still uses the 9-byte score.

Under that simplification, exact search selects naive node-id order
`node-a → node-b → node-c` at 37.171905 ms. The reverse order
`node-a → node-c → node-b` costs 37.553655 ms. The selected order therefore
**does change** between directed asymmetric scoring and the symmetric-link
simplification.

## Controlled null result

The null fixture gives all six directions RTT 10 ms and bandwidth
10,000,000 B/s, again with zero jitter, loss, and geolocation floor.
Production scoring returns 5.153600 ms for a 1,536-byte activation and
5.000900 ms for a 9-byte envelope on every edge.

Both possible anchored cycles cost exactly 15.308100 ms. Optimal and naive
therefore tie with a **0.000000 ms delta**, and the stable tie-break selects
`node-a → node-b → node-c`. This is an intentional null result: when the
synthetic links carry no directional distinction, exact search offers no cost
benefit.

## Rejection and RED-to-GREEN record

A sparse scored fixture containing only `node-a → node-b` and
`node-b → node-c` has no closing edge and no alternative complete cycle.
`cycle_cost` returns infinity and `search_cycle` rejects it with
`no feasible directed cycle`.

The initial RED used a mildly asymmetric complete fixture. Production exact
search honestly retained naive order, so the required order-difference
assertion failed. This was an insufficient experimental contrast, not a source
defect. The final controlled asymmetric fixture above made that assertion
GREEN. Existing focused tests remain responsible for arbitrary-index rotation
coverage; this offline test additionally preserves the selected cycle's
explicit `(last, first)` loopback.

## Reproduce

Run under `umask 022`:

```text
/opt/homebrew/bin/python3.14 -m pytest -q -p no:cacheprovider \
  test_layer_planner_v1_cycle_exact.py \
  test_layer_planner_v1_cycle_scaled.py \
  tests/integration/test_cycle_search_offline.py
```
