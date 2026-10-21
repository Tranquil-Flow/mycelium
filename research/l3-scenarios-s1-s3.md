# L3 scenarios S1-S3 on the shipped request plane (packet `m1-l3-scenarios-s1-s3`)

Lane: `v2/m1-l3-scenarios-s1-s3-r1`, base `e45719ccba97a431241eab9e525b6999535fd131`.
This is **capability, not the M2 fix**: no worker replacement was implemented, `mycelium_a13_macos/**`
was not touched, and S4-S7 are not claimed. The deliverable is the ability to run PLAN v2 §2.3's three
M1-demo scenarios S1/S2/S3 on the shipped path, one command each, one JSON evidence record each, with
the verdict recorded honestly.

## 1. What was read first, and what was reused

Read, in this order:

1. PLAN v2 §2.1-§2.3 (`~/Documents/mycelium-core-program/PLAN.md`) - the L3 definition
   ("real packaged app or real `a13_macos` managed worker, real 0.5B model, real iroh between two
   processes, fault proxy, real process kills"), the quiet-host guard (load1 ≤ 8), and the S1-S7 table.
2. The sibling lane's landing `f7f4665` (ancestor of the base commit): `mycelium_testbed/real_worker_kill.py`
   and `tests/l3/` - **reused as the pattern, not rebuilt**: the "capability, not fix" framing, one
   evidence record per scenario, `quiet_host_guard`, the explicit real/simulated listing, the
   preconditions-asserted-before-verdict discipline, and `tests/l3/conftest.py`'s `sys.path` seam.
3. `tests/e2e_request_iroh/harness.py` (1033 lines) - the **shipped request plane** this capability
   drives: the real ASGI gateway application, the real `RouterSessionBackend`, two real production
   `Router`s, two real native `mycelium-iroh-sidecar` processes, a real `IrohTransport` pair, and the
   `CompleteRequestEvidence` / `CancellationEvidence` shapes.
4. `mycelium_request_gateway/{service,contracts,asgi,backend,auth}.py` - admission, the event/kinds
   contract (`accepted`/`token`/`completed`/`cancelled`/`failed`), `subscribe`, `cancel`, and the
   gateway knobs (`max_buffered_events`, `max_sessions`, `max_concurrent_requests`,
   `max_pending_requests`).
5. `mycelium_testbed/{iroh_e2e,real_worker_kill}.py`, root `conftest.py`, `pytest.ini` - the `_patched`
   substitution seam, `_product_package_digest`, and the ladder registration rules.
6. `mycelium_router/mlx_runtime.py`, `mycelium_live/route.py`, `scripts/build_qwen_live_route.py`,
   `two_process_inference_qualification.py` - the real-model seam (see §4).

Reused, not rebuilt:

| Reused | From |
|---|---|
| The real sidecar pair, the real Router pair, the real `IrohTransport` pair, the synthetic qualification fixture, the ASGI surface | `tests/e2e_request_iroh/harness.py` (`_Topology`, `_synthetic_qualification`, `_QualificationSource`, `_DistributedRouterPort`, `_DeterministicCodec`, `_post`, `BINARY`, `PROMPT`, `AUTH_TOKEN`) |
| The executor substitution seam | `mycelium_testbed/iroh_e2e.py`'s `_patched(module, attribute, replacement)` pattern (also used for the stall-gated runtime subclass) |
| Product digest for the evidence record | `mycelium_testbed/iroh_e2e.py::_product_package_digest` |
| `sys.path` seam for a test module outside `tests/a13_macos`/`tests/e2e_request_iroh` | `tests/l3/conftest.py` (added by the sibling lane) |
| The real native sidecar binary | copied from `mycelium-core-pilot/.build-native/release/mycelium-iroh-sidecar` to `native/iroh_transport/target/debug/` (the path `harness.BINARY` requires); `native/iroh_transport/.gitignore` already ignores `/target/`, so the tree stays clean. `tests/e2e_request_iroh/conftest.py` rebuilds it offline (`cargo build --locked`, `CARGO_NET_OFFLINE=true`) with no download |

Built (the new capability): `mycelium_testbed/l3_scenarios.py` (~1400 lines) -
`ScenarioStack` (the shipped plane, re-composed for N requests), `RequestRun` (one request's measured
life, submission → terminal), `StallGate` + `GatedDeterministicStageRuntime` (a re-armable mid-stream
interlock so S3's cancellations are issued while a decode of *that* request is genuinely in flight),
`local_model_facts`, `repository_facts`, `quiet_host_guard`, `concurrency_window`, the three verdict
evaluators, `write_evidence`, and a CLI. Plus `tests/l3/test_scenarios_s1_s3.py` (4 tests, `emulation`).

Two harness properties are single-request by construction and are therefore re-composed rather than
"reused": `_FixedRequestId` raises on the second request, and `_service_stack` leaves
`max_concurrent_requests` at its default of **4**, which cannot express S2's eight. The product classes
are imported and the counts are parameters; both facts are recorded in the evidence
(`infrastructure.gateway_knobs`).

## 2. What is REAL in a scenario run

* `mycelium_request_gateway.asgi.RequestGatewayASGIApplication`, driven through a real ASGI
  scope/receive/send cycle (real 202 admission, real `409 route_dropped` for an unqualified POST).
* `mycelium_request_gateway.service.RequestGatewayService`: admission, per-request executor thread,
  bounded replay, acknowledgement cursors, cancellation latch, terminal publication.
* `mycelium_request_gateway.backend.RouterSessionBackend` and two production
  `mycelium_router.router.Router`s: admission, decode scheduling, and the request-scoped
  capacity/KV/path release that a cancellation has to prove.
* `mycelium_router.transports.iroh.IrohTransport` over **two real native sidecar processes**
  (recorded PIDs, verified as native children of another process; the request really crosses a process
  boundary as wire frames).
* The client stream itself: token events with indexes, acks, `completed` / `cancelled` terminals.
* The evidence record's own identity: commit, branch, tracked modifications, product package digest,
  sidecar binary digest, gateway knobs, quiet-host load.

## 3. What is SIMULATED (labelled in every record)

1. **The model.** `model.loaded_by_this_capability = false`, with the real snapshot's presence recorded
   as a measurement (`real_0_5b_present_locally: true`, `selected: Qwen/Qwen2.5-0.5B-Instruct`, weights
   bytes and snapshot path recorded). The executor is
   `tests/e2e_request_iroh/harness.py::_DeterministicStageRuntime`: deterministic token ids, no weights,
   no tokenizer. **This is the single biggest difference between this capability and PLAN's L3
   definition.**
2. Route qualification: the in-repo synthetic fixture (`local_evidence_only=true`, `route_ready=false`).
3. `mycelium_router.fakes`: `ManualClock`, `FakeCapacityPort`, `FakeTopologyProvider`,
   `FakeDeviceStateProvider` (device state is a fixture, not a measurement).
4. No fault proxy (`mycelium_testbed/faults.py` untouched by these scenarios): no latency, no loss.
5. One Mac, loopback only: no second device, no cross-host clock offset, no physical boundary.
6. Graded timings are properties of this plane on this host, not of the hardware.

## 4. Verdicts and the exact evidence

Run on commit `e45719ccba97a431241eab9e525b6999535fd131` (product digest
`sha256:19cc93c72fb3ecba01319f9eae42cf835dcb053e64e7c683462e27587d924257`, sidecar
`sha256:9c12c1e0ae1c9be30...`), one command per scenario:

```
cd ~/Documents/playground/mycelium-v2-m1-l3-scenarios
env -u GIT_OBJECT_DIRECTORY -u GIT_ALTERNATE_OBJECT_DIRECTORIES -u PYTHONPATH -u PYTHONHOME \
    -u VIRTUAL_ENV PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. \
    ~/Documents/playground/mycelium-v2-m0-test-ladder/.venv/bin/python \
    -m mycelium_testbed.l3_scenarios S1 --evidence-dir <abs dir>          # S2, S3, or all
```

exit code **0** when every scenario is GREEN and **1** when any is RED - the graded run exited **1**
because S2 was RED (see "run by run" below); the three scenarios take ~17-20 s in total:

| Scenario | Command | Verdict | Counts | Record |
|---|---|---|---|---|
| S1 | `... l3_scenarios S1 --evidence-dir ...` | **GREEN** | 20/20 admitted (202), 20/20 `completed`, 4 tokens each, 0 failed | `runs/v2/l3-scenarios-s1-s3-r1/evidence/S1-sequential-20.json` |
| S2 | `... l3_scenarios S2 --evidence-dir ...` | **GREEN in 4 observed runs, RED in the graded one** | GREEN run: 8/8 admitted, 8/8 `completed`, peak in-flight **8**, 2.59 s with >1 in flight. Graded run: 8/8 admitted, peak in-flight **8**, 6/8 `completed`, **2/8 `failed:backend_failed`** | `.../S2-concurrent-8.json` (the graded RED) and `.../S2-concurrent-8-run3-RED-backend_failed.json` (the same record under an explicit name) |
| S3 | `... l3_scenarios S3 --evidence-dir ...` | **GREEN** | 10/10 cancellations accepted, 10/10 **`cancelled`** terminals with the stall entered on a real decode of that request, 1 client token delivered before each cancel, cancel→terminal 22-57 ms, both ends' paths released, 0 pending deliveries; **the 11th request `completed`** with 4 tokens | `.../S3-cancellations-10.json` |

Measured detail worth naming:

* S1: per-request `seconds_to_terminal` 0.28-0.55 s, tokens `[0,1,2,3]` for all 20.
* S2: submissions 0.0-0.056 s apart (all eight admitted inside 56 ms), terminals 2.29-2.59 s -
  the eight requests really overlapped inside the shipped gateway, and the gate knobs used are recorded
  (`max_concurrent_requests=8`, `max_pending_requests=16`, `max_sessions=32`).
* S3: the stall entered on every iteration (`holds` 1..10, `stall_timeouts` 0); each cancelled request's
  stream stopped at `[0]` - it did **not** complete its four tokens - and the following request streamed
  `[0,1,2,3]` to `completed`. `adapter_cancellation_latch: false` is the harness's own
  `adapter_released` condition (the latch is *released* after cleanup), not a failure.
* pytest wrapper: `tests/l3/test_scenarios_s1_s3.py` - 4 passed in 14.3 s; the tests assert the plane was
  real (native children, `IrohTransport` pair, refused unqualified POST) **before** the verdict, so a RED
  cannot come from a degraded harness.

### The caveat that matters: run by run, including the one RED

Every run of every scenario in this lane, in order, with the load it ran under (S1/S2/S3 in the "graded"
row is the run whose records are in `runs/v2/l3-scenarios-s1-s3-r1/evidence/`):

| # | Commit | load1 (S1/S2/S3) | S1 | S2 | S3 |
|---|---|---|---|---|---|
| 1 | `e45719c` + the three uncommitted files | 34.9 / 31.1 / 29.1 | GREEN | GREEN | GREEN |
| 2 | `3ddfb76`, tree clean at run time | 7.6 / 9.9 / 10.7 | GREEN | GREEN | GREEN |
| 3 (graded) | `3d756ed`, tree clean at run time | 13.3 / 15.1 / 17.6 | GREEN | **RED** | GREEN |
| 4, 5 | `3d756ed` (S2 only, `runs/v2/l3-scenarios-s1-s3-r1/s2-repro/`) | - / 8-15 / - | - | GREEN | - |

**Run 3's S2 is RED and it was not tuned away.** 8/8 requests were admitted and really overlapped
(`peak_in_flight: 8`), 6/8 reached `completed`, and **2/8 terminated `failed` with code
`backend_failed` mid-stream** - `l3-s2-001` after 3 tokens at 3.076 s, `l3-s2-002` after 2 tokens at
4.371 s (record copied to `evidence/S2-concurrent-8-run3-RED-backend_failed.json`). Two further S2 runs
immediately afterwards were GREEN, and its own gap list (`request_N_terminal:'failed':'backend_failed'`)
is what the pytest wrapper asserts, so the scenario is a genuine RED detector rather than a green claim.

Sibling lanes were loading this host during runs 1 and 3. The recorded verdicts are state and
terminal-state facts (a `completed`/`cancelled` terminal, a released path both ends, a refused
unqualified POST) with sub-second stability, not timing flukes - but two things follow from the ledger
and neither is being explained away:

* **S2 is not stable at 8 concurrent requests on a loaded host.** Its failure mode - a `failed`
  terminal at ~3.0 s after 2-3 streamed tokens - matches the harness's own
  `delivery_timeout_seconds=3.0` (`tests/e2e_request_iroh/harness.py:~631`), which is a *harness*
  budget, not a PLAN requirement. Whether the shipped plane or the harness's 3-second delivery window
  is what fails is exactly the question a quiet-host S2 qualification has to answer, and this lane
  deliberately did not raise the timeout to make the scenario pass (§5.8).
* **PLAN v2 §2.1 requires L3 qualification under load1 ≤ 8.** Only S1's run 2 is inside that ceiling;
  S2 and S3 need a quiet re-run before anyone calls them qualified. The guard is recorded, never
  enforced, because a skip would hide exactly the finding the run exists to produce; the load travels
  in each record so a reader can weigh it.



## 5. Seams and findings recorded while building this (not fixed here)

1. **The harness cannot express more than one request.** `tests/e2e_request_iroh/harness.py:~390`
   `_FixedRequestId.__call__` raises `AssertionError("qualification harness admitted more than one
   request")`, and `harness._service_stack` (~683) hard-codes `request_id_source=_FixedRequestId()` with
   `max_sessions=2`. This capability re-composes those two arguments; if S1-S3 are to be a standard
   qualification, the harness itself needs a multi-request entry point.
2. **The gateway's default concurrency is 4.** `RequestGatewayService.__init__` defaults
   `max_concurrent_requests=4` (`mycelium_request_gateway/service.py:~292`). S2's eight concurrent
   requests only exist because this capability raises the knob to 8; whoever runs the physical round must
   set it deliberately, or S2 measures four.
3. **No shipped loader on this plane.** The real 0.5B weights are on disk
   (`~/.cache/huggingface/hub/models--Qwen--Qwen2.5-0.5B-Instruct/snapshots/*/model.safetensors`) but the
   execution seam under the Router is `mycelium_router.mlx_runtime.MLXRuntimePort`
   (`mycelium_router/mlx_runtime.py:408`), which requires a per-host route bundle (stage packs, load
   proofs, assignment) produced by `scripts/build_qwen_live_route.py`; `mycelium_live/route.py:1114`
   `PhysicalLiveRoute` is the real route that consumes one. Building that bundle is M1 feature work
   outside this packet's ownership, so the executor stayed deterministic and the model stayed at
   `loaded: false`. Nothing was downloaded (the packet's prohibition), and the MLX interpreter *is*
   present in the pinned venv (`mlx 0.31.1`), so the only missing piece is the bundle.
4. **`path_id` naming is per-request but not self-describing** (`path-1`, `path-2`, ... from the Router's
   own `SequenceIdSource`), so the cleanup facts are only meaningful paired with the request id they were
   read for - which is how they are recorded.
5. **The session token is a live credential** for the loopback plane; the evidence stores its SHA-256
   digest and redacts the value (`session_token_digest`).
6. **`git worktree add -b <branch> <path> <repo-path>`** as written in the packet fails on git 2.50.1 with
   `fatal: invalid reference: <repo-path>` (the third positional is treated as a commit-ish). The
   worktree was created with the explicit base SHA: `git worktree add -b <branch> <path> e45719c...`.
7. **The `emulation` property holds.** `pytest -m "not scaffold_red and not flaky and not emulation and
   not physical" tests/l3/` → `no tests collected (6 deselected)`; `pytest -m emulation tests/l3/` collects
   the sibling's 2 plus these 4. The new module carries `pytestmark = pytest.mark.emulation`, and its name
   is deliberately outside the root `conftest.py`'s `_RISKY_NAME` pattern, so the ladder never has to
   classify an unregistered higher-level scenario.
8. **S2's eight concurrent requests sit on the harness's 3-second native delivery budget.**
   `tests/e2e_request_iroh/harness.py:~631` builds both transports with `delivery_timeout_seconds=3.0`,
   and the one RED S2 run failed 2/8 requests at 3.076 s / 4.371 s with `backend_failed` after 2-3
   tokens - the shape of a delivery window expiring under a loaded host, not of a router or gateway
   decision. The scenario keeps the harness's number **on purpose**: raising it would make S2 pass
   without answering whether the shipped plane can hold eight concurrent streams inside the budget it
   actually ships with. A quiet-host S2 run is the next step, and it belongs to whoever owns S2's
   qualification, not to this capability lane.
9. **The cancellation latch reads as released, not held.** `adapter_cancellation_latch: false` in the
   cleanup facts is the harness's own `adapter_released` condition (`REQUEST_ID not in
   backend._cancelled`) - i.e. the adapter released the request after cleanup. It is not a failure, and
   the field name is this capability's, not the product's.

## 6. Shipped-path elements NOT yet covered

1. **The real 0.5B model**: real load, real tokenizer, real tokens, real TTFT/TPOT. S1's "useful model"
   wording is therefore **not** met yet - the request plane is real, the model is not (§5.3).
2. **Real iroh between two machines**: both sidecars here are local processes of one host.
3. **The a13 managed-worker plane**: `TargetApplication.start_work` → `ManagedTargetRuntime` →
   `ManagedWorker` → native child. These scenarios drive the *gateway/router* plane. The managed-worker
   plane has its own L3 capability (`mycelium_testbed/real_worker_kill.py`).
4. **S4/S5** (kill the worker, recover within the bound, next request succeeds), **S6** (network change),
   **S7** (100+ mixed requests inside the 1800 s daemon bound).
5. **The fault proxy** (`mycelium_testbed/faults.py`) on these legs: no latency, no loss, no half-close.
6. **Owner→target control path** during a streamed request (the control mailbox / pump step).
7. **In-flight request cut by a real process death** - the other half of the M1 demo statement
   ("in-flight requests end with a named error and a retry button"). S3 cancels a request through the
   shipped cancellation path; it does not kill a process under one.
8. **Two-Mac clock offsets and cross-host timing** (L4).
9. **Quiet-host qualification of S2 and S3**: S1's graded run was inside load1 ≤ 8; S2 (9.9) and S3 (10.7)
   were just outside it, so the capability is here and only S1 is also qualified (§4).

## 7. What I could not determine

* Whether eight concurrent *real-model* requests behave like eight concurrent stub requests: throughput,
  KV pressure and TTFT under the real executor are unmeasured, because no route bundle exists on this host.
* Whether S3's 22-57 ms cancel→terminal holds when the far stage is doing real MLX work rather than a
  deterministic stub.
* Whether the shipped gateway's default `max_concurrent_requests=4` is deliberate policy or an artifact;
  the packet did not say, and the file that owns it is outside this lane.
* Whether any *other* lane is concurrently loading this host (load1 ~30): the guard is recorded, and the
  per-lane attribution is not mine to make.
