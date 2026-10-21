# SPDX-License-Identifier: AGPL-3.0-only
"""Local A6 receiver contract. No listener or physical fault is started here."""
from __future__ import annotations

import asyncio
import json

import pytest

from mycelium_a6_fault_control import EXACT_FLEET_GRANT, INJECTION_POINTS
from mycelium_a6_http_fault_control import A6LocalFaultReceiver

CASE = "no_successor"
PATH = "/a6/controlled-fault"
GRANT_HEADER = b"x-mycelium-a6-fleet-grant"


class MeasuredSeam:
    def __init__(self):
        self.seam_id = INJECTION_POINTS[CASE].seam_id
        self.calls = []

    def arm(self, *, case_id):
        self.calls.append(("arm", case_id))
        return dict(INJECTION_POINTS[case_id].required_pre_state)

    def apply(self, *, case_id):
        self.calls.append(("apply", case_id))
        return dict(INJECTION_POINTS[case_id].expected_pre_recovery_state)


def receiver(seam, **kwargs):
    return A6LocalFaultReceiver(
        seams={seam.seam_id: seam}, clock_ms=lambda: 1000,
        path=PATH, test_only=True, **kwargs,
    )


def request(app, *, peer=("127.0.0.1", 1234), server=("127.0.0.1", 9999),
            path=PATH, raw_path=None, method="POST", body=None, headers=None,
            messages=None):
    if body is None:
        body = json.dumps({"action": "execute_controlled_source_failure", "case_id": CASE}).encode()
    if headers is None:
        headers = [(b"content-type", b"application/json"),
                   (b"content-length", str(len(body)).encode()),
                   (GRANT_HEADER, EXACT_FLEET_GRANT.encode())]
    if messages is None:
        messages = [{"type": "http.request", "body": body, "more_body": False}]
    scope = {"type": "http", "method": method, "path": path,
             "raw_path": raw_path if raw_path is not None else path.encode(),
             "client": peer, "server": server, "headers": headers}
    sent = []
    queue = iter(messages)

    async def receive():
        try:
            return next(queue)
        except StopIteration:
            return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    asyncio.run(app(scope, receive, send))
    assert len(sent) == 2
    return sent[0]["status"], json.loads(sent[1]["body"])


def test_explicit_opt_in_and_direct_loopback_request_executes_real_mechanism_once():
    seam = MeasuredSeam()
    with pytest.raises(ValueError):
        A6LocalFaultReceiver(seams={seam.seam_id: seam}, clock_ms=lambda: 0, path=PATH)
    app = receiver(seam)
    status, result = request(app)
    assert status == 200 and result == {"code": "controlled_fault_executed"}
    assert seam.calls == [("arm", CASE), ("apply", CASE)]
    status, result = request(app)
    assert status == 409 and result == {"code": "controlled_fault_refused"}
    assert len(seam.calls) == 2


@pytest.mark.parametrize("changes", [
    {"peer": None}, {"peer": ("10.0.0.9", 1234)},
    {"peer": ("::ffff:127.0.0.1", 1234)},
    {"server": None}, {"server": ("0.0.0.0", 9999)},
    {"path": "/other"}, {"raw_path": b"/a6/%63ontrolled-fault"},
    {"method": "GET"},
])
def test_remote_or_wrong_route_never_arms(changes):
    seam = MeasuredSeam()
    status, result = request(receiver(seam), **changes)
    assert status >= 400 and result["code"] == "controlled_fault_refused"
    assert seam.calls == []


@pytest.mark.parametrize("bad", [
    [(b"content-type", b"application/json"), (b"content-length", b"89")],
    [(b"content-type", b"text/plain"), (GRANT_HEADER, EXACT_FLEET_GRANT.encode())],
    [(b"content-type", b"application/json"), (GRANT_HEADER, b"wrong")],
    [(b"content-type", b"application/json"), (GRANT_HEADER, EXACT_FLEET_GRANT.encode()),
     (GRANT_HEADER, EXACT_FLEET_GRANT.encode())],
    [(b"content-type", b"application/json"), (GRANT_HEADER, EXACT_FLEET_GRANT.encode()),
     (b"x-forwarded-for", b"127.0.0.1")],
    [(b"content-type", b"application/json"), (GRANT_HEADER, EXACT_FLEET_GRANT.encode()),
     (b"forwarded", b"for=127.0.0.1")],
    [(b"content-type", b"application/json"), (GRANT_HEADER, EXACT_FLEET_GRANT.encode()),
     (b"x-real-ip", b"127.0.0.1")],
    [(b"content-type", b"application/json"), (GRANT_HEADER, EXACT_FLEET_GRANT.encode()),
     (b"via", b"1.1 proxy")],
])
def test_header_spoof_and_invalid_headers_do_not_arm(bad):
    seam = MeasuredSeam()
    status, result = request(receiver(seam), peer=("10.1.2.3", 44), headers=bad)
    assert status >= 400 and result == {"code": "controlled_fault_refused"}
    assert seam.calls == []


def test_loopback_peer_cannot_smuggle_forwarding_headers():
    seam = MeasuredSeam()
    body = b'{"action":"execute_controlled_source_failure","case_id":"no_successor"}'
    base = [(b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
            (GRANT_HEADER, EXACT_FLEET_GRANT.encode())]
    for extra in [(b"x-forwarded-for", b"127.0.0.1"),
                  (b"forwarded", b"for=127.0.0.1"),
                  (b"x-real-ip", b"127.0.0.1")]:
        assert request(receiver(seam), body=body, headers=base + [extra])[0] == 403
    assert request(receiver(seam), peer=("10.1.2.3", 1234), body=body,
                   headers=base + [(b"host", b"127.0.0.1")])[0] == 403
    for grant in [b"wrong", b"", EXACT_FLEET_GRANT.encode() + b"x"]:
        assert request(receiver(seam), body=body, headers=base[:2] + [(GRANT_HEADER, grant)])[0] == 403
    assert request(receiver(seam), body=body, headers=base[:2])[0] == 403
    assert seam.calls == []


@pytest.mark.parametrize("body", [
    b"{}", b"not json", b"[]",
    b'{"action":"wrong","case_id":"no_successor"}',
    b'{"action":"execute_controlled_source_failure","case_id":"unknown"}',
    b'{"action":"execute_controlled_source_failure","case_id":"no_successor","caller":"127.0.0.1"}',
    b'{"action":"execute_controlled_source_failure","case_id":"no_successor","case_id":"no_successor"}',
    b" " * 513,
])
def test_malformed_wrong_or_oversized_body_refused_without_seam(body):
    seam = MeasuredSeam()
    assert request(receiver(seam), body=body)[0] >= 400
    assert seam.calls == []


def test_partial_disconnect_or_wrong_length_refused_without_seam():
    seam = MeasuredSeam()
    app = receiver(seam)
    body = b'{"action":"execute_controlled_source_failure","case_id":"no_successor"}'
    partial = [{"type": "http.request", "body": body[:10], "more_body": True},
               {"type": "http.disconnect"}]
    assert request(app, body=body, messages=partial)[0] >= 400
    assert request(app, body=body, headers=[(b"content-type", b"application/json"),
        (b"content-length", b"1"), (GRANT_HEADER, EXACT_FLEET_GRANT.encode())])[0] >= 400
    assert seam.calls == []


def test_mechanism_failure_is_generic_and_never_exposes_case_or_record():
    seam = MeasuredSeam()
    seam.arm = lambda *, case_id: {"request_in_flight": False}
    app = receiver(seam)
    status, result = request(app)
    assert status >= 400 and result == {"code": "controlled_fault_refused"}
    assert CASE not in json.dumps(result) and EXACT_FLEET_GRANT not in json.dumps(result)
    assert seam.calls == []


def test_partial_seam_failure_consumes_case_before_any_retry():
    seam = MeasuredSeam()
    def fail_after_touch(*, case_id):
        seam.calls.append(("partial_apply", case_id))
        raise RuntimeError("private case/grant detail")
    seam.apply = fail_after_touch
    app = receiver(seam)
    assert request(app)[0] == 500
    assert request(app)[0] == 409
    assert seam.calls == [("arm", CASE), ("partial_apply", CASE)]


def test_partial_body_times_out_without_arming():
    seam = MeasuredSeam()
    app = receiver(seam, body_timeout_seconds=.01)
    body = b'{"action":"execute_controlled_source_failure","case_id":"no_successor"}'
    scope = {"type": "http", "method": "POST", "path": PATH, "raw_path": PATH.encode(),
             "client": ("127.0.0.1", 9998), "server": ("127.0.0.1", 9999),
             "headers": [(b"content-type", b"application/json"),
                         (b"content-length", str(len(body)).encode()),
                         (GRANT_HEADER, EXACT_FLEET_GRANT.encode())]}
    sent = []
    async def receive():
        await asyncio.sleep(.1)
        return {"type": "http.request", "body": body, "more_body": False}
    async def send(value):
        sent.append(value)
    asyncio.run(app(scope, receive, send))
    assert sent[0]["status"] == 400 and seam.calls == []


def test_a_refused_fault_logs_its_bounded_code_and_unmet_measurements(caplog):
    import logging

    class UnprovenSeam(MeasuredSeam):
        def apply(self, *, case_id):
            self.calls.append(("apply", case_id))
            return {"source_failure_observed": True, "candidate_available": True,
                    "truthful_abort_required": False, "after_phase": "active",
                    "actual_incident": {"subject_id": "node-a"}}

    app = receiver(UnprovenSeam())
    with caplog.at_level(logging.WARNING, logger="mycelium_a6_http_fault_control"):
        status, result = request(app)
    assert status == 403 and result == {"code": "controlled_fault_refused"}
    logged = [json.loads(record.getMessage()) for record in caplog.records
              if record.name == "mycelium_a6_http_fault_control"]
    assert logged == [{"event": "a6_controlled_fault_refused", "case_id": CASE,
                       "code": f"controlled_fault_injection_unproven:{CASE}",
                       "unmet": {"candidate_available": True,
                                 "truthful_abort_required": False},
                       "measured": {"after_phase": "active"}}]
