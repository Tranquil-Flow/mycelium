# SPDX-License-Identifier: AGPL-3.0-only
"""A11 Wave D: the node's staged draft binding and its infer_start/infer_decode path."""

from __future__ import annotations

import json
import threading

import mlx.core as mx
import pytest

import mycelium_a11_draft_runtime as dr
from mycelium_a11_node_wiring import (
    PROTOCOL,
    A11NodeWiring,
    A11WiringError,
    TargetOnlyBaseline,
    load_node_wiring,
    validate_node_config,
)
from mycelium_router.fakes import ManualClock
from physical_inference_node import NodeCommandError, PhysicalNodeService
from a11_route_fixtures import varied_two_stage_case
from test_a11_route_speculation import PROMPT, Mesh

DIGEST = "a" * 64


def _config(**overrides):
    document = {
        "protocol": PROTOCOL,
        "deployment_id": "dep-1",
        "node_id": "node-a",
        "draft": {
            "model_dir": "a11/draft",
            "model_id": "Qwen/Qwen2.5-0.5B-Instruct",
            "resolved_commit": "7ae557604adf67be50417f59c2c2f167def9a775",
            "expected_sha256": {"model.safetensors": DIGEST},
            "quantize_bits": 8,
        },
        "policy": {"width": 4, "warm_target_tpot_ms": None, "minimum_baseline_samples": 4},
    }
    for key, value in overrides.items():
        section, _, field = key.partition("__")
        if field:
            document[section] = {**document[section], field: value}
        else:
            document[section] = value
    return document


def test_valid_binding_is_accepted_and_closed():
    config = validate_node_config(_config(), deployment_id="dep-1", node_id="node-a")
    assert config["policy"]["width"] == 4 and config["draft"]["quantize_bits"] == 8


@pytest.mark.parametrize(
    "overrides, code",
    [
        ({"node_id": "node-b"}, "a11_node_config_binding_mismatch"),
        ({"protocol": "other"}, "invalid_a11_node_config"),
        ({"draft__model_dir": "../outside"}, "invalid_a11_model_dir"),
        ({"draft__model_dir": "/abs"}, "invalid_a11_model_dir"),
        ({"draft__resolved_commit": "main"}, "invalid_a11_draft_binding"),
        ({"draft__expected_sha256": {}}, "invalid_a11_draft_binding"),
        ({"draft__quantize_bits": 3}, "invalid_a11_draft_binding"),
        ({"policy__width": 17}, "invalid_a11_policy"),
        ({"policy__warm_target_tpot_ms": {"p50": 50.0, "p95": 40.0}}, "invalid_a11_policy"),
        ({"policy__minimum_baseline_samples": 0}, "invalid_a11_policy"),
    ],
)
def test_malformed_bindings_fail_closed(overrides, code):
    with pytest.raises(A11WiringError, match=code):
        validate_node_config(_config(**overrides), deployment_id="dep-1", node_id="node-a")


def test_unknown_fields_are_refused():
    document = _config()
    document["extra"] = 1
    with pytest.raises(A11WiringError, match="invalid_a11_node_config"):
        validate_node_config(document, deployment_id="dep-1", node_id="node-a")


def test_model_dir_must_resolve_inside_the_artifact_root(tmp_path):
    root = tmp_path / "artifacts"
    (root / "a11").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "a11" / "draft").symlink_to(outside)
    config = validate_node_config(_config(), deployment_id="dep-1", node_id="node-a")
    with pytest.raises(A11WiringError, match="invalid_a11_model_dir"):
        load_node_wiring(root, config, load_model=lambda *a, **k: None)


def test_baseline_is_unmeasured_until_enough_target_only_rounds():
    baseline = TargetOnlyBaseline(3)
    baseline.record(10.0)
    baseline.record(30.0)
    assert baseline.measured() is None
    baseline.record(20.0)
    assert baseline.measured() == (20.0, 30.0)


# ---------------------------------------------------------------------------
# Node service: infer_start(speculation) + infer_decode over the real route
# ---------------------------------------------------------------------------

DRAFT_CONFIG = {
    "model_type": "qwen2", "hidden_size": 32, "intermediate_size": 48, "num_hidden_layers": 2,
    "num_attention_heads": 4, "num_key_value_heads": 2, "rms_norm_eps": 1e-6, "rope_theta": 10000.0,
    "vocab_size": 16, "tie_word_embeddings": True, "use_sliding_window": False,
}


def _tiny_draft():
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
    return dr.Qwen2DraftModel(DRAFT_CONFIG, weights, quantize_bits=None)


@pytest.fixture(scope="module")
def case():
    temporary, value = varied_two_stage_case()
    yield value
    temporary.cleanup()


def _request(request_id, max_new_tokens=12):
    return {
        "request_id": request_id, "prompt_token_ids": list(PROMPT), "max_new_tokens": max_new_tokens,
        "expected_new_tokens": max_new_tokens, "qos_class": "interactive", "admitted_at": 1.0,
        "target_ttft_ms": 10_000.0, "target_tpot_ms": 10_000.0, "target_tokens_per_second": 1.0,
        "sampling_seed": 0, "generation_config_digest": "sha256:" + "c" * 64,
    }


def _service(mesh, wiring):
    service = object.__new__(PhysicalNodeService)
    service.state = "RUNNING"
    service.router = mesh.entry
    service.runtime = mesh.ports[0]
    service.graph = mesh.case.graph
    service.transport = None
    service.command_timeout = 5.0
    service._clock = ManualClock(1.0)
    service._sinks = {}
    service._control_lock = threading.RLock()
    service._request_controls = {}
    service._pending_cancellations = {}
    service._a11_wiring = wiring
    service.retired = []
    service._retire_legacy_terminal_path = service.retired.append
    service._signed_result = lambda event, details: {"event": event, "details": details}
    return service


def _generate(service, request_id, speculation=None):
    payload = {"request": _request(request_id)}
    if speculation is not None:
        payload["speculation"] = {"preference": speculation}
    started = service._infer_start(payload)
    status = started["details"]["status"]
    while status == "DECODING":
        decoded = service._infer_decode({"request_id": request_id, "count": 1})
        status = decoded["details"]["status"]
    return list(decoded["details"]["output"]["token_ids"]), status


def test_node_speculates_only_for_an_explicit_preference_after_a_measured_baseline(case):
    plain = Mesh(case)
    reference, status = _generate(_service(plain, None), "reference")
    assert status == "COMPLETED"

    mesh = Mesh(case)
    config = validate_node_config(_config(), deployment_id="dep-1", node_id="node-a")
    wiring = A11NodeWiring(config, _tiny_draft())
    wiring.attach(mesh.entry)
    service = _service(mesh, wiring)

    # Before the warm baseline exists the preference cannot admit speculation.
    early, _ = _generate(service, "early", "speculative_requested")
    assert early == reference
    assert wiring.controller.completed == [] and wiring.baseline.measured() is not None
    # The default is target-only: no preference, no VERIFY.
    default, _ = _generate(service, "default")
    assert default == reference
    verify_before = sum(1 for d in mesh.mesh.hop_deliveries if d.header.phase == "VERIFY")
    assert verify_before == 0
    speculative, status = _generate(service, "spec", "speculative_requested")
    assert status == "COMPLETED" and speculative == reference
    summary = wiring.controller.completed[-1]
    assert summary["admission"]["mode"] == "speculative_admitted"
    assert summary["rounds"] >= 1
    assert sum(1 for d in mesh.mesh.hop_deliveries if d.header.phase == "VERIFY") > 0
    assert mesh.active_kv() == [0, 0]


def test_node_without_a_staged_draft_ignores_the_preference(case):
    mesh = Mesh(case)
    service = _service(mesh, None)
    output, status = _generate(service, "nodraft", "speculative_requested")
    assert status == "COMPLETED"
    assert not [d for d in mesh.mesh.hop_deliveries if d.header.phase == "VERIFY"]


def test_malformed_speculation_field_is_refused_before_any_work(case):
    mesh = Mesh(case)
    service = _service(mesh, None)
    with pytest.raises(NodeCommandError, match="invalid_infer_start_speculation"):
        service._infer_start({"request": _request("bad"), "speculation": {"preference": "always"}})
    with pytest.raises(NodeCommandError, match="invalid_infer_start_speculation"):
        service._infer_start({"request": _request("bad2"), "speculation": {"preference": "target_only", "x": 1}})
    assert mesh.entry.entry._requests == {}


def test_staged_binding_loads_through_the_node_configure_hook(tmp_path, case):
    root = tmp_path / "artifacts"
    (root / "a11" / "draft").mkdir(parents=True)
    (root / "a11" / "node.json").write_text(json.dumps(_config()))
    service = object.__new__(PhysicalNodeService)
    service.artifact_root = root
    service.node_id = "node-a"
    service.deployment_id = "dep-1"
    service._safe_document = lambda relative, code: json.loads((root / relative).read_text())

    class _Runtime:
        decode_mode = "stage_local_kv"

    with pytest.raises(NodeCommandError, match="draft_artifact_missing"):
        service._a11_configure(_Runtime())

    class _Replay:
        decode_mode = "complete_context_replay"

    with pytest.raises(NodeCommandError, match="a11_requires_stage_local_kv"):
        service._a11_configure(_Replay())
    (root / "a11" / "node.json").unlink()
    assert service._a11_configure(_Runtime()) is None
