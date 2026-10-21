# SPDX-License-Identifier: AGPL-3.0-only
"""Tests that failed on a loaded host must keep their deadlines behind the host-speed factor.

The L2 rehearsals of 2026-10-19/20 failed on timing alone while Spotlight and other suites loaded the host: the
same tests pass in isolation.  Each function below used to carry a bare wall-clock literal as a wait, join, poll
deadline or probe timeout.  This guard keeps them behind ``tests.host_speed.scaled`` / ``wait_until`` so a literal
cannot creep back.  It checks deadlines only; the assertions of those tests are unchanged.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

#: bare wall-clock literals in a wait / join / deadline / probe-timeout position
_SECONDS = r"(?:[1-9]|0\.[2-9])"  # a guard, not a sub-200 ms poll interval
BARE = re.compile(
    rf"\.(?:wait|join|wait_for_wake|next_event)\(\s*(?:timeout\s*=\s*)?{_SECONDS}"
    rf"|[a-z_]*timeout\w*\s*=\s*{_SECONDS}"
    r"|monotonic\(\)\s*\+\s*(?:\d|timeout\b|seconds\b)"
    rf"|\bsleep\(\s*{_SECONDS}"
)

GUARDED = {
    # a12 deterministic: node CLI status waits and the 1.5 s negative window
    "tests/a12_mobile_deterministic/test_node_lease_recovery.py": ("Node.wait", "Node.stop"),
    "tests/a12_mobile_deterministic/test_route_assignment_delivery.py": (
        "_wait_state", "test_an_unverifiable_delivery_drops_the_applied_route"),
    # a13 pairing / pump / managed control / peer model source
    "tests/a13_macos/test_browser_target.py": ("test_browser_owner_target_encrypted_pairing",),
    "tests/a13_macos/test_concurrent_pump.py": (
        "until_ack", "test_polling_and_control_continue_while_one_dispatch_is_blocked"),
    "tests/a13_macos/test_managed_control.py": ("test_a_held_poll_never_serialises_the_owners_own_enqueue",),
    "tests/a13_macos/test_peer_model_source.py": ("test_lan_listener_refuses_non_members_and_oversized_frames",),
    # live: local physical recovery waits, runtime archive import probes
    "tests/live/test_a6_local_physical_recovery.py": (
        "_drain", "_run_case", "_wait_ready", "watchdog",
        "test_full_replica_source_kill_recovers_on_the_disjoint_successor"),
    "tests/live/test_runtime_archive.py": (
        "test_emitted_runtime_fingerprint_is_actual_source_bytes", "probe",
        "test_rebuilt_changed_source_changes_fingerprint_and_resources_resolve"),
    # node_main reap / cleanup deadlines
    "tests/node_agent/test_node_main.py": (
        "test_handshake_release_failure_closes_fds_and_reaps_blocked_launcher",
        "test_leader_dead_inherited_sidecar_group_is_cleaned"),
    # control delivery wake-up: waits, joins and the wake-up bound
    "tests/a13_macos/test_control_delivery_wakeup.py": (
        "_poll_in_background",
        "test_enqueue_wakes_a_parked_poll_instead_of_waiting_for_the_poll_interval",
        "test_an_enqueue_that_lands_during_an_attempt_is_not_a_lost_wakeup",
        "test_a_command_whose_window_is_shorter_than_the_delivery_interval_still_completes",
        "test_a_woken_command_is_not_failed_by_a_slow_host_inside_its_window",
        "test_native_completion_wakes_the_control_loop_before_its_interval_expires"),
}

#: product inputs under test, and Playwright's default restored in milliseconds: not guards; they stay literal
#: (_wait_ready scales its own argument, so its call sites pass the base seconds)
ALLOWED = ("response_timeout_seconds=0.05", "expect.set_options(timeout=5000)")  # input / reset in ms


def _functions(tree: ast.AST):
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    yield f"{node.name}.{item.name}", item
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node.name, node


def _code(source: str, node: ast.AST) -> str:
    text = ast.get_source_segment(source, node) or ""
    lines = []
    for line in text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("#") or stripped.startswith('"""'):
            continue
        lines.append(re.sub(r"\s+#.*$", "", line))
    out = "\n".join(lines)
    for allowed in ALLOWED:
        out = out.replace(allowed, "")
    return out


CASES = [(path, name) for path, names in GUARDED.items() for name in names]


@pytest.mark.parametrize(("path", "name"), CASES, ids=[f"{Path(p).name}::{n}" for p, n in CASES])
def test_deadlines_in_load_sensitive_tests_are_host_scaled(path, name):
    source = (ROOT / path).read_text()
    functions = dict(_functions(ast.parse(source)))
    assert name in functions, f"{path} has no {name}"
    code = _code(source, functions[name])
    bare = [line.strip() for line in code.splitlines() if BARE.search(line) and "scaled(" not in line]
    assert not bare, f"{path}::{name} keeps bare wall-clock deadlines: {bare}"
