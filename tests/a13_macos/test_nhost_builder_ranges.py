# SPDX-License-Identifier: AGPL-3.0-only
"""Managed builder threads an owner memory-proportional split into preparation.

Model-free: preparation is replaced by a capturing seam that stops the build,
so these tests prove only that 2..N managed hosts and the exact ranges reach
the canonical preparer (and that bad ranges refuse before any preparation).
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import build_qwen_live_route as builder

RUN_ID = "macdemo-qwen-20260913T090000Z"
DEPLOYMENT_ID = "5f6f2a46-0a1c-4b8e-9f11-2c2f5aa7b1c3"
_NOW = datetime(2026, 9, 13, 9, 0, 30, tzinfo=timezone.utc).timestamp()


class _Stop(Exception):
    pass


def _node(index: int) -> dict:
    return {"node_id": f"node-{index}", "process_transport": "managed_https",
            "ssh_target": None, "ssh_identity_file": None,
            "staging_root": f"/opt/mycelium/managed/node-{index}",
            "python_executable": "/usr/bin/python3",
            "sidecar_binary": f"/opt/mycelium/managed/node-{index}/sidecar",
            "endpoint_secret_file": f"/opt/mycelium/managed/node-{index}/node.key",
            "endpoint_id": f"endpoint-{index}", "membership_generation": index + 1,
            "runtime_backend": "mlx"}


def _args(tmp_path: Path, count: int, **extra) -> SimpleNamespace:
    topology = tmp_path / "topology.json"
    topology.write_text(json.dumps({"protocol": builder._TOPOLOGY_PROTOCOL,
                                    "nodes": [_node(i) for i in range(count)]}))
    observations = tmp_path / "observations.json"
    observations.write_text(json.dumps({
        "protocol": "mycelium.managed_hello_observations.v1",
        "deployment_id": DEPLOYMENT_ID, "run_id": RUN_ID,
        "captured_at_unix_ms": int(_NOW * 1000),
        "observations": [{
            "node_id": f"node-{i}",
            "host_id": "host-" + hashlib.sha256(f"h{i}".encode()).hexdigest()[:32],
            "boot_id": "boot-" + hashlib.sha256(f"b{i}".encode()).hexdigest()[:32],
        } for i in range(count)]}))
    (tmp_path / "template.json").write_text("{}")
    values = dict(template_plan=tmp_path / "template.json", model_root=tmp_path / "model",
                  output_root=tmp_path / "candidate", topology=topology, route_label="a13",
                  managed_transport=True, managed_hello_observations=observations,
                  deployment_id=DEPLOYMENT_ID)
    values.update(extra)
    return SimpleNamespace(**values)


@pytest.fixture
def captured(monkeypatch):
    monkeypatch.setattr(builder.time, "time", lambda: _NOW)
    calls = []

    def prepare(root, **kwargs):
        calls.append(kwargs)
        raise _Stop()

    monkeypatch.setattr(builder, "prepare_assignment_artifacts", prepare)
    return calls


@pytest.mark.parametrize("ranges", [[[0, 5], [5, 7], [7, 24]],
                                    [[0, 1], [1, 9], [9, 19], [19, 27], [27, 28]]])
def test_managed_build_passes_owner_split_for_n_hosts(tmp_path, captured, ranges):
    args = _args(tmp_path, len(ranges), managed_layer_ranges=ranges)
    with pytest.raises(_Stop):
        builder.build(args)
    (call,) = captured
    assert call["node_ids"] == tuple(f"node-{i}" for i in range(len(ranges)))
    assert [(r.start, r.stop) for r in call["layer_ranges"]] == [tuple(r) for r in ranges]


def test_managed_build_without_split_keeps_balanced_default(tmp_path, captured):
    with pytest.raises(_Stop):
        builder.build(_args(tmp_path, 3))
    assert captured[0]["layer_ranges"] is None


@pytest.mark.parametrize("ranges", [
    [[0, 5], [5, 24]],                 # wrong stage count for 3 hosts
    [[0, 5], [6, 7], [7, 24]],         # gap
    [[1, 5], [5, 7], [7, 24]],         # does not start at 0
    [[0, 5], [5, 5], [5, 24]],         # empty stage
    [[0, 5], [5, 7], [7, True]],       # non-integer bound
    "0-5,5-24",
])
def test_managed_build_refuses_invalid_split_before_preparation(tmp_path, captured, ranges):
    with pytest.raises(RuntimeError, match="managed_layer_ranges_invalid"):
        builder.build(_args(tmp_path, 3, managed_layer_ranges=ranges))
    assert captured == []
    assert not (tmp_path / "candidate").exists()


def test_split_requires_managed_transport(tmp_path, captured):
    args = _args(tmp_path, 2, managed_layer_ranges=[[0, 1], [1, 2]])
    with pytest.raises(RuntimeError, match="managed_layer_ranges_require_managed_transport"):
        builder._managed_layer_ranges(args.managed_layer_ranges, node_ids=("a", "b"),
                                      managed_transport=False)
