# SPDX-License-Identifier: AGPL-3.0-only
"""Managed builder topology/admission; parser-level only, no models or SSH.

These tests pin the source-only preparation seam: a fully managed_https
topology (no SSH identity/target) can express peers for the macOS A13 demo,
startup requests match the selected model contract (sampling_seed 0), and
remote SSH preflight/refresh is forbidden for managed peers. Identities for
managed peers come only from externally admitted native hello observations.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from datetime import datetime, timezone

import pytest

from scripts import build_qwen_live_route as builder


RUN_ID = "macdemo-qwen-20260913T090000Z"
DEPLOYMENT_ID = "5f6f2a46-0a1c-4b8e-9f11-2c2f5aa7b1c3"
_OTHER_DEPLOYMENT_ID = "8f14e45f-ceea-467f-8bee-3b2a58d04a2c"
_OLD_RUN_ID = "olddemo-qwen-20260101T000000Z"
_HOST_MATERIAL = hashlib.sha256(b"demo-host-material").hexdigest()[:32]
_BOOT_MATERIAL = hashlib.sha256(b"demo-boot-material").hexdigest()[:32]


@pytest.fixture(autouse=True)
def fixed_observation_clock(monkeypatch):
    # Parser-only fixture: make freshness tests deterministic across wall dates.
    now=datetime(2026,9,13,9,0,tzinfo=timezone.utc).timestamp()
    monkeypatch.setattr(builder.time,'time',lambda:now)


def _managed_node(index: int) -> dict:
    return {
        "node_id": f"node-{index}",
        "process_transport": "managed_https",
        "ssh_target": None,
        "ssh_identity_file": None,
        "staging_root": f"/opt/mycelium/managed/node-{index}",
        "python_executable": "/usr/bin/python3",
        "sidecar_binary": f"/opt/mycelium/managed/node-{index}/sidecar",
        "endpoint_secret_file": f"/opt/mycelium/managed/node-{index}/node.key",
        "endpoint_id": f"endpoint-{index}",
        "membership_generation": index + 1,
        "runtime_backend": "mlx",
    }


def _ssh_node(index: int) -> dict:
    node = _managed_node(index)
    node["process_transport"] = "local" if index == 0 else "ssh"
    node["ssh_target"] = None if index == 0 else f"operator@remote-{index}"
    node["ssh_identity_file"] = None if index == 0 else f"/keys/remote-{index}"
    return node


def _observations_document(
    *,
    run_id: str = RUN_ID,
    deployment_id: str = DEPLOYMENT_ID,
    node_ids: tuple[str, ...] = ("node-0", "node-1"),
    captured_at_unix_ms: int | None = None,
) -> dict:
    captured = (
        int(time.time() * 1_000) if captured_at_unix_ms is None else captured_at_unix_ms
    )
    return {
        "protocol": "mycelium.managed_hello_observations.v1",
        "deployment_id": deployment_id,
        "run_id": run_id,
        "captured_at_unix_ms": captured,
        "observations": [
            {
                "node_id": node_id,
                "host_id": (
                    "host-"
                    + hashlib.sha256(
                        (_HOST_MATERIAL + ":" + node_id).encode()
                    ).hexdigest()[:32]
                ),
                "boot_id": (
                    "boot-"
                    + hashlib.sha256(
                        (_BOOT_MATERIAL + ":" + node_id).encode()
                    ).hexdigest()[:32]
                ),
            }
            for node_id in node_ids
        ],
    }


def _admit(document: dict) -> tuple[str, dict[str, tuple[str, str]]]:
    return builder._managed_hello_observations(
        document, ("node-0", "node-1"), deployment_id=DEPLOYMENT_ID
    )


def _topology_path(tmp_path: Path, nodes: list[dict]) -> Path:
    document = {"protocol": builder._TOPOLOGY_PROTOCOL, "nodes": nodes}
    path = tmp_path / "topology.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def _template() -> dict:
    peers = [
        {
            "node_id": f"node-{index}",
            "ssh_target": None,
            "host_id": f"host-{index}",
            "boot_id": f"boot-{index}",
            "staging_root": f"/opt/mycelium/managed/node-{index}",
            "process_transport": "managed_https",
            "ssh_identity_file": None,
        }
        for index in range(2)
    ]
    runtime_nodes = [
        {
            "node_id": f"node-{index}",
            "python_executable": "/usr/bin/python3",
            "sidecar_binary": f"/opt/mycelium/managed/node-{index}/sidecar",
            "endpoint_secret_file": f"/opt/mycelium/managed/node-{index}/node.key",
        }
        for index in range(2)
    ]
    endpoint_records = [
        {"node_id": f"node-{index}", "endpoint_id": f"endpoint-{index}"}
        for index in range(2)
    ]
    return {
        "controller": {
            "peers": peers,
            "run_plan": {"nodes": runtime_nodes},
            "membership_snapshot": {
                "assignment_offers": [
                    {
                        "message": {
                            "recipient_node_id": f"node-{index}",
                            "generation": index + 1,
                            "peer_endpoint_records": [
                                {
                                    **endpoint_records[1 - index],
                                    "membership_generation": (1 - index) + 1,
                                }
                            ],
                        }
                    }
                    for index in range(2)
                ]
            },
        }
    }


def test_managed_topology_requires_explicit_opt_in(tmp_path: Path) -> None:
    topology = _topology_path(tmp_path, [_managed_node(0), _managed_node(1)])

    with pytest.raises(RuntimeError, match="topology_process_transport_invalid"):
        builder._topology_nodes(_template(), topology)

    nodes = builder._topology_nodes(
        _template(), topology, managed_transport=True
    )
    assert [node["process_transport"] for node in nodes] == [
        "managed_https",
        "managed_https",
    ]
    assert all(node["ssh_target"] is None for node in nodes)
    assert all(node["ssh_identity_file"] is None for node in nodes)


def test_managed_topology_must_be_all_managed(tmp_path: Path) -> None:
    mixed = [_managed_node(0), _managed_node(1)]
    mixed[0]["process_transport"] = "local"
    topology = _topology_path(tmp_path, mixed)

    with pytest.raises(RuntimeError, match="topology_managed_transport_mixed"):
        builder._topology_nodes(_template(), topology, managed_transport=True)


def test_managed_topology_rejects_ssh_authority(tmp_path: Path) -> None:
    polluted = [_managed_node(0), _managed_node(1)]
    polluted[1]["ssh_target"] = "operator@remote"
    topology = _topology_path(tmp_path, polluted)

    with pytest.raises(RuntimeError, match="managed_peer_ssh_authority_invalid"):
        builder._topology_nodes(_template(), topology, managed_transport=True)

    identity = [_managed_node(0), _managed_node(1)]
    identity[1]["ssh_identity_file"] = "/keys/remote"
    topology = _topology_path(tmp_path, identity)
    with pytest.raises(RuntimeError, match="managed_peer_ssh_authority_invalid"):
        builder._topology_nodes(_template(), topology, managed_transport=True)


def test_ssh_transport_fields_still_accepted_without_opt_in(
    tmp_path: Path,
) -> None:
    topology = _topology_path(tmp_path, [_ssh_node(0), _ssh_node(1)])

    nodes = builder._topology_nodes(_template(), topology)

    assert [node["process_transport"] for node in nodes] == ["local", "ssh"]
    assert [node["ssh_identity_file"] for node in nodes] == [None, "/keys/remote-1"]


def test_preflight_skips_managed_peers(monkeypatch: pytest.MonkeyPatch) -> None:
    def explode(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("managed preflight must not spawn any subprocess")

    monkeypatch.setattr(builder.subprocess, "run", explode)

    builder.preflight_remote_peers([_managed_node(0), _managed_node(1)])


def test_refresh_without_observations_fails_closed() -> None:
    peers = [_managed_node(0), _managed_node(1)]
    with pytest.raises(RuntimeError, match="managed_peer_observations_missing"):
        builder.refresh_peer_identities(
            peers,
            RUN_ID,
            managed_observations={"node-0": ("host-"+_HOST_MATERIAL, "boot-"+_BOOT_MATERIAL)},
        )


def test_refresh_binds_admitted_identities_without_ssh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def explode(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("managed refresh must not spawn any subprocess")

    monkeypatch.setattr(builder.subprocess, "run", explode)
    run_id, identities = _admit(_observations_document())
    peers = builder.refresh_peer_identities(
        [_managed_node(0), _managed_node(1)],
        run_id,
        managed_observations=identities,
    )

    assert run_id == RUN_ID
    assert [peer["host_id"] for peer in peers] == [
        identities["node-0"][0],
        identities["node-1"][0],
    ]
    assert [peer["boot_id"] for peer in peers] == [
        identities["node-0"][1],
        identities["node-1"][1],
    ]


def test_refresh_rejects_non_admitted_synthetics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def explode(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("managed refresh must not spawn any subprocess")

    monkeypatch.setattr(builder.subprocess, "run", explode)

    with pytest.raises(RuntimeError, match="managed_observation_identity_synthetic"):
        builder.refresh_peer_identities(
            [_managed_node(0), _managed_node(1)],
            RUN_ID,
            managed_observations={
                "node-0": ("host-abc", "boot-def"),
                "node-1": ("host-abc", "boot-def"),
            },
        )


def test_observations_reject_synthetic_host_alias() -> None:
    document = _observations_document()
    document["observations"][0]["host_id"] = "host-macbook-pro-local"
    with pytest.raises(RuntimeError, match="managed_observation_identity_synthetic"):
        _admit(document)


def test_observations_reject_host_alias_shape() -> None:
    document = _observations_document()
    document["observations"][0]["host_id"] = "macbook-pro.local"
    with pytest.raises(RuntimeError, match="managed_observation_identity_synthetic"):
        _admit(document)


def test_observations_reject_duplicate_hosts() -> None:
    document = _observations_document()
    document["observations"][1]["host_id"] = document["observations"][0]["host_id"]
    with pytest.raises(RuntimeError, match="managed_observation_hosts_not_distinct"):
        _admit(document)


def test_observations_reject_node_set_mismatch() -> None:
    document = _observations_document(node_ids=("node-0", "node-2"))
    with pytest.raises(RuntimeError, match="managed_observation_node_mismatch"):
        _admit(document)


def test_observations_reject_stale_capture() -> None:
    document = _observations_document(
        captured_at_unix_ms=int(time.time() * 1_000) - 11 * 60 * 1_000
    )
    with pytest.raises(RuntimeError, match="managed_observation_stale"):
        _admit(document)


def test_observations_reject_old_run_id() -> None:
    document = _observations_document(run_id=_OLD_RUN_ID)
    with pytest.raises(RuntimeError, match="managed_observation_run_id_stale"):
        _admit(document)


def test_observations_reject_deployment_mismatch() -> None:
    document = _observations_document(deployment_id=_OTHER_DEPLOYMENT_ID)
    with pytest.raises(RuntimeError, match="managed_observation_deployment_mismatch"):
        _admit(document)


def test_observations_reject_unknown_protocol() -> None:
    document = _observations_document()
    document["protocol"] = "mycelium.managed_hello_observations.v2"
    with pytest.raises(RuntimeError, match="managed_observation_protocol_invalid"):
        _admit(document)


def test_startup_request_matches_selected_model_contract() -> None:
    request = builder._startup_challenge_request("macdemo", (1, 5, 9), (7, 11))

    assert request["sampling_seed"] == 0
    assert (
        request["generation_config_digest"]
        == builder._digest(
            builder._bytes({"max_new_tokens": 2, "sampling_seed": 0})
        )
    )
    assert request["max_new_tokens"] == 2
    assert request["prompt_token_ids"] == [1, 5, 9]
