# SPDX-License-Identifier: AGPL-3.0-only
"""Production assembly preserves explicit per-peer process transport."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from mycelium_physical_runner.assembly import build_production_runner
from mycelium_physical_runner.config import parse_operator_plan
from mycelium_qualification.signing import generate_ed25519_signer

from tests.physical_runner.conftest import operator_plan_payload


def test_production_assembly_preserves_local_and_ssh_process_transports(
    workspace: Path,
) -> None:
    payload = operator_plan_payload(workspace)
    signer = generate_ed25519_signer(endpoint_id="assembly-test")
    public_key = signer.public_key_record()
    payload["verification_keys"] = {
        "gossip": [public_key],
        "load_proof": [public_key],
    }
    payload["controller"]["node_transfer_manifests"] = {
        "protocol": "mycelium.controller_node_transfer_manifests.v1",
        "manifests": {
            node_id: payload["controller"]["transfer_manifest"]
            for node_id in ("node-a", "node-b")
        },
    }
    payload["controller"]["prepositioned_artifacts"] = {
        "protocol": "mycelium.controller_prepositioned_artifacts.v1",
        "members": {
            node_id: [
                {
                    "destination_path": "deployment/stage.safetensors",
                    "source_path": f"/srv/{node_id}/stage.safetensors",
                    "size_bytes": 17,
                    "content_digest": "sha256:" + "a" * 64,
                }
            ]
            for node_id in ("node-a", "node-b")
        },
    }
    config = parse_operator_plan(payload)

    runner = build_production_runner(config)

    controller_adapter: Any = runner._controller
    controller: Any = controller_adapter._controller
    assert [peer.process_transport for peer in controller.peers] == ["local", "ssh"]
    assert [peer.ssh_identity_file for peer in controller.peers] == [
        None,
        payload["controller"]["peers"][1]["ssh_identity_file"],
    ]
    assert controller._node_transfer_manifests == payload["controller"][
        "node_transfer_manifests"
    ]
    assert controller._prepositioned_artifacts == payload["controller"][
        "prepositioned_artifacts"
    ]


def test_production_assembly_preserves_managed_authority_and_providers(workspace: Path) -> None:
    payload=operator_plan_payload(workspace)
    key=generate_ed25519_signer(endpoint_id='managed-assembly').public_key_record()
    payload['verification_keys']={'gossip':[key],'load_proof':[key]}
    for peer in payload['controller']['peers']:
        peer.update(process_transport='managed_https',ssh_target=None,ssh_identity_file=None)
    config=parse_operator_plan(payload)
    # A configured plan must also construct without changing null into SSH authority.
    runner=build_production_runner(config)
    assert all(p.ssh_target is None for p in runner._controller._controller.peers)
    class Stager:
        def prepare(self,**kw):raise AssertionError('construction must not stage')
        def cleanup(self,**kw):raise AssertionError('construction must not cleanup')
    staging=Stager()
    def sessions(**kw):raise AssertionError('construction must not execute native work')
    runner=build_production_runner(config,session_factory=sessions,staging_adapter=staging)
    assert runner._controller._controller._session_factory is sessions
    assert runner._controller._controller._staging_adapter is staging

