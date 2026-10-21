# SPDX-License-Identifier: AGPL-3.0-only
"""The live serve mounts the A10 verification control only when asked, with the grant."""

from __future__ import annotations

import json
import socket
from pathlib import Path
from types import SimpleNamespace

import pytest

from mycelium_a10_verify_control import CONTROL_PATH, EXACT_FLEET_GRANT, GRANT_ENVIRONMENT
from mycelium_demo import cli
from mycelium_live import supervisor as supervisor_module


def _post(port: int, body: dict) -> tuple[int, dict]:
    raw = json.dumps(body).encode("utf-8")
    head = (f"POST {CONTROL_PATH} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
            f"Content-Type: application/json\r\nContent-Length: {len(raw)}\r\n"
            f"X-Mycelium-A10-Fleet-Grant: {EXACT_FLEET_GRANT}\r\nConnection: close\r\n\r\n")
    with socket.create_connection(("127.0.0.1", port), timeout=10) as sock:
        sock.sendall(head.encode("ascii") + raw)
        reply = bytearray()
        while chunk := sock.recv(65536):
            reply.extend(chunk)
    status = int(bytes(reply).split(b"\r\n", 1)[0].split(b" ")[1])
    return status, json.loads(bytes(reply).split(b"\r\n\r\n", 1)[1])


def _mount(router):
    return supervisor_module._mount_a10_verify_control(
        stack=SimpleNamespace(router=router), product_port=8787, control_port=0,
        public_origin=None, trusted_https_proxy=False)


def test_mount_refuses_startup_without_the_grant(monkeypatch):
    monkeypatch.delenv(GRANT_ENVIRONMENT, raising=False)
    with pytest.raises(RuntimeError, match="startup_a10_verify_control_refused:explicit_fleet"):
        _mount(SimpleNamespace(a10_target_verify=lambda *a, **k: None))


def test_mount_refuses_a_product_without_the_dispatcher(monkeypatch):
    monkeypatch.setenv(GRANT_ENVIRONMENT, EXACT_FLEET_GRANT)
    with pytest.raises(RuntimeError, match="startup_a10_verify_control_dispatcher_unavailable"):
        _mount(None)


def test_mount_hands_requests_to_the_product_dispatcher(monkeypatch):
    monkeypatch.setenv(GRANT_ENVIRONMENT, EXACT_FLEET_GRANT)
    calls = []

    def dispatch(node_id, operation, *, timeout_s):
        calls.append((node_id, operation, timeout_s))
        return "negative", {"case_id": "stale_prefix"}

    control = _mount(SimpleNamespace(a10_target_verify=dispatch))
    try:
        assert control.status()["mounted"] is True
        assert _post(control.port, {"node_id": "node-0", "operation": {"k": 1},
                                    "timeout_s": 30}) == (
            200, {"kind": "negative", "result": {"case_id": "stale_prefix"}})
        assert calls == [("node-0", {"k": 1}, 30)]
    finally:
        control.close()


def test_supervisor_main_forwards_the_port_only_when_requested(tmp_path: Path, monkeypatch):
    calls: list[dict] = []
    monkeypatch.setattr(supervisor_module, "run_physical_server",
                        lambda **kwargs: calls.append(kwargs) or 0)
    base = ["--operator-plan", str(tmp_path / "one.json"),
            "--seed-state-root", str(tmp_path / "seed")]
    assert supervisor_module.main(base) == 0
    assert "a10_verify_control_port" not in calls[-1]
    assert supervisor_module.main([*base, "--a10-verify-control-port", "8789"]) == 0
    assert calls[-1]["a10_verify_control_port"] == 8789
    with pytest.raises(SystemExit):
        supervisor_module.main([*base, "--a10-verify-control-port", "80"])
    with pytest.raises(SystemExit):
        supervisor_module.main([*base, "--operator-plan", str(tmp_path / "two.json"),
                                "--a10-verify-control-port", "8789"])


def test_demo_serve_forwards_the_port_to_live_but_fixture_rejects_it(tmp_path: Path):
    calls: list[list[str]] = []
    argv = ["serve", "--mode", "live", "--operator-plan", str(tmp_path / "plan.json"),
            "--seed-state-root", str(tmp_path / "seed"), "--a10-verify-control-port", "8789"]
    assert cli.main(argv, live_server_main=lambda a: calls.append(list(a)) or 0,
                    environ={}) == 0
    assert calls[0][-2:] == ["--a10-verify-control-port", "8789"]
    with pytest.raises(SystemExit):
        cli.main(["serve", "--mode", "fixture", "--a10-verify-control-port", "8789"], environ={})
