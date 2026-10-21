# SPDX-License-Identifier: AGPL-3.0-only
"""A11 draft staging: the operator-plan extension ships the authorised draft to the entry only.

A small synthetic operator plan (two nodes, the builder's bundle layout) is
extended with a tiny real Qwen2 draft snapshot.  Staging is simulated exactly as
the stager verifies it (each selected file copied and checked against its
transfer-manifest content digest), and the entry node's configure hook then
loads the draft from its staged artifact root through the real loader, which
checks the binding's weight digests again.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import mlx.core as mx
import pytest

from mycelium_a11_node_wiring import A11NodeWiring, validate_node_config
from mycelium_a11_operator_plan import A11PlanError, extend_operator_plan
from physical_inference_node import NodeCommandError, PhysicalNodeService
from physical_inference_qualification import node_transfer_manifests
from scripts.build_qwen_live_route import _transfer_manifest, _write_document
from test_a11_node_wiring import DRAFT_CONFIG

NODES = ("node-0", "node-1")
COMMIT = "7ae557604adf67be50417f59c2c2f167def9a775"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def snapshot(tmp_path: Path) -> Path:
    root = tmp_path / "draft-snapshot"
    root.mkdir()
    (root / "config.json").write_text(json.dumps(DRAFT_CONFIG))
    mx.random.seed(5)
    hidden, inter, kv = 32, 48, 16
    weights = {"model.embed_tokens.weight": mx.random.normal((16, hidden)), "model.norm.weight": mx.ones((hidden,))}
    for layer in range(2):
        prefix = f"model.layers.{layer}."
        for name, shape in (
            ("self_attn.q_proj.weight", (hidden, hidden)), ("self_attn.k_proj.weight", (kv, hidden)),
            ("self_attn.v_proj.weight", (kv, hidden)), ("self_attn.o_proj.weight", (hidden, hidden)),
            ("mlp.gate_proj.weight", (inter, hidden)), ("mlp.up_proj.weight", (inter, hidden)),
            ("mlp.down_proj.weight", (hidden, inter)),
        ):
            weights[prefix + name] = mx.random.normal(shape) * 0.3
        weights[prefix + "input_layernorm.weight"] = mx.ones((hidden,))
        weights[prefix + "post_attention_layernorm.weight"] = mx.ones((hidden,))
    mx.save_safetensors(str(root / "model.safetensors"), weights)
    # Tokenizer files are not part of the draft identity and are never staged.
    (root / "tokenizer.json").write_text("{}")
    return root


def _plan(tmp_path: Path) -> dict:
    bundle = tmp_path / "transfer-bundle"
    _write_document(bundle / "control" / "execution-graph.json", {"graph": "g"})
    (bundle / "deployment").mkdir(parents=True)
    (bundle / "deployment" / "stage-0.safetensors").write_bytes(b"\x00" * 64)
    (bundle / "deployment" / "stage-1.safetensors").write_bytes(b"\x01" * 64)
    (bundle / "physical_inference_node.py").write_text("# node\n")
    transfer = _transfer_manifest(bundle)
    common = ["control/execution-graph.json", "physical_inference_node.py"]
    return {
        "plan_id": "plan-r7",
        "run_id": "run-r7",
        "controller": {
            "source_root": str(bundle),
            "transfer_manifest": transfer,
            "node_transfer_manifests": node_transfer_manifests(
                transfer,
                {
                    "node-0": sorted([*common, "deployment/stage-0.safetensors"]),
                    "node-1": sorted([*common, "deployment/stage-1.safetensors"]),
                },
            ),
            "run_plan": {
                "deployment_id": "dep-r7",
                "entry_node_id": "node-0",
                "nodes": [{"node_id": node} for node in NODES],
            },
        },
    }


def _extend(plan: dict, tmp_path: Path, snapshot: Path, label: str = "a11", **overrides):
    arguments = {
        "out_bundle": tmp_path / f"bundle-{label}",
        "draft_snapshot": snapshot,
        "model_id": "Qwen/Qwen2.5-0.5B-Instruct",
        "resolved_commit": COMMIT,
        "expected_sha256": {"model.safetensors": _sha(snapshot / "model.safetensors")},
        "quantize_bits": None,
        "minimum_baseline_samples": 4,
    }
    arguments.update(overrides)
    return extend_operator_plan(plan, **arguments)


def _stage(plan: dict, node_id: str, root: Path) -> None:
    """Copy one node's selection and verify every content digest, as staging does."""

    bundle = Path(plan["controller"]["source_root"])
    for record in plan["controller"]["node_transfer_manifests"]["manifests"][node_id]["files"]:
        target = root / record["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(bundle / record["path"], target)
        assert "sha256:" + _sha(target) == record["content_digest"]
        assert target.stat().st_size == record["size_bytes"]


def _node(root: Path, node_id: str) -> PhysicalNodeService:
    service = object.__new__(PhysicalNodeService)
    service.artifact_root = root
    service.node_id = node_id
    service.deployment_id = "dep-r7"
    service._safe_document = lambda relative, code: json.loads((root / relative).read_text())
    return service


class _StageLocalKv:
    decode_mode = "stage_local_kv"


def test_the_draft_and_its_binding_ship_to_the_entry_only(tmp_path: Path, snapshot: Path) -> None:
    base = _plan(tmp_path)
    plan, summary = _extend(base, tmp_path, snapshot)
    controller = plan["controller"]
    assert plan["plan_id"] == "plan-r7-a11"
    assert controller["run_plan"]["decode_mode"] == "stage_local_kv"
    selections = {node: {r["path"] for r in m["files"]} for node, m in controller["node_transfer_manifests"]["manifests"].items()}
    a11 = {"a11/node-0/node.json", "a11/draft/config.json", "a11/draft/model.safetensors"}
    assert a11 <= selections["node-0"]
    assert not any(path.startswith("a11/") for path in selections["node-1"])
    # Unrelated selections are unchanged and nothing but the loader's files ships.
    assert selections["node-1"] == {
        "control/execution-graph.json", "deployment/stage-1.safetensors", "physical_inference_node.py"
    }
    assert {p for p in selections["node-0"] if p.startswith("a11/")} == a11
    binding = json.loads((Path(controller["source_root"]) / "a11/node-0/node.json").read_text())
    assert validate_node_config(binding, deployment_id="dep-r7", node_id="node-0") == binding
    assert binding["draft"]["expected_sha256"] == {"model.safetensors": _sha(snapshot / "model.safetensors")}
    assert summary["entry_node_id"] == "node-0" and summary["decode_mode"] == "stage_local_kv"
    # The source plan and bundle are untouched.
    assert "decode_mode" not in base["controller"]["run_plan"]
    assert not (tmp_path / "transfer-bundle" / "a11").exists()


def test_the_staged_entry_loads_the_verified_draft_and_the_other_node_stays_target_only(tmp_path: Path, snapshot: Path) -> None:
    plan, _summary = _extend(_plan(tmp_path), tmp_path, snapshot)
    entry_root, other_root = tmp_path / "stage-0", tmp_path / "stage-1"
    _stage(plan, "node-0", entry_root)
    _stage(plan, "node-1", other_root)
    wiring = _node(entry_root, "node-0")._a11_configure(_StageLocalKv())
    assert isinstance(wiring, A11NodeWiring)
    assert wiring.snapshot()["width"] == 4
    assert _node(other_root, "node-1")._a11_configure(_StageLocalKv()) is None
    # A staged weight file that no longer matches the binding refuses the load.
    weights = entry_root / "a11/draft/model.safetensors"
    data = weights.read_bytes()
    weights.write_bytes(data[:-1] + bytes([data[-1] ^ 0xFF]))
    with pytest.raises(NodeCommandError, match="draft_artifact_digest_mismatch"):
        _node(entry_root, "node-0")._a11_configure(_StageLocalKv())


@pytest.mark.parametrize(
    "overrides, code",
    [
        ({"expected_sha256": {"model.safetensors": "0" * 64}}, "a11_draft_digest_mismatch"),
        ({"expected_sha256": {"other.safetensors": "0" * 64}}, "a11_draft_weight_set_mismatch"),
        ({"expected_sha256": {"model.safetensors": "xyz"}}, "a11_plan_expected_sha256_invalid"),
        ({"entry_node_id": "node-1"}, "a11_plan_entry_invalid"),
    ],
)
def test_unauthorised_or_mismatched_drafts_are_refused_before_any_bundle(tmp_path: Path, snapshot: Path, overrides, code) -> None:
    with pytest.raises(A11PlanError, match=code):
        _extend(_plan(tmp_path), tmp_path, snapshot, **overrides)
    assert not (tmp_path / "bundle-a11").exists()


def test_a_plan_is_extended_once(tmp_path: Path, snapshot: Path) -> None:
    plan, _summary = _extend(_plan(tmp_path), tmp_path, snapshot)
    with pytest.raises(A11PlanError, match="a11_plan_already_extended"):
        _extend(plan, tmp_path, snapshot, label="twice")


def test_an_invalid_policy_is_refused_by_the_node_binding_validator(tmp_path: Path, snapshot: Path) -> None:
    from mycelium_a11_node_wiring import A11WiringError

    with pytest.raises(A11WiringError, match="invalid_a11_policy"):
        _extend(_plan(tmp_path), tmp_path, snapshot, width=0)
    assert not (tmp_path / "bundle-a11").exists()


def test_cli_extend_writes_a_private_plan_and_summary(tmp_path: Path, snapshot: Path) -> None:
    from scripts.build_a11_operator_plan import main

    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(_plan(tmp_path)))
    out_plan, summary = tmp_path / "out" / "plan-a11.json", tmp_path / "out" / "summary.json"
    assert main([
        "extend", "--plan", str(plan_path), "--draft-snapshot", str(snapshot),
        "--expected-sha256", f"model.safetensors={_sha(snapshot / 'model.safetensors')}",
        "--quantize-bits", "0", "--minimum-baseline-samples", "4",
        "--out-bundle", str(tmp_path / "cli-bundle"), "--out-plan", str(out_plan), "--summary-out", str(summary),
    ]) == 0
    assert out_plan.stat().st_mode & 0o777 == 0o600
    document = json.loads(out_plan.read_text())
    assert document["controller"]["source_root"] == str(tmp_path / "cli-bundle")
    assert json.loads(summary.read_text())["draft"]["quantize_bits"] is None
