# Recorded latency profile importers (v1 and v2)

## V2: current two-Mac device receipts

`latency-profiles --device-v2 SAVED_DEVICE_ROOT NEW_OUTPUT.json` imports immediate
`attempt<digits><optional lowercase suffix>` directories only. It never traverses
unpack trees, executes an application, contacts a device or reads live state.
V1 API/data and all simulation traces stay unchanged; V2 is an additive data
format, not an automatically admitted simulator/calibration profile.

Inputs are `RESULT.json`, `events.jsonl`, `stdout.log`, `soak-events.jsonl`,
`soak-stdout.log`, `SOAK-RESULT.json`, `probe-stop-inflight.log`, the older
`probe-stop-duration-1.log`, and `quit-{owner,laptop}.json`. Events take precedence over stdout; summary chat
arrays are fallback only. Rollover start/end pairs and compiler run IDs dedupe
repeated status/summary observations. Every selected file has a byte count and
SHA-256 of the exact parsed bytes (system `/usr/bin/shasum`, stdin only). Missing
files and malformed historical Quit receipts have explicit dispositions. A bad
structured event, invalid digest, negative/nonfinite timing or symlink refuses
import. Unknown fields use serde's ignored-field path, not a catch-all Value.
No cookies, grants, run IDs, prompt/token text, top-process commands or error
payloads are emitted. Sources are relative file/line or JSON pointers.

Profiles separate each attempt, phase and owner/laptop/route role. They retain
raw numeric measurements, method, outcome, source/executable identities, model
chooser ID, recorded macOS and same-event load vectors. Separate timestamped
host observations retain context without pretending an earlier load is a
per-sample measurement. Hardware stays null when unrecorded; `a9-route` is a
chooser ID, not an independently verified checkpoint/tensor identity. Route-wide
measurements are not assigned to an invented single machine.

- `compile`: recorded `owner_run.build_seconds`, once per run, not duration of
  every repeated status. `publish` and `start`: observed polling transitions.
- `stop`: stop_sent to observed stopped; cleanup_unproven remains a failed
  outcome, never native-close proof. Quit is AppleEvent-to-observed-exit.
- `rollover`: first observed unavailable→serving with recorded upper bound.
- `ttft`/`decode`: client polling metrics, NOT native per-token timings. Decode
  unit is **tokens/s**, not milliseconds. Failed totals remain diagnostic data.
- Native `close` and isolated `relay_rtt` remain empty/BLOCKED when absent;
  status HTTP and SSH transport time are not relabeled as relay RTT.

Nearest-rank descriptive p50 uses the existing >=2 exact-metric-sample policy.
Polling/composite phase boundaries do not receive native phase percentiles.
The existing minimum of 100 independent comparable exact samples is not a tail
confidence guarantee. These retained device runs do not establish independent
trials (repeated status polling is not independent replication), so V2 never
promotes their p99 to calibration: null p99 plus explicit BLOCKED reasons.
It does not guess hardware, missing load, model identity or an independence
attestation. Missing measurements require owner/fleet follow-up. A future
qualified measurement protocol must be separately reviewed; this importer is
not a switch to waive calibration. 2x/3x variants scale only millisecond values,
retain provenance and are labelled SYNTHETIC_NOT_MEASURED. Count/statistical
rank constants are the pre-existing descriptive method, not runtime budgets;
this code introduces no timeout, retention or allocation policy.

Evidence, source inventory and actual imported results are retained externally
under `runs/v2/v02/FOUNDATION-EXIT-evidence/`; the foundation report seals their
identities. No raw device receipts or multi-megabyte duplicate synthetic output
are embedded in the product repository.

## V1 (retained replay compatibility)


Crate `latency-profiles`; no device launches or historical application execution.
`Bundle`, `Profile`, `Sample`, `Statistics` in `src/` define serde schema version 1.
The top-level object is `{schema_version, files, profiles}`. Each profile has a
stable id, phase, unit (`ms` or `bytes`), origin, multiplier, model/build/device,
exact-vs-censored flag, raw samples and computed statistics. Samples retain value,
source file/line or JSON pointer, measurement method and nullable host load.
Unknown device hardware and missing load remain explicit, never inferred from
battery, current host load or a different attempt. Builds are separated by the
attempt RESULT executable digest. `a9-route` is the saved model id, not a claim of
an independently pinned tensor manifest. Metadata is cohort attribution from the
saved RESULT, not per-sample executable attestation.

## Privacy allowlist

Only `attempt6`, `attempt7`, `attempt8`, and only `RESULT.json`, `SOAK-RESULT.json`,
`events.jsonl`, `soak-events.jsonl` are opened. No recursive discovery. The exact
raw-input DTO allowlist is:

- event, elapsed_s, latency_s, ttft_s, total_s;
- owner_run.step (nullable), owner_run.state, for phase-transition matching only;
- member.status_latency_s; member.run_model.selected;
- member_join.run_model.selected (only `a9-route` retained, otherwise UNKNOWN);
- acquisition.downloaded_bytes, acquisition.total_bytes;
- summary.mailbox_bytes;
- exe_sha256 (validated 64 hex digits), chats[].ttft_s/total_s.

Everything else is skipped by serde `IgnoredAny`, not parsed into a catch-all
Value, logged, retained or reserialized. In particular no grants, joins, keys,
cookies, token payloads, window.json or native-control.json are copied/opened.
Parse failures expose a fixed safe code, not serde's input-value error text.
Tests place canaries in nested sensitive fields and malformed excluded files.
File inventory records missing result/event files rather than pretending they ran.

Events are primary; RESULT chats are fallback only when that event stream has no
TTFT samples, to avoid counting the same observation twice. Failed chat totals
remain diagnostic data and are not silently discarded. Status elapsed is **not**
relay RTT. Compile/publish/start are polling-transition deltas, explicitly
interval-censored: they cannot yield native phase percentiles. Start requires
confirmed serving state, not merely entering the serving step. Attempt7 has no
completed start sample.

## Statistics and reported observations

Nearest-rank descriptive p50 requires at least two comparable exact-metric samples.
An empirical p99 requires at least 100, exact-metric boundaries, finite nonnegative
values and recorded load for every sample. This minimum is not a statistical
confidence guarantee or proof of production tail stability. Fewer samples,
interval censoring or missing load yields `p99_ms: null`, `status: BLOCKED`.
Sparse observations are not pooled across builds to manufacture support.

**The saved corpus has zero qualified measured p99s.** Even attempt6's 132 owner
status samples have no recorded host load. TTFT is client token-chunk polling
with 250 ms resolution, not native decode timing. Missing stop/close/relay samples
stay empty. The L13 report separately supplies one approximate 33 s route-stop
observation at approximate load17 and one approximate 160 s composite startup
observation with no load; their origin is `REPORTED_APPROXIMATE`, never p99.
The report's 160 s boundary (first model phase to entering serving) differs from
this importer's confirmed-serving boundary (192.704/212.575 s in attempts6/8).

L13 numbers are consumed through L1 `ObservedStop` and `ObservedStart`, with
`L13-ROLLSTOP.md:61-64` provenance. L10's `CorrectedOwnerShare` is retained as
`PLANNER_ESTIMATE_NOT_DEVICE` from `L10-SPLIT.md:31-33`, not physical allocation.
L12 is inventoried as `POLICY_ONLY_NO_LATENCY_SAMPLES` (`L12-WALL.md:22-29`); its
600/480/570 s limits are policy, not measurements. The report projection is
explicitly curated from these named report rows, not a generic Markdown scraper.

Every millisecond profile has labelled **SYNTHETIC 2x and 3x** variants retaining
sample provenance. Scaled observations do not become measured samples, and
`qualified_p99()` never admits synthetic/reported values. Bytes are not scaled.
Do not treat a synthetic close-only control as full loaded-chain qualification.

After host-priority admission:

```text
cargo run --offline -p latency-profiles -j 2 -- SAVED_DEVICE_ROOT NEW_OUTPUT.json
```

Output is create-new, never silent overwrite. `data/v014-v1.json` is the retained
allowlist-only import. It contains 106 profiles (including empty mandatory phases
and synthetic variants); empty phases remain visible blockers. This is data, not
a live-device or release-ready claim.
