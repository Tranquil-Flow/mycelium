<!-- SPDX-License-Identifier: AGPL-3.0-only -->
# Device-calibration import through the reviewed qualification path (round 4)

Input: `evidence/device-calib/device-root/attempt1..7` on branch `v02/device-calib` @ `5e0fdd7` (read-only; not copied here — every file is
bound by sha256 in `trial-declaration.json`, which is itself bound by the digest passed on the command line).
Command: `latency-profiles --device-qualify ROOT trial-declaration.json <sha256 of the declaration> OUT.json [--event-level-only]`
Declaration sha256: `d8f042df41f07a22cf703d302493e8020ae5c141e419d30fe2c1ea5ffd5afc07`. **The declaration is a proposal by this lane; the owner must review it** (reviewer field says so).

## Qualifying cells (rule unchanged: n >= 100 valid, exact, load-known, per phase/model/build/device pair; nearest rank)
- **1 of 14 cells: `chat_total`** — 112 valid exact load-known (3 failed chats counted separately, 0 load-unknown), p99 **8762.5 ms** (reference only; build f4e366e9, model a9-route, owner Mac16,8 macOS 26.5.1 + laptop Mac16,1 macOS 15.6.1). With `--event-level-only` it is **0 of 14** (chat_total ends at a <=250 ms polled terminal observation).
- Not qualifying, each with a recorded reason: ttft 112 / decode 112 (250 ms polling, never exact); relay_https_probe 109 (public HTTPS GET, not the required relay RTT phase); relay_rtt 0 (no carrier probe recorded: BLOCKED `no_carrier_rtt_samples`); close 0 (BLOCKED `no_close_samples`); compile 10 (all cache hits 6-7 s, 0 cold); publish 9, start 8, stop 10, quit 1 (polled/composite, n<100); rollover, restart (no ingestion producer).

## Descriptive 2x comparison (UNQUALIFIED: n<100, polled boundaries; nearest-rank p99 over all observed samples; no budget changed)
| phase | n | descriptive p99 | 2x | L1 budget | verdict |
|---|---|---|---|---|---|
| compile[cache_hit] | 10 | 7.0 s | 13.9 s | Compile 3600 s | OK |
| publish | 9 | 61.9 s | 123.8 s | Publish 600 s | OK |
| start | 8 | 309.3 s | 618.7 s | Descriptors 180 s | **FAILS** |
| stop | 10 | 170.7 s | 341.4 s | EffectiveStopAllowance (lane-derived 110 s) 110 s | **FAILS** |

These match the calibration lane's figures (compile 13.9 s, publish 124 s, start 619 s, stop 341 s). Budgets read from `crates/budgets/src/table.rs`: Descriptors 180000 ms, Compile 3600000, Publish 600000; the 110 s stop allowance is the lane's derivation (EffectiveStopAllowance is derived), not re-derived here.

## Findings for the owner
1. **start 2x p99 (619 s) vs Descriptors step budget 180 s FAILS; stop 2x (341 s) vs 110 s FAILS** (stop is bimodal: ~3.5 s vs 160-171 s). Measured under owner load 2.9-18.4 from parallel builds; descriptive and unqualified. Caveat: `start` here is a composite (Start POST to first serving, ~1.6 s polling) compared against the single-step Descriptors budget; the whole-sequence budget (StartupWaitSum) is a different quantity. Budget decisions are the owner's; nothing was changed.
2. **chat_total qualifies only under a policy choice:** it is the client's own monotonic elapsed time, but the terminal is observed by a <=250 ms poll. `--event-level-only` removes it. Owner to rule which reading L1.3 requires.
3. **Hardware identity is declared, not recorded:** the events carry macOS versions but no model identifier. The importer accepts a recorded `summary.hardware_model` (must agree with the declaration; provenance `recorded_and_declared`); this evidence has only `declared_only` (basis: the lane README).
4. **Independence is declared by the reviewer, not tested.** Consecutive 16 s-spaced chats inside a window share host load/caches; no autocorrelation test exists. This is stated in the output (`independence`).
5. Still no sample for relay carrier RTT, close, cold compile, rollover, restart: L1.3/L2.3 stay BLOCKED for those phases. L1.3 as a whole stays BLOCKED (only one phase has a qualified p99, and chat phases have no mapped L1 budget).
