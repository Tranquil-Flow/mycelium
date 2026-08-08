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
    config = parse_operator_plan(payload)

    runner = build_production_runner(config)

    controller_adapter: Any = runner._controller
    controller: Any = controller_adapter._controller
    assert [peer.process_transport for peer in controller.peers] == ["local", "ssh"]
    assert [peer.ssh_identity_file for peer in controller.peers] == [
        None,
        payload["controller"]["peers"][1]["ssh_identity_file"],
    ]
