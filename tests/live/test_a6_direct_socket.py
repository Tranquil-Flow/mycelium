# SPDX-License-Identifier: AGPL-3.0-only
"""Real local TCP transport checks; run only under the shared test-owner lock."""
import json
import socket

import pytest

from mycelium_a6_fault_control import EXACT_FLEET_GRANT, INJECTION_POINTS
from mycelium_a6_http_fault_control import A6LocalFaultReceiver
from mycelium_a6_direct_socket import A6DirectSocketServer

CASE = "no_successor"
PATH = "/a6/controlled-fault"
BODY = json.dumps({"action": "execute_controlled_source_failure", "case_id": CASE}).encode()


class Seam:
    def __init__(self):
        self.seam_id = INJECTION_POINTS[CASE].seam_id
        self.calls = []

    def arm(self, *, case_id):
        self.calls.append(("arm", case_id))
        return dict(INJECTION_POINTS[case_id].required_pre_state)

    def apply(self, *, case_id):
        self.calls.append(("apply", case_id))
        return dict(INJECTION_POINTS[case_id].expected_pre_recovery_state)


def make_server(seam):
    receiver = A6LocalFaultReceiver(seams={seam.seam_id: seam}, clock_ms=lambda: 1000,
                                    path=PATH, test_only=True)
    return A6DirectSocketServer(receiver=receiver, bind_host="127.0.0.1",
                                port=0, test_only=True)


def wire(*, method="POST", path=PATH, body=BODY, grant=EXACT_FLEET_GRANT,
         host=None, port=None, headers=(), length=None):
    if host is None:
        host = f"127.0.0.1:{port}"
    lines = [f"{method} {path} HTTP/1.1", f"Host: {host}",
             "Content-Type: application/json",
             f"Content-Length: {len(body) if length is None else length}"]
    if grant is not None:
        lines.append(f"X-Mycelium-A6-Fleet-Grant: {grant}")
    lines.extend(headers)
    return ("\r\n".join(lines) + "\r\n\r\n").encode("ascii") + body


def send(server, request, *, shut_write=False):
    with socket.create_connection(("127.0.0.1", server.port), timeout=3) as sock:
        sock.settimeout(3)
        sock.sendall(request)
        if shut_write:
            sock.shutdown(socket.SHUT_WR)
        reply = bytearray()
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            reply.extend(chunk)
    return int(bytes(reply).split(b"\r\n", 1)[0].split(b" ")[1]), json.loads(
        bytes(reply).split(b"\r\n\r\n", 1)[1])


def test_real_socket_executes_once_and_repeat_is_409():
    seam = Seam()
    with make_server(seam) as server:
        assert server.host == "127.0.0.1" and server.port > 0
        request = wire(port=server.port)
        assert send(server, request) == (200, {"code": "controlled_fault_executed"})
        assert send(server, request) == (409, {"code": "controlled_fault_refused"})
    assert seam.calls == [("arm", CASE), ("apply", CASE)]


@pytest.mark.parametrize("kwargs", [
    {"grant": None}, {"grant": "wrong"},
    {"host": "evil.test"}, {"host": "127.0.0.1:9999"},
    {"headers": ("X-Forwarded-For: 127.0.0.1",)},
    {"headers": ("Forwarded: for=127.0.0.1",)},
    {"headers": ("Via: 1.1 proxy",)},
    {"method": "GET"},
    {"headers": ("Content-Length: 75",)},
    {"headers": ("Transfer-Encoding: chunked",)},
    {"headers": ("Content-Length: 75", "Transfer-Encoding: chunked")},
    {"length": "+75"}, {"length": "999999"},
    {"length": "0"}, {"length": "75, 75"},
    {"body": BODY + b"unexpected"},
    {"body": BODY + b"unexpected", "length": len(BODY)},
])
def test_refusal_never_touches_seam(kwargs):
    seam = Seam()
    with make_server(seam) as server:
        status, result = send(server, wire(port=server.port, **kwargs))
    assert status >= 400 and result == {"code": "controlled_fault_refused"}
    assert seam.calls == []


def test_truncated_and_disconnected_body_never_touch_seam():
    seam = Seam()
    with make_server(seam) as server:
        for partial in (BODY[:5], BODY[:-1]):
            status, result = send(server, wire(port=server.port, body=partial,
                                               length=len(BODY)), shut_write=True)
            assert status >= 400 and result == {"code": "controlled_fault_refused"}
    assert seam.calls == []


def test_unfinished_body_times_out_without_touching_seam():
    seam = Seam()
    with make_server(seam) as server:
        status, result = send(server, wire(port=server.port, body=BODY[:5],
                                           length=len(BODY)))
        assert status == 400 and result == {"code": "controlled_fault_refused"}
    assert seam.calls == []


@pytest.mark.parametrize("host,port,test_only", [
    ("0.0.0.0", 0, True), ("::1", 0, True), ("localhost", 0, True),
    ("127.0.0.2", 0, True), ("192.168.1.1", 0, True),
    ("127.0.0.1", 8000, True), ("127.0.0.1", 0, False),
])
def test_invalid_bind_or_non_test_composition_rejected(host, port, test_only):
    seam = Seam()
    receiver = A6LocalFaultReceiver(seams={seam.seam_id: seam}, clock_ms=lambda: 0,
                                    path=PATH, test_only=True)
    with pytest.raises(ValueError):
        A6DirectSocketServer(receiver=receiver, bind_host=host, port=port,
                             test_only=test_only)
    assert seam.calls == []
