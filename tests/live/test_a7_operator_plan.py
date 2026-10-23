# SPDX-License-Identifier: AGPL-3.0-only
"""The A7 operator-plan extension, staged and served over two real local nodes.

A synthetic operator plan is assembled from a prepared local deployment in the
builder's layout (``transfer-bundle/control`` + ``deployment``).  The test then
follows the run-day order end to end on this Mac:

1. ``extend_operator_plan(variant="parity")`` and stage each node's selection;
2. the cross-host parity workers measure the standby against the source from
   the staged roots and produce the qualification record;
3. ``extend_operator_plan(variant="positive", qualification=...)`` and stage
   again; each node finds its own ``a7/<node_id>/node.json``;
4. a real ``PhysicalLiveRoute`` opens both node processes from the staged roots,
   the supervisor installs the authority from the nodes' attestations, and the
   plug-in's measurement kills the source and observes a fenced cutover.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from mycelium_a7_operator_plan import A7PlanError, extend_operator_plan, parity_worker_argv
from mycelium_a7_parity_measurement import CONFIG_PROTOCOL, measure_parity
from mycelium_a7_physical_measurement import MeasurementSettings, measure
from mycelium_live.route import PhysicalLiveRoute, _ConcurrentNodeSession
from mycelium_physical_runner.remote_probe import derive_local_run_scoped_identity
from mycelium_qualification.physical_deployment import (
    build_execution_graph,
    build_physical_device_states,
    prepare_physical_deployment,
)
from mycelium_router.layer_builder import layer_load_proof_digest  # noqa: F401 - imported for parity with the builder
from physical_inference_node import execution_graph_from_document
from physical_inference_qualification import PeerIdentity, QualificationController, node_transfer_manifests
from runtime_loader import load_assignment_stage
from scripts.build_qwen_live_route import _bytes, _digest, _physical_graph_document, _transfer_manifest, _write_document
from tests.live import test_a7_product_path as harness
from tests.live.test_a7_physical_measurement import _MAX_NEW as _MEASURED_MAX_NEW
from tests.live.test_a7_physical_measurement import _DecodeHold, _HoldReleasingTarget, _RouteDriver, assert_hold_fired, gate
from tests.physical_qualification.test_node_service import NODE_SCRIPT, ROOT, SIDECAR_BINARY

pytestmark = pytest.mark.skipif(not SIDECAR_BINARY.is_file(), reason="native iroh sidecar not built")
_NODES = (harness._NODE_A, harness._NODE_B)
_CANDIDATE = "d" * 40


def _synthetic_plan(root: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    monkeypatch.setattr(harness.gateway_contracts, "qualification_digest", lambda _q: harness._QUALIFICATION_DIGEST)
    deployment = prepare_physical_deployment(root / "deployment-source", runtime_backend="mlx")
    loaded = [
        load_assignment_stage(assignment, report, load_generation=7)
        for assignment, report in zip(deployment.assignments, deployment.artifact_reports, strict=True)
    ]
    graph = build_execution_graph(deployment.assignments, [stage.proof for stage in loaded], link_scheme="iroh", runtime_scheme="iroh")
    graph_document = _physical_graph_document(graph)
    bundle = root / "transfer-bundle"
    shutil.copytree(deployment.root, bundle / "deployment")
    shutil.copy2(NODE_SCRIPT, bundle / "physical_inference_node.py")
    control = bundle / "control"
    _write_document(control / "model-manifest.json", deployment.manifest)
    _write_document(control / "execution-graph.json", graph_document)
    for index, node in enumerate(_NODES):
        _write_document(control / f"node-{index}-assignment.json", deployment.assignments[index])
        _write_document(control / f"node-{index}-stage-pack.json", deployment.stage_packs[index])
    transfer = _transfer_manifest(bundle)
    every = [record["path"] for record in transfer["files"]]
    states = json.loads(json.dumps({node: asdict(state) for node, state in build_physical_device_states(graph).items()}))
    run_id = "a7plan" + uuid.uuid4().hex[:20]
    return {
        "protocol": "mycelium.physical_runner_operator_plan.v1",
        "plan_id": "a7-local-plan",
        "run_id": run_id,
        "controller": {
            "source_root": str(bundle),
            "peers": [{"node_id": node, "staging_root": str(root / "stage" / node)} for node in _NODES],
            "transfer_manifest": transfer,
            "node_transfer_manifests": node_transfer_manifests(transfer, {node: list(every) for node in _NODES}),
            "membership_snapshot": {
                "assignment_offers": [
                    {"message": {"recipient_node_id": node, "graph_digest": _digest(_bytes(graph_document))}} for node in _NODES
                ]
            },
            "run_plan": {
                "protocol": "mycelium.controller_run_plan.v1",
                "run_id": run_id,
                "deployment_id": graph.deployment_id,
                "entry_node_id": _NODES[0],
                "nodes": [
                    {
                        "node_id": node,
                        "configure": {
                            "assignment_file": f"control/node-{index}-assignment.json",
                            "manifest_file": "control/model-manifest.json",
                            "stage_pack_file": f"control/node-{index}-stage-pack.json",
                            "graph": graph_document,
                            "device_states": states,
                            "load_generation": 7,
                        },
                    }
                    for index, node in enumerate(_NODES)
                ],
            },
        },
        "_reference_tokens": list(harness._reference_tokens(deployment)),
    }


def _stage(plan: dict[str, Any]) -> None:
    """Copy each node's transfer selection from the plan's bundle into its staging root."""

    controller = plan["controller"]
    bundle = Path(controller["source_root"])
    for peer in controller["peers"]:
        target = Path(peer["staging_root"])
        if target.exists():
            shutil.rmtree(target)
        for record in controller["node_transfer_manifests"]["manifests"][peer["node_id"]]["files"]:
            destination = target / record["path"]
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(bundle / record["path"], destination)


def _parity_config(plan: dict[str, Any], summary: dict[str, Any], tmp_path: Path) -> dict[str, Any]:
    graph = execution_graph_from_document(plan["controller"]["run_plan"]["nodes"][0]["configure"]["graph"])
    entry = graph.stages[0].placements[0]
    source = graph.stages[1].placements[0]

    def worker(node: str, placement: str, assignment: str, pack: str) -> list[str]:
        return parity_worker_argv(plan, role_node=node, placement_id=placement, assignment_file=assignment, stage_pack_file=pack, python=sys.executable, code_root=str(ROOT), ssh=None)

    return {
        "protocol": CONFIG_PROTOCOL,
        "source_node_id": summary["source_node_id"],
        "standby_node_id": summary["standby_node_id"],
        "prompt_token_ids": list(harness._PROMPT),
        "challenge_index": 1,
        "workers": {
            "entry": worker(_NODES[0], entry.placement_id, "control/node-0-assignment.json", "control/node-0-stage-pack.json"),
            "source": worker(_NODES[1], source.placement_id, "control/node-1-assignment.json", "control/node-1-stage-pack.json"),
            "standby": worker(_NODES[0], summary["standby_placement_id"], "a7/standby-assignment.json", "a7/standby-stage-pack.json"),
        },
        "max_checkpoint_bytes": 1 << 24,
        "worker_timeout_s": 120,
        "qualification": {"out": str(tmp_path / "qualification.json"), "reserved_kv_bytes": 1 << 20, "ttl_ms": 3_600_000},
    }


class _StagedSessionFactory:
    def __init__(self, roots: dict[str, Path], socket_roots: dict[str, Path]) -> None:
        self._roots = roots
        self._socket_roots = socket_roots
        self.sessions: dict[str, Any] = {}
        self.token_files = {}

    def __call__(self, *, argv: tuple[str, ...], node_id: str, run_id: str, deployment_id: str, timeout_seconds: float) -> Any:
        del argv
        command = (
            sys.executable, "-B", str(NODE_SCRIPT), "--run-id", run_id, "--deployment-id", deployment_id,
            "--node-id", node_id, "--artifact-root", str(self._roots[node_id]), "--socket-root", str(self._socket_roots[node_id]),
            "--sidecar-binary", str(SIDECAR_BINARY), "--sidecar-local-only", "--command-timeout", "900", "--decode-mode", "stage_local_kv",
        )
        if self.token_files:
            command += (
                "--endpoint-secret-file", str(self.token_files[node_id][0]),
                "--token-policy-trust-file", str(self.token_files[node_id][1]))
        session = _ConcurrentNodeSession(argv=command,
            node_id=node_id, run_id=run_id, deployment_id=deployment_id, timeout_seconds=timeout_seconds)
        self.sessions[node_id] = session
        return session


def _route_from_plan(plan: dict[str, Any], root: Path) -> SimpleNamespace:
    controller = plan["controller"]
    run_plan = controller["run_plan"]
    graph = execution_graph_from_document(run_plan["nodes"][0]["configure"]["graph"])
    roots = {peer["node_id"]: Path(peer["staging_root"]) for peer in controller["peers"]}
    socket_roots = {node: root / "sockets" / node for node in _NODES}
    for path in [*roots.values(), *socket_roots.values()]:
        path.mkdir(parents=True, exist_ok=True)
        path.chmod(0o700)
    for node in _NODES:
        (root / "identity" / node).mkdir(parents=True, exist_ok=True)
    host_id, boot_id = derive_local_run_scoped_identity(plan["run_id"])
    peers = tuple(
        PeerIdentity(node_id=node, ssh_target="local@example.com", host_id=host_id, boot_id=boot_id, staging_root=str(roots[node]), process_transport="local")
        for node in _NODES
    )
    route_plan = {
        **{key: value for key, value in run_plan.items() if key != "nodes"},
        "nodes": [
            {
                **node,
                "python_executable": sys.executable,
                "socket_root": str(socket_roots[node["node_id"]]),
                "sidecar_binary": str(SIDECAR_BINARY),
                "endpoint_secret_file": str(root / "identity" / node["node_id"] / "endpoint.key"),
            }
            for node in run_plan["nodes"]
        ],
        "request": harness._request("startup-a7-plan"),
        "decode_count": harness._MAX_NEW - 1,
        "expected_token_ids": plan["_reference_tokens"],
    }
    controller_object = QualificationController(
        mode="local",
        peers=peers,
        source_root=ROOT,
        transfer_manifest={"protocol": "mycelium.controller_transfer_manifest.v1", "files": [{"path": "physical_inference_node.py", "size_bytes": NODE_SCRIPT.stat().st_size, "content_digest": harness._file_digest(NODE_SCRIPT)}]},
        membership_snapshot={"protocol": "mycelium.controller_membership_snapshot.v1", "deployment_id": graph.deployment_id, "assignment_offers": []},
        now=time.time(),
        run_plan=route_plan,
    )
    factory = _StagedSessionFactory(roots, socket_roots)
    from tests.live.local_token_authority import LocalTokenAuthority
    endpoints = {node: {"endpoint_id": f"pending-{node}", "membership_generation": index + 1}
                 for index, node in enumerate(_NODES)}
    token_authority = LocalTokenAuthority(SimpleNamespace(
        root=root, node_ids=_NODES, endpoints_template=endpoints,
        run_plan=route_plan, session_factory=factory))
    factory.token_files = token_authority.files
    route = PhysicalLiveRoute(
        controller=controller_object,
        endpoints=endpoints,
        run_plan=route_plan,
        session_factory=factory,
        token_path_owner=token_authority.owner,
    )
    harness._install_endpoint_learning_verifier(route)
    return SimpleNamespace(route=route, graph=graph, session_factory=factory)


_EXPECTED = {
    "positive": ("fenced_kv_successor", None),
    "lagging": ("full_context_replay", "route_watermark_unavailable"),
    "abort": ("aborted", "route_watermark_unavailable"),
    "incompatible": ("full_context_replay", None),
}


@pytest.mark.parametrize(
    "variant",
    [
        "abort",
        pytest.param(
            "incompatible",
            marks=pytest.mark.skip(
                reason="the numpy standby needs stage_local_kv, which numpy supports for qwen2 (the physical 0.5B) but not for this gpt2 fixture model"
            ),
        ),
        "lagging",
        "positive",
    ],
)
def test_extension_stages_measures_and_serves_each_case_variant(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, variant: str) -> None:
    from mycelium_a7_parity_measurement import measure_identity_qualification

    # The measurement kills the source only on a settled watermark under the
    # freeze; an unsettled freeze is resumed and the request advances.  A
    # 5-token request leaves about three freezes after the second commit, so
    # under load it often completed before one settled
    # (a7_request_ended_before_fault).  Give the measured request the same
    # headroom as the physical measurement tests (the plan's reference tokens
    # and startup decode count follow ``_MAX_NEW``).
    monkeypatch.setattr(harness, "_MAX_NEW", _MEASURED_MAX_NEW)
    root = Path(tempfile.mkdtemp(prefix="mycelium-a7-plan-", dir="/tmp")).resolve()
    root.chmod(0o700)
    state = None
    try:
        base = _synthetic_plan(root, monkeypatch)
        staging_variant = "parity_incompatible" if variant == "incompatible" else "parity"
        parity_plan, parity_summary = extend_operator_plan(base, out_bundle=root / "bundle-parity", variant=staging_variant, source_node_id=_NODES[1], standby_node_id=_NODES[0])
        assert parity_summary["configured_nodes"] == []
        _stage(parity_plan)
        config = _parity_config(parity_plan, parity_summary, tmp_path)
        if variant == "incompatible":
            qualification = measure_identity_qualification(config)
            assert qualification["qualified"] is False and qualification["reason"] == "identity_incompatible", qualification
            assert "runtime.backend" in qualification["mismatched_fields"]
        else:
            qualification = measure_parity(config, candidate_commit=_CANDIDATE)["qualification"]
            assert qualification["qualified"] is True, qualification

        plan, summary = extend_operator_plan(base, out_bundle=root / f"bundle-{variant}", variant=variant, source_node_id=_NODES[1], standby_node_id=_NODES[0], qualification=qualification)
        controller = plan["controller"]
        graph_document = controller["run_plan"]["nodes"][0]["configure"]["graph"]
        bundle = Path(controller["source_root"])
        # The graph file, every node's configure graph and every offer agree.
        assert json.loads((bundle / "control/execution-graph.json").read_text()) == graph_document
        assert {e["message"]["graph_digest"] for e in controller["membership_snapshot"]["assignment_offers"]} == {_digest(_bytes(graph_document))}
        assert controller["run_plan"]["decode_mode"] == "stage_local_kv"
        selections = {node: {r["path"] for r in m["files"]} for node, m in controller["node_transfer_manifests"]["manifests"].items()}
        # The standby's key, assets and the qualification never reach the source.
        assert {"a7/standby.key", "a7/standby-assignment.json", "a7/qualification.json", f"a7/{_NODES[0]}/node.json"} <= selections[_NODES[0]]
        assert not any(path.startswith("a7/") and path != f"a7/{_NODES[1]}/node.json" for path in selections[_NODES[1]])
        assert f"a7/{_NODES[1]}/node.json" in selections[_NODES[1]]
        with pytest.raises(A7PlanError):
            extend_operator_plan(plan, out_bundle=root / "bundle-twice", variant=variant, source_node_id=_NODES[1], standby_node_id=_NODES[0], qualification=qualification)
        _stage(plan)

        from mycelium_a7_evidence_control import RouteA7Evidence

        # Measured once, never retried: the request is held after the second
        # token until the kill lands (``_DecodeHold``), so the request cannot
        # end before the fault however fast the local node decodes.  A lagging
        # standby's entry has already recorded its ack lag at that point (the
        # second token's decode waited out the ack window for the first).
        state = _route_from_plan(plan, root / "route")
        harness._open_with_a7_authority(state)
        state.route._deployment_qualification = SimpleNamespace(route_ready=True)
        hold = _DecodeHold(2)
        try:
            fields, observations = measure(
                driver=_RouteDriver(state, hold=hold),
                evidence=RouteA7Evidence(state.route),
                fault=_HoldReleasingTarget(transport="local", process_pattern=re.escape(str(NODE_SCRIPT)) + r" .*--run-id " + re.escape(plan["run_id"]) + r" .*--node-id " + re.escape(_NODES[1]) + r"(\s|$)", hold=hold),
                settings=MeasurementSettings(kill_after_committed=2, settle_timeout_s=10.0, result_timeout_s=20.0, terminal_timeout_s=120.0),
            )
        finally:
            hold.release("measurement_returned")
        assert_hold_fired(hold, observations, 2)
        outcome, reason = _EXPECTED[variant]
        assert fields["outcome"] == outcome, observations["a7_result"]
        if reason is not None:
            assert fields["reason"] == reason, observations["a7_result"]
        assert fields["fault"] == "source_process_kill"
        assert fields["cleanup"] == {_NODES[0]: {name: 0 for name in gate._CLEANUP_FIELDS}}
        if variant == "positive":
            assert fields["reference_parity"] is True
            assert fields["successor_recovery_prefill_operations"] == 0 and fields["successor_decode_operations"] >= 1
        else:
            assert fields["successor_decode_operations"] == 0 and fields["kv_continuity_claimed"] is False
        if variant in {"lagging", "abort"}:
            assert observations["fault"]["kill_condition"] == "entry_recorded_ack_lag"
            assert observations["acknowledged_watermark_absent"] is True
    finally:
        if state is not None:
            state.route.close()
        shutil.rmtree(root, ignore_errors=True)


@pytest.mark.parametrize(
    ("variant", "check"),
    [
        ("abort", lambda configs: configs["node-a"]["entry"]["a6_available"] is False and configs["node-a"]["standby"]["max_checkpoint_bytes"] == 1),
        ("lagging", lambda configs: configs["node-a"]["standby"]["max_checkpoint_bytes"] == 1 and configs["node-a"]["entry"]["ack_timeout_seconds"] == 0.05),
        ("positive", lambda configs: configs["node-a"]["entry"]["a6_available"] is True),
    ],
)
def test_case_variants_change_only_their_controlled_knob(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, variant: str, check: Any) -> None:
    base = _synthetic_plan(tmp_path, monkeypatch)
    from mycelium_a7_standby_qualification import parity_challenge_result, qualify_standby

    identity = {"lifecycle_state": "ACTIVE", "placement_id": "s", "node_id": "node-b", "load_proof_digest": "p"}
    qualification = qualify_standby(
        source_identity=identity,
        standby_identity={**identity, "lifecycle_state": "STANDBY", "placement_id": "t", "node_id": "node-a"},
        parity_challenge=parity_challenge_result(challenge_id="c", next_position=3, source_output=b"x", standby_output=b"x"),
        reserved_kv_bytes=1,
        available_bytes=2,
        issued_at_unix_ms=1,
        ttl_ms=10,
    )
    plan, summary = extend_operator_plan(base, out_bundle=tmp_path / f"bundle-{variant}", variant=variant, source_node_id="node-b", standby_node_id="node-a", qualification=qualification)
    bundle = Path(plan["controller"]["source_root"])
    configs = {node: json.loads((bundle / f"a7/{node}/node.json").read_text()) for node in _NODES}
    assert check(configs)
    assert summary["standby_runtime_backend"] == "mlx"


def test_incompatible_variant_stages_a_numpy_standby(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    base = _synthetic_plan(tmp_path, monkeypatch)
    from mycelium_a7_standby_qualification import qualify_standby

    identity = {"lifecycle_state": "ACTIVE", "placement_id": "s", "node_id": "node-b", "load_proof_digest": "p", "runtime.backend": "mlx"}
    qualification = qualify_standby(
        source_identity=identity,
        standby_identity={**identity, "lifecycle_state": "STANDBY", "placement_id": "t", "node_id": "node-a", "runtime.backend": "numpy"},
        parity_challenge=None,
        reserved_kv_bytes=1,
        available_bytes=2,
        issued_at_unix_ms=1,
        ttl_ms=10,
    )
    assert qualification["qualified"] is False
    plan, summary = extend_operator_plan(base, out_bundle=tmp_path / "bundle-incompatible", variant="incompatible", source_node_id="node-b", standby_node_id="node-a", qualification=qualification)
    graph = execution_graph_from_document(plan["controller"]["run_plan"]["nodes"][0]["configure"]["graph"])
    standby = next(p for s in graph.stages for p in s.placements if p.lifecycle_state == "STANDBY")
    assert summary["standby_runtime_backend"] == "numpy" and standby.runtime_backend == "numpy"
    assert json.loads((Path(plan["controller"]["source_root"]) / "a7/standby-assignment.json").read_text())["runtime"]["backend"] == "numpy"


def _run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-B", str(ROOT / "scripts" / "build_a7_operator_plan.py"), *args], cwd=ROOT, capture_output=True, text=True, timeout=300, check=False)


def test_cli_topology_runs_the_laptop_source_on_mlx_from_the_bound_base_plan(tmp_path: Path) -> None:
    def offer(recipient: str, other: str, generation: int) -> dict[str, Any]:
        return {"message": {"recipient_node_id": recipient, "generation": generation, "peer_endpoint_records": [{"node_id": other, "endpoint_id": f"endpoint-{other}"}]}}

    base = {
        "controller": {
            "peers": [
                {"node_id": "node-0", "process_transport": "local", "ssh_target": "me@m4pro", "ssh_identity_file": None, "staging_root": "/Users/me/w6-n0"},
                {"node_id": "node-2", "process_transport": "ssh", "ssh_target": "member@laptop", "ssh_identity_file": "/k", "staging_root": "/Users/member/w6-n2"},
            ],
            "run_plan": {"nodes": [
                {"node_id": "node-0", "python_executable": "/py0", "sidecar_binary": "/sc0", "endpoint_secret_file": "/id0"},
                {"node_id": "node-2", "python_executable": "/py2", "sidecar_binary": "/sc2", "endpoint_secret_file": "/id2"},
            ]},
            "membership_snapshot": {"assignment_offers": [offer("node-0", "node-2", 25), offer("node-2", "node-0", 48)]},
        }
    }
    (tmp_path / "base.json").write_text(json.dumps(base))
    completed = _run_cli(
        "topology", "--base-plan", str(tmp_path / "base.json"), "--out", str(tmp_path / "topology.json"),
        "--staging-root", "node-0=/Users/me/a7-n0", "--staging-root", "node-2=/Users/member/a7-n2",
        "--python", "node-2=/Users/member/mycelium-pm-x/venv/bin/python", "--backend", "node-2=mlx",
    )
    assert completed.returncode == 0, completed.stderr
    topology = json.loads((tmp_path / "topology.json").read_text())
    by_node = {node["node_id"]: node for node in topology["nodes"]}
    assert topology["protocol"] == "mycelium.qwen_live_topology.v1"
    assert by_node["node-2"]["runtime_backend"] == "mlx" and by_node["node-0"]["runtime_backend"] == "mlx"
    assert by_node["node-2"]["endpoint_id"] == "endpoint-node-2" and by_node["node-2"]["membership_generation"] == 48
    assert by_node["node-2"]["python_executable"].endswith("venv/bin/python") and by_node["node-0"]["python_executable"] == "/py0"
    assert by_node["node-2"]["staging_root"] == "/Users/member/a7-n2"
    assert _run_cli("topology", "--base-plan", str(tmp_path / "base.json"), "--out", str(tmp_path / "t2.json"), "--backend", "node-9=mlx").returncode != 0


def test_cli_extend_and_parity_config_for_a_remote_source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    base = _synthetic_plan(tmp_path, monkeypatch)
    base["controller"]["peers"][1].update(process_transport="ssh", ssh_target="member@laptop", ssh_identity_file="/k")
    base["controller"]["run_plan"]["request"] = {"prompt_token_ids": list(harness._PROMPT)}
    (tmp_path / "plan.json").write_text(json.dumps(base))
    completed = _run_cli(
        "extend", "--plan", str(tmp_path / "plan.json"), "--variant", "parity", "--source-node", "node-b", "--standby-node", "node-a",
        "--out-bundle", str(tmp_path / "bundle"), "--out-plan", str(tmp_path / "parity-plan.json"), "--summary-out", str(tmp_path / "summary.json"),
    )
    assert completed.returncode == 0, completed.stderr
    completed = _run_cli(
        "parity-config", "--plan", str(tmp_path / "parity-plan.json"), "--summary", str(tmp_path / "summary.json"),
        "--out", str(tmp_path / "parity-config.json"), "--qualification-out", str(tmp_path / "q.json"),
        "--local-python", "/opt/homebrew/bin/python3.14", "--local-code-root", "/c", "--remote-python", "/Users/member/venv/bin/python",
        "--remote-code-root", "/Users/member/mycelium-pm-x/src",
    )
    assert completed.returncode == 0, completed.stderr
    config = json.loads((tmp_path / "parity-config.json").read_text())
    source = config["workers"]["source"]
    assert source[:1] == ["ssh"] and "member@laptop" in source and "/k" in source
    assert "PYTHONPATH=/Users/member/mycelium-pm-x/src" in source[-1] and "mycelium_a7_parity_worker" in source[-1]
    assert config["workers"]["standby"][:2] == ["/usr/bin/env", "PYTHONPATH=/c"]
    assert "a7/standby-assignment.json" in config["workers"]["standby"]
    assert oct((tmp_path / "parity-config.json").stat().st_mode & 0o777) == "0o600"
