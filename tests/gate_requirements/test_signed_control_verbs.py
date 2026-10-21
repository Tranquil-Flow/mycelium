# SPDX-License-Identifier: AGPL-3.0-only
"""Signed gate-scoped control verbs, executed by a member's own agent.

The local implementation drives real child processes on this host, standing in
for the member agents of a bound gate (no SSH, no device).
"""
from __future__ import annotations

import json
import subprocess
import sys

import pytest

from mycelium_gate_requirements import binding as gb
from mycelium_gate_requirements import control as gc
from mycelium_gate_requirements import evidence as ge
from mycelium_gate_requirements import spec as gs
from mycelium_qualification.signing import generate_ed25519_signer

from ._fleet import NOW, macs


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> float:
        return self.now


@pytest.fixture()
def fleet():
    members, reports, synthetic = macs(2)
    binding = gb.bind_roles(gs.load_pinned_spec("A7"), members=members, reports=reports, now=NOW)
    owner = generate_ed25519_signer(endpoint_id="owner")
    clock = Clock()
    processes = []
    agents = {}
    links = {}
    for member in synthetic:
        process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
        processes.append(process)
        executor = gc.LocalProcessExecutor()
        executor.register_process("stage_node", process)
        executor.register_link("seed_link", lambda up, name=member.node_id: links.__setitem__(name, up))
        grant = gc.ControlGrant(gate_id="A7", binding_digest=binding["binding_digest"],
                                verbs=frozenset(gc.VERBS) - {"advance_generation"},
                                expires_at=NOW + 3600)
        agents[member.node_id] = gc.MemberControlAgent(
            node_id=member.node_id, generation=member.generation,
            owner_key_digest=owner.verification_key_digest, grant=grant,
            executor=executor, signer=member.signer, clock=clock)
    channel = gc.LocalControlChannel(agents)
    injector = gc.SignedFaultInjector(channel=channel, owner=owner, binding=binding, clock=clock,
                                      member_keys={m["node_id"]: m["verification_key_digest"]
                                                   for m in members})
    try:
        yield {"binding": binding, "owner": owner, "clock": clock, "processes": processes,
               "agents": agents, "channel": channel, "injector": injector, "links": links,
               "members": members}
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=10)


def test_kill_verb_reaches_only_the_bound_role_member_and_returns_a_signed_receipt(fleet) -> None:
    source, entry = fleet["processes"][1], fleet["processes"][0]
    receipt = fleet["injector"].inject(role="source", verb="kill_process", target="stage_node")
    assert receipt["outcome"] == {"state": "executed", "code": None, "exit_status": -9}
    assert source.poll() == -9 and entry.poll() is None
    assert receipt["role"] == "source" and receipt["verb"] == "kill_process"
    forbidden = ge.forbidden_values(fleet["members"])
    # The signed receipt is private: its signature names the member's endpoint.
    assert ge.host_identifier_findings(receipt, forbidden_values=forbidden)
    public = gc.public_control_receipt(receipt)
    assert ge.host_identifier_findings(public, forbidden_values=forbidden) == []
    assert public["receipt_digest"].startswith("sha256:")
    assert public["outcome"] == receipt["outcome"] and public["role"] == "source"


def test_stop_verb_terminates_gracefully(fleet) -> None:
    receipt = fleet["injector"].inject(role="entry", verb="stop_process", target="stage_node")
    assert receipt["outcome"]["exit_status"] == -15
    assert fleet["processes"][0].poll() == -15


def test_partition_and_heal_drive_the_registered_link_hook(fleet) -> None:
    fleet["injector"].inject(role="source", verb="partition_link", target="seed_link")
    assert fleet["links"] == {"mac-1": False}
    fleet["injector"].inject(role="source", verb="heal_link", target="seed_link")
    assert fleet["links"] == {"mac-1": True}


def test_unknown_target_or_unsupported_verb_is_a_signed_failure_not_an_action(fleet) -> None:
    receipt = fleet["injector"].inject(role="entry", verb="kill_process", target="ghost")
    assert receipt["outcome"] == {"state": "failed", "code": "control_target_unknown",
                                  "exit_status": None}
    assert all(process.poll() is None for process in fleet["processes"])


def _request(fleet, **overrides):
    values = dict(owner=fleet["owner"], binding=fleet["binding"], role="source",
                  verb="kill_process", target="stage_node", request_id="r-1",
                  now=fleet["clock"]())
    values.update(overrides)
    return gc.issue_control_request(**values)


@pytest.mark.parametrize(("make", "code"), [
    (lambda f: _request(f, owner=generate_ed25519_signer(endpoint_id="intruder")),
     "control_request_issuer_untrusted"),
    (lambda f: {**_request(f), "target": "other"}, "control_request_signature_invalid"),
    (lambda f: _request(f, now=f["clock"]() - 3600), "control_request_expired"),
    (lambda f: {**_request(f), "protocol": "mycelium.other.v1"}, "control_request_invalid"),
])
def test_agents_refuse_unauthentic_or_stale_requests(fleet, make, code) -> None:
    with pytest.raises(gc.GateControlError, match=code):
        fleet["channel"].send(make(fleet))
    assert all(process.poll() is None for process in fleet["processes"])


def test_agents_refuse_replay_wrong_member_and_ungranted_scope(fleet) -> None:
    request = _request(fleet)
    fleet["channel"].send(request)
    with pytest.raises(gc.GateControlError, match="control_request_replayed"):
        fleet["channel"].send(request)
    misrouted = _request(fleet, request_id="r-2")
    with pytest.raises(gc.GateControlError, match="control_request_wrong_member"):
        fleet["agents"]["mac-0"].handle(misrouted)
    other_binding = json.loads(json.dumps(fleet["binding"]))
    other_binding["binding_digest"] = "sha256:" + "0" * 64
    with pytest.raises(gc.GateControlError, match="control_request_not_granted"):
        fleet["channel"].send(_request(fleet, binding=other_binding, request_id="r-3"))
    with pytest.raises(gc.GateControlError, match="control_verb_not_granted"):
        fleet["channel"].send(_request(fleet, verb="advance_generation", request_id="r-4"))
    fleet["clock"].now = NOW + 7200
    with pytest.raises(gc.GateControlError, match="control_grant_expired"):
        fleet["channel"].send(_request(fleet, request_id="r-5"))


def test_receipt_verification_binds_request_and_member_key(fleet) -> None:
    request = _request(fleet)
    receipt = fleet["channel"].send(request)
    key = fleet["members"][1]["verification_key_digest"]
    assert gc.verify_control_receipt(receipt, request=request, member_key_digest=key) == receipt
    with pytest.raises(gc.GateControlError, match="control_receipt_signature_invalid"):
        gc.verify_control_receipt({**receipt, "verb": "stop_process"}, request=request,
                                  member_key_digest=key)
    with pytest.raises(gc.GateControlError, match="control_receipt_untrusted"):
        gc.verify_control_receipt(receipt, request=request,
                                  member_key_digest=fleet["members"][0]["verification_key_digest"])
