from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.run_m23_kv_gate import (
    CAPTURE_PROTOCOL,
    GateError,
    _digest,
    derive_operator_plan,
    seal,
)


def _capture(mode: str) -> dict[str, object]:
    is_kv = mode == "stage_local_kv"
    decode_input_tokens = 3 if is_kv else 240
    activation_bytes = 20_000 if is_kv else 500_000
    peer_delta = {
        "frames_sent": 5,
        "frames_received": 5,
        "applied_operation_count": 4,
        "prefill_operation_count": 1,
        "prefill_input_token_count": 72,
        "decode_operation_count": 3 if is_kv else 0,
        "decode_input_token_count": decode_input_tokens if is_kv else 0,
        "activation_output_bytes": activation_bytes if is_kv else 0,
    }
    terminal = {
        "backend": "numpy",
        "architecture": "qwen2" if is_kv else None,
        "active_state_count": 0,
        "active_kv_bytes": 0,
        "peak_kv_bytes": 4096 if is_kv else 0,
        "release_state": "released" if is_kv else "unknown",
        "last_release_reason": "normal_completion" if is_kv else None,
    }
    document: dict[str, object] = {
        "protocol": CAPTURE_PROTOCOL,
        "captured_at_unix_ms": 1,
        "mode": mode,
        "route": {
            "route_identity_digest": "sha256:" + "a" * 64,
            "deployment_id": "deployment",
            "model_id": "Qwen/model",
            "topology_version": 1,
            "stages": [
                {"node_id": "node-a", "start_layer": 0, "end_layer_exclusive": 1},
                {"node_id": "node-b", "start_layer": 1, "end_layer_exclusive": 2},
                {"node_id": "node-c", "start_layer": 2, "end_layer_exclusive": 3},
            ],
        },
        "request": {
            "request_id": f"request:{mode}",
            "prompt": "fixed",
            "prompt_digest": _digest("fixed"),
            "maximum_new_tokens": 4,
            "output_text": "13",
            "output_digest": _digest("13"),
            "output_token_count": 4,
            "event_types": ["accepted", "token", "token", "token", "token", "completed"],
        },
        "timing": {
            "tpot_ms": 10.0 if is_kv else 100.0,
            "total_ms": 100.0 if is_kv else 500.0,
        },
        "counter_deltas": {
            node: dict(peer_delta) for node in ("node-a", "node-b", "node-c")
        },
        "terminal_kv": {
            node: dict(terminal) for node in ("node-a", "node-b", "node-c")
        },
        "fatal": None,
    }
    document["capture_digest"] = _digest(document)
    return document


def _write(path: Path, document: dict[str, object]) -> None:
    path.write_text(json.dumps(document), encoding="utf-8")


def test_derive_operator_plan_binds_route_wide_decode_mode(tmp_path: Path) -> None:
    base = tmp_path / "base.json"
    output = tmp_path / "derived.json"
    _write(
        base,
        {
            "protocol": "mycelium.physical_runner_operator_plan.v1",
            "plan_id": "fixed-plan",
            "controller": {
                "run_plan": {"protocol": "mycelium.controller_run_plan.v1"}
            },
        },
    )

    document = derive_operator_plan(
        base_path=base,
        mode="stage_local_kv",
        output=output,
    )

    assert document["controller"]["run_plan"]["decode_mode"] == "stage_local_kv"
    assert document["plan_id"] == "fixed-plan-m23-stage_local_kv"
    assert json.loads(output.read_text("utf-8")) == document


def test_seal_promotes_measured_three_host_kv_gate(tmp_path: Path) -> None:
    replay_path = tmp_path / "replay.json"
    kv_path = tmp_path / "kv.json"
    output = tmp_path / "gate.json"
    _write(replay_path, _capture("complete_context_replay"))
    _write(kv_path, _capture("stage_local_kv"))

    document = seal(replay_path=replay_path, kv_path=kv_path, output=output)

    assert document["implemented"] is True
    assert document["performance_qualified"] is True
    assert document["promotion_state"] == "qualified"
    assert document["gates"]["one_token_decode_every_stage"] is True
    assert document["measurements"]["tpot_improvement_ratio"] == 0.9
    assert json.loads(output.read_text("utf-8"))["evidence_digest"] == document[
        "evidence_digest"
    ]


def test_seal_withholds_on_output_drift_or_dirty_kv_cleanup(tmp_path: Path) -> None:
    replay = _capture("complete_context_replay")
    kv = _capture("stage_local_kv")
    kv["request"]["output_text"] = "different"
    kv["terminal_kv"]["node-c"]["active_state_count"] = 1
    kv["capture_digest"] = _digest({key: value for key, value in kv.items() if key != "capture_digest"})
    replay_path = tmp_path / "replay.json"
    kv_path = tmp_path / "kv.json"
    output = tmp_path / "gate.json"
    _write(replay_path, replay)
    _write(kv_path, kv)

    with pytest.raises(GateError, match="m23_gate_withheld"):
        seal(replay_path=replay_path, kv_path=kv_path, output=output)

    withheld = json.loads(output.read_text("utf-8"))
    assert withheld["promotion_state"] == "withheld"
    assert withheld["gates"]["exact_output_parity"] is False
    assert withheld["gates"]["kv_active_then_terminally_released"] is False


def test_seal_accepts_successful_terminal_cancellation_cleanup(tmp_path: Path) -> None:
    replay = _capture("complete_context_replay")
    kv = _capture("stage_local_kv")
    for index, state in enumerate(kv["terminal_kv"].values()):
        state["last_release_reason"] = "cancellation" if index < 2 else "cancelled"
    kv["capture_digest"] = _digest(
        {key: value for key, value in kv.items() if key != "capture_digest"}
    )
    replay_path = tmp_path / "replay.json"
    kv_path = tmp_path / "kv.json"
    output = tmp_path / "gate.json"
    _write(replay_path, replay)
    _write(kv_path, kv)

    document = seal(replay_path=replay_path, kv_path=kv_path, output=output)

    assert document["gates"]["kv_active_then_terminally_released"] is True
    assert document["implemented"] is True


def test_seal_uses_runtime_decode_counters_not_visible_token_events(tmp_path: Path) -> None:
    replay = _capture("complete_context_replay")
    kv = _capture("stage_local_kv")
    for capture in (replay, kv):
        capture["request"]["output_token_count"] = 2
        capture["request"]["event_types"] = [
            "accepted",
            "token",
            "token",
            "completed",
        ]
        capture["capture_digest"] = _digest(
            {key: value for key, value in capture.items() if key != "capture_digest"}
        )
    replay_path = tmp_path / "replay.json"
    kv_path = tmp_path / "kv.json"
    output = tmp_path / "gate.json"
    _write(replay_path, replay)
    _write(kv_path, kv)

    document = seal(replay_path=replay_path, kv_path=kv_path, output=output)

    assert document["gates"]["one_token_decode_every_stage"] is True
