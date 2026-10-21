# SPDX-License-Identifier: AGPL-3.0-only
"""A refused A6 recovery says which branch ended it and why.

A6 c0dc4350-a1 case 15 ended through the router's cancelled branch with no
log line at all, and the browser's blocked successor logged only the port's
withholding reason. Every recovery end now logs one bounded JSON event: the
cancelled branch names the cancellation evidence that fired and the command's
cancellation generation; the failed branch keeps its event and adds the
route's own failure underneath and the successor placements. Simulated route:
wiring only, never physical evidence.
"""
from __future__ import annotations

import json
import logging

from mycelium_request_gateway.contracts import InferenceSubmission, qualification_binding
from tests.live.test_a6_live_router_recovery import (
    FailingThenRecoveringRoute,
    _runtime,
    _submit_and_drain,
)


def _events(caplog, event):
    found = []
    for record in caplog.records:
        if record.name != "mycelium_live.router_port":
            continue
        try:
            document = json.loads(record.getMessage())
        except ValueError:
            continue
        if isinstance(document, dict) and document.get("event") == event:
            found.append(document)
    return found


class _BlockedSuccessorRoute(FailingThenRecoveringRoute):
    """Every successor fails the way a cleanup-unproven route refusal does."""

    def infer(self, *args, **kwargs):
        try:
            return super().infer(*args, **kwargs)
        except RuntimeError as error:
            if str(error) != "injected_successor_prefill_failure":
                raise
            raise RuntimeError("request_terminal_blocked_cleanup_unproven") from (
                RuntimeError("route_decode_stalled"))


def test_failed_recovery_logs_the_route_cause_and_successor(tmp_path, caplog) -> None:
    caplog.set_level(logging.WARNING, logger="mycelium_live.router_port")
    route = _BlockedSuccessorRoute(fail_successor=True)
    _coordinator, _router, service, qualification = _runtime(tmp_path, route)
    try:
        request_id, events = _submit_and_drain(service, qualification)
        assert [e.kind for e in events if e.kind in {"completed", "failed", "cancelled"}] == [
            "failed"]
    finally:
        service.close()
    (failed,) = _events(caplog, "a6_recovery_failed")
    assert failed["request_id"] == request_id
    assert failed["error_code"] == "request_terminal_blocked_cleanup_unproven"
    assert failed["route_failure_code"] == "route_decode_stalled"
    assert failed["successor_placement_ids"] == list(route.calls[-1]["selected_placement_ids"])
    assert _events(caplog, "a6_recovery_cancelled") == []


def test_cancelled_recovery_logs_which_condition_fired(tmp_path, caplog) -> None:
    caplog.set_level(logging.WARNING, logger="mycelium_live.router_port")
    route = FailingThenRecoveringRoute(block_successor=True)
    _coordinator, _router, service, qualification = _runtime(tmp_path, route)
    subscription = None
    try:
        request_id = service.submit(InferenceSubmission(
            prompt="live recovery prompt", max_new_tokens=4,
            qualification=qualification_binding(qualification)))
        subscription = service.subscribe(request_id, last_event_id=None)
        assert route.successor_started.wait(timeout=2)
        assert service.cancel(request_id) is True
        kinds = []
        while (event := subscription.next_event(timeout=None)) is not None:
            subscription.ack(event.sequence)
            kinds.append(event.kind)
        assert "cancelled" in kinds
    finally:
        if subscription is not None:
            subscription.close()
        service.close()
    (cancelled,) = _events(caplog, "a6_recovery_cancelled")
    assert cancelled["request_id"] == request_id
    assert set(cancelled["conditions"]) >= {"inference_cancelled", "cancellation_requested"}
    assert cancelled["cancellation_linearized"] is True
    assert cancelled["cutover_committed"] is False
    assert isinstance(cancelled["command_cancellation_generation"], int)
    assert cancelled["command_cancellation_generation"] >= 1
    assert _events(caplog, "a6_recovery_failed") == []


class _UnprovenBlockedSuccessorRoute(_BlockedSuccessorRoute):
    """A real-shaped route that proves the source's cleanup, never the successor's."""

    is_simulated = False

    def request_cleanup_receipt_scoped(self, request_id, *, request_attempt, path_id,
                                       path_attempt, path_digest, cleanup_owner_id):
        if request_attempt != 1:
            return None
        command = next(call["command_identity"] for call in self.calls
                       if call["command_identity"].get("request_attempt") == 1)
        return {**command, "request_id": request_id, "path_id": path_id,
                "path_attempt": path_attempt, "path_digest": path_digest,
                "cleanup_owner_id": cleanup_owner_id, "node_ids": []}


def test_blocked_recovery_writes_the_blocked_terminal_record(
        tmp_path, caplog, monkeypatch) -> None:
    # c0dc4350: the window set MYCELIUM_LIVE_BLOCKED_TERMINAL_LOG, yet no record
    # existed for a blocked successor, because only the ordinary route path
    # wrote one. The recovery path now writes it too, with the route's cause.
    log = tmp_path / "blocked.jsonl"
    monkeypatch.setenv("MYCELIUM_LIVE_BLOCKED_TERMINAL_LOG", str(log))
    caplog.set_level(logging.WARNING, logger="mycelium_live.router_port")
    route = _UnprovenBlockedSuccessorRoute(fail_successor=True)
    _coordinator, router, service, qualification = _runtime(tmp_path / "rt", route)
    try:
        request_id, _events_seen = _submit_and_drain(service, qualification)
        assert router.request_status(request_id) == "TERMINAL_BLOCKED"
    finally:
        service.close()
    (failed,) = _events(caplog, "a6_recovery_failed")
    assert failed["terminal_blocked_reason"] == "command_cleanup_receipt_missing"
    records = [json.loads(line) for line in log.read_text().splitlines()]
    assert len(records) == 1, records
    assert "command_cleanup_receipt_missing" in json.dumps(records[0])
    assert "route_decode_stalled" in json.dumps(records[0])
