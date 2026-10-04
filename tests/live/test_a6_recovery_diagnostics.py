# SPDX-License-Identifier: AGPL-3.0-or-later
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

    def infer(self, prompt, *, max_new_tokens, request_id, sink, selected_placement_ids,
              **_kwargs):
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


@pytest.mark.parametrize(
    ("reason", "expected"),
    [("recovery_physical_qualification_required", "recovery_physical_qualification_required"),
     (None, None), ("Not A Safe Code!", None)],
)
def test_backend_surfaces_only_safe_recovery_codes(reason, expected) -> None:
    backend = RouterSessionBackend.__new__(RouterSessionBackend)
    backend._router = SimpleNamespace(recovery_failure_reason=lambda _request_id: reason)
    assert backend.terminal_failure_code("req-1") == expected
