# SPDX-License-Identifier: AGPL-3.0-only
"""Three-engine browser lifecycle: independent one-use receiver lifetimes.

A partial set of engine receipts is never an aggregate success.  The receipts
here are transport/lifecycle witnesses over the owned direct-loopback receiver;
browser scenario evidence is a separate, still-pending qualification.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from mycelium_a6_browser_lifecycle import (
    ENGINES,
    LIFECYCLE_PROTOCOL,
    LifecycleRefused,
    aggregate_engine_lifetimes,
    engine_lifetime_receipt,
)
from mycelium_a6_direct_socket import A6DirectSocketServer
from mycelium_a6_fault_control import EXACT_FLEET_GRANT, INJECTION_POINTS
from mycelium_a6_http_fault_control import A6LocalFaultReceiver

ROOT = Path(__file__).resolve().parents[2]
CASES = ("ordinary_browser_source_failure_recovery", "no_successor")
PATH = "/a6/controlled-fault"


class _FixtureSeam:
    def __init__(self, case_id):
        self.point = INJECTION_POINTS[case_id]
        self.seam_id = self.point.seam_id

    def arm(self, *, case_id):
        return dict(self.point.required_pre_state)

    def apply(self, *, case_id):
        return dict(self.point.expected_pre_recovery_state)


def _receiver():
    seams = [_FixtureSeam(case) for case in CASES]
    return A6LocalFaultReceiver(seams={item.seam_id: item for item in seams},
                                clock_ms=lambda: 1000, path=PATH, test_only=True)


def _wire(port, case_id):
    body = json.dumps({"action": "execute_controlled_source_failure", "case_id": case_id}).encode()
    head = ("POST " + PATH + " HTTP/1.1\r\nHost: 127.0.0.1:%d\r\n"
            "Content-Type: application/json\r\nContent-Length: %d\r\n"
            "X-Mycelium-A6-Fleet-Grant: %s\r\n\r\n" % (port, len(body), EXACT_FLEET_GRANT))
    return head.encode() + body


def _post(server, case_id):
    import socket
    with socket.create_connection(("127.0.0.1", server.port), timeout=3) as sock:
        sock.settimeout(3)
        sock.sendall(_wire(server.port, case_id))
        reply = bytearray()
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            reply.extend(chunk)
    return int(bytes(reply).split(b"\r\n", 1)[0].split(b" ")[1])


def test_each_engine_receives_its_own_one_use_receiver_lifetime():
    receipts = []
    for engine in ENGINES:
        receiver = _receiver()
        with A6DirectSocketServer(receiver=receiver, bind_host="127.0.0.1", port=0,
                                  test_only=True) as server:
            lifetime = server.lifetime_id
            statuses = tuple(_post(server, case) for case in CASES)
            follow_up = _post(server, CASES[0])
        receipts.append(engine_lifetime_receipt(
            engine=engine, lifetime_id=lifetime, case_ids=CASES, statuses=statuses,
            executed_case_ids=receiver.executed_case_ids, torn_down=True,
            repeat_status=follow_up, source_binding="sha256:" + "a" * 64,
            candidate_binding="sha256:" + "b" * 64))
    assert len({item["lifetime_id"] for item in receipts}) == 3
    aggregate = aggregate_engine_lifetimes(receipts)
    assert aggregate["protocol"] == LIFECYCLE_PROTOCOL
    assert aggregate["passed"] is True
    assert aggregate["engines"] == list(ENGINES)
    assert aggregate["independent_receiver_lifetimes"] == 3
    assert aggregate["live_evidence"] is False
    assert aggregate["physical_qualification_pending"] is True
    assert aggregate["qualification_claim"] is False


def _valid_receipt(engine, lifetime):
    return engine_lifetime_receipt(
        engine=engine, lifetime_id=lifetime, case_ids=CASES, statuses=(200, 200),
        executed_case_ids=CASES, torn_down=True, repeat_status=409,
        source_binding="sha256:" + "a" * 64, candidate_binding="sha256:" + "b" * 64)


def test_partial_or_duplicated_engine_sets_are_never_aggregate_success():
    lifetimes = [f"{index:064x}" for index in range(1, 5)]
    full = [_valid_receipt(engine, lifetimes[index]) for index, engine in enumerate(ENGINES)]
    assert aggregate_engine_lifetimes(full)["passed"] is True
    for partial in (full[:2], full[:1], [full[0], full[1], full[2], full[0]]):
        with pytest.raises(LifecycleRefused, match="engine_set_invalid"):
            aggregate_engine_lifetimes(partial)
    duplicated = [_valid_receipt(ENGINES[0], lifetimes[0]),
                  _valid_receipt(ENGINES[1], lifetimes[0]),
                  _valid_receipt(ENGINES[2], lifetimes[2])]
    with pytest.raises(LifecycleRefused, match="receiver_lifetime_not_independent"):
        aggregate_engine_lifetimes(duplicated)
    for mutate, message in (
        (lambda item: item.update({"torn_down": False}), "receiver_not_torn_down"),
        (lambda item: item.update({"statuses": ((CASES[0], 200), (CASES[1], 409))}),
         "receiver_case_not_executed"),
        (lambda item: item.update({"repeat_status": 200}), "receiver_case_not_one_use"),
        (lambda item: item.update({"executed_case_ids": (CASES[0],)}), "executed_case_set_mismatch"),
        (lambda item: item.update({"live_evidence": True}), "lifecycle_claim_boundary_invalid"),
        (lambda item: item.update({"physical_qualification": True}), "lifecycle_claim_boundary_invalid"),
    ):
        broken = [dict(item) for item in full]
        mutate(broken[1])
        with pytest.raises(LifecycleRefused, match=message):
            aggregate_engine_lifetimes(broken)


def test_fixture_worker_reports_three_independent_one_use_lifetimes():
    lifetimes = []
    for _engine in ENGINES:
        process = subprocess.Popen(
            [sys.executable, "-B", str(ROOT / "mycelium_a6_direct_socket.py"), "--fixture-worker"],
            cwd=ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, env={"PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": str(ROOT),
                            "PATH": "/usr/bin:/bin:/opt/homebrew/bin"})
        try:
            ready = json.loads(process.stdout.readline())
            assert len(ready["lifetime_id"]) == 64
            statuses = tuple(_post(_Address(ready["port"]), case) for case in CASES)
            assert statuses == (200, 200)
            process.stdin.write("close\n")
            process.stdin.flush()
            final = json.loads(process.stdout.readline())
        finally:
            process.stdin.close()
            process.wait(timeout=10)
        assert process.returncode == 0
        assert final == {"lifetime_id": ready["lifetime_id"], "case_ids": list(CASES),
                         "torn_down": True}
        lifetimes.append(ready["lifetime_id"])
    assert len(set(lifetimes)) == 3


class _Address:
    def __init__(self, port):
        self.port = port
