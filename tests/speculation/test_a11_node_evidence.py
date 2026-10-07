"""A11 node evidence for prereg R9 §6 bars 5 and 6: signed, bound to the plan, verified offline.

A miniature R9 run (one request per window) goes through the real two-stage MLX
route with the entry's speculation controller.  Each node's evidence document is
built exactly as the node's ``a11_snapshot`` builds it and signed with an Ed25519
key whose public key is the node's endpoint id in the plan's assignment offers.
"""

from __future__ import annotations

import copy
import json
import os
import threading
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from mycelium_a11_evidence_control import A11EvidenceUnavailable, RouteA11Evidence
from mycelium_a11_node_evidence import (
    A11EvidenceError,
    WINDOW_MODES,
    verify_node_evidence,
)
from mycelium_node.identity import _signer
from physical_inference_node import NODE_OBSERVATION_PROTOCOL, NodeCommandError, PhysicalNodeService
from test_a11_route_speculation import ScriptedDraft, _policy, _speculative_mesh, _target_only

RUN_ID, DEPLOYMENT_ID = "run-a11-evidence", "dep-a11-evidence"


def _keys(node_ids):
    keys = {}
    for node_id in node_ids:
        private = Ed25519PrivateKey.generate()
        from cryptography.hazmat.primitives import serialization

        raw = private.private_bytes(
            serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption()
        )
        public_hex = private.public_key().public_bytes_raw().hex()
        keys[node_id] = _signer(raw, endpoint_id=public_hex)
    return keys


def _plan(signers, entry):
    records = [{"node_id": node_id, "endpoint_id": signer.endpoint_id} for node_id, signer in signers.items()]
    return {
        "run_id": RUN_ID,
        "controller": {
            "run_plan": {"deployment_id": DEPLOYMENT_ID, "entry_node_id": entry},
            "membership_snapshot": {
                "assignment_offers": [{"message": {"peer_endpoint_records": records}}]
            },
        },
    }


def _envelope(signer, node_id, details):
    statement = {
        "protocol": NODE_OBSERVATION_PROTOCOL,
        "event": "a11_snapshot",
        "monotonic_ns": 1,
        "run_id": RUN_ID,
        "deployment_id": DEPLOYMENT_ID,
        "node_id": node_id,
        "host_id": f"host-{node_id}",
        "process_id": 7,
        "endpoint_id": signer.endpoint_id,
        "peer_generation": 1,
        "state": "RUNNING",
        "route_ready": False,
        "details": json.loads(json.dumps(details)),
    }
    return {"observation": statement, "signature": signer.sign(statement), "verification_key": signer.public_key_record()}


class _Run:
    """The miniature R9 run on the real route, with door documents before and after."""

    def __init__(self, case, *, unavailable_rounds=(4, 7, 10), max_new_tokens=8, leave_open=False):
        reference = _target_only(case, max_new_tokens)
        draft = ScriptedDraft(reference, error_offsets=[1, 4], unavailable_rounds=set(unavailable_rounds))
        self.mesh, self.controller = _speculative_mesh(case, draft, policy=_policy())
        self.node_ids = [assignment["node_id"] for assignment in case.assignments]
        self.entry = self.node_ids[0]
        self.signers = _keys(self.node_ids)
        self.plan = _plan(self.signers, self.entry)
        self._smoke(max_new_tokens)
        self.before = self.door()
        phases = ["target_only", "speculative", *WINDOW_MODES]
        for index, mode in enumerate(phases):
            request_id = f"bench-{index}"
            self.controller.set_preference(request_id, "speculative_requested" if mode == "speculative" else "target_only")
            self.mesh.start(request_id, max_new_tokens)
            assert self.mesh.run(request_id, self.mesh.entry.speculate_one_distributed) == "COMPLETED"
        if leave_open:
            # A stray request still holding stage KV when the evidence is collected.
            self.mesh.start("stray", max_new_tokens)
        self.after = self.door()
        self.benchmark = {"requests": [{"mode": mode} for mode in WINDOW_MODES]}

    def _smoke(self, max_new_tokens):
        # Requests before the watermark (smoke, startup challenge) never count.
        self.controller.set_preference("smoke", "speculative_requested")
        self.mesh.start("smoke", max_new_tokens)
        assert self.mesh.run("smoke", self.mesh.entry.speculate_one_distributed) == "COMPLETED"

    def details(self, node_id):
        port = self.mesh.ports[self.node_ids.index(node_id)]
        speculation = None
        if node_id == self.entry:
            speculation = {"draft_identity_digest": "sha256:" + "d" * 64, "width": 4, "baseline_samples": 16,
                           "baseline_measured": True, **self.controller.evidence()}
        return {"protocol": "mycelium.a11_node_evidence.v1", "kv": port.kv_release_log(), "speculation": speculation}

    def door(self):
        return {
            "protocol": "mycelium.a11_route_node_evidence.v1",
            "nodes": {node_id: _envelope(self.signers[node_id], node_id, self.details(node_id)) for node_id in self.node_ids},
        }

    def verify(self, **overrides):
        values = {"before": self.before, "after": self.after, "plan": self.plan, "benchmark": self.benchmark}
        values.update(overrides)
        return verify_node_evidence(**values)


@pytest.fixture(scope="module")
def case():
    from a11_route_fixtures import varied_two_stage_case

    temporary, value = varied_two_stage_case()
    yield value
    temporary.cleanup()


def test_a_clean_run_meets_bars_5_and_6_from_signed_node_evidence(case):
    verdict = _Run(case).verify()
    assert verdict["bar5_zero_kv_residue"] is True, verdict
    assert verdict["bar6_coverage"] is True, verdict
    assert verdict["requests_per_window"] == 1
    for node in verdict["kv_residue"].values():
        assert node["benchmark_requests_released"] == 8 and node["abnormal_release_requests"] == 0
        assert set(node["active_states_after_window"].values()) == {0}
    totals = verdict["speculative_totals"]
    assert totals["proposed_count"] > 0 and totals["accepted_count"] > 0 and totals["deadline_fallbacks"] > 0


def test_a_request_still_holding_kv_fails_bar_5(case):
    verdict = _Run(case, leave_open=True).verify()
    assert verdict["bar5_zero_kv_residue"] is False


def test_a_run_without_any_fallback_fails_bar_6(case):
    verdict = _Run(case, unavailable_rounds=()).verify()
    assert verdict["coverage"]["fallback"] is False and verdict["bar6_coverage"] is False


@pytest.mark.parametrize("tamper", ["details", "impostor_endpoint", "drop_node"])
def test_edited_or_unbound_evidence_is_refused(case, tamper):
    run = _Run(case)
    after = copy.deepcopy(run.after)
    node_id = run.node_ids[1]
    if tamper == "details":
        details = after["nodes"][node_id]["observation"]["details"]
        details["kv"]["releases"] = []
        code = "a11_evidence_signature_invalid"
    elif tamper == "impostor_endpoint":
        # Validly signed, but by an endpoint the plan does not name for this node.
        after["nodes"][node_id] = _envelope(_keys([node_id])[node_id], node_id, run.details(node_id))
        code = "a11_evidence_observation_invalid"
    else:
        del after["nodes"][node_id]
        code = "a11_evidence_node_set_mismatch"
    with pytest.raises(A11EvidenceError, match=code):
        run.verify(after=after)


def test_a_signature_claiming_the_endpoint_id_without_the_endpoint_key_is_refused(case):
    run = _Run(case)
    after = copy.deepcopy(run.after)
    node_id = run.node_ids[0]
    forged = _signer(os.urandom(32), endpoint_id=run.signers[node_id].endpoint_id)
    after["nodes"][node_id] = _envelope(forged, node_id, run.details(node_id))
    with pytest.raises(A11EvidenceError, match="a11_evidence_key_not_bound_to_endpoint"):
        run.verify(after=after)


def test_mode_order_and_request_count_are_checked(case):
    run = _Run(case)
    with pytest.raises(A11EvidenceError, match="a11_evidence_request_count_mismatch"):
        run.verify(benchmark={"requests": [{"mode": mode} for mode in WINDOW_MODES * 2]})
    with pytest.raises(A11EvidenceError, match="a11_evidence_request_count_mismatch"):
        run.verify(before=run.after)


# ---------------------------------------------------------------------------
# Node command and door
# ---------------------------------------------------------------------------


def test_node_answers_a_signed_read_only_snapshot():
    service = object.__new__(PhysicalNodeService)
    service.state = "RUNNING"
    service.runtime = SimpleNamespace(kv_release_log=lambda: {"release_sequence": 3, "active_state_count": 0, "releases": []})
    service._a11_wiring = None
    service._signed_result = lambda event, details: {"event": event, "details": details}
    result = service._a11_snapshot({})
    assert result["event"] == "a11_snapshot"
    assert result["details"]["kv"]["release_sequence"] == 3 and result["details"]["speculation"] is None
    with pytest.raises(NodeCommandError, match="invalid_a11_snapshot_fields"):
        service._a11_snapshot({"request_id": "x"})
    service.state = "CONFIGURED"
    with pytest.raises(NodeCommandError, match="invalid_state_for_a11_snapshot"):
        service._a11_snapshot({})


def test_door_returns_the_verified_signed_envelopes_of_every_live_node():
    sent, verified = [], []

    class Route:
        _plan = {"run_id": RUN_ID, "deployment_id": DEPLOYMENT_ID, "entry_node_id": "node-0"}
        _peers = {"node-0": object(), "node-1": object()}
        _sessions = {"node-0": SimpleNamespace(returncode=None), "node-1": SimpleNamespace(returncode=None)}
        _lock = threading.Lock()

        def _command_id(self, node_id, action):
            return f"{node_id}-{action}"

        def _send_before(self, node_id, *, command_id, command, payload, deadline_monotonic_s):
            sent.append((node_id, command, payload))
            return {"ok": True, "result": {"observation": {"node_id": node_id}, "signature": {}, "verification_key": {}}}

        def _verify_observation(self, node_id, response, *, event):
            verified.append((node_id, event))
            return response["result"]["observation"]

    route = Route()
    document = RouteA11Evidence(route, clock_ms=lambda: 5).collect()
    assert sent == [("node-0", "a11_snapshot", {}), ("node-1", "a11_snapshot", {})]
    assert verified == [("node-0", "a11_snapshot"), ("node-1", "a11_snapshot")]
    assert set(document["nodes"]) == {"node-0", "node-1"}
    assert document["nodes"]["node-1"]["observation"] == {"node_id": "node-1"}

    route._sessions["node-1"] = SimpleNamespace(returncode=0)
    with pytest.raises(A11EvidenceUnavailable, match="a11_evidence_node_not_live"):
        RouteA11Evidence(route).collect()

    def refuse(*_args, **_kwargs):
        raise RuntimeError("node_observation_signature_invalid")

    route._sessions["node-1"] = SimpleNamespace(returncode=None)
    route._verify_observation = refuse
    with pytest.raises(A11EvidenceUnavailable, match="a11_evidence_snapshot_failed"):
        RouteA11Evidence(route).collect()


def test_runtime_release_log_records_every_release_in_order(case):
    run = _Run(case)
    for port in run.mesh.ports:
        log = port.kv_release_log()
        sequences = [item["sequence"] for item in log["releases"]]
        assert sequences == sorted(sequences) and log["release_sequence"] == sequences[-1]
        assert {item["reason"] for item in log["releases"]} == {"normal_completion"}
        assert all(item["active_state_count_after"] == 0 for item in log["releases"])


def test_door_is_grant_gated_loopback_and_get_only():
    import urllib.error
    import urllib.request

    from mycelium_a11_evidence_control import (
        CONTROL_PATH,
        EXACT_FLEET_GRANT,
        GRANT_HEADER,
        A11EvidenceControl,
        A11EvidenceControlRefused,
    )

    class Source(RouteA11Evidence):
        def __init__(self):
            pass

        def collect(self):
            return {"protocol": "mycelium.a11_route_node_evidence.v1", "nodes": {}}

    with pytest.raises(A11EvidenceControlRefused, match="explicit_fleet_grant_required"):
        A11EvidenceControl(grant="FLEET GRANTED TO A7", source=Source(), product_port=8000)
    with pytest.raises(A11EvidenceControlRefused, match="requires_direct_loopback_product"):
        A11EvidenceControl(grant=EXACT_FLEET_GRANT, source=Source(), product_port=8000, public_origin="https://x")
    control = A11EvidenceControl(grant=EXACT_FLEET_GRANT, source=Source(), product_port=8000)
    control.start()
    try:
        url = f"http://127.0.0.1:{control.port}{CONTROL_PATH}"

        def get(headers, path=url, method="GET"):
            request = urllib.request.Request(path, headers=headers, method=method)
            try:
                with urllib.request.urlopen(request, timeout=5) as response:
                    return response.status, json.loads(response.read())
            except urllib.error.HTTPError as error:
                return error.code, json.loads(error.read())

        assert get({})[0] == 403
        assert get({GRANT_HEADER: "FLEET GRANTED TO A7"})[0] == 403
        assert get({GRANT_HEADER: EXACT_FLEET_GRANT, "X-Forwarded-For": "1.2.3.4"})[0] == 403
        assert get({GRANT_HEADER: EXACT_FLEET_GRANT}, path=url + "?kind=x")[0] == 403
        assert get({GRANT_HEADER: EXACT_FLEET_GRANT}, method="POST")[0] == 403
        status, document = get({GRANT_HEADER: EXACT_FLEET_GRANT})
        assert status == 200 and document["evidence"]["nodes"] == {}
        assert control.status()["reads"] == 1
    finally:
        control.close()
