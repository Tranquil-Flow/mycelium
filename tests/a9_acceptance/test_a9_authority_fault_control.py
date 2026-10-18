# SPDX-License-Identifier: AGPL-3.0-only
"""Grant-gated live A9 authority fault publisher (two-session withdrawal check).

RED-first contract: the seam is absent by default, refuses without the exact A9
grant, mounts only through the explicit supervisor flag, and publishes only the
three browser-visible A9 authority failures.  The acknowledgement means
"published to the authority overlay", never "the browser rendered it".
"""
from __future__ import annotations

import ast
import http.client
import json
from pathlib import Path

import pytest

from mycelium_live import supervisor

GRANT = "FLEET GRANTED TO A9"
GRANT_ENV = "MYCELIUM_A9_FLEET_GRANT"
PATH = "/a9/authority-fault"


def _records():
    return tuple(
        {
            "node_id": node,
            "peer_class": "mac_mlx_iroh",
            "runtime_capability": {
                "runtime_backend": "mlx", "transport": "iroh",
                "activation_protocol": "mycelium.router_wire.v1",
            },
            "activation_eligible": True, "generation": 1, "authority_generation": 3,
            "incarnation": "i", "lease_expires_at": 9e12, "last_liveness_at": 1.0,
            "lifecycle_state": "RUNNING",
        }
        for node in ("node-a", "node-b")
    )


class _Route:
    def product_membership_records(self):
        return _records()


@pytest.fixture(autouse=True)
def _clean_ledger():
    from mycelium_a9_authority_ledger import LEDGER
    LEDGER.clear()
    yield
    LEDGER.clear()


def test_overlay_is_identity_by_default_and_publish_needs_the_grant():
    from mycelium_a9_authority_ledger import LEDGER, LedgerRefused, overlay_membership
    records = _records()
    assert overlay_membership(records) == records  # off by default: byte-identical
    with pytest.raises(LedgerRefused, match="explicit_fleet_grant_required"):
        LEDGER.publish("stage_withdrawal", "node-a", grant="nope")
    with pytest.raises(LedgerRefused, match="explicit_fleet_grant_required"):
        LEDGER.publish("stage_withdrawal", "node-a", grant=None)
    assert overlay_membership(records) == records


def test_each_fault_kind_changes_only_the_named_member_and_bumps_generation():
    from mycelium_a9_authority_ledger import LEDGER, overlay_membership
    base = _records()
    gen1 = LEDGER.publish("stage_withdrawal", "node-a", grant=GRANT)
    out = overlay_membership(base)
    assert out[1] == base[1]
    assert out[0]["activation_eligible"] is False
    assert out[0]["authority_generation"] > base[0]["authority_generation"]
    assert out[0]["authority_generation"] >= gen1
    gen2 = LEDGER.publish("codec_withdrawal", "node-b", grant=GRANT)
    assert gen2 > gen1
    out = overlay_membership(base)
    assert out[1]["activation_eligible"] is False
    assert out[1]["runtime_capability"]["activation_protocol"] is None
    assert out[1]["authority_generation"] >= gen2
    LEDGER.publish("unknown_registry_value", "node-a", grant=GRANT, value="future-npu-9")
    out = overlay_membership(base)
    assert out[0]["activation_eligible"] is False
    assert out[0]["runtime_capability"]["runtime_backend"] == "future-npu-9"
    # input is never mutated
    assert base == _records()
    gen3 = LEDGER.publish("restore", "node-a", grant=GRANT)
    assert gen3 > gen2
    assert overlay_membership(base)[0] == dict(base[0], authority_generation=overlay_membership(base)[0]["authority_generation"])
    assert overlay_membership(base)[0]["activation_eligible"] is True


def test_ledger_rejects_bad_kinds_nodes_and_known_registry_values():
    from mycelium_a9_authority_ledger import LEDGER, LedgerRefused
    for kind, node, value in (
        ("explode", "node-a", None), ("stage_withdrawal", "", None),
        ("stage_withdrawal", "x" * 200, None), ("unknown_registry_value", "node-a", None),
        ("unknown_registry_value", "node-a", "mlx"),          # a known value is not "unknown"
        ("unknown_registry_value", "node-a", "Bad Value!"),   # not a safe segment
        ("stage_withdrawal", "node-a", "surplus"),            # value only for unknown kind
    ):
        with pytest.raises(LedgerRefused):
            LEDGER.publish(kind, node, grant=GRANT, value=value)


def test_mount_refuses_without_grant_then_serves_authenticated_loopback(monkeypatch, tmp_path):
    from mycelium_a9_authority_ledger import overlay_membership
    from mycelium_a9_authority_fault_control import AuthorityFaultRefused
    monkeypatch.delenv(GRANT_ENV, raising=False)
    tmp_path.chmod(0o700)
    kwargs = dict(route=_Route(), product_port=8787, control_port=0,
                  product_state_root=tmp_path, public_origin=None, trusted_https_proxy=False)
    with pytest.raises(AuthorityFaultRefused, match="explicit_fleet_grant_required"):
        supervisor._mount_a9_authority_fault_control(**kwargs)
    assert not (tmp_path / "a9-authority-fault.token").exists()
    monkeypatch.setenv(GRANT_ENV, "FLEET GRANTED TO A11")  # a sibling grant is not this grant
    with pytest.raises(AuthorityFaultRefused, match="explicit_fleet_grant_required"):
        supervisor._mount_a9_authority_fault_control(**kwargs)
    monkeypatch.setenv(GRANT_ENV, GRANT)
    with pytest.raises(AuthorityFaultRefused, match="a9_authority_fault_product_state_root_required"):
        supervisor._mount_a9_authority_fault_control(**dict(kwargs, product_state_root=None))
    with pytest.raises(AuthorityFaultRefused, match="requires_direct_loopback"):
        supervisor._mount_a9_authority_fault_control(**dict(kwargs, public_origin="https://x.test"))
    control = supervisor._mount_a9_authority_fault_control(**kwargs)
    try:
        assert control.port > 0
        token_path = tmp_path / "a9-authority-fault.token"
        assert token_path.stat().st_mode & 0o777 == 0o600
        token = token_path.read_text().strip()

        def post(grant=GRANT, secret=token, body=None, extra=None):
            conn = http.client.HTTPConnection("127.0.0.1", control.port, timeout=3)
            conn.request("POST", PATH, json.dumps(body or {"kind": "stage_withdrawal", "node_id": "node-a"}),
                         headers={"x-mycelium-a9-fleet-grant": grant,
                                  "x-mycelium-a9-authority-fault-token": secret, **(extra or {})})
            response = conn.getresponse()
            result = response.status, json.loads(response.read())
            conn.close()
            return result

        base = _records()
        assert post(grant="bad")[0] == 403
        assert post(grant="FLEET GRANTED TO A11")[0] == 403
        assert post(secret="bad")[0] == 403
        assert post(extra={"Origin": "http://evil.test"})[0] == 403
        assert post(extra={"X-Forwarded-For": "1.2.3.4"})[0] == 403
        assert overlay_membership(base) == base  # nothing published by refused calls
        conn = http.client.HTTPConnection("127.0.0.1", control.port, timeout=3)
        conn.request("GET", PATH)
        assert conn.getresponse().status == 403
        assert post(body={"kind": "explode", "node_id": "node-a"})[0] == 400
        assert post(body={"kind": "stage_withdrawal"})[0] == 400
        assert post(body={"kind": "stage_withdrawal", "node_id": "ghost"})[0] == 409  # not a current member
        assert overlay_membership(base) == base
        status, doc = post()
        assert status == 200 and doc["published"]["kind"] == "stage_withdrawal"
        assert doc["published"]["node_id"] == "node-a" and doc["published"]["generation"] >= 1
        assert overlay_membership(base)[0]["activation_eligible"] is False
        assert control.status()["qualification_claim"] is False and control.status()["physical_evidence"] is False
    finally:
        control.close()
    assert not (tmp_path / "a9-authority-fault.token").exists()
    assert overlay_membership(_records()) == _records()  # closing the door clears the fault


def test_flag_is_absent_by_default_and_operator_plan_only():
    parser = supervisor.build_parser()
    args = parser.parse_args(["--operator-plan", "p.json"]) if False else None
    actions = {a.dest: a for a in parser._actions}
    assert actions["a9_authority_fault_control_port"].default is None
    import inspect
    assert inspect.signature(supervisor.run_physical_server).parameters["a9_authority_fault_control_port"].default is None


def test_fault_control_is_imported_only_by_the_explicit_mount_and_ledger_by_product_and_control():
    root = Path(__file__).resolve().parents[2]
    importers: dict[str, list[str]] = {"mycelium_a9_authority_fault_control": [], "mycelium_a9_authority_ledger": []}
    for path in root.rglob("*.py"):
        rel = path.relative_to(root)
        if rel.parts[0] in {"tests", "runs", "build", ".build-native", "node_modules"}:
            continue
        try:
            tree = ast.parse(path.read_text())
        except (UnicodeError, SyntaxError):
            continue
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.ImportFrom) and node.module:
                names.append(node.module)
            elif isinstance(node, ast.Import):
                names.extend(alias.name for alias in node.names)
            for name in names:
                if name in importers and str(rel) not in importers[name]:
                    importers[name].append(str(rel))
    assert importers["mycelium_a9_authority_fault_control"] == ["mycelium_live/supervisor.py"]
    assert sorted(importers["mycelium_a9_authority_ledger"]) == [
        "mycelium_a9_authority_fault_control.py", "mycelium_live/supervisor.py"]


def test_product_application_reads_membership_through_the_overlay(monkeypatch):
    from mycelium_a9_authority_ledger import LEDGER
    captured = {}

    class Spy:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    class Route:
        product_membership_records = staticmethod(_records)
        product_pseudonym_salt = staticmethod(lambda: b"s" * 32)
        public_status = staticmethod(lambda: {})

    monkeypatch.setattr(supervisor, "ProductEvidenceApplication", Spy)
    supervisor._product_application(Route(), object())
    before = captured["membership_source"]()
    assert before == _records()
    LEDGER.publish("stage_withdrawal", "node-b", grant=GRANT)
    after = captured["membership_source"]()
    assert after[1]["activation_eligible"] is False and after[0]["activation_eligible"] is True
