# SPDX-License-Identifier: AGPL-3.0-only
"""Slice mode against the builder's REAL stage-shard digest path.

The member files of a slice plan are the deployment safetensors that the
builder writes. Here they come from the builder's own code, not a stand-in:
``_prepare_model_representation`` (stage-shard writer + assignment
preparation) and ``_transfer_manifest`` / ``_node_transfer_manifests``. Only
the route challenge, runtime closure and signing of ``build()`` are left out,
because they do not touch weight bytes. Runtime dtype and quantization are
applied at stage load, so the shard bytes are raw source tensor bytes and a
member rebuilds them byte for byte from pinned source ranges. No model load,
device, network or qualification is exercised or claimed.
"""
from __future__ import annotations

from argparse import Namespace
import hashlib
import json
from pathlib import Path

import pytest

from mycelium_a13_macos import serving
from mycelium_a13_macos.slice_acquisition import fetch_node_slice, node_slice_plan
from test_nhost_chain_compile import _app, _compile, _register_tiny_shape

RUNTIMES = ["float32+none", "float32+int8-weight-only"]


def _sha_file(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _real_digest_build(captured: dict, *, runtime: str):
    from scripts import build_qwen_live_route as builder
    from mycelium_qualification.physical_deployment import (
        LocalModelSource, prepare_assignment_artifacts)

    dtype, quantization = runtime.split("+")

    def build(args: Namespace) -> dict:
        captured["args"] = args
        nodes = [n["node_id"] for n in json.loads(Path(args.topology).read_text())["nodes"]]
        ranges = [range(s, e) for s, e in args.managed_layer_ranges]
        bundle = Path(args.output_root) / "transfer-bundle"
        deployment = bundle / "deployment"
        deployment.mkdir(parents=True)

        def prepare(model_root: Path):
            return prepare_assignment_artifacts(
                deployment, node_ids=tuple(nodes),
                model_source=LocalModelSource(root=model_root, model_id=args.model_id,
                    requested_revision="main", resolved_commit=args.resolved_commit),
                runtime_dtype=dtype, runtime_backends_by_node={n: "mlx" for n in nodes},
                runtime_quantization=quantization, deployment_id=args.deployment_id,
                layer_ranges=ranges)

        prepared, stage_sharding = builder._prepare_model_representation(
            args, prepare, shard_count=len(nodes), layer_ranges=ranges)
        captured["deployment"] = deployment
        # Stand-in for _copy_runtime_closure (runtime code, never weight bytes).
        (bundle / "physical_inference_node.py").write_text("# runtime closure stand-in\n")
        manifests = builder._node_transfer_manifests(
            builder._transfer_manifest(bundle), list(prepared.stage_packs))
        (Path(args.output_root) / "operator-plan.json").write_text(
            json.dumps({"controller": {"node_transfer_manifests": manifests}}))
        return {"stage_sharding": stage_sharding, "route_ready": False}

    return build


class _SourceRanges:
    """``read_range`` over the pinned local snapshot (stands in for HF HTTPS)."""

    def __init__(self, snapshot: Path) -> None:
        self.snapshot = snapshot
        self.requests: list[tuple[str, int, int]] = []

    def read_range(self, model_id, commit, path, offset, length):
        self.requests.append((path, offset, length))
        with (self.snapshot / path).open("rb") as handle:
            handle.seek(offset)
            return handle.read(length)


def _slice_compile(tmp_path, monkeypatch, runtime):
    from scripts import build_qwen_live_route as builder
    app, captured, nodes, snapshot = _app(tmp_path, monkeypatch, (8, 16, 48),
                                         model_type="qwen3")
    app._observation_context.deployment_id = "6f1c2f1e-8d1b-4b7a-9a43-0f6f1c2f1e8d"
    monkeypatch.setattr(serving, "SLICE_FETCH_MIN_SOURCE_BYTES", 0)  # tiny stand-in model
    _register_tiny_shape(monkeypatch, snapshot)
    app._chain_slice_fetch = True
    monkeypatch.setattr(builder, "build", _real_digest_build(captured, runtime=runtime))
    state = _compile(app)
    log = app.root / "chain-runs/test-nhost/compile-error.private.log"
    assert state["state"] == "compiled", log.read_text() if log.exists() else state
    plan = json.loads((app.root / "chain-runs/test-nhost/slice-plan.json").read_text())
    return app, captured, nodes, snapshot, state, plan


@pytest.mark.parametrize("runtime", RUNTIMES)
def test_slice_compile_reads_the_shards_where_the_real_builder_emits_them(
        tmp_path, monkeypatch, runtime):
    _, captured, nodes, _, state, plan = _slice_compile(tmp_path, monkeypatch, runtime)
    assert Path(captured["args"].emit_stage_shards).name == "stage-shards"
    assert state["slice_plan_digest"] == plan["plan_digest"]
    assert sorted(plan["nodes"]) == nodes
    for node in nodes:
        for row in plan["nodes"][node]["files"]:
            built = captured["deployment"] / Path(row["path"]).name
            assert row["content_digest"] == _sha_file(built)


@pytest.mark.parametrize("runtime", RUNTIMES)
def test_member_rebuilds_the_builders_shards_byte_identical_from_source_ranges(
        tmp_path, monkeypatch, runtime):
    _, captured, nodes, snapshot, state, plan = _slice_compile(tmp_path, monkeypatch, runtime)
    whole = sum(p.stat().st_size for p in snapshot.glob("*.safetensors"))
    for node in nodes:
        node_plan = node_slice_plan(plan, node)
        assert node_plan["node_plan_digest"] == state["node_slice_plan_digests"][node]
        destination = (tmp_path / "member" / node).resolve()
        destination.mkdir(parents=True)
        transport = _SourceRanges(snapshot)
        report = fetch_node_slice(node_plan, expected_node_plan_digest=node_plan["node_plan_digest"],
            destination_root=destination, transport=transport,
            max_download_bytes=whole, max_disk_bytes=whole)
        assert report["downloaded_bytes"] == node_plan["download_bytes"] < whole
        assert {p for p, _, _ in transport.requests} <= {"model.safetensors"}
        for row in node_plan["files"]:
            fetched = destination / row["path"]
            built = captured["deployment"] / Path(row["path"]).name
            assert fetched.read_bytes() == built.read_bytes()


def test_runtime_dtype_and_quantization_do_not_change_shard_bytes(tmp_path, monkeypatch):
    digests = []
    for runtime in RUNTIMES:
        _, captured, _, _, _, plan = _slice_compile(tmp_path / runtime, monkeypatch, runtime)
        digests.append({p.name: _sha_file(p)
                        for p in sorted(captured["deployment"].glob("*.safetensors"))})
    assert digests[0] == digests[1] and len(digests[0]) == 5


@pytest.mark.parametrize("report", [None, {"stage_sharding": {"emitted_root": "elsewhere"}}],
                         ids=["unreported", "other-root"])
def test_slice_compile_fails_closed_when_the_build_names_another_shard_root(
        tmp_path, monkeypatch, report):
    from scripts import build_qwen_live_route as builder
    app, captured, _, snapshot = _app(tmp_path, monkeypatch, (8, 16, 48), model_type="qwen3")
    app._observation_context.deployment_id = "6f1c2f1e-8d1b-4b7a-9a43-0f6f1c2f1e8d"
    monkeypatch.setattr(serving, "SLICE_FETCH_MIN_SOURCE_BYTES", 0)
    _register_tiny_shape(monkeypatch, snapshot)
    app._chain_slice_fetch = True
    real = _real_digest_build(captured, runtime=RUNTIMES[1])
    monkeypatch.setattr(builder, "build", lambda args: (real(args), report)[1])
    assert _compile(app)["state"] == "failed"
    log = (app.root / "chain-runs/test-nhost/compile-error.private.log").read_text()
    assert "stage_shard_root_" in log and "workflow" not in captured
