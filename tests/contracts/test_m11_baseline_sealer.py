from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.seal_m11_successor_baseline import (
    PROTOCOL,
    _reject_private_fields,
    build_bundle,
)


def _write(path: Path, document: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document), encoding="utf-8")


def test_baseline_projection_pins_sources_without_copying_private_fields(tmp_path: Path) -> None:
    topology = {
        "protocol": "mycelium.qwen_live_topology.v1",
        "nodes": [
            {
                "node_id": "private-hostname",
                "runtime_backend": "mlx",
                "process_transport": "ssh",
                "ssh_target": "private-user@private-host",
                "endpoint_secret_file": "/private/endpoint-secret",
            }
        ],
    }
    for name in (
        "m7-qwen-three-host-surface-topology.json",
        "m8-qwen-two-host-mlx-topology.json",
        "m9-qwen15-two-host-mlx-topology.json",
    ):
        _write(tmp_path / name, topology)
    _write(
        tmp_path / "m10-deployment-registry.json",
        {
            "protocol": "mycelium.live_deployment_registry.v1",
            "selected_deployment_id": "deployment-1",
            "switching_allowed": False,
            "deployments": [
                {
                    "deployment_id": "deployment-1",
                    "health": "qualified",
                    "model_id": "model-1",
                    "qualification_id": "sha256:" + "a" * 64,
                    "qualified_at_unix_ms": 1,
                    "quantization": "int8",
                    "topology_size": 1,
                    "private_path": "/private/model",
                }
            ],
        },
    )
    _write(
        tmp_path / "m11-device-lab/pixel8-physical-proof.json",
        {
            "ok": True,
            "record": {
                "protocol": "mycelium.interactive_inference_record.v1",
                "local_evidence_only": True,
                "route_ready": False,
                "required_distinct_peers": 1,
                "observed_distinct_peers": 1,
                "max_intermediate_error": 0.0,
                "max_logit_error": 0.0,
                "initial_tokens": [1, 2, 3],
                "generated_tokens": [4],
            },
        },
    )
    _write(
        tmp_path
        / "m8-qwen2.5-int8-two-host-mlx-v4/review-live-status-20260810.json",
        {
            "protocol": "mycelium.live_route_status.v1",
            "route_alive": True,
            "simulated": False,
            "deployment_id": "deployment-1",
            "model_id": "model-1",
            "topology_version": 1,
            "decode_mode": "stage_local_kv",
            "counters": {},
            "stages": [{}],
            "peers": [{}],
            "recent_inferences": [{"prompt": "private", "output": "private"}],
            "incidents": [],
        },
    )

    evidence, pins = build_bundle(tmp_path, sealed_at="2026-08-10T10:30:00Z")

    wire = json.dumps(evidence, sort_keys=True)
    assert evidence["protocol"] == PROTOCOL
    assert len(pins) == 6
    assert "private-hostname" not in wire
    assert "private-user" not in wire
    assert '"prompt"' not in wire
    assert '"output"' not in wire
    assert '"initial_tokens"' not in wire


def test_baseline_privacy_guard_rejects_inference_content() -> None:
    with pytest.raises(ValueError, match="denied field"):
        _reject_private_fields({"nested": {"prompt": "must not be sealed"}})
