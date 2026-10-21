# SPDX-License-Identifier: AGPL-3.0-only
"""A11 node evidence for prereg R9 §6 bars 5 and 6: signed, anchored, bound, verified offline.

A miniature R9 run (one request per window) goes through the real two-stage MLX
route with the entry's speculation controller.  Each node's evidence is built
exactly as the node's ``a11_snapshot`` builds it (the node service code itself)
and signed with an Ed25519 key whose public key is the node's endpoint id.  The
bound plan's assignment offers are signed by a seed key whose digest is pinned
outside the bundle.  Every attack below passed the verifier at c4a999c.
"""

from __future__ import annotations

import base64
import copy
import json
import os
import threading
import urllib.error
import urllib.request
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from mycelium_a11_evidence_control import (
    CONTROL_PATH,
    EXACT_FLEET_GRANT,
    GRANT_HEADER,
    TOKEN_HEADER,
    A11EvidenceControl,
    A11EvidenceControlRefused,
    A11EvidenceUnavailable,
    RouteA11Evidence,
)
from mycelium_a11_node_evidence import (
    A11EvidenceError,
    WINDOW_MODES,
    main as verifier_main,
    sha256_digest,
    verify_node_evidence,
)
from mycelium_membership.contracts import ASSIGNMENT_OFFER_PROTOCOL, sign_membership_message
from mycelium_node.identity import _signer
from mycelium_qualification.signing import generate_ed25519_signer
from physical_inference_node import NODE_OBSERVATION_PROTOCOL, NodeCommandError, PhysicalNodeService
from test_a11_route_speculation import ScriptedDraft, _policy, _speculative_mesh, _target_only

RUN_ID, DEPLOYMENT_ID = "run-a11-evidence", "dep-a11-evidence"
PREREG = "sha256:" + "6" * 64
WINDOW = "a11-window-0123456789abcdef"
NOW = 1_800_000_000.0


def _node_signer(endpoint_id=None):
    private = Ed25519PrivateKey.generate()
    raw = private.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())
    return _signer(raw, endpoint_id=endpoint_id or private.public_key().public_bytes_raw().hex())


def _plan(endpoints, entry, seed):
    offers = []
    for index, recipient in enumerate(sorted(endpoints)):
        records = sorted(
            (
                {"node_id": node_id, "endpoint_id": endpoint_id, "deployment_epoch": 1,
                 "membership_generation": 1, "valid_from": NOW, "valid_until": NOW + 120.0}
                for node_id, endpoint_id in endpoints.items()
                if node_id != recipient
            ),
            key=lambda item: item["node_id"],
        )
        message = {
            "protocol": ASSIGNMENT_OFFER_PROTOCOL, "message_id": f"offer-{index}", "swarm_id": "swarm-a11",
            "sender_node_id": "seed-node", "sender_endpoint_id": seed.endpoint_id, "recipient_node_id": recipient,
            "incarnation": "seed-incarnation", "generation": 1, "issued_at": NOW, "expires_at": NOW + 120.0,
            "deployment_id": DEPLOYMENT_ID, "deployment_epoch": 1, "assignment_id": f"assignment-{index}",
            "assignment_digest": "sha256:" + "a" * 64, "stage_pack_digest": "sha256:" + "b" * 64,
            "graph_digest": "sha256:" + "c" * 64, "load_generation": 1, "placement_provenance": "frozen_fixture",
            "peer_endpoint_records": records,
        }
        offers.append(sign_membership_message(signer=seed, message=message))
    return {
        "run_id": RUN_ID,
        "controller": {
            "run_plan": {"deployment_id": DEPLOYMENT_ID, "entry_node_id": entry},
            "membership_snapshot": {"assignment_offers": offers},
        },
    }


def _bytes(document):
    return json.dumps(document, sort_keys=True).encode()


class _Run:
    """The miniature R9 run on the real route, with door documents before and after."""

    def __init__(self, case, *, unavailable_rounds=(4, 7, 10), max_new_tokens=8, leave_open=False, window=WINDOW):
        reference = _target_only(case, max_new_tokens)
        draft = ScriptedDraft(reference, error_offsets=[1, 4], unavailable_rounds=set(unavailable_rounds))
        self.mesh, self.controller = _speculative_mesh(case, draft, policy=_policy())
        self.node_ids = [assignment["node_id"] for assignment in case.assignments]
        self.entry = self.node_ids[0]
        self.signers = {node_id: _node_signer() for node_id in self.node_ids}
        self.seed = generate_ed25519_signer(endpoint_id="seed-endpoint")
        self.plan_raw = _bytes(_plan({n: s.endpoint_id for n, s in self.signers.items()}, self.entry, self.seed))
        self.window = window
        self._monotonic = 0
        self.controller.set_preference("smoke", "speculative_requested")
        self.mesh.start("smoke", max_new_tokens)
        assert self.mesh.run("smoke", self.mesh.entry.speculate_one_distributed) == "COMPLETED"
        self.before = self.door("before", None)
        for index, mode in enumerate(["target_only", "speculative", *WINDOW_MODES]):
            request_id = f"bench-{index}"
            self.controller.set_preference(request_id, "speculative_requested" if mode == "speculative" else "target_only")
            self.mesh.start(request_id, max_new_tokens)
            assert self.mesh.run(request_id, self.mesh.entry.speculate_one_distributed) == "COMPLETED"
        if leave_open:
            self.mesh.start("stray", max_new_tokens)  # still holding stage KV at collection
        self.benchmark_raw = _bytes({"benchmark": {"plan_digest": PREREG}, "requests": [{"mode": m} for m in WINDOW_MODES]})
        self.after = self.door("after", sha256_digest(self.benchmark_raw))

    def binding(self, phase, benchmark_digest):
        return {"window_id": self.window, "phase": phase, "plan_digest": sha256_digest(self.plan_raw),
                "prereg_digest": PREREG, "benchmark_digest": benchmark_digest}

    def node_service(self, node_id):
        service = object.__new__(PhysicalNodeService)
        service.state = "RUNNING"
        service.runtime = self.mesh.ports[self.node_ids.index(node_id)]
        wiring = None
        if node_id == self.entry:
            wiring = SimpleNamespace(
                model=SimpleNamespace(identity_digest=lambda: "sha256:" + "d" * 64),
                config={"policy": {"width": 4}},
                baseline=SimpleNamespace(count=lambda: 16, measured=lambda: (1.0, 2.0)),
                controller=self.controller,
            )
        service._a11_wiring = wiring
        signer = self.signers[node_id]

        def signed(event, details):
            self._monotonic += 1
            statement = {
                "protocol": NODE_OBSERVATION_PROTOCOL, "event": event, "monotonic_ns": self._monotonic,
                "run_id": RUN_ID, "deployment_id": DEPLOYMENT_ID, "node_id": node_id, "host_id": f"host-{node_id}",
                "process_id": 7, "endpoint_id": signer.endpoint_id, "peer_generation": 1, "state": "RUNNING",
                "route_ready": False, "details": json.loads(json.dumps(details)),
            }
            return {"observation": statement, "signature": signer.sign(statement), "verification_key": signer.public_key_record()}

        service._signed_result = signed
        return service

    def door(self, phase, benchmark_digest):
        binding = self.binding(phase, benchmark_digest)
        return {"nodes": {n: self.node_service(n)._a11_snapshot({"binding": binding}) for n in self.node_ids}}

    def verify(self, **overrides):
        values = {
            "before": self.before, "after": self.after, "plan_raw": self.plan_raw, "benchmark_raw": self.benchmark_raw,
            "expected_plan_digest": sha256_digest(self.plan_raw), "prereg_digest": PREREG,
            "seed_key_digest": self.seed.verification_key_digest, "window_id": self.window,
        }
        values.update(overrides)
        return verify_node_evidence(**values)


@pytest.fixture(scope="module")
def case():
    from a11_route_fixtures import varied_two_stage_case

    temporary, value = varied_two_stage_case()
    yield value
    temporary.cleanup()


@pytest.fixture(scope="module")
def run(case):
    return _Run(case)


def test_a_clean_run_meets_bars_5_and_6_from_signed_node_evidence(run):
    verdict = run.verify()
    assert verdict["bar5_zero_kv_residue"] is True and verdict["bar6_coverage"] is True, verdict
    assert verdict["requests_per_window"] == 1
    for node in verdict["kv_residue"].values():
        assert node["benchmark_requests_released"] == 8 and node["abnormal_release_requests"] == 0
        assert set(node["active_states_after_window"].values()) == {0}
    totals = verdict["speculative_totals"]
    assert totals["proposed_count"] > 0 and totals["accepted_count"] > 0 and totals["deadline_fallbacks"] > 0


def test_a_request_still_holding_kv_fails_bar_5(case):
    assert _Run(case, leave_open=True).verify()["bar5_zero_kv_residue"] is False


def test_a_run_without_any_fallback_fails_bar_6(case):
    verdict = _Run(case, unavailable_rounds=()).verify()
    assert verdict["coverage"]["fallback"] is False and verdict["bar6_coverage"] is False


# -- trust anchor -------------------------------------------------------------


def test_a_forged_plan_with_self_signed_envelopes_is_refused(run):
    attacker_seed = generate_ed25519_signer(endpoint_id="seed-endpoint")
    attackers = {node_id: _node_signer() for node_id in run.node_ids}
    forged_plan = _bytes(_plan({n: s.endpoint_id for n, s in attackers.items()}, run.entry, attacker_seed))
    forged = copy.copy(run)
    forged.signers, forged.plan_raw = attackers, forged_plan
    before, after = forged.door("before", None), forged.door("after", sha256_digest(run.benchmark_raw))
    # Even when the forged plan's digest is the one handed in, its offers are not the pinned seed's.
    with pytest.raises(A11EvidenceError, match="a11_evidence_plan_offer_unverified"):
        run.verify(before=before, after=after, plan_raw=forged_plan, expected_plan_digest=sha256_digest(forged_plan))
    # And the real pinned plan digest refuses the substituted plan outright.
    with pytest.raises(A11EvidenceError, match="a11_evidence_plan_digest_mismatch"):
        run.verify(before=before, after=after, plan_raw=forged_plan)


def test_a_wrong_seed_pin_is_refused(run):
    with pytest.raises(A11EvidenceError, match="a11_evidence_plan_offer_unverified"):
        run.verify(seed_key_digest="sha256:" + "0" * 64)


# -- envelope bypasses --------------------------------------------------------


def _after_with(run, edit):
    after = copy.deepcopy(run.after)
    edit(after, run.node_ids[1])
    return after


@pytest.mark.parametrize(
    "edit, code",
    [
        (lambda doc, n: doc["nodes"][n]["observation"]["details"]["kv"].update(releases=[]), "a11_evidence_signature_invalid"),
        (lambda doc, n: doc["nodes"][n].pop("signature"), "a11_evidence_envelope_invalid"),
        (lambda doc, n: doc["nodes"][n].update(signature={}), "a11_evidence_observation_invalid"),
        (lambda doc, n: doc["nodes"][n]["observation"].update(extra="outside the signed fields"), "a11_evidence_observation_invalid"),
        (lambda doc, n: doc["nodes"][n].update(extra="outside"), "a11_evidence_envelope_invalid"),
        (lambda doc, n: doc["nodes"].pop(n), "a11_evidence_node_set_mismatch"),
        (lambda doc, n: doc["nodes"].update({"node-x": doc["nodes"][n]}), "a11_evidence_node_set_mismatch"),
        (lambda doc, n: doc.update(nodes={}), "a11_evidence_node_set_mismatch"),
    ],
    ids=["tampered-byte", "stripped-signature", "empty-signature", "field-outside-signed-region",
         "envelope-extra", "missing-node", "extra-node", "empty-envelopes"],
)
def test_edited_or_partial_evidence_is_refused(run, edit, code):
    with pytest.raises(A11EvidenceError, match=code):
        run.verify(after=_after_with(run, edit))


def test_an_attacker_key_carried_in_the_envelope_is_refused(run):
    node_id = run.node_ids[0]
    attacker = _node_signer()
    for endpoint_id, code in (
        (attacker.endpoint_id, "a11_evidence_observation_invalid"),
        (run.signers[node_id].endpoint_id, "a11_evidence_key_not_bound_to_endpoint"),
    ):
        forged_run = copy.copy(run)
        forged_run.signers = {**run.signers, node_id: _signer(
            attacker._private_key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                                                serialization.NoEncryption()),
            endpoint_id=endpoint_id,
        )}
        after = copy.deepcopy(run.after)
        after["nodes"][node_id] = forged_run.door("after", sha256_digest(run.benchmark_raw))["nodes"][node_id]
        with pytest.raises(A11EvidenceError, match=code):
            run.verify(after=after)


def test_an_unknown_details_version_is_refused_even_when_signed(run):
    node_id = run.node_ids[1]
    signer = run.signers[node_id]
    envelope = copy.deepcopy(run.after["nodes"][node_id])
    envelope["observation"]["details"]["protocol"] = "mycelium.a11_node_evidence.v2"
    envelope["signature"] = signer.sign(envelope["observation"])
    after = copy.deepcopy(run.after)
    after["nodes"][node_id] = envelope
    with pytest.raises(A11EvidenceError, match="a11_evidence_details_invalid"):
        run.verify(after=after)


def test_duplicate_keys_in_a_document_are_refused(run, tmp_path):
    raw = json.dumps({"evidence": run.after})
    duplicated = raw.replace('"nodes": {', '"nodes": {"' + run.node_ids[1] + '": {}, ', 1)
    files = {}
    for name, content in (("before", json.dumps({"evidence": run.before})), ("after", duplicated)):
        files[name] = tmp_path / f"{name}.json"
        files[name].write_text(content)
    (tmp_path / "plan.json").write_bytes(run.plan_raw)
    (tmp_path / "bench.json").write_bytes(run.benchmark_raw)
    argv = ["--before", str(files["before"]), "--after", str(files["after"]), "--plan", str(tmp_path / "plan.json"),
            "--benchmark", str(tmp_path / "bench.json"), "--plan-digest", sha256_digest(run.plan_raw),
            "--prereg-digest", PREREG, "--seed-key-digest", run.seed.verification_key_digest, "--window-id", WINDOW]
    assert verifier_main(argv) == 2
    files["after"].write_text(raw)
    assert verifier_main(argv) == 0


# -- replay -------------------------------------------------------------------


def test_evidence_from_another_window_run_or_benchmark_is_refused(case, run):
    # Same nodes and plan, another window: the signed binding names the other window.
    window = copy.copy(run)
    window.window = "a11-window-ffffffffffffffff"
    replay_before = window.door("before", None)
    replay_after = window.door("after", sha256_digest(run.benchmark_raw))
    with pytest.raises(A11EvidenceError, match="a11_evidence_binding_mismatch"):
        run.verify(before=replay_before, after=replay_after)
    with pytest.raises(A11EvidenceError, match="a11_evidence_binding_mismatch"):
        run.verify(window_id="a11-window-ffffffffffffffff")
    with pytest.raises(A11EvidenceError, match="a11_evidence_binding_mismatch"):
        run.verify(prereg_digest="sha256:" + "7" * 64,
                   benchmark_raw=_bytes({"benchmark": {"plan_digest": "sha256:" + "7" * 64},
                                         "requests": [{"mode": m} for m in WINDOW_MODES]}))
    with pytest.raises(A11EvidenceError, match="a11_evidence_binding_mismatch"):
        run.verify(benchmark_raw=_bytes({"benchmark": {"plan_digest": PREREG},
                                         "requests": [{"mode": m} for m in WINDOW_MODES], "x": 1}))
    with pytest.raises(A11EvidenceError, match="a11_evidence_benchmark_not_bound_to_prereg"):
        run.verify(benchmark_raw=_bytes({"benchmark": {"plan_digest": "sha256:" + "7" * 64}, "requests": []}))
    # Another run's evidence is signed by that run's nodes for that run's plan.
    other = _Run(case, window="a11-window-ffffffffffffffff")
    with pytest.raises(A11EvidenceError, match="a11_evidence_observation_invalid"):
        run.verify(before=other.before, after=other.after)


def test_swapped_phases_and_a_stale_before_are_refused(run):
    with pytest.raises(A11EvidenceError, match="a11_evidence_binding_mismatch"):
        run.verify(before=run.after, after=run.before)
    with pytest.raises(A11EvidenceError, match="a11_evidence_binding_mismatch"):
        run.verify(before=run.after)


def test_mode_order_and_request_count_are_checked(run):
    for requests in ([{"mode": m} for m in WINDOW_MODES * 2], [{"mode": "speculative"} for _ in WINDOW_MODES]):
        with pytest.raises(A11EvidenceError):
            run.verify(benchmark_raw=_bytes({"benchmark": {"plan_digest": PREREG}, "requests": requests}))


# ---------------------------------------------------------------------------
# Node command and door
# ---------------------------------------------------------------------------


def test_node_snapshot_requires_a_closed_binding():
    service = object.__new__(PhysicalNodeService)
    service.state = "RUNNING"
    service.runtime = SimpleNamespace(kv_release_log=lambda: {"release_sequence": 3, "active_state_count": 0, "releases": []})
    service._a11_wiring = None
    service._signed_result = lambda event, details: {"event": event, "details": details}
    binding = {"window_id": WINDOW, "phase": "before", "plan_digest": PREREG, "prereg_digest": PREREG, "benchmark_digest": None}
    result = service._a11_snapshot({"binding": binding})
    assert result["details"]["binding"] == binding and result["details"]["speculation"] is None
    for payload in ({}, {"binding": {**binding, "phase": "during"}}, {"binding": {**binding, "benchmark_digest": PREREG}},
                    {"binding": {**binding, "window_id": "short"}}, {"binding": {**binding, "x": 1}}):
        with pytest.raises(NodeCommandError):
            service._a11_snapshot(payload)
    service.state = "CONFIGURED"
    with pytest.raises(NodeCommandError, match="invalid_state_for_a11_snapshot"):
        service._a11_snapshot({"binding": binding})


class _Route:
    _plan = {"run_id": RUN_ID, "deployment_id": DEPLOYMENT_ID, "entry_node_id": "node-0"}
    _peers = {"node-0": object(), "node-1": object()}
    _lock = threading.Lock()

    def __init__(self):
        self._sessions = {"node-0": SimpleNamespace(returncode=None), "node-1": SimpleNamespace(returncode=None)}
        self.sent, self.verified = [], []

    def _command_id(self, node_id, action):
        return f"{node_id}-{action}"

    def _send_before(self, node_id, *, command_id, command, payload, deadline_monotonic_s):
        self.sent.append((node_id, command, payload))
        return {"ok": True, "result": {"observation": {"node_id": node_id}, "signature": {}, "verification_key": {}}}

    def _verify_observation(self, node_id, response, *, event):
        self.verified.append((node_id, event))
        return response["result"]["observation"]


BINDING = {"window_id": WINDOW, "phase": "before", "plan_digest": PREREG, "prereg_digest": PREREG, "benchmark_digest": None}


def test_door_source_sends_the_binding_to_every_live_node_and_returns_verified_envelopes():
    route = _Route()
    document = RouteA11Evidence(route, clock_ms=lambda: 5).collect(BINDING)
    assert route.sent == [("node-0", "a11_snapshot", {"binding": BINDING}), ("node-1", "a11_snapshot", {"binding": BINDING})]
    assert route.verified == [("node-0", "a11_snapshot"), ("node-1", "a11_snapshot")]
    assert set(document["nodes"]) == {"node-0", "node-1"}
    with pytest.raises(A11EvidenceUnavailable, match="a11_evidence_binding_invalid"):
        RouteA11Evidence(route).collect({**BINDING, "phase": "after"})
    route._sessions["node-1"] = SimpleNamespace(returncode=0)
    with pytest.raises(A11EvidenceUnavailable, match="a11_evidence_node_not_live"):
        RouteA11Evidence(route).collect(BINDING)


@pytest.fixture
def door(tmp_path):
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    control = A11EvidenceControl(grant=EXACT_FLEET_GRANT, source=RouteA11Evidence(_Route()),
                                 token_file=private / "a11-evidence.token", product_port=8000)
    control.start()
    token = (private / "a11-evidence.token").read_text().strip()
    yield control, token, private
    control.close()


def _get(control, headers, *, query=None, method="GET", host=None):
    query = query if query is not None else "&".join(f"{k}={v}" for k, v in BINDING.items() if v is not None)
    request = urllib.request.Request(f"http://127.0.0.1:{control.port}{CONTROL_PATH}?{query}", headers=headers, method=method)
    if host is not None:
        request.add_unredirected_header("Host", host)
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


def test_door_requires_grant_token_exact_host_and_no_browser_context(door):
    control, token, private = door
    good = {GRANT_HEADER: EXACT_FLEET_GRANT, TOKEN_HEADER: token}
    assert len(token) == 64 and (os.stat(private / "a11-evidence.token").st_mode & 0o777) == 0o600
    status, document = _get(control, good)
    assert status == 200 and document["evidence"]["binding"] == BINDING
    refused = [
        {},
        {GRANT_HEADER: EXACT_FLEET_GRANT},
        {GRANT_HEADER: EXACT_FLEET_GRANT, TOKEN_HEADER: token[:-1] + ("0" if token[-1] != "0" else "1")},
        {GRANT_HEADER: "FLEET GRANTED TO A7", TOKEN_HEADER: token},
        {**good, "Origin": "http://rebound.example"},
        {**good, "Sec-Fetch-Site": "cross-site"},
        {**good, "Sec-Fetch-Mode": "cors"},
        {**good, "X-Forwarded-For": "1.2.3.4"},
    ]
    for headers in refused:
        assert _get(control, headers)[0] == 403, headers
    # DNS rebinding: a page on a rebound hostname reaches the loopback socket with its own Host.
    assert _get(control, good, host=f"rebound.example:{control.port}")[0] == 403
    assert _get(control, good, host=f"localhost:{control.port}")[0] == 403
    assert _get(control, good, method="POST")[0] == 403
    # The secret is never accepted from the query string.
    assert _get(control, {GRANT_HEADER: EXACT_FLEET_GRANT}, query=f"token={token}")[0] == 403
    assert _get(control, good, query="phase=before")[0] == 409


def test_door_token_file_is_owner_only_and_removed_on_close(tmp_path):
    shared = tmp_path / "shared"
    shared.mkdir(mode=0o755)
    with pytest.raises(A11EvidenceControlRefused, match="a11_evidence_control_token_dir_invalid"):
        A11EvidenceControl(grant=EXACT_FLEET_GRANT, source=RouteA11Evidence(_Route()),
                           token_file=shared / "t", product_port=8000)
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    (private / "t").write_text("pre-existing")
    control = A11EvidenceControl(grant=EXACT_FLEET_GRANT, source=RouteA11Evidence(_Route()),
                                 token_file=private / "t", product_port=8000)
    with pytest.raises(A11EvidenceControlRefused, match="a11_evidence_control_token_file_invalid"):
        control.start()
    (private / "t").unlink()
    control = A11EvidenceControl(grant=EXACT_FLEET_GRANT, source=RouteA11Evidence(_Route()),
                                 token_file=private / "t", product_port=8000)
    control.start()
    assert (private / "t").exists()
    control.close()
    assert not (private / "t").exists()
    with pytest.raises(A11EvidenceControlRefused, match="explicit_fleet_grant_required"):
        A11EvidenceControl(grant="", source=RouteA11Evidence(_Route()), token_file=private / "t", product_port=8000)


def test_runtime_release_log_records_every_release_in_order(run):
    for port in run.mesh.ports:
        log = port.kv_release_log()
        sequences = [item["sequence"] for item in log["releases"]]
        assert sequences == sorted(sequences) and log["release_sequence"] == sequences[-1]
        assert {item["reason"] for item in log["releases"]} == {"normal_completion"}
        assert all(item["active_state_count_after"] == 0 for item in log["releases"])


def test_key_record_is_the_endpoint_key():
    signer = _node_signer()
    assert base64.b64decode(signer.public_key_record()["verification_key"]).hex() == signer.endpoint_id
