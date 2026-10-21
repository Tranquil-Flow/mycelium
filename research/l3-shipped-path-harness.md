# L3 shipped-path harness: kill the REAL managed worker (packet `m1-harness-real-worker-kill`)

Lane: `v2/m1-harness-real-worker-kill-r1`, base `61257b2514ade1c4196fa2ae012ddb2db970756b`.
This is PLAN v2 §2.2's first application of the **fidelity rule** (root cause R1). It is a *harness
capability*, not the M2 fix: no worker replacement was implemented and
`managed_runtime.py:_replace_worker_if_dead` was not touched (it is in the forbidden
`mycelium_a13_macos/**`). A green harness change would have been a failure of the packet.

## 1. The gap, verified by tool (not restated from the packet)

* `grep -rn "a13\|managed_runtime\|ManagedWorker\|TargetApplication" mycelium_testbed/*.py` → **no
  match** in any of the six testbed modules. The testbed has never constructed the shipped managed
  worker; R1 is not merely under-tested, the seam is absent.
* `mycelium_testbed/soak.py::_restart_owner` (~2088) re-subscribes a simulated mailbox
  (`self._subscribe(last_event_id=resumed_from, ...)`).
* `soak.py::_restart_peer` (~2140) calls `session.kill_peer("node-a")` → `SoakSession.kill_peer`
  (~1303) → `sidecar.stop()` on a **`SoakStageRuntime`** (`soak.py:1376`, a
  `iroh_e2e.TracedStageRuntime` delegating to a router runtime port) registered through
  `register_sidecar()`; `sidecar_running()` then reads `sidecar.process.poll()`. So even the "real
  process death" in the soak is a death of whatever object a caller registered, never of
  `ManagedWorker`/its native child, and the recovery path is a topology rebuild — not
  `_replace_worker_if_dead`.

## 2. What I read first (and what I did not)

Read, in this order:

1. `mycelium_a13_macos/managed_runtime.py` in full (`ManagedTargetRuntime.__init__`, `start`,
   `_replace_worker_if_dead`, `_serve`, `close`) — the code the p84 failure lives in.
2. `mycelium_a13_macos/worker.py` (`start`, `_send`/`_commit_command`, `requires_cold_restart`,
   `replacement_refusal`, `process_group_present`, `status`, `close`) — the boundary that decides
   whether a death is replaceable at all.
3. The two existing L1 suites that already call the real `_replace_worker_if_dead`
   (`tests/a13_macos/test_worker_replacement_readiness.py`,
   `test_worker_replacement_authority.py`): they fabricate the death (`old_session.returncode = 143`
   / `runtime.worker is old` with a `FakeConcurrentSession`). That is the fidelity hole in one line.
4. `tests/a13_macos/test_minimal_managed_flow.py` — the closest existing *shipped-path* one-Mac
   integration, reused as the substrate (signed world → offer → `TargetApplication.start_work`).
5. `mycelium_testbed/{soak,emulation,faults,iroh_e2e}.py`, `conftest.py` (ladder markers), `pytest.ini`.

**Not read: Petals `src/petals/client/inference_session.py`, exo `rust/networking/src/swarm.rs`.**
They are not on this host (`find ~ -maxdepth 4 -iname inference_session.py` → no
match; same for `swarm.rs`; both are named only inside PLAN.md). That is also deliberate: what those
two files carry is a *replacement/re-authorisation policy*, and adopting one here would be
designing the fix PLAN assigns to M2 step 2 (`research/worker-replacement.md`). This lane's job is
the substrate that can measure the gap. `fb0314c`'s `_replace_worker_if_dead` was read (see §6).

## 3. What was reused (nothing rebuilt)

| Reused | From |
|---|---|
| Signed TLS world, control mailbox, member authority, staged archive | `tests/a13_macos/test_control_pump.py::_world`, `test_managed_control.py`, `test_managed_staging.py::bundle` |
| The real shipped start path (`TargetApplication.start_work` → `ManagedTargetRuntime` → `ManagedWorker` → real native process → `native_runtime.installed_sidecar`) | `tests/a13_macos/test_minimal_managed_flow.py`'s flow, driven the same way |
| `WorkerLimits`/resource policy/observer shapes | `tests/a13_macos/test_worker.py` (`POLICY`, `SAFE`), `target_app.start_work` |
| Ladder level plumbing | root `conftest.py` + `pytest.ini` (`emulation` marker; `l2_marker_expression` excludes it from L2 — verified: `-m "not scaffold_red and not flaky and not emulation and not physical"` collects `no tests (2 deselected)`) |
| Real iroh sidecar binary | copied from the same-commit worktree `mycelium-core-pilot/.build-native/release/mycelium-iroh-sidecar` (17 130 416 B; `.build-native/` is git-ignored, so the tree stays clean) |

Built (the actual new capability, `mycelium_testbed/real_worker_kill.py`, ~430 lines):
`real_worker_facts` (pid/pgid-leader/group-state/configure-dispatch facts),
`process_group_state` (3-state probe: `present` / `absent` / `permission_denied`),
`kill_real_worker` (SIGKILL to the **real process group**, refuses a target with no real pid or
that is not a group leader, waits for the kernel's reaped `-SIGKILL`),
`AuthorisationLog` (the `authorize_load` callable the shipped lifecycle consults, stamped per
envelope), `observe_recovery` (pure observer bounded by a recorded bound),
`recovery_gap`, `write_evidence`, `quiet_host_guard`.

Test: `tests/l3/test_real_worker_kill.py` (new), `tests/l3/conftest.py` (new; makes
`tests/a13_macos` importable by explicit `sys.path` instead of copying its helpers).

Recording the bound: `DEFAULT_RECOVERY_BOUND_SECONDS = 45.0`, written into the evidence together
with the observed elapsed time, the route state, the runtime's own
`worker-replacement.private.json`, and the quiet-host load.

## 4. What is still simulated / not mounted

* **No model weights.** `configure` is *dispatched* through the shipped lifecycle (authorizer
  consulted, `_configure_dispatched` set, real native round-trip) but the native load result is not
  this capability's subject, so "the next inference request succeeds" is **not** asserted — only
  the configure/start re-authorisation that has to precede it.
* **No two-process iroh inference and no fault proxy** (`faults.py` / `iroh_e2e.py` are untouched
  by this scenario; the sidecar binary is installed but no inference traverses it).
* **One Mac, one worker**, so peer/owner kill asymmetry (S5 vs S4) is not exercised.
* **No real in-flight request.** The bounded-named-failure half of the contract is recorded from the
  worker's and runtime's own `error_code`s, not from a client whose stream is cut mid-flight.

## 5. RED evidence on the candidate (exact command and output)

```
cd ~/Documents/playground/mycelium-v2-m1-harness
env -u GIT_OBJECT_DIRECTORY -u GIT_ALTERNATE_OBJECT_DIRECTORIES -u PYTHONPATH -u PYTHONHOME \
    -u VIRTUAL_ENV PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. \
    ~/Documents/playground/mycelium-v2-m0-test-ladder/.venv/bin/python \
    -m pytest -q -p no:cacheprovider -W ignore::pytest.PytestWarning tests/l3/test_real_worker_kill.py
```
exit code **1** — `2 failed in 28.67s`:

* `test_real_worker_kill_after_configure_requires_reauthorised_replacement` —
  `unmet requirements: ["replacement_not_published_within_bound: the shipped runtime never
  replaced the killed worker inside 45.0s (route_state='blocked',
  route_error_code='runtime_cold_restart_required')", "replacement_not_reauthorised: the shipped
  lifecycle consulted the load authorizer for [] after the kill; required ['configure'], observed
  ['configure']", "route_did_not_recover_within_bound: …"]`, `observed_seconds: 1.505`,
  `replacement_pid: null`.
* `test_real_worker_kill_preload_replacement_is_not_reauthorised` —
  `unmet requirements: ["replacement_not_reauthorised: … required ['configure'], observed []"]`,
  `replacement_published: true`, `replacement_pid: 37073`, `route_recovered: true`,
  `all_authorisation_commands: []`.

The kill itself is not in doubt in either run: `died_by_requested_signal: true`,
`session_returncode_after: -9`, `pgid_is_leader: true`, group state `present` → `absent`,
reap observed in 55 ms. Both tests assert those facts **before** the semantic assertion, so a
harness that had degraded to a mailbox double would fail on the capability, not on the gap.

What the two observations distinguish:

* configured boundary (61257b2) → **no replacement at all**: the runtime publishes a terminal
  `runtime_cold_restart_required` and abandons the route;
* pre-load boundary (61257b2) → a replacement *is* minted (real new process, `outcome: "replaced"`,
  `previous_error_code: "worker_native_exited"`) but is **never re-authorised** through the
  configure/start lifecycle, so it is an unauthorised executor.

That pair is the local reproduction of the p84 hypothesis, and it is the failing scenario S4/S5
needs before any hardware round.

## 6. `fb0314c` (PLAN's baseline): also RED, and why

Reachable read-only at `fb0314cf1cc5a57d14a2096226878267506b27f6` (on `integration/core-pilot` and
several `preserve/tree-*` branches; 105 files / −23 558 lines vs `61257b2`, i.e. it predates the
candidate). Probe: `git worktree add --detach …/mycelium-fb0314c-l3-probe fb0314c`, same sidecar
copy, same two files, same command → exit code **1**, `2 failed in 22.59s`. The probe worktree was
removed afterwards with `git worktree remove --force`.

* pre-load kill: **identical gap** — `replacement_not_reauthorised: … required ['configure'],
  observed []`.
* configured boundary: `fb0314c`'s `ManagedWorker` has **no** `_configure_dispatched` and **no**
  `requires_cold_restart` (grep: absent from `worker.py`), and its `_replace_worker_if_dead`
  replaces unconditionally — no cold-restart guard, no `cleanup_proven`/`process_group_present`
  pre-check. The configured boundary therefore **cannot be expressed on that revision**; the test
  fails with `the configured boundary under test was never reached: {'configure_dispatched_symbol':
  False, 'requires_cold_restart_symbol': False, …}`. RED, by a different mechanism, which is itself
  a finding: the boundary this scenario measures did not exist at `fb0314c`.

## 7. Seams found while building this (fidelity findings, not fixes)

1. `ManagedWorker.process_group_present()` raises `PermissionError: [Errno 1]` when called on a
   SIGKILLed-but-not-yet-reaped group (Darwin returns EPERM, not ESRCH, for signal 0 on a
   zombie-only group). Measured only because a real kill leaves an unreaped zombie; the L1 suites
   that set `returncode = 143` can never see it. The harness records this as its own state
   (`process_group_state == "permission_denied"`) instead of folding it into "absent". Whether the
   shipped flow can observe it is not established: `_replace_worker_if_dead` calls
   `process_group_present()` only *after* `previous.close()` has reaped the child, where it returns
   `False` cleanly (`product_process_group_present_error_after: null` in both runs).
2. `ManagedTargetRuntime.status()` has no `route_ready` for a dead worker; the only route-level
   signal after a configured kill is `state='blocked'` + `error_code='runtime_cold_restart_required'`
   (+ `worker-replacement.private.json` `outcome: "refused"`). An owner watching only `status()` gets
   a named code, but no replacement identity and no re-authorisation trail.

## 8. Shipped-path elements NOT yet covered by this capability

1. **Real model load and a real successful terminal request after recovery** (S1–S3 substrate):
   `configure` is dispatched, never honoured; no MLX/Qwen load, no `infer_start`, no stream.
2. **Owner→target control path** (control mailbox claim/ack, `ConcurrentTargetControlPump.step()`):
   the scenario starts the runtime directly and never enqueues an owner command, so
   `pump.step()`-driven dispatch of the replacement is unexercised.
3. **In-flight request cut by the kill** and its bounded named failure / client retry (the other
   half of the M1 demo statement).
4. **The owner/peer asymmetry** (S5) and the second Mac (L4): only one local worker is killed.
5. **`_MAX_WORKER_REPLACEMENTS` exhaustion** and the `runtime_replacement_cleanup_unproven`
   branch, reached only by killing a replacement repeatedly.
6. **Peer/relay rotation** (`_binding_advanced`) concurrent with a replacement.
7. **Fault proxy + real iroh transport** on the replacement's route (`faults.py`, `iroh_e2e.py`).
8. **`soak.py` itself**: this lane did not change the soak; `_restart_owner`/`_restart_peer` remain
   simulated (R1's original object). Whether the soak should be re-pointed at this capability, or
   replaced by it for S4/S5, is a decision for the lanes that own S4/S5.
9. **Quiet-host guard is recorded, not enforced** (`load1 = 140.2` at the run that produced the
   evidence above). The failure is a state/authorisation fact with a 1.5 s terminal, not a timing
   flake, but an L3 *qualification* run must still happen under `load1 ≤ 8`.
