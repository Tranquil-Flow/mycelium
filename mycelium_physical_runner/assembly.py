"""Production composition root for the physical runner."""
from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from mycelium_qualification import QualificationAuthority, build_ed25519_verifier
from physical_inference_qualification import PeerIdentity, QualificationController

from .adapters import (
    build_authority_publisher,
    build_seal_adapter,
)
from .config import RunnerConfig
from .errors import RunnerError
from .frozen_evidence import (
    FROZEN_ROUTE_AUTHORITY_PROFILE,
    build_frozen_route_authority_documents,
)
from .runner import PhysicalRunner


class _RunnerControllerAdapter:
    """Expose sealed-only qualification input while preserving other commands."""

    def __init__(self, controller: QualificationController) -> None:
        self._controller = controller

    def execute(self, command: str) -> Mapping[str, Any]:
        if command == "seal":
            return self._controller.seal_evidence()
        return self._controller.execute(command)


def build_production_runner(config: RunnerConfig) -> PhysicalRunner:
    controller_config = dict(config.controller)
    peers_raw = controller_config["peers"]
    try:
        peers = tuple(
            PeerIdentity(
                node_id=str(peer["node_id"]),
                ssh_target=str(peer["ssh_target"]),
                host_id=str(peer["host_id"]),
                boot_id=str(peer["boot_id"]),
                staging_root=str(peer["staging_root"]),
                process_transport=str(peer["process_transport"]),
                ssh_identity_file=(
                    None
                    if peer["ssh_identity_file"] is None
                    else str(peer["ssh_identity_file"])
                ),
            )
            for peer in peers_raw
        )
        gossip_verifier = build_ed25519_verifier(config.gossip_verification_keys)
        load_verifier = build_ed25519_verifier(config.load_proof_verification_keys)
    except Exception as exc:
        raise RunnerError("runner_assembly_invalid") from exc

    configured_documents = controller_config.get("authority_documents")
    authority_profile = controller_config.get("authority_profile")
    if authority_profile == FROZEN_ROUTE_AUTHORITY_PROFILE:
        def document_builder(evidence: Mapping[str, Any]) -> Mapping[str, Any]:
            return build_frozen_route_authority_documents(
                controller_config=controller_config,
                evidence=evidence,
            )
    elif not isinstance(configured_documents, Mapping):
        def missing_documents(_evidence: Mapping[str, Any]) -> Mapping[str, Any]:
            raise RunnerError("authority_documents_missing")

        document_builder = missing_documents
    else:
        documents_snapshot = json.loads(json.dumps(dict(configured_documents), sort_keys=True))

        def document_builder(_evidence: Mapping[str, Any]) -> Mapping[str, Any]:
            return documents_snapshot

    seal_adapter = build_seal_adapter(
        output_dir=Path(config.evidence_output_dir),
        document_builder=document_builder,
    )
    controller = QualificationController(
        mode="physical",
        peers=peers,
        source_root=Path(str(controller_config["source_root"])),
        transfer_manifest=dict(controller_config["transfer_manifest"]),
        membership_snapshot=dict(controller_config["membership_snapshot"]),
        now=float(controller_config["now"]),
        run_plan=dict(controller_config["run_plan"]),
        seal_adapter=seal_adapter,
    )
    authority = QualificationAuthority(clock_unix_ms=lambda: config.now_unix_ms)
    publisher = build_authority_publisher(
        authority=authority,
        verify_gossip_signature=gossip_verifier,
        verify_load_proof_signature=load_verifier,
    )
    return PhysicalRunner(
        config=config,
        controller=_RunnerControllerAdapter(controller),
        publisher=publisher,
        clock_unix_ms=lambda: config.now_unix_ms,
    )


__all__ = ["build_production_runner"]
