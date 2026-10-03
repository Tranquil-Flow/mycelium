"""The grant-gated, read-only A7 evidence door and its product mount."""

from __future__ import annotations

import http.client
import json
from pathlib import Path
from typing import Any

import pytest

from mycelium_a7_evidence_control import (
    EXACT_FLEET_GRANT,
    GRANT_HEADER,
    A7EvidenceControl,
    A7EvidenceControlRefused,
    RouteA7Evidence,
)


def _get(port: int, query: str, *, grant: str | None, forwarded: bool = False) -> tuple[int, Any]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        connection.putrequest("GET", "/a7/evidence?" + query, skip_accept_encoding=True)
        if grant is not None:
            connection.putheader(GRANT_HEADER, grant)
        if forwarded:
            connection.putheader("X-Forwarded-For", "203.0.113.9")
        connection.endheaders()
        response = connection.getresponse()
        return response.status, json.loads(response.read().decode("utf-8"))
    finally:
        connection.close()


def test_evidence_door_requires_the_grant_at_mount_and_per_request() -> None:
    with pytest.raises(A7EvidenceControlRefused):
        A7EvidenceControl(grant=None, source=RouteA7Evidence(object()), product_port=8787, port=0)
    with pytest.raises(A7EvidenceControlRefused):
        A7EvidenceControl(grant=EXACT_FLEET_GRANT, source=RouteA7Evidence(object()), product_port=8787, port=0, public_origin="https://mycelium.now")
    control = A7EvidenceControl(grant=EXACT_FLEET_GRANT, source=RouteA7Evidence(object()), product_port=8787, port=0)
    control.start()
    try:
        assert _get(control.port, "kind=resources", grant="wrong") == (403, {"code": "a7_evidence_control_refused"})
        assert _get(control.port, "kind=resources", grant=None) == (403, {"code": "a7_evidence_control_refused"})
        assert _get(control.port, "kind=resources", grant=EXACT_FLEET_GRANT, forwarded=True) == (403, {"code": "a7_evidence_control_refused"})
        assert _get(control.port, "kind=request", grant=EXACT_FLEET_GRANT)[0] == 400
        assert _get(control.port, "kind=resources&request_id=x", grant=EXACT_FLEET_GRANT)[0] == 400
        assert _get(control.port, "kind=delete", grant=EXACT_FLEET_GRANT)[0] == 400
        # A granted read on a non-route source fails closed with a stable code.
        assert _get(control.port, "kind=resources", grant=EXACT_FLEET_GRANT) == (409, {"code": "a7_evidence_route_unsupported"})
        assert control.status()["reads"] == {"request": 0, "resources": 0}
    finally:
        control.close()


def test_supervisor_forwards_and_mounts_the_evidence_door_only_when_requested(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    import mycelium_live.supervisor as supervisor_module

    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(supervisor_module, "run_physical_server", lambda **kwargs: calls.append(kwargs) or 0)
    base = ["--operator-plan", str(tmp_path / "one.json"), "--seed-state-root", str(tmp_path / "seed")]
    assert supervisor_module.main(base) == 0
    assert "a7_evidence_control_port" not in calls[-1]
    assert supervisor_module.main([*base, "--a7-evidence-control-port", "18797"]) == 0
    assert calls[-1]["a7_evidence_control_port"] == 18797
    with pytest.raises(SystemExit):
        supervisor_module.main([*base, "--a7-evidence-control-port", "80"])
    with pytest.raises(SystemExit):
        supervisor_module.main([*base, "--operator-plan", str(tmp_path / "two.json"), "--a7-evidence-control-port", "18797"])

    monkeypatch.delenv("MYCELIUM_A7_FLEET_GRANT", raising=False)
    with pytest.raises(RuntimeError, match="startup_a7_evidence_control_refused:explicit_fleet_grant_required"):
        supervisor_module._mount_a7_evidence_control(stack=SimpleNamespace(router=None), route=object(), product_port=8787, control_port=0, public_origin=None, trusted_https_proxy=False)
    monkeypatch.setenv("MYCELIUM_A7_FLEET_GRANT", EXACT_FLEET_GRANT)
    control = supervisor_module._mount_a7_evidence_control(stack=SimpleNamespace(router=None), route=object(), product_port=8787, control_port=0, public_origin=None, trusted_https_proxy=False)
    try:
        status = control.status()
        assert status["mounted"] is True and status["port"] == control.port
        assert "claim_boundary" in status
    finally:
        control.close()


def test_node_side_counters_the_door_reads(tmp_path: Path) -> None:
    from mycelium_a7_fence_store import DurableFenceStore
    from mycelium_a7_standby_store import StandbyShadowStore

    store = DurableFenceStore(tmp_path / "fences")
    assert store.record_count() == 0
    begun = store.advance("request-a7", expected_fencing_generation=0)
    store.grant_publication("request-a7", generation=begun["fencing_generation"])
    (tmp_path / "fences" / ".partial.tmp").write_bytes(b"x")
    assert store.record_count() == 1
    store.mark_terminal("request-a7", generation=begun["fencing_generation"], outcome="completed")
    assert store.release("request-a7") is True
    assert store.record_count() == 0

    shadow = StandbyShadowStore(max_entry_bytes=1024, max_entries=4)
    assert shadow.snapshot() == {"shadow_entry_count": 0, "shadow_kv_bytes": 0}
    assert shadow.checkpoint_snapshot() == {"shadow_checkpoint_count": 0, "shadow_checkpoint_payload_bytes": 0}


def test_demo_cli_forwards_the_evidence_port_to_the_live_serve() -> None:
    from mycelium_demo.cli import _fixture_has_live_only_arguments, _live_arguments, build_parser

    plain = build_parser().parse_args(["serve", "--mode", "live"])
    assert "--a7-evidence-control-port" not in _live_arguments(plain)
    args = build_parser().parse_args(["serve", "--mode", "live", "--a7-evidence-control-port", "18797"])
    forwarded = _live_arguments(args)
    assert forwarded[forwarded.index("--a7-evidence-control-port") + 1] == "18797"
    assert _fixture_has_live_only_arguments(args) is True
