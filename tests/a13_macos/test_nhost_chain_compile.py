# SPDX-License-Identifier: AGPL-3.0-only
"""N-host owner compile, slice-plan compile and 'rebuild with current members'.

Unit seams only (builder, workflow and observation publication are replaced):
no model load, device, network or qualification is exercised or claimed.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import threading
import time
from types import SimpleNamespace as NS

import pytest

from mycelium_a13_macos import chain_compile as cc
from mycelium_a13_macos import layer_split as ls
from mycelium_a13_macos import serving
from _nhost_fixtures import COMMIT, file_rows, tiny_dense_snapshot

GiB = 1 << 30


def _sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _app(tmp_path, monkeypatch, memories, *, capabilities=True, layers=12, model_type="qwen2"):
    from scripts import build_qwen_live_route as builder
    snapshot = tiny_dense_snapshot(tmp_path / "hf", num_layers=layers, model_type=model_type)
    nodes = [f"node-{chr(97 + i)}" for i in range(len(memories))]
    prepared = NS(source_root=snapshot, manifest_digest="sha256:" + "1" * 64,
                  manifest={"files": file_rows(snapshot)},
                  source_bytes=sum(r["size_bytes"] for r in file_rows(snapshot)
                                   if r["path"].endswith(".safetensors")),
                  model=NS(model_id="test-org/tiny-dense", resolved_commit=COMMIT,
                           source_representation="bfloat16-safetensors", runtime_dtype="float32",
                           runtime_quantization="int8-weight-only"),
                  resource_proposal=serving.RESOURCE_PROPOSAL)
    members = [{"node_id": n, "generation": 3, "incarnation": "inc", "verification_key_digest": "key",
                "endpoint_id": "endpoint-" + n, "lease_expires_at": time.time() + 300} for n in nodes]
    # Descriptor order deliberately reversed: topology must still be node-id ordered.
    facts = {m["node_id"]: {**{k: "fixture" for k in builder._TOPOLOGY_FIELDS},
             "source_root": str(snapshot), "manifest_digest": prepared.manifest_digest,
             "node_id": m["node_id"], "activation_endpoint_id": m["node_id"][-1] * 64}
             for m in reversed(members)}
    context = NS(retire_bootstrap=lambda: None, run_id="test-nhost", deployment_id="dep",
                 members=members, facts=facts, factory=NS(owner=object()),
                 observations={"observations": [{"node_id": n, "host_id": f"host-{n}"} for n in nodes]})
    app = NS(root=tmp_path / "app", guard=threading.RLock(), closed=threading.Event(),
             installation_role="owner", running=True, _resource_check=lambda: None,
             acquisition=NS(prepared_source=lambda: prepared), _observation_context=context,
             authority=NS(coordinator=NS(member=lambda n: dict(next(m for m in members if m["node_id"] == n)),
                                         signer=NS(public_key_record=lambda: {}))),
             _chain_target_keys=[])
    if capabilities:
        app._member_capability_reports = {
            n: {"node_id": n, "generation": 3, "expires_at": time.time() + 300, "memory_bytes": g * GiB}
            for n, g in zip(nodes, memories)}
    captured = {}
    monkeypatch.setattr(cc, "fresh_operator_template", lambda *a: {})
    monkeypatch.setattr(builder, "build", lambda args: captured.setdefault("args", args))

    def prepare(**kwargs):
        captured["workflow"] = kwargs
        def bind_progress_identity(**identity):
            assert identity == {"run_id":context.run_id,"deployment_id":context.deployment_id,
                                "epoch":getattr(app,"_chain_epoch",0)}
            captured["progress_identity"] = identity
        return NS(launch=NS(operator_plan_digest="unit", physical_host_ids=tuple(nodes)),
                  bind_progress_identity=bind_progress_identity)

    monkeypatch.setattr(cc, "prepare_serving_workflow", prepare)
    return app, captured, nodes, snapshot


def _compile(app):
    cc.start_compile(app, acknowledged=True)
    app._chain_compile_thread.join(10)
    assert not app._chain_compile_thread.is_alive()
    return app._chain_compile_state


@pytest.mark.parametrize("memories", [(16, 32, 48), (8, 16, 24, 32, 64)], ids=["3-host", "5-host"])
def test_n_host_compile_threads_memory_proportional_split(tmp_path, monkeypatch, memories):
    app, captured, nodes, _ = _app(tmp_path, monkeypatch, memories)
    state = _compile(app)
    assert state["state"] == "compiled"
    args = captured["args"]
    topology = json.loads((app.root / "chain-runs/test-nhost/topology.json").read_text())
    assert [n["node_id"] for n in topology["nodes"]] == nodes
    split = json.loads((app.root / "chain-runs/test-nhost/layer-split.json").read_text())
    assert args.managed_layer_ranges == ls.split_ranges(split)
    counts = [e - s for s, e in args.managed_layer_ranges]
    assert len(counts) == len(memories) and sum(counts) == 12 and min(counts) >= 1
    assert counts == sorted(counts)  # monotone in memory
    assert state["layer_split"]["split_digest"] == split["split_digest"]
    assert [s["node_id"] for s in state["layer_split"]["stages"]] == nodes


def test_n_host_compile_is_reproducible(tmp_path, monkeypatch):
    digests = []
    for run in range(2):
        app, _, _, _ = _app(tmp_path / str(run), monkeypatch, (16, 24, 40))
        digests.append(_compile(app)["layer_split"]["split_digest"])
    assert digests[0] == digests[1]


def test_undersized_member_is_refused_before_compilation(tmp_path, monkeypatch):
    app, captured, nodes, _ = _app(tmp_path, monkeypatch, (16, 1, 32))
    with pytest.raises(ls.SplitRefused) as caught:
        cc.start_compile(app, acknowledged=True)
    assert caught.value.code == "member_cannot_hold_share" and caught.value.node_id == nodes[1]
    assert not hasattr(app, "_chain_compile_thread") and "args" not in captured


def test_more_than_two_hosts_require_capability_reports(tmp_path, monkeypatch):
    app, _, _, _ = _app(tmp_path, monkeypatch, (16, 16, 16), capabilities=False)
    with pytest.raises(ValueError, match="member_capability_report_required"):
        cc.start_compile(app, acknowledged=True)


def test_two_hosts_without_reports_keep_the_legacy_balanced_split(tmp_path, monkeypatch):
    app, captured, _, _ = _app(tmp_path, monkeypatch, (16, 16), capabilities=False)
    assert _compile(app)["state"] == "compiled"
    assert captured["args"].managed_layer_ranges is None


@pytest.mark.parametrize("change", ["generation", "expired", "missing"])
def test_stale_capability_reports_refuse(tmp_path, monkeypatch, change):
    app, _, nodes, _ = _app(tmp_path, monkeypatch, (16, 16, 16))
    report = app._member_capability_reports[nodes[2]]
    if change == "generation":
        report["generation"] = 2
    elif change == "expired":
        report["expires_at"] = time.time() - 1
    else:
        del app._member_capability_reports[nodes[2]]
    with pytest.raises(ValueError, match="member_capability_report_(stale|required)"):
        cc.start_compile(app, acknowledged=True)


def test_host_count_bounds(tmp_path, monkeypatch):
    app, _, _, _ = _app(tmp_path, monkeypatch, (16,) * 9, layers=12)
    with pytest.raises(ValueError, match="host_count_exceeds_limit"):
        cc.start_compile(app, acknowledged=True)
    app2, _, _, _ = _app(tmp_path / "dup", monkeypatch, (16, 16, 16))
    app2._observation_context.observations["observations"][2]["host_id"] = "host-node-a"
    with pytest.raises(ValueError, match="two_distinct_hosts_required"):
        cc.start_compile(app2, acknowledged=True)


def _register_tiny_shape(monkeypatch, snapshot, layers=12):
    """Slice fetch needs a registered per-model resource derivation (D-1 relaxed)."""
    from mycelium_a13_macos import model_resources as R
    shape = R.ModelShape("tiny-dense", layers, 8, 16, 1, 1, 8, 32, True, False, True,
                         snapshot_bytes=sum(r["size_bytes"] for r in file_rows(snapshot)), non_weight_bytes=0)
    monkeypatch.setitem(R.MODEL_SHAPES, ("test-org/tiny-dense", COMMIT), shape)


def test_slice_mode_refuses_a_model_without_a_resource_derivation(tmp_path, monkeypatch):
    app, _, _, _ = _app(tmp_path, monkeypatch, (16, 32, 48), model_type="qwen3")
    app._chain_slice_fetch = True
    with pytest.raises(ValueError, match="slice_fetch_model_not_permitted"):
        cc.start_compile(app, acknowledged=True)


def test_slice_mode_compiles_digest_pinned_member_slices(tmp_path, monkeypatch):
    from scripts import build_qwen_live_route as builder
    from mycelium_qualification.safetensors_sharding import shard_qwen2_checkpoint
    app, captured, nodes, snapshot = _app(tmp_path, monkeypatch, (16, 32, 48), model_type="qwen3")
    # The tiny fixture stands in for a slice-sized model (> 4 GB source).
    monkeypatch.setattr(serving, "SLICE_FETCH_MIN_SOURCE_BYTES", 0)
    _register_tiny_shape(monkeypatch, snapshot)
    app._chain_slice_fetch = True

    def fake_build(args):
        captured["args"] = args
        ranges = [range(s, e) for s, e in args.managed_layer_ranges]
        # Where the real builder writes them (test_slice_real_builder runs its code).
        emitted = builder.emitted_stage_shard_root(args.emit_stage_shards)
        shard_qwen2_checkpoint(args.model_root, emitted, shard_count=3, layer_ranges=ranges)
        manifests = {}
        stage_names = sorted(p.name for p in emitted.glob("model-stage-*"))
        for index, node in enumerate(nodes):
            names = [stage_names[index]]
            if index == 0:
                names.append("model-static-embedding.safetensors")
            if index == 2:
                names += ["model-static-embedding.safetensors", "model-static-output.safetensors"]
            rows = [{"path": "deployment/" + n, "size_bytes": (emitted / n).stat().st_size,
                     "content_digest": _sha((emitted / n).read_bytes())}
                    for n in sorted(names)]
            manifests[node] = {"protocol": "mycelium.controller_transfer_manifest.v1", "files": rows}
        out = Path(args.output_root)
        out.mkdir(parents=True)
        (out / "operator-plan.json").write_text(json.dumps({"controller": {"node_transfer_manifests": {
            "protocol": "mycelium.controller_node_transfer_manifests.v1", "manifests": manifests}}}))
        return {"stage_sharding": {"emitted_root": str(emitted)}}

    monkeypatch.setattr(builder, "build", fake_build)
    state = _compile(app)
    assert state["state"] == "compiled", (app.root / "chain-runs/test-nhost/compile-error.private.log").read_text()
    args = captured["args"]
    assert args.stage_sharded is True and Path(args.emit_stage_shards).name == "stage-shards"
    plan = json.loads((app.root / "chain-runs/test-nhost/slice-plan.json").read_text())
    assert state["slice_plan_digest"] == plan["plan_digest"]
    assert sorted(state["node_slice_plan_digests"]) == nodes
    assert captured["workflow"]["slice_plan"]["plan_digest"] == plan["plan_digest"]
    whole = sum(r["size_bytes"] for r in file_rows(snapshot) if r["path"].endswith(".safetensors"))
    for node in nodes:
        from mycelium_a13_macos.slice_acquisition import node_slice_plan
        assert node_slice_plan(plan, node)["download_bytes"] < whole
    # Per-member resources from this split, and the run's proposal built from them.
    rows = {r["node_id"]: r for r in state["resource_plan"]["members"]}
    assert sorted(rows) == nodes
    stages = {s["node_id"]: s for s in state["layer_split"]["stages"]}
    for node in nodes:
        assert rows[node]["stage_source_bytes"] == stages[node]["stage_bytes"]
        assert rows[node]["max_disk_bytes"] >= stages[node]["stage_bytes"]
    proposal = state["execution_proposal"]
    assert proposal["max_combined_rss_bytes"] == 2 * max(r["max_rss_bytes"] for r in rows.values())
    assert proposal["max_additional_disk_bytes"] == 2 * max(r["max_disk_bytes"] for r in rows.values())


def test_slice_mode_refuses_models_outside_the_relaxed_d1_rule(tmp_path, monkeypatch):
    app, _, _, _ = _app(tmp_path / "qwen2", monkeypatch, (16, 32, 48))
    app._chain_slice_fetch = True
    with pytest.raises(ValueError, match="slice_fetch_model_not_permitted"):
        cc.start_compile(app, acknowledged=True)
    app, _, _, _ = _app(tmp_path, monkeypatch, (16, 32, 48), model_type="qwen3")
    app._chain_slice_fetch = True
    app.acquisition.prepared_source().source_bytes = serving.SLICE_FETCH_MAX_SOURCE_BYTES + 1
    with pytest.raises(ValueError, match="slice_fetch_model_not_permitted"):
        cc.start_compile(app, acknowledged=True)


def test_stop_during_build_supersedes_the_old_compile(tmp_path, monkeypatch):
    from scripts import build_qwen_live_route as builder
    app, _, _, _ = _app(tmp_path, monkeypatch, (16, 32, 48))
    monkeypatch.setattr(builder, "build", lambda args: setattr(app, "_chain_epoch", 99))
    state = _compile(app)
    # The superseded compile publishes nothing for the old member set.
    assert state["state"] != "compiled" and not hasattr(app, "_chain_workflow")


# ---------------------------------------------------------------- rebuild


class _Mailbox:
    def __init__(self):
        self.pending = {}
        self.confirmed = set()
        self.cleanup_requests = []

    def pending_offer(self, member):
        return self.pending.get(member["node_id"])

    def retire_offer(self, offer):
        if offer["offer_id"] not in self.confirmed:
            raise ValueError("offer_cleanup_unconfirmed")
        self.pending = {k: v for k, v in self.pending.items() if v is not offer}
        return {"offer": offer}

    def request_stage_cleanup(self, offer):
        self.cleanup_requests.append(offer["offer_id"])


def _rebuild_app(tmp_path, monkeypatch, *, vanished=(), serving_alive=False):
    nodes = ["node-a", "node-b", "node-c"]
    members = {n: {"node_id": n, "generation": 3, "incarnation": "inc", "verification_key_digest": "key",
                   "endpoint_id": "endpoint-" + n,
                   "lease_expires_at": time.time() + (-1 if n in vanished else 300)} for n in nodes}
    stops = []
    release = threading.Event()
    thread = threading.Thread(target=(lambda: release.wait(5)) if serving_alive else (lambda: None))
    thread.start()
    if not serving_alive:
        thread.join()
    mailbox = _Mailbox()
    app = NS(root=tmp_path / "app", guard=threading.RLock(), closed=threading.Event(),
             installation_role="owner", running=True,
             authority=NS(coordinator=NS(member=lambda n: dict(members[n]), swarm_id="swarm"),
                          control_mailbox=mailbox),
             _chain_workflow=NS(stop=lambda: stops.append(1)), _chain_serving_thread=thread,
             _chain_compile_state={"state": "compiled"}, _observation_context=NS(run_id="old-run"))
    prepared = []

    def verify(card, *, member, swarm_id, run_id, deployment_id, now=None):
        if time.time() >= member["lease_expires_at"]:
            raise ValueError("installation_descriptor_invalid")
        return {"node_id": member["node_id"]}

    def prepare(app_, *, run_id, deployment_id, descriptors, acknowledged):
        assert acknowledged is True
        prepared.append([d["statement"]["target_node_id"] for d in descriptors])
        return NS(run_id=run_id, deployment_id=deployment_id,
                  facts={d["statement"]["target_node_id"]: {} for d in descriptors})

    monkeypatch.setattr(cc, "verify_installation_descriptor", verify)
    monkeypatch.setattr(cc, "prepare_observation", prepare)
    cards = [{"statement": {"target_node_id": n}, "verification_key": {"k": n}} for n in nodes]
    return app, cards, stops, prepared, mailbox, release


def _rebuild(app, cards, run="rebuild-qwen-20261013T120000Z"):
    return cc.rebuild_with_current_members(app, acknowledged=True, run_id=run,
                                           deployment_id="dep-2", descriptors=cards, stop_timeout=0.2)


def test_rebuild_drops_vanished_member_and_republishes_current_ones(tmp_path, monkeypatch):
    app, cards, stops, prepared, _, _ = _rebuild_app(tmp_path, monkeypatch, vanished=("node-c",))
    result = _rebuild(app, cards)
    assert result["state"] == "awaiting_bootstrap_workers" and result["route_ready"] is False
    assert result["node_ids"] == ["node-a", "node-b"] and result["dropped_node_ids"] == ["node-c"]
    assert stops == [1] and prepared == [["node-a", "node-b"]]
    assert app._chain_compile_state is None and app._chain_workflow is None
    assert app._observation_context.run_id == "rebuild-qwen-20261013T120000Z"
    assert app._chain_target_keys == [{"k": "node-a"}, {"k": "node-b"}]


def test_rebuild_is_idempotent(tmp_path, monkeypatch):
    app, cards, stops, prepared, _, _ = _rebuild_app(tmp_path, monkeypatch)
    first = _rebuild(app, cards)
    second = _rebuild(app, cards)
    assert first == second and stops == [1] and len(prepared) == 1
    # Same run id with a different member set is a conflict, not a silent reuse.
    with pytest.raises(ValueError, match="rebuild_run_id_reused"):
        _rebuild(app, cards[:2])


def test_rebuild_refuses_without_two_current_members_and_changes_nothing(tmp_path, monkeypatch):
    app, cards, stops, prepared, _, _ = _rebuild_app(tmp_path, monkeypatch, vanished=("node-b", "node-c"))
    with pytest.raises(ValueError, match="rebuild_requires_two_current_members"):
        _rebuild(app, cards)
    assert stops == [] and prepared == [] and app._chain_compile_state == {"state": "compiled"}


def test_rebuild_waits_for_member_cleanup_then_resumes(tmp_path, monkeypatch):
    app, cards, stops, prepared, mailbox, _ = _rebuild_app(tmp_path, monkeypatch)
    offer = {"offer_id": "old-offer-b"}
    mailbox.pending["node-b"] = offer
    waiting = _rebuild(app, cards)
    assert waiting["state"] == "awaiting_member_cleanup" and waiting["waiting_node_ids"] == ["node-b"]
    assert mailbox.cleanup_requests == ["old-offer-b"] and prepared == []
    mailbox.confirmed.add("old-offer-b")
    done = _rebuild(app, cards)
    assert done["state"] == "awaiting_bootstrap_workers" and prepared == [["node-a", "node-b", "node-c"]]
    assert stops == [1]  # the second stop finds nothing to drain: idempotent


def test_rebuild_refuses_when_serving_does_not_stop(tmp_path, monkeypatch):
    app, cards, stops, prepared, _, release = _rebuild_app(tmp_path, monkeypatch, serving_alive=True)
    try:
        with pytest.raises(ValueError, match="rebuild_stop_timeout"):
            _rebuild(app, cards)
        assert prepared == []
    finally:
        release.set()
    assert _rebuild(app, cards)["state"] == "awaiting_bootstrap_workers"


def test_rebuild_requires_owner_consent(tmp_path, monkeypatch):
    app, cards, stops, prepared, _, _ = _rebuild_app(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="rebuild_owner_consent_required"):
        cc.rebuild_with_current_members(app, acknowledged=False, run_id="r", deployment_id="d",
                                        descriptors=cards)
    app.installation_role = "target"
    with pytest.raises(ValueError, match="rebuild_owner_consent_required"):
        _rebuild(app, cards)
    assert stops == [] and prepared == []
