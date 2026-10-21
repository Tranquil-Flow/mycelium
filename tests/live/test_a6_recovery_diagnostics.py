# SPDX-License-Identifier: AGPL-3.0-only
"""A6 successor-challenge and recovery-refusal diagnostics are observable."""
from __future__ import annotations

import json
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from mycelium_live.a6_recovery import LiveA6RecoveryQualifier
from mycelium_request_gateway.backend import RouterSessionBackend

_EXPECTED = (7, 8, 9, 10)


class _Route:
    startup_challenge = ((1, 2, 3), _EXPECTED)

    def __init__(self, *, output=_EXPECTED, infer_error=None, release_error=None):
        self.output = tuple(output)
        self.infer_error = infer_error
        self.release_error = release_error
        self.released: list[str] = []
        self.kwargs: dict = {}

    def infer(self, prompt, *, max_new_tokens, request_id, sink, selected_placement_ids,
              **kwargs):
        self.kwargs = kwargs
        if self.infer_error is not None:
            raise self.infer_error
        for index, token in enumerate(self.output):
            sink.emit(index, token)
        return SimpleNamespace(request_id=request_id, token_ids=self.output)

    def release_request(self, request_id):
        self.released.append(request_id)
        if self.release_error is not None:
            raise self.release_error


def _qualifier(tmp_path: Path, route: _Route, *, now=1_000) -> LiveA6RecoveryQualifier:
    graph = SimpleNamespace(deployment_id="d", deployment_epoch=1, topology_version=1,
                            model_id="m", resolved_commit="r",
                            manifest_digest="sha256:" + "a" * 64)
    return LiveA6RecoveryQualifier(graph=graph, deployment_dir=tmp_path,
                                   tokenizer_digest="sha256:" + "9" * 64,
                                   route=route, clock_ms=lambda: now)


def _challenge(qualifier):
    return qualifier._run_physical_challenge(
        request_id="req-1", placement_ids=("placement-001",),
        absolute_deadline_unix_ms=60_000)


def _logged(caplog, event):
    documents = []
    for record in caplog.records:
        try:
            document = json.loads(record.getMessage())
        except ValueError:
            continue
        if document.get("event") == event:
            documents.append((record.levelno, document))
    return documents


def test_passed_challenge_is_recorded_at_info(tmp_path, caplog) -> None:
    caplog.set_level(logging.INFO, logger="mycelium_live.a6_recovery")
    route = _Route()
    qualifier = _qualifier(tmp_path, route)
    assert _challenge(qualifier)["passed"] is True
    outcome = qualifier.physical_challenge_outcome("req-1")
    assert outcome["outcome"] == "passed" and outcome["emitted_token_count"] == 4
    [(level, document)] = _logged(caplog, "a6_successor_physical_challenge")
    assert level == logging.INFO and document["request_id"] == "req-1"
    assert len(route.released) == 1
    # The challenge compares the exact startup output: no product stop filter.
    assert route.kwargs == {"_stop_token_ids_override": frozenset()}


def test_truncated_output_reports_mismatch_index_and_counts(tmp_path, caplog) -> None:
    qualifier = _qualifier(tmp_path, _Route(output=_EXPECTED[:2]))
    assert _challenge(qualifier)["passed"] is False
    outcome = qualifier.physical_challenge_outcome("req-1")
    assert outcome["outcome"] == "output_mismatch"
    assert outcome["observed_token_count"] == 2
    assert outcome["emitted_token_count"] == 2
    assert outcome["first_mismatch_index"] == 2
    [(level, document)] = _logged(caplog, "a6_successor_physical_challenge")
    assert level == logging.WARNING and document["placement_ids"] == ["placement-001"]


def test_infer_exception_type_and_message_are_reported(tmp_path, caplog) -> None:
    route = _Route(infer_error=RuntimeError("selected_route_not_open"))
    qualifier = _qualifier(tmp_path, route)
    assert _challenge(qualifier)["passed"] is False
    outcome = qualifier.physical_challenge_outcome("req-1")
    assert (outcome["outcome"], outcome["error_type"], outcome["error"]) == (
        "infer_error", "RuntimeError", "selected_route_not_open")
    assert route.released, "the probe is still released after a failed infer"
    assert _logged(caplog, "a6_successor_physical_challenge")[0][0] == logging.WARNING


def test_release_failure_fails_the_challenge_and_is_reported(tmp_path) -> None:
    qualifier = _qualifier(tmp_path, _Route(release_error=KeyError("probe")))
    assert _challenge(qualifier)["passed"] is False
    outcome = qualifier.physical_challenge_outcome("req-1")
    assert outcome["outcome"] == "release_error"
    assert outcome["release_error_type"] == "KeyError"


def test_elapsed_deadline_is_reported(tmp_path) -> None:
    qualifier = _qualifier(tmp_path, _Route(), now=60_000)
    assert _challenge(qualifier)["passed"] is False
    assert qualifier.physical_challenge_outcome("req-1")["outcome"] == "deadline_elapsed"


def test_diagnostic_message_is_single_line_and_bounded(tmp_path) -> None:
    error = RuntimeError("x\n" * 400)
    qualifier = _qualifier(tmp_path, _Route(infer_error=error))
    _challenge(qualifier)
    message = qualifier.physical_challenge_outcome("req-1")["error"]
    assert "\n" not in message and len(message) <= 160


class _ExitedSshRoute(_Route):
    """A route whose challenged node's ssh client exited on a lost link."""

    def __init__(self, *, stderr: bytes, returncode=255) -> None:
        super().__init__(infer_error=RuntimeError("node_process_exited"))
        placements = (SimpleNamespace(placement_id="placement-000", node_id="node-0"),
                      SimpleNamespace(placement_id="placement-001", node_id="node-2"),
                      SimpleNamespace(placement_id="placement-002", node_id="node-3"))
        self.execution_graph = SimpleNamespace(stages=(SimpleNamespace(placements=placements),))
        self._sessions = {
            "node-0": SimpleNamespace(returncode=-9, stderr=b"source killed\n"),
            "node-2": SimpleNamespace(returncode=returncode, stderr=stderr),
            "node-3": SimpleNamespace(returncode=None, stderr=b""),
        }


def test_exited_challenge_participant_is_named_with_its_exit_and_last_line(
        tmp_path, caplog) -> None:
    line = "Timeout, server 203.0.113.12 not responding."
    route = _ExitedSshRoute(stderr=b"physical node ready\n" + line.encode() + b"\n\n")
    qualifier = _qualifier(tmp_path, route)
    assert _challenge(qualifier)["passed"] is False
    outcome = qualifier.physical_challenge_outcome("req-1")
    assert outcome["error"] == "node_process_exited"
    # Only the challenged placement's node, never the killed source.
    assert outcome["participant_exits"] == [
        {"node_id": "node-2", "returncode": 255, "stderr_last_line": line}]
    [(_level, document)] = _logged(caplog, "a6_successor_physical_challenge")
    assert document["participant_exits"] == outcome["participant_exits"]


def test_participant_stderr_line_is_bounded_and_missing_stderr_is_null(tmp_path) -> None:
    qualifier = _qualifier(tmp_path, _ExitedSshRoute(stderr=b"x" * 400 + b"\n"))
    _challenge(qualifier)
    [exited] = qualifier.physical_challenge_outcome("req-1")["participant_exits"]
    assert len(exited["stderr_last_line"]) <= 160
    qualifier = _qualifier(tmp_path, _ExitedSshRoute(stderr=b"", returncode=-9))
    _challenge(qualifier)
    assert qualifier.physical_challenge_outcome("req-1")["participant_exits"] == [
        {"node_id": "node-2", "returncode": -9, "stderr_last_line": None}]


def test_unreadable_route_reports_no_participant_exits(tmp_path) -> None:
    qualifier = _qualifier(tmp_path, _Route(infer_error=RuntimeError("node_process_exited")))
    _challenge(qualifier)
    assert qualifier.physical_challenge_outcome("req-1")["participant_exits"] == []
    passed = _qualifier(tmp_path, _Route())
    _challenge(passed)
    assert passed.physical_challenge_outcome("req-1")["participant_exits"] == []


def test_running_challenge_is_visible_in_flight_and_not_logged(tmp_path, caplog) -> None:
    caplog.set_level(logging.INFO, logger="mycelium_live.a6_recovery")
    seen: list = []

    class _Watching(_Route):
        def infer(self, *args, **kwargs):
            seen.append((qualifier.physical_challenge_outcome("req-1"),
                         _logged(caplog, "a6_successor_physical_challenge")))
            return super().infer(*args, **kwargs)

    qualifier = _qualifier(tmp_path, _Watching())
    assert _challenge(qualifier)["passed"] is True
    [(during, logged_during)] = seen
    assert during["outcome"] == "in_flight" and logged_during == []
    assert qualifier.physical_challenge_outcome("req-1")["outcome"] == "passed"


@pytest.mark.parametrize(
    ("reason", "expected"),
    [("recovery_physical_qualification_required", "recovery_physical_qualification_required"),
     (None, None), ("Not A Safe Code!", None)],
)
def test_backend_surfaces_only_safe_recovery_codes(reason, expected) -> None:
    backend = RouterSessionBackend.__new__(RouterSessionBackend)
    backend._router = SimpleNamespace(recovery_failure_reason=lambda _request_id: reason)
    assert backend.terminal_failure_code("req-1") == expected


@pytest.mark.parametrize("override", [None, frozenset()])
def test_owner_forwards_the_stop_override_only_when_requested(override, monkeypatch) -> None:
    from mycelium_live.supervisor import ColdRouteRestartOwner

    route = _Route()
    owner = ColdRouteRestartOwner.__new__(ColdRouteRestartOwner)
    owner._lock = __import__("threading").RLock()
    owner._request_routes = {}
    monkeypatch.setattr(owner, "_ready_route_or_raise", lambda: (route, 1), raising=False)
    monkeypatch.setattr(ColdRouteRestartOwner, "_route_alive", staticmethod(lambda _r: True))
    extra = {} if override is None else {"_stop_token_ids_override": override}
    owner.infer((1, 2, 3), max_new_tokens=4, request_id="probe", sink=SimpleNamespace(
        emit=lambda *_a: None), selected_placement_ids=("placement-001",), **extra)
    forwarded = {key: value for key, value in route.kwargs.items() if value is not None}
    assert forwarded == extra
