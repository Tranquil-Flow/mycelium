"""A11's approved evidence mount is explicit, private, and lifecycle-bound."""
from pathlib import Path

import pytest


def test_supervisor_a11_mount_requires_explicit_port(tmp_path, monkeypatch):
    from mycelium_live import supervisor

    calls = []
    monkeypatch.setattr(supervisor, "run_physical_server", lambda **kw: calls.append(kw) or 0)
    args = ["--operator-plan", str(tmp_path / "plan.json"), "--seed-state-root", str(tmp_path / "seed")]
    assert supervisor.main(args) == 0
    assert "a11_evidence_control_port" not in calls[-1]
    assert supervisor.main([*args, "--a11-evidence-control-port", "18798"]) == 0
    assert calls[-1]["a11_evidence_control_port"] == 18798
    with pytest.raises(SystemExit):
        supervisor.main([*args, "--a11-evidence-control-port", "80"])
    with pytest.raises(SystemExit):
        supervisor.main([*args, "--operator-plan", str(tmp_path / "second.json"), "--a11-evidence-control-port", "18798"])


def test_demo_cli_forwards_a11_only_when_requested():
    from mycelium_demo.cli import build_parser, _live_arguments, _fixture_has_live_only_arguments

    parser = build_parser()
    plain = parser.parse_args(["serve", "--mode", "live"])
    assert "--a11-evidence-control-port" not in _live_arguments(plain)
    enabled = parser.parse_args(["serve", "--mode", "live", "--a11-evidence-control-port", "18798"])
    forwarded = _live_arguments(enabled)
    assert forwarded[forwarded.index("--a11-evidence-control-port") + 1] == "18798"
    assert _fixture_has_live_only_arguments(enabled)


def test_real_a11_mount_requires_private_state_and_removes_token(tmp_path: Path, monkeypatch):
    import json
    import http.client
    from mycelium_live.supervisor import _mount_a11_evidence_control
    from mycelium_a11_evidence_control import EXACT_FLEET_GRANT, GRANT_HEADER, TOKEN_HEADER

    from typing import Any
    args: dict[str, Any] = dict(route=object(), product_port=8787, control_port=0, public_origin=None, trusted_https_proxy=False)
    with pytest.raises(RuntimeError, match="product_state_root_required"):
        _mount_a11_evidence_control(product_state_root=None, **args)
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    monkeypatch.delenv("MYCELIUM_A11_FLEET_GRANT", raising=False)
    with pytest.raises(RuntimeError, match="explicit_fleet_grant_required"):
        _mount_a11_evidence_control(product_state_root=private, **args)
    monkeypatch.setenv("MYCELIUM_A11_FLEET_GRANT", EXACT_FLEET_GRANT)
    control = _mount_a11_evidence_control(product_state_root=private, **args)
    token_file = private / "a11-evidence.token"
    try:
        token = token_file.read_text().strip()
        assert len(token) == 64
        assert token_file.stat().st_mode & 0o777 == 0o600
        status = control.status()
        assert status["mounted"]
        assert token not in json.dumps(status)
        conn = http.client.HTTPConnection("127.0.0.1", control.port, timeout=5)
        try:
            conn.request("GET", "/a11/evidence", headers={GRANT_HEADER: EXACT_FLEET_GRANT})
            response = conn.getresponse()
            assert response.status == 403
            assert token not in response.read().decode()
        finally:
            conn.close()
        assert control.token_matches(token)
        assert TOKEN_HEADER not in status
    finally:
        control.close()
    assert not token_file.exists()
    assert not control.token_matches(token)
    assert not control.status()["mounted"]


def test_demo_cli_forwards_a11_draft_loss_port_only_when_requested():
    """f7b2: drive.py started the A11 serve with --a11-draft-loss-control-port and the demo CLI
    refused it (unrecognized arguments), although the supervisor mounts the draft-loss seam."""
    from mycelium_demo.cli import build_parser, _live_arguments, _fixture_has_live_only_arguments

    parser = build_parser()
    plain = parser.parse_args(["serve", "--mode", "live"])
    assert "--a11-draft-loss-control-port" not in _live_arguments(plain)
    enabled = parser.parse_args(["serve", "--mode", "live", "--a11-draft-loss-control-port", "18812"])
    forwarded = _live_arguments(enabled)
    assert forwarded[forwarded.index("--a11-draft-loss-control-port") + 1] == "18812"
    assert _fixture_has_live_only_arguments(enabled)
