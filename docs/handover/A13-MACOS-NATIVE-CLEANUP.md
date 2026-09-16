# A13 macOS native owned-process cleanup

## Scope and result

This change fixes the native macOS `PhysicalNodeProcess` cleanup failure without weakening PID/PGID incarnation checks or broadening signaling to ancestry. It is a local process-supervision qualification only; it is **not** evidence of model serving, a physical fleet run, or a public endpoint.

Test host: macOS 26.5.1 (build 25F80), arm64, Python 3.14.4. Lane: `demo/a13-macos` at base `b9001e6ac3fc11dd9a16f451426453621b24852a`.

## Root cause

Darwin `libproc` inventory itself was valid: `proc_pidinfo(PROC_PIDTBSDINFO)` returned the complete 136-byte structure, with stable PID, parent PID, PGID, session ID, and `start_seconds:start_microseconds` token. The failure was at the policy boundary after inventory.

On framework Python for macOS, the supervised process can perform another legitimate `exec` after the launch handshake. The observed executable moved between two objects already pinned before launch:

- pinned launcher: `.../Resources/Python.app/Contents/MacOS/Python`
- pinned target: `.../Versions/3.14/bin/python3.14`

The process incarnation did not change, but the pre-fix checks in `PhysicalNodeProcess._signal_process_group` and `_owned_group_state` additionally required the whole current `_ProcessIdentity` to equal one transient post-handshake `_launch_identity`. That exact equality included the executable object, so the approved launcher/target transition made the owned group appear unsafe. Cleanup therefore declined both SIGTERM and SIGKILL, returned `node_process_cleanup_failed`, and left non-daemon pipe-reader threads blocked behind the live child.

The fix is at `mycelium_node/process.py:1758-1769` and `mycelium_node/process.py:1846-1851`: both cleanup paths now use `_identity_matches_pre_exec_ownership`. That predicate still requires exact PID, parent PID, PGID, session ID, and start token, and permits only the two executable identities captured before launch. Existing protected-ancestry discovery, process-group member/session validation, self/parent exclusion, and `killpg` target checks remain unchanged at `mycelium_node/process.py:1770-1808` and `mycelium_node/process.py:1852-1882`.

## Bounded RED evidence

A direct, isolated dummy-child probe first reproduced the native failure and reaped its exact owned process group in `finally`. Evidence is retained at:

- `outputs/a13-macos-evidence/native-cleanup-direct-red.log`

Observed: unchanged incarnation and group, `launch == current`, but `_owned_group_state(...) == "unsafe"`; `close()` raised `node_process_cleanup_failed`; exact owned PID/PGID was then SIGKILLed and `wait()` returned `-9`.

The focused regression `tests/a13_macos/test_native_cleanup.py:22-83` deterministically starts `/bin/sh` in a new session, records the owned incarnation, then execs to the already-pinned macOS Python launcher. Its failure cleanup validates PID, parent PID, PGID, session ID, start token, and approved executable before signaling only that owned group, waits for the leader, and calls `close()` again to release reader threads.

Command:

```sh
cd /Users/evinova-self/Documents/playground/mycelium-a13-macos-demo && \
.venv/bin/python -m pytest tests/a13_macos/test_native_cleanup.py -vv --tb=long \
  2>&1 | tee outputs/a13-macos-evidence/native-cleanup-pytest-red.log; \
exit ${PIPESTATUS[0]}
```

Result before the production change: **1 failed in 0.27s**, at `supervised.close()` with `node_process_cleanup_failed`. The test-owned process was reaped; a later exact-path inventory found zero owned probe leftovers.

## GREEN evidence

Focused regression:

```sh
cd /Users/evinova-self/Documents/playground/mycelium-a13-macos-demo && \
.venv/bin/python -m pytest tests/a13_macos/test_native_cleanup.py -vv --tb=long \
  2>&1 | tee outputs/a13-macos-evidence/native-cleanup-pytest-green.log; \
exit ${PIPESTATUS[0]}
```

Result: **1 passed in 0.53s**.

All six native failures from `native-regression-03.log`, plus the new regression:

```sh
cd /Users/evinova-self/Documents/playground/mycelium-a13-macos-demo && \
.venv/bin/python -m pytest -vv --tb=long \
  tests/a13_macos/test_native_cleanup.py \
  tests/node_agent/test_node_main.py::test_sigterm_reaps_node_service_and_sidecar_within_deadline \
  tests/node_agent/test_node_main.py::test_pre_exec_launcher_blocks_target_until_owned_release \
  tests/node_agent/test_node_main.py::test_owned_group_receives_term_before_kill \
  tests/node_agent/test_node_main.py::test_process_cleanup_retry_and_handshake_fd_thread_inventory_stay_stable \
  tests/node_agent/test_node_main.py::test_handshake_release_failure_closes_fds_and_reaps_blocked_launcher \
  tests/node_agent/test_process.py::test_request_scoped_timeout_does_not_terminate_shared_node \
  2>&1 | tee outputs/a13-macos-evidence/native-cleanup-affected-green.log; \
exit ${PIPESTATUS[0]}
```

Result: **7 passed in 5.32s**.

Adjacent node-agent gate:

```sh
cd /Users/evinova-self/Documents/playground/mycelium-a13-macos-demo && \
.venv/bin/python -m pytest tests/node_agent -q --tb=short \
  2>&1 | tee outputs/a13-macos-evidence/native-cleanup-node-agent-green.log; \
exit ${PIPESTATUS[0]}
```

Result: **150 passed, 1 skipped in 17.48s**. The command exited normally; it did not hang on non-daemon readers.

Additional checks:

```sh
.venv/bin/python -m py_compile mycelium_node/process.py tests/a13_macos/test_native_cleanup.py
git diff --check
```

Both passed. `.venv/bin/python -m ruff ...` could not run because this lane's virtual environment does not contain `ruff` (`No module named ruff`); no dependency was installed for this bounded fix.

## Files

Modified/created in this lane:

- `mycelium_node/process.py`
- `tests/a13_macos/test_native_cleanup.py`
- `docs/handover/A13-MACOS-NATIVE-CLEANUP.md`
- the five logs under `outputs/a13-macos-evidence/native-cleanup-*.log`

No changes were made to `tests/node_agent/test_process.py` or `tests/node_agent/test_node_main.py`. Nothing was staged, committed, or pushed.
