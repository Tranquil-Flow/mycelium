# SPDX-License-Identifier: AGPL-3.0-only
"""Request-scoped draft loss on the entry node; CPU route, not physical proof."""
from __future__ import annotations

from types import SimpleNamespace
import ast
import http.client
import json
from pathlib import Path

import pytest

from mycelium_a11_evidence_control import EXACT_FLEET_GRANT, GRANT_ENV
from physical_inference_node import NODE_CONTROL_PROTOCOL, NodeCommandError, PhysicalNodeService
from mycelium_live import supervisor
from test_a11_route_speculation import (
    ScriptedDraft, _policy, _speculative_mesh, _target_only, case,
)


def _node(controller):
    node = object.__new__(PhysicalNodeService)
    node.state = "RUNNING"
    node.run_id = "run"
    node.deployment_id = "deployment"
    node._a11_wiring = SimpleNamespace(controller=controller)
    node._signed_result = lambda event, details: {"event": event, "details": details}
    return node


def test_armed_mid_request_loss_continues_from_target_watermark_and_is_scoped(case, monkeypatch):
    reference = _target_only(case)
    draft = ScriptedDraft(reference, error_offsets=[1])
    mesh, controller = _speculative_mesh(case, draft, policy=_policy(width=1, target_bonus=False))
    controller.set_preference("R", "speculative_requested")
    sink = mesh.start("R", 14)
    transitions = []
    state_machine = mesh.entry.entry.get_request("R").state_machine
    original_transition = state_machine.transition
    def track_transition(target, *, path_attempt):
        transitions.append(target)
        return original_transition(target, path_attempt=path_attempt)
    state_machine.transition = track_transition
    # One target-verified accepted token, followed by an explicit armed loss.
    mesh.entry.speculate_one_distributed("R")
    assert controller.snapshot("R")["accepted_count"] == 1
    monkeypatch.setenv(GRANT_ENV, EXACT_FLEET_GRANT)
    node = _node(controller)
    signed = node.dispatch({"protocol": NODE_CONTROL_PROTOCOL, "command_id": "arm-R", "run_id": "run",
                            "deployment_id": "deployment", "command": "a11_draft_loss",
                            "payload": {"grant": EXACT_FLEET_GRANT, "request_id": "R", "after_accepted": 2}})
    assert signed["details"]["request_id"] == "R"
    mesh.entry.speculate_one_distributed("R")
    assert controller.snapshot("R")["accepted_count"] == 2
    assert controller.active("R")
    assert mesh.run("R", mesh.entry.speculate_one_distributed) == "COMPLETED"
    assert sink.token_ids == reference and sink.token_indexes == list(range(len(reference)))
    assert bytes(sink.token_ids) == bytes(reference)
    assert mesh.entry.request_status("R") == "COMPLETED"
    assert transitions.count("COMPLETED") == 1
    assert not set(transitions) & {"FAILED", "CANCELLED"}
    history = controller.evidence()["requests"][-1]
    assert history["stop_reason"] == "draft_lost"
    assert history["accepted_count"] == 2
    assert history["decode_rounds"] > 0
    node.runtime = SimpleNamespace(kv_release_log=lambda: {"active_state_count": 0})
    node._a11_wiring = SimpleNamespace(
        controller=controller, model=SimpleNamespace(identity_digest=lambda: "sha256:" + "a" * 64),
        config={"policy": {"width": 1}}, baseline=SimpleNamespace(count=lambda: 1, measured=lambda: (1, 2)),
    )
    binding = {"window_id": "draft-loss-window-0001", "phase": "before", "plan_digest": "sha256:" + "1" * 64,
               "prereg_digest": "sha256:" + "2" * 64, "benchmark_digest": None}
    signed_history = node._a11_snapshot({"binding": binding})["details"]["speculation"]["requests"]
    assert signed_history[-1]["stop_reason"] == "draft_lost"
    assert signed_history[-1]["fallback_reason"] is None  # admission was genuinely speculative
    assert mesh.active_kv() == [0, 0]
    # The product's other requests still have an available draft.
    controller.set_preference("other", "speculative_requested")
    other = mesh.start("other", 14)
    assert mesh.run("other", mesh.entry.speculate_one_distributed) == "COMPLETED"
    assert other.token_ids == reference
    assert controller.evidence()["requests"][-1]["stop_reason"] is None


def test_unarmed_no_effect_and_no_grant_or_rearm_refused(case, monkeypatch):
    reference = _target_only(case)
    draft = ScriptedDraft(reference, error_offsets=[1])
    mesh, controller = _speculative_mesh(case, draft, policy=_policy(width=1, target_bonus=False))
    controller.set_preference("R", "speculative_requested")
    sink = mesh.start("R", 14)
    node = _node(controller)
    with pytest.raises(NodeCommandError, match="explicit_fleet_grant_required"):
        node._a11_draft_loss({"grant": EXACT_FLEET_GRANT, "request_id": "R", "after_accepted": 1})
    monkeypatch.setenv(GRANT_ENV, EXACT_FLEET_GRANT)
    with pytest.raises(NodeCommandError, match="explicit_fleet_grant_required"):
        node._a11_draft_loss({"grant": "wrong", "request_id": "R", "after_accepted": 1})
    with pytest.raises(NodeCommandError, match="invalid_a11_draft_loss_fields"):
        node._a11_draft_loss({"grant": EXACT_FLEET_GRANT, "request_id": "R", "after_accepted": 0})
    assert mesh.run("R", mesh.entry.speculate_one_distributed) == "COMPLETED"
    assert sink.token_ids == reference
    assert controller.evidence()["requests"][-1]["stop_reason"] is None
    with pytest.raises(NodeCommandError, match="a11_draft_loss_request_unavailable"):
        node._a11_draft_loss({"grant": EXACT_FLEET_GRANT, "request_id": "R", "after_accepted": 2})


def test_armed_draft_resources_are_released_without_touching_target(case, monkeypatch):
    reference = _target_only(case)
    draft = ScriptedDraft(reference, error_offsets=[1])
    draft.close_calls = 0
    draft.close = lambda: setattr(draft, "close_calls", draft.close_calls + 1)
    mesh, controller = _speculative_mesh(case, draft, policy=_policy(width=1, target_bonus=False))
    controller.set_preference("R", "speculative_requested")
    sink = mesh.start("R", 14)
    monkeypatch.setenv(GRANT_ENV, EXACT_FLEET_GRANT)
    mesh.entry.speculate_one_distributed("R")
    _node(controller)._a11_draft_loss({"grant": EXACT_FLEET_GRANT, "request_id": "R", "after_accepted": 2})
    mesh.entry.speculate_one_distributed("R")
    assert controller.snapshot("R")["accepted_count"] == 2
    mesh.entry.speculate_one_distributed("R")
    assert controller.snapshot("R")["stop_reason"] == "draft_lost"
    assert draft.close_calls == 1
    assert mesh.run("R", mesh.entry.speculate_one_distributed) == "COMPLETED"
    assert sink.token_ids == reference


def test_unreached_threshold_is_disarmed_at_request_release(case, monkeypatch):
    reference = _target_only(case)
    mesh, controller = _speculative_mesh(case, ScriptedDraft(reference, error_offsets=[1]),
                                         policy=_policy(width=1, target_bonus=False))
    controller.set_preference("R", "speculative_requested")
    sink = mesh.start("R", 14)
    mesh.entry.speculate_one_distributed("R")
    monkeypatch.setenv(GRANT_ENV, EXACT_FLEET_GRANT)
    _node(controller)._a11_draft_loss({"grant": EXACT_FLEET_GRANT, "request_id": "R", "after_accepted": 100})
    assert mesh.run("R", mesh.entry.speculate_one_distributed) == "COMPLETED"
    assert sink.token_ids == reference
    assert controller.evidence()["requests"][-1]["stop_reason"] is None
    assert controller._draft_loss_after_accepted == {}


def test_armed_multi_token_verify_stops_at_exact_accepted_threshold(case, monkeypatch):
    reference = _target_only(case)
    draft = ScriptedDraft(reference, error_offsets=[4])
    mesh, controller = _speculative_mesh(case, draft, policy=_policy(width=4, target_bonus=False))
    controller.set_preference("R", "speculative_requested")
    sink = mesh.start("R", 14)
    mesh.entry.speculate_one_distributed("R")
    assert controller.snapshot("R")["accepted_count"] == 4
    monkeypatch.setenv(GRANT_ENV, EXACT_FLEET_GRANT)
    _node(controller)._a11_draft_loss({"grant": EXACT_FLEET_GRANT, "request_id": "R", "after_accepted": 6})
    mesh.entry.speculate_one_distributed("R")
    assert controller.snapshot("R")["accepted_count"] == 6
    assert controller.active("R")
    mesh.entry.speculate_one_distributed("R")
    assert controller.snapshot("R")["stop_reason"] == "draft_lost"
    assert mesh.run("R", mesh.entry.speculate_one_distributed) == "COMPLETED"
    assert sink.token_ids == reference
    assert controller.evidence()["requests"][-1]["accepted_count"] == 6


def test_supervisor_mount_routes_authenticated_control_to_entry_only(monkeypatch, tmp_path):
    from mycelium_a11_draft_loss_control import DraftLossRefused, RouteDraftLoss
    class Route:
        _plan = {"entry_node_id": "node-0", "run_id": "run", "deployment_id": "deployment"}
        _peers = {"node-0": object(), "node-1": object()}
        _sessions = {"node-0": SimpleNamespace(returncode=None)}
        def __init__(self):
            self.sent = []
        def _command_id(self, node, command):
            return f"{node}-{command}"
        def _send_before(self, node, *, command_id, command, payload, deadline_monotonic_s):
            self.sent.append((node, command, payload))
            return {"result": {"observation": {"details": {"request_id": payload["request_id"], "after_accepted": payload["after_accepted"], "outcome": "armed"}}}}
        def _verify_observation(self, node, response, *, event):
            assert event == "a11_draft_loss" and node == "node-0"
            return response["result"]["observation"]
    route = Route()
    source = RouteDraftLoss(route)
    with pytest.raises(DraftLossRefused, match="explicit_fleet_grant_required"):
        supervisor._mount_a11_draft_loss_control(route=route, product_port=8787, control_port=0,
            product_state_root=tmp_path, public_origin=None, trusted_https_proxy=False)
    assert not route.sent
    monkeypatch.setenv(GRANT_ENV, EXACT_FLEET_GRANT)
    tmp_path.chmod(0o700)
    control = supervisor._mount_a11_draft_loss_control(route=route, product_port=8787, control_port=0,
        product_state_root=tmp_path, public_origin=None, trusted_https_proxy=False)
    try:
        assert control.port > 0
        token = (tmp_path / "a11-draft-loss.token").read_text().strip()
        def post(grant=EXACT_FLEET_GRANT, secret=token, body=None, extra=None):
            conn = http.client.HTTPConnection("127.0.0.1", control.port, timeout=3)
            conn.request("POST", "/a11/draft-loss", json.dumps(body or {"request_id": "R", "after_accepted": 2}),
                         headers={"x-mycelium-a11-fleet-grant": grant,
                                  "x-mycelium-a11-draft-loss-token": secret, **(extra or {})})
            response = conn.getresponse()
            result = response.status, json.loads(response.read())
            conn.close()
            return result
        assert post(grant="bad")[0] == 403
        assert post(secret="bad")[0] == 403
        assert post(extra={"Origin": "http://evil.test"})[0] == 403
        assert not route.sent
        assert post()[1] == {"armed": {"request_id": "R", "after_accepted": 2}}
        assert route.sent == [("node-0", "a11_draft_loss", {"request_id": "R", "after_accepted": 2, "grant": EXACT_FLEET_GRANT})]
        def lost_ack(*_args, **_kwargs):
            raise OSError("signed acknowledgement lost after node action")
        route._verify_observation = lost_ack
        assert post(body={"request_id": "uncertain", "after_accepted": 2}) == (
            409, {"code": "a11_draft_loss_outcome_unknown"})
        assert len(route.sent) == 2  # never misreport an unconfirmed action as inert
    finally:
        control.close()
    assert not (tmp_path / "a11-draft-loss.token").exists()


def test_fault_control_is_imported_only_by_the_explicit_mount():
    root = Path(__file__).resolve().parents[2]
    importers = []
    for path in root.rglob("*.py"):
        rel = path.relative_to(root)
        if rel.parts[0] in {"tests", "runs", "build", ".build-native"}:
            continue
        try:
            tree = ast.parse(path.read_text())
        except UnicodeError:
            continue
        if any(isinstance(node, ast.ImportFrom) and node.module == "mycelium_a11_draft_loss_control"
               or isinstance(node, ast.Import) and any(alias.name == "mycelium_a11_draft_loss_control" for alias in node.names)
               for node in ast.walk(tree)):
            importers.append(str(rel))
    assert importers == ["mycelium_live/supervisor.py"]
