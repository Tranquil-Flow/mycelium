# SPDX-License-Identifier: AGPL-3.0-only
"""The product serve process mounts the A6 receiver only behind the exact grant."""
from __future__ import annotations

import json
import socket
from pathlib import Path

import pytest

from mycelium_a6_direct_socket import A6DirectSocketServer
from mycelium_a6_fault_control import EXACT_FLEET_GRANT, INJECTION_POINTS
from mycelium_a6_http_fault_control import A6LocalFaultReceiver
from mycelium_a6_product_fault_control import (
    CONTROL_PATH,
    GRANT_ENVIRONMENT,
    STATUS_PROTOCOL,
    ProductFaultControl,
    ProductFaultControlRefused,
    grant_from_environment,
)
from mycelium_demo import cli
from mycelium_live import supervisor as supervisor_module

CASE = "no_successor"


class _MeasuringSeam:
    """Reports what it was driven to measure; the test drives a real counter."""

    def __init__(self, case_id: str = CASE) -> None:
        self.point = INJECTION_POINTS[case_id]
        self.seam_id = self.point.seam_id
        self.calls: list[tuple[str, str]] = []
        self.state = {"admitted_through_ordinary_gateway": True,
                      "request_in_flight": True, "source_attempt": 1}

    def arm(self, *, case_id: str) -> dict:
        self.calls.append(("arm", case_id))
        return dict(self.state)

    def apply(self, *, case_id: str) -> dict:
        self.calls.append(("apply", case_id))
        return {"source_failure_observed": True, "candidate_available": False,
                "truthful_abort_required": True}


def _post(port: int, *, grant: str | None = EXACT_FLEET_GRANT, case_id: str = CASE,
          method: str = "POST") -> tuple[int, dict]:
    body = json.dumps({"action": "execute_controlled_source_failure",
                       "case_id": case_id}).encode("ascii")
    head = (f"{method} {CONTROL_PATH} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
            f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\n")
    if grant is not None:
        head += f"X-Mycelium-A6-Fleet-Grant: {grant}\r\n"
    with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
        sock.sendall((head + "\r\n").encode("ascii") + body)
        reply = bytearray()
        while chunk := sock.recv(4096):
            reply.extend(chunk)
    status = int(bytes(reply).split(b"\r\n", 1)[0].split(b" ")[1])
    return status, json.loads(bytes(reply).split(b"\r\n\r\n", 1)[1])


def _control(seam=None, **overrides):
    seam = seam or _MeasuringSeam()
    options = dict(grant=EXACT_FLEET_GRANT, seams={seam.seam_id: seam},
                   product_port=8787, port=0)
    options.update(overrides)
    return ProductFaultControl(**options)


@pytest.mark.parametrize("grant", [None, "", "FLEET GRANTED TO A7",
                                   "fleet granted to a6", EXACT_FLEET_GRANT + " "])
def test_mount_refuses_without_the_exact_grant(grant):
    with pytest.raises(ProductFaultControlRefused, match="explicit_fleet_grant_required"):
        _control(grant=grant)


@pytest.mark.parametrize("overrides", [
    {"public_origin": "https://mycelium.now"},
    {"trusted_https_proxy": True},
])
def test_mount_refuses_behind_a_proxy_or_public_origin(overrides):
    with pytest.raises(ProductFaultControlRefused, match="direct_loopback_product"):
        _control(**overrides)


@pytest.mark.parametrize("port,code", [(8787, "port_is_product_origin"), (80, "port_invalid"),
                                       (70_000, "port_invalid"), (True, "port_invalid")])
def test_mount_refuses_invalid_or_product_port(port, code):
    with pytest.raises(ProductFaultControlRefused, match=code):
        _control(port=port)


def test_mount_refuses_a_seam_registered_under_a_foreign_identity():
    seam = _MeasuringSeam()
    with pytest.raises(ProductFaultControlRefused, match="seams_invalid"):
        ProductFaultControl(grant=EXACT_FLEET_GRANT, seams={"a6.fault.other": seam},
                            product_port=8787)


def test_grant_is_read_only_from_the_exact_environment_value():
    assert grant_from_environment({GRANT_ENVIRONMENT: EXACT_FLEET_GRANT}) == EXACT_FLEET_GRANT
    assert grant_from_environment({GRANT_ENVIRONMENT: "yes"}) is None
    assert grant_from_environment({}) is None


def test_receiver_and_listener_still_refuse_ungranted_non_test_composition():
    seam = _MeasuringSeam()
    with pytest.raises(ValueError, match="test_only_composition_required"):
        A6LocalFaultReceiver(seams={seam.seam_id: seam}, clock_ms=lambda: 0,
                             path=CONTROL_PATH, product_grant="FLEET GRANTED TO A7")
    receiver = A6LocalFaultReceiver(seams={seam.seam_id: seam}, clock_ms=lambda: 0,
                                    path=CONTROL_PATH, product_grant=EXACT_FLEET_GRANT)
    for kwargs in ({"port": 8000}, {"port": 8000, "product_grant": "nope"},
                   {"port": 80, "product_grant": EXACT_FLEET_GRANT},
                   {"port": 0, "product_grant": EXACT_FLEET_GRANT, "bind_host": "0.0.0.0"}):
        options = {"bind_host": "127.0.0.1", **kwargs}
        with pytest.raises(ValueError):
            A6DirectSocketServer(receiver=receiver, **options)
    assert seam.calls == []


def test_mounted_receiver_executes_one_case_once_and_refuses_everything_else():
    seam = _MeasuringSeam()
    with _control(seam) as control:
        status = control.status()
        assert status["protocol"] == STATUS_PROTOCOL and status["mounted"] is True
        assert status["url"] == f"http://127.0.0.1:{control.port}{CONTROL_PATH}"
        assert status["qualification_claim"] is False and status["physical_evidence"] is False
        assert _post(control.port, grant=None)[0] == 403
        assert _post(control.port, grant="FLEET GRANTED TO A7")[0] == 403
        assert _post(control.port, method="GET")[0] == 400
        assert seam.calls == []
        # No seam for this case: the mechanism refuses and records nothing.
        assert _post(control.port, case_id="stale_qualification")[0] == 403
        assert _post(control.port) == (200, {"code": "controlled_fault_executed"})
        assert _post(control.port)[0] == 409
        assert seam.calls == [("arm", CASE), ("apply", CASE)]
        assert control.executed_case_ids == (CASE,)
        port = control.port
    assert control.status()["mounted"] is False
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", port), timeout=1).close()


def test_supervisor_mount_refuses_startup_without_the_grant(monkeypatch):
    monkeypatch.delenv(GRANT_ENVIRONMENT, raising=False)
    with pytest.raises(RuntimeError, match="startup_a6_fault_control_refused:explicit_fleet"):
        supervisor_module._mount_a6_product_fault_control(
            stack=None, route=None, product_port=8787, control_port=0,
            public_origin=None, trusted_https_proxy=False, request_stop=lambda: None)


def test_supervisor_mount_listens_with_the_grant(monkeypatch):
    monkeypatch.setenv(GRANT_ENVIRONMENT, EXACT_FLEET_GRANT)
    monkeypatch.setattr(supervisor_module, "_a6_product_fault_seams",
                        lambda **_kwargs: {})
    control = supervisor_module._mount_a6_product_fault_control(
        stack=None, route=None, product_port=8787, control_port=0,
        public_origin=None, trusted_https_proxy=False, request_stop=lambda: None)
    try:
        assert control.status()["mounted"] is True
        assert _post(control.port)[0] == 403  # mounted, but no seam is bound
    finally:
        control.close()


def test_supervisor_mount_teardown_witness_is_refused_while_listening(monkeypatch):
    monkeypatch.setenv(GRANT_ENVIRONMENT, EXACT_FLEET_GRANT)
    monkeypatch.setattr(supervisor_module, "_a6_product_fault_seams",
                        lambda **_kwargs: {})
    control = supervisor_module._mount_a6_product_fault_control(
        stack=None, route=None, product_port=8787, control_port=0,
        public_origin=None, trusted_https_proxy=False, request_stop=lambda: None)
    try:
        with pytest.raises(RuntimeError, match="a6_fault_control_teardown_before_close"):
            control.teardown_witness()
    finally:
        control.close()


def test_supervisor_mount_teardown_witness_is_receiver_derived_after_close(monkeypatch):
    seam = _MeasuringSeam()
    monkeypatch.setenv(GRANT_ENVIRONMENT, EXACT_FLEET_GRANT)
    monkeypatch.setattr(supervisor_module, "_a6_product_fault_seams",
                        lambda **_kwargs: {seam.seam_id: seam})
    control = supervisor_module._mount_a6_product_fault_control(
        stack=None, route=None, product_port=8787, control_port=0,
        public_origin=None, trusted_https_proxy=False, request_stop=lambda: None)
    lifetime_id = control.status()["lifetime_id"]
    assert _post(control.port)[0] == 200
    assert _post(control.port)[0] == 409  # a refused repeat never enters the witness
    control.close()
    assert control.teardown_witness() == {
        "protocol": supervisor_module.A6_FAULT_CONTROL_TEARDOWN_PROTOCOL,
        "lifetime_id": lifetime_id,
        "executed_case_ids": [CASE],
        "torn_down": True,
    }


def test_supervisor_main_forwards_the_port_only_when_requested(tmp_path: Path, monkeypatch):
    calls: list[dict] = []
    monkeypatch.setattr(supervisor_module, "run_physical_server",
                        lambda **kwargs: calls.append(kwargs) or 0)
    base = ["--operator-plan", str(tmp_path / "one.json"),
            "--seed-state-root", str(tmp_path / "seed")]
    assert supervisor_module.main(base) == 0
    assert "a6_fault_control_port" not in calls[-1]
    assert supervisor_module.main([*base, "--a6-fault-control-port", "8788"]) == 0
    assert calls[-1]["a6_fault_control_port"] == 8788
    with pytest.raises(SystemExit):
        supervisor_module.main([*base, "--a6-fault-control-port", "80"])
    with pytest.raises(SystemExit):
        supervisor_module.main([*base, "--operator-plan", str(tmp_path / "two.json"),
                                "--a6-fault-control-port", "8788"])


def test_demo_serve_forwards_the_port_to_live_but_fixture_rejects_it(tmp_path: Path):
    calls: list[list[str]] = []
    argv = ["serve", "--mode", "live", "--operator-plan", str(tmp_path / "plan.json"),
            "--seed-state-root", str(tmp_path / "seed"), "--a6-fault-control-port", "8788"]
    assert cli.main(argv, live_server_main=lambda a: calls.append(list(a)) or 0,
                    environ={}) == 0
    assert calls[0][-2:] == ["--a6-fault-control-port", "8788"]
    with pytest.raises(SystemExit):
        cli.main(["serve", "--mode", "fixture", "--a6-fault-control-port", "8788"], environ={})
