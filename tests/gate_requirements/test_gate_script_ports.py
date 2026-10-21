# SPDX-License-Identifier: AGPL-3.0-only
"""In-repo gate scripts take roles from a requirements binding, not host literals.

Legacy fleet defaults stay callable for the old physical runbooks; every new
path is driven by a verified role binding of the gate's pinned spec.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import pytest

from mycelium_gate_requirements import binding as gb
from mycelium_gate_requirements import control as gc
from mycelium_gate_requirements import spec as gs
from mycelium_qualification.signing import generate_ed25519_signer

from ._fleet import NOW, SyntheticMember, macs

ROOT = Path(__file__).resolve().parents[2]


def _script(name: str):
    spec = importlib.util.spec_from_file_location(f"gate_port_{name}", ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _binding(gate_id: str, count: int = 3, names=None):
    members, reports, synthetic = macs(count, names=names)
    return gb.bind_roles(gs.load_pinned_spec(gate_id), members=members, reports=reports,
                         now=NOW), members, synthetic


def _write(tmp_path, document, name="binding.json"):
    path = tmp_path / name
    path.write_text(json.dumps(document), "utf-8")
    return path


# -- A7 ----------------------------------------------------------------------

def test_a7_plan_topology_comes_from_the_role_binding() -> None:
    gate = _script("run_a7_kv_successor_gate")
    legacy = gate.build_plan("a" * 40)
    assert legacy["topology"]["source_node"] == "node-2"  # legacy runbook path unchanged
    binding, _, _ = _binding("A7", 2, names=("alpha", "beta"))
    plan = gate.build_plan("a" * 40, role_binding=binding)
    topology = plan["topology"]
    assert (topology["entry_node"], topology["source_node"], topology["standby_node"]) == (
        "alpha", "beta", "alpha")
    assert topology["standby_backend"] == "mlx" and topology["covered_stage_index"] == 1
    assert plan["role_binding_digest"] == binding["binding_digest"]
    assert plan["device_classes"]["source"][0]["os_family"] == "macos"
    assert gate._verify_plan(plan) == plan
    wrong, _, _ = _binding("A6", 3)
    with pytest.raises(ValueError, match="gate_role_binding_spec_mismatch"):
        gate.build_plan("a" * 40, role_binding=wrong)


def test_a7_standby_node_resolves_from_binding_or_legacy_flag(tmp_path) -> None:
    standby = _script("build_a7_standby_plan")
    binding, _, _ = _binding("A7", 2, names=("alpha", "beta"))
    path = _write(tmp_path, binding)
    assert standby.resolve_standby_node(argparse.Namespace(standby_node=None, role_binding=path)) == "alpha"
    assert standby.resolve_standby_node(argparse.Namespace(standby_node="x-1", role_binding=None)) == "x-1"
    assert standby.resolve_standby_node(
        argparse.Namespace(standby_node=None, role_binding=None)) == standby.LEGACY_STANDBY_NODE
    with pytest.raises(ValueError, match="standby_node_conflicts_with_role_binding"):
        standby.resolve_standby_node(argparse.Namespace(standby_node="beta", role_binding=path))


# -- A6 ----------------------------------------------------------------------

def test_a6_plan_placements_come_from_a_verified_role_binding(tmp_path) -> None:
    builder = _script("build_a6_execution_plan")
    gate = _script("run_a6_recovery_gate")
    binding, _, _ = _binding("A6", 3, names=("alpha", "beta", "gamma"))
    plan = builder.build_plan(repo_root=ROOT, operator_plan=tmp_path / "operator-plan.json",
                              evidence_root=tmp_path / "evidence", role_binding=binding)
    assert plan["expected_placements"] == ["alpha", "beta", "gamma"]
    assert plan["role_binding"]["binding_digest"] == binding["binding_digest"]
    assert gate.validate_plan(plan)["expected_placements"] == ["alpha", "beta", "gamma"]
    tampered = json.loads(json.dumps(plan))
    tampered["expected_placements"] = ["alpha", "gamma", "beta"]
    with pytest.raises(ValueError, match="physical_plan_placements_invalid"):
        gate.validate_plan(tampered)
    forged = json.loads(json.dumps(plan))
    forged["role_binding"]["roles"]["entry"][0]["node_id"] = "delta"
    with pytest.raises(ValueError, match="physical_plan_role_binding_invalid"):
        gate.validate_plan(forged)
    # The legacy fleet placements remain admissible without a binding.
    legacy = builder.build_plan(repo_root=ROOT, operator_plan=tmp_path / "operator-plan.json",
                                evidence_root=tmp_path / "evidence")
    assert legacy["expected_placements"] == list(gate.EXPECTED_PLACEMENTS)
    assert gate.preparation_plan()["requirements_spec_digest"] == gs.load_pinned_spec("A6").digest


# -- A5 ----------------------------------------------------------------------

def test_a5_replica_kill_goes_through_a_signed_control_verb(tmp_path) -> None:
    gate = _script("run_a5_replica_loss_gate")
    binding, members, synthetic = _binding("A5", 3)
    owner = generate_ed25519_signer(endpoint_id="owner")
    import subprocess
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    try:
        replica = gb.role_node_ids(binding, "replica")[-1]
        member = next(item for item in synthetic if item.node_id == replica)
        executor = gc.LocalProcessExecutor()
        executor.register_process("replica_data_sidecar", process)
        agent = gc.MemberControlAgent(
            node_id=replica, generation=member.generation,
            owner_key_digest=owner.verification_key_digest,
            grant=gc.ControlGrant(gate_id="A5", binding_digest=binding["binding_digest"],
                                  verbs=frozenset({"kill_process"}), expires_at=NOW + 600),
            executor=executor, signer=member.signer, clock=lambda: NOW)
        injector = gc.SignedFaultInjector(
            channel=gc.LocalControlChannel({replica: agent}), owner=owner, binding=binding,
            clock=lambda: NOW, member_keys={m["node_id"]: m["verification_key_digest"] for m in members})
        fault = gate.SignedReplicaKill(injector=injector, member_index=1)
        observed = fault.inject()
        assert process.wait(timeout=10) == -9
        assert observed["fault_transport"] == "signed_control"
        assert observed["control_receipt_digest"].startswith("sha256:")
        assert "sidecar_pid" not in observed
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)


def test_a5_legacy_ssh_kill_is_kept_and_marked_legacy(monkeypatch) -> None:
    gate = _script("run_a5_replica_loss_gate")
    calls = []
    monkeypatch.setattr(gate, "_ssh", lambda _args, remote, timeout=30.0: calls.append(remote) or "4242")
    args = argparse.Namespace(sidecar_socket_path="/tmp/sock", ssh_host=gate.DEFAULT_SSH_HOST,
                              ssh_key=None)
    observed = gate.LegacySshSidecarKill(args).inject()
    assert observed == {"fault_transport": "legacy_ssh", "sidecar_pid": 4242}
    assert calls == ["lsof -t /tmp/sock", "kill 4242"]
    assert gate.DEFAULT_SSH_HOST == gate.LEGACY_SSH_HOST


# -- A4 ----------------------------------------------------------------------

def test_a4_negative_peers_come_from_the_binding_or_legacy_flags(tmp_path) -> None:
    gate = _script("run_a4_negative_data_plane_gate")
    binding, _, _ = _binding("A1", 2, names=("alpha", "beta"))
    path = _write(tmp_path, binding)
    assert gate.peer_roles(argparse.Namespace(role_binding=path, faulted_node=None,
                                              healthy_node=None)) == ("beta", "alpha")
    assert gate.peer_roles(argparse.Namespace(role_binding=None, faulted_node=None,
                                              healthy_node=None)) == ("node-2", "node-0")


def test_a4_completion_record_placements_are_a_parameter() -> None:
    gate = _script("run_a4_qualification_gate")
    assert gate.LEGACY_A4_PLACEMENT_NODES == ("node-0", "node-2")
    import inspect
    assert "placement_nodes" in inspect.signature(gate.write_completion_record).parameters


# -- route building ----------------------------------------------------------

def test_stage_live_route_defaults_to_the_running_interpreter(monkeypatch, tmp_path) -> None:
    stage = _script("stage_live_route")
    parser_defaults = stage.build_parser().parse_args(["--operator-plan", str(tmp_path / "p.json")])
    assert parser_defaults.python == sys.executable


def test_route_topology_must_match_the_role_binding() -> None:
    builder = _script("build_qwen_live_route")
    binding, _, _ = _binding("A1", 2, names=("alpha", "beta"))
    topology = [{"node_id": "alpha", "runtime_backend": "mlx"},
                {"node_id": "beta", "runtime_backend": "mlx"}]
    builder.check_topology_against_role_binding(topology, binding)
    with pytest.raises(RuntimeError, match="topology_role_binding_mismatch"):
        builder.check_topology_against_role_binding(list(reversed(topology)), binding)
    with pytest.raises(RuntimeError, match="topology_role_binding_backend_mismatch"):
        builder.check_topology_against_role_binding(
            [topology[0], {"node_id": "beta", "runtime_backend": "numpy"}], binding)


def test_template_backend_prefers_the_declared_runtime_backend() -> None:
    builder = _script("build_qwen_live_route")
    assert builder._template_backend({"runtime_backend": "numpy"}, index=0) == "numpy"
    assert builder._template_backend({}, index=0) == "mlx"
    assert builder._template_backend({}, index=1) == "numpy"


# -- seed + platform admission ------------------------------------------------

def test_route_assignment_member_comes_from_a_binding_role(tmp_path) -> None:
    from mycelium_seed import route_assignment as ra
    phone = SyntheticMember("phone-a", peer_class="android_native_iroh")
    mac_members, mac_reports, _ = macs(2)
    members = mac_members + [phone.seed_record()]
    binding = gb.bind_roles(gs.load_pinned_spec("A12"), members=members,
                            reports=mac_reports + [phone.v2_report()], now=NOW)
    database = tmp_path / "state.sqlite3"
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE seed_members (node_id TEXT, peer_class TEXT, generation INTEGER, "
                       "lifecycle_state TEXT, lease_expires_at REAL)")
    for record in members:
        connection.execute("INSERT INTO seed_members VALUES (?,?,?,?,?)",
                           (record["node_id"], record["peer_class"], record["generation"],
                            record["lifecycle_state"], record["lease_expires_at"]))
    connection.commit()
    connection.close()
    assert ra.member_for_gate_role(database, binding, "phone", now=lambda: NOW) == "phone-a"
    with pytest.raises(ra.SeedCoordinatorError, match="seed_member_not_current"):
        ra.member_for_gate_role(database, binding, "phone", now=lambda: NOW + 3600)


def test_linux_admission_declares_a_device_class_not_a_host() -> None:
    from mycelium_a13_linux import platform_admission as pa
    assert "node" not in pa.DECLARED_TARGET["device_label"]
    assert pa.DECLARED_TARGET["device_class"] == {
        "os_family": "linux", "architectures": sorted(pa.ADMITTED_ARCHITECTURES)}


def test_bind_gate_roles_cli_binds_or_refuses_by_name(tmp_path) -> None:
    import subprocess

    members, reports, _ = macs(2, names=("Evis-MacBook-Pro", "astra-sb2"))
    (tmp_path / "members.json").write_text(json.dumps(members), "utf-8")
    (tmp_path / "reports").mkdir()
    for index, report in enumerate(reports):
        (tmp_path / "reports" / f"{index}.json").write_text(json.dumps(report), "utf-8")
    command = [sys.executable, str(ROOT / "scripts" / "bind_gate_roles.py"), "--members",
               str(tmp_path / "members.json"), "--reports", str(tmp_path / "reports"),
               "--now", str(NOW)]
    bound = subprocess.run([*command, "--gate", "A7", "--binding-out", str(tmp_path / "b.json"),
                            "--receipt-out", str(tmp_path / "r.json")],
                           capture_output=True, text=True, timeout=60, check=False)
    assert bound.returncode == 0, bound.stderr
    receipt = (tmp_path / "r.json").read_text("utf-8")
    assert "Evis-MacBook-Pro" not in receipt and "astra-sb2" not in receipt
    assert "Evis-MacBook-Pro" in (tmp_path / "b.json").read_text("utf-8")
    assert (tmp_path / "b.json").stat().st_mode & 0o777 == 0o600
    refused = subprocess.run([*command, "--gate", "A5"], capture_output=True, text=True,
                             timeout=60, check=False)
    assert refused.returncode == 2
    refusal = json.loads(refused.stdout)
    # Each role alone has two eligible Macs; three distinct members are needed.
    assert refusal["reasons"] == ["requirements_unsatisfied:joint_assignment"]
    assert refusal["detail"]["joint_assignment"]["first_unfillable_role"] == "replica"
    assert "Evis-MacBook-Pro" not in refused.stdout


def test_route_assignment_cli_accepts_a_gate_role_binding_on_every_command(tmp_path) -> None:
    from mycelium_seed import route_assignment as ra

    parser = ra._parser()
    status = parser.parse_args(["--state-root", str(tmp_path), "status", "--wait-applied", "1",
                                "--gate-role-binding", str(tmp_path / "b.json")])
    assert status.wait_applied == 1.0 and status.gate_role == "phone"
    withdraw = parser.parse_args(["--state-root", str(tmp_path), "withdraw", "--gate-role", "final"])
    assert withdraw.gate_role == "final" and withdraw.gate_role_binding is None
