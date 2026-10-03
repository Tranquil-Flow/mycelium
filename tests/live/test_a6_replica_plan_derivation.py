"""Offline derivation of the two-disjoint-path A6 plan (complete-model replicas).

Uses the tiny offline deployment; never touches a peer or the fleet.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from mycelium_m16_runtime import build_live_m16_runtime
from mycelium_qualification.physical_deployment import prepare_physical_deployment
from mycelium_router.contracts import RequestContext
from scripts import derive_a6_replica_plan as derive


def _deployment(tmp_path: Path):
    return prepare_physical_deployment(tmp_path / "deployment", runtime_backend="mlx")


def _request(request_id: str) -> RequestContext:
    return RequestContext(
        request_id=request_id, prompt_token_ids=(1, 2, 3), max_new_tokens=2,
        expected_new_tokens=2, qos_class="interactive", admitted_at=0.0,
        target_ttft_ms=1_000.0, target_tpot_ms=1_000.0, target_tokens_per_second=1.0,
        sampling_seed=17, generation_config_digest="sha256:" + "a" * 64)


def test_replica_documents_bind_two_complete_disjoint_paths(tmp_path) -> None:
    deployment = _deployment(tmp_path)
    documents = derive.replica_documents(
        manifest=deployment.manifest,
        templates=deployment.assignments,
        local_artifact_root=deployment.root,
        load_generation=7,
        challenge_prompt=(1, 2, 3),
        challenge_count=3,
    )
    graph = documents["graph"]
    assert len(graph.stages) == 1
    placements = graph.stages[0].placements
    assert [(p.placement_id, p.node_id) for p in placements] == [
        ("placement-000", "node-a"), ("placement-001", "node-b")]
    for assignment in documents["assignments"]:
        assert tuple(assignment["components"]) == derive.FULL_COMPONENTS
        assert assignment["range"]["start_layer"] == 0
        assert assignment["range"]["end_layer_exclusive"] == deployment.manifest["num_layers"]
    # Each pack carries every shard: both hosts can serve the whole model.
    shard_sets = [
        {artifact["upstream_path"] for artifact in pack["artifacts"]}
        for pack in documents["packs"]
    ]
    assert shard_sets[0] == shard_sets[1]
    outputs = documents["challenge_outputs"]
    assert outputs["node-a"] == outputs["node-b"] and len(outputs["node-a"]) == 3

    # The ordinary M16 runtime finds a disjoint successor for the source path.
    coordinator = build_live_m16_runtime(graph)
    request = _request("request-a6r")
    source = coordinator.admit(request, workload_profile_id="interactive_chat_v1")
    assert coordinator.next_dispatch() == request.request_id
    source_placements = {hop.placement_id for hop in source.ordered_hops}
    successor = coordinator.reserve_recovery_successor(
        request.request_id,
        recovery_request=request,
        expected_source_identity=coordinator.route_identity(request.request_id),
        excluded_placements=frozenset(source_placements),
    )
    successor_placements = {hop.placement_id for hop in successor.ordered_hops}
    assert successor_placements and successor_placements.isdisjoint(source_placements)


# node-b's plan root lives on another host (as node-2's does on the fleet): it
# was provisioned for the old split, so only an operator assertion proves it.
REMOTE_PLAN_ROOT = "/remote-host/old-split/deployment"
REMOTE_FULL_ROOT = "/remote-host/full-model/deployment"
ROOTS = {"node-b": REMOTE_FULL_ROOT}


def _synthetic_base(tmp_path: Path, deployment) -> dict:
    bundle = tmp_path / "bundle"
    (bundle / "control").mkdir(parents=True)
    shutil.copytree(deployment.root, bundle / "deployment",
                    ignore=shutil.ignore_patterns("reference*"))
    (bundle / "control" / "model-manifest.json").write_text(json.dumps(deployment.manifest))
    shutil.copy2(Path(derive.ROOT) / "physical_inference_node.py",
                 bundle / "physical_inference_node.py")
    nodes = []
    offers = []
    for index, assignment in enumerate(deployment.assignments):
        node_id = assignment["node_id"]
        name = f"control/node-{index}-assignment.json"
        template = dict(assignment)
        template["artifact_cache_root"] = (
            str((bundle / "deployment").resolve()) if index == 0 else REMOTE_PLAN_ROOT)
        (bundle / name).write_text(json.dumps(template))
        nodes.append({"node_id": node_id, "configure": {
            "assignment_file": name, "manifest_file": "control/model-manifest.json",
            "stage_pack_file": f"control/node-{index}-stage-pack.json",
            "graph": {}, "device_states": {}, "load_generation": 7}})
        offers.append({"message": {"recipient_node_id": node_id, "assignment_id": "old"},
                       "signature": {}})
    return {
        "plan_id": "synthetic-two-node",
        "run_id": "synthetic-run",
        "controller": {
            "source_root": str(bundle),
            "membership_snapshot": {"assignment_offers": offers},
            "run_plan": {"entry_node_id": "node-a", "nodes": nodes,
                         "request": {"prompt_token_ids": [1, 2, 3]},
                         "expected_token_ids": [0, 0, 0]},
        },
    }


def test_derive_plan_rewrites_only_the_replica_bindings(tmp_path) -> None:
    deployment = _deployment(tmp_path)
    base = _synthetic_base(tmp_path, deployment)
    plan, report = derive.derive_plan(
        base, bundle_out=tmp_path / "bundle-a6r", artifact_roots=ROOTS)
    controller = plan["controller"]
    assert controller["source_root"] == str(tmp_path / "bundle-a6r")
    assert plan["run_id"] == base["run_id"]
    assert plan["plan_id"] == "synthetic-two-node-a6-replica"
    graph = controller["run_plan"]["nodes"][0]["configure"]["graph"]
    assert len(graph["stages"]) == 1 and len(graph["stages"][0]["placements"]) == 2
    for node in controller["run_plan"]["nodes"]:
        configure = node["configure"]
        assert configure["graph"] == graph
        assert configure["assignment_file"] == f"control/a6r-{node['node_id']}-assignment.json"
        assert (tmp_path / "bundle-a6r" / configure["assignment_file"]).is_file()
    # Every node now receives every model shard.
    selections = {
        node_id: {r["path"] for r in document["files"] if r["path"].endswith(".safetensors")}
        for node_id, document in controller["node_transfer_manifests"]["manifests"].items()
    }
    assert selections["node-a"] == selections["node-b"] and selections["node-a"]
    for envelope in controller["membership_snapshot"]["assignment_offers"]:
        message = envelope["message"]
        placement = next(item for item in report["placements"]
                         if item["node_id"] == message["recipient_node_id"])
        assert message["assignment_id"] == placement["assignment_id"]
        assert message["graph_digest"] == report["graph_digest"]
    assert controller["run_plan"]["expected_token_ids"] == report["expected_token_ids"]
    assert report["challenge_outputs"]["node-a"] == report["challenge_outputs"]["node-b"]
    # The base bundle is never written: new documents sit beside byte copies.
    assert not (Path(base["controller"]["source_root"]) / "control" /
                "a6r-execution-graph.json").exists()


def test_derive_plan_never_hard_links_the_bundle(tmp_path) -> None:
    # Staging refuses transfer files with st_nlink != 1, in both bundles.
    deployment = _deployment(tmp_path)
    base = _synthetic_base(tmp_path, deployment)
    derive.derive_plan(base, bundle_out=tmp_path / "bundle-a6r", artifact_roots=ROOTS)
    for root in (Path(base["controller"]["source_root"]), tmp_path / "bundle-a6r"):
        files = [path for path in root.rglob("*") if path.is_file()]
        assert files
        assert all(path.stat().st_nlink == 1 for path in files)


def test_derive_plan_refuses_backends_that_disagree(tmp_path, monkeypatch) -> None:
    deployment = _deployment(tmp_path)
    base = _synthetic_base(tmp_path, deployment)
    outputs = iter([(1, 2, 3), (1, 2, 4)])
    monkeypatch.setattr(derive, "_run_challenge", lambda *_args: next(outputs))
    with pytest.raises(derive.DerivationError, match="backend_parity_mismatch"):
        derive.derive_plan(base, bundle_out=tmp_path / "bundle-a6r", artifact_roots=ROOTS)
    assert not (tmp_path / "bundle-a6r").exists()


def test_derive_plan_refuses_an_output_bundle_that_exists(tmp_path) -> None:
    deployment = _deployment(tmp_path)
    base = _synthetic_base(tmp_path, deployment)
    (tmp_path / "bundle-a6r").mkdir()
    with pytest.raises(derive.DerivationError, match="bundle_exists"):
        derive.derive_plan(base, bundle_out=tmp_path / "bundle-a6r", artifact_roots=ROOTS)


def test_derive_plan_refuses_a_remote_node_without_an_asserted_root(tmp_path) -> None:
    # The derive-time challenge only measures the controller's local copy, so
    # it can never prove that node-b's own root holds every shard.
    deployment = _deployment(tmp_path)
    base = _synthetic_base(tmp_path, deployment)
    with pytest.raises(derive.DerivationError, match="a6_replica_artifact_root_unproven"):
        derive.derive_plan(base, bundle_out=tmp_path / "bundle-a6r")
    with pytest.raises(derive.DerivationError, match="a6_replica_artifact_root_unknown_node"):
        derive.derive_plan(base, bundle_out=tmp_path / "bundle-a6r",
                           artifact_roots={**ROOTS, "node-z": REMOTE_FULL_ROOT})
    with pytest.raises(derive.DerivationError, match="a6_replica_artifact_root_invalid"):
        derive.derive_plan(base, bundle_out=tmp_path / "bundle-a6r",
                           artifact_roots={"node-b": "relative/deployment"})
    assert not (tmp_path / "bundle-a6r").exists()


def test_derive_plan_compiles_each_node_against_its_proven_root(tmp_path) -> None:
    deployment = _deployment(tmp_path)
    base = _synthetic_base(tmp_path, deployment)
    bundle_out = tmp_path / "bundle-a6r"
    plan, report = derive.derive_plan(base, bundle_out=bundle_out, artifact_roots=ROOTS)
    local_root = str((Path(base["controller"]["source_root"]) / "deployment").resolve())
    expected = {"node-a": local_root, "node-b": REMOTE_FULL_ROOT}
    for node in plan["controller"]["run_plan"]["nodes"]:
        configure = node["configure"]
        assignment = json.loads((bundle_out / configure["assignment_file"]).read_text())
        pack = json.loads((bundle_out / configure["stage_pack_file"]).read_text())
        assert assignment["artifact_cache_root"] == expected[node["node_id"]]
        assert pack["artifact_root"] == expected[node["node_id"]]
    assert report["artifact_roots"] == {
        "node-a": {"artifact_root": local_root, "proof": "local_disk_checked"},
        "node-b": {"artifact_root": REMOTE_FULL_ROOT, "proof": "operator_asserted"},
    }


@pytest.mark.parametrize("damage", ["missing", "truncated"])
def test_derive_plan_refuses_a_local_root_missing_a_full_model_file(tmp_path, damage) -> None:
    deployment = _deployment(tmp_path)
    base = _synthetic_base(tmp_path, deployment)
    shard = (Path(base["controller"]["source_root"]) / "deployment" /
             deployment.assignments[1]["files"][0]["path"])
    if damage == "missing":
        shard.unlink()
    else:
        shard.write_bytes(shard.read_bytes()[:-1])
    with pytest.raises(derive.DerivationError, match="a6_replica_local_artifact_missing"):
        derive.derive_plan(base, bundle_out=tmp_path / "bundle-a6r", artifact_roots=ROOTS)
    assert not (tmp_path / "bundle-a6r").exists()


def test_cli_refuses_a_malformed_artifact_root(tmp_path, capsys) -> None:
    code = derive.main([
        "--base-plan", str(tmp_path / "base.json"), "--output-plan", str(tmp_path / "o.json"),
        "--bundle-out", str(tmp_path / "b"), "--report", str(tmp_path / "r.json"),
        "--unbound", "--artifact-root", "node-b"])
    assert code == 2
    assert "a6_replica_artifact_root_invalid" in capsys.readouterr().out
