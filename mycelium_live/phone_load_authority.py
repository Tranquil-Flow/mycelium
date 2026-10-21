# SPDX-License-Identifier: AGPL-3.0-only
"""Current phone load authority for final qualification and renewal.

The route's plan carries only the phone's five-field configure. That copy is
not authority. For every phone placement this module reads the packaged proof,
compiled assignment and full-model manifest out of the plan's own transfer
manifest (each file verified against the digest the plan pins), and has the
pinned seed validate them against its current member and applied-route rows.

The result is trusted consumer context for the live qualifier, produced
outside the attestation it will be compared with. Any refusal raises
``ControllerError`` with the refusing check's stable code: there is no
advisory result and no fallback to the plan's configure.
"""
from __future__ import annotations

import json
import time
from typing import Any

from physical_inference_qualification import ControllerError, _read_verified_transfer_file


def _packaged_document(controller: Any, path: str) -> Any:
    records = controller._transfer_manifest.get("files")
    matches = [r for r in records if isinstance(r, dict) and r.get("path") == path] if isinstance(records, list) else []
    if len(matches) != 1:
        raise ControllerError("phone_packaged_proof_missing")
    try:
        _verified, content = _read_verified_transfer_file(controller.source_root, matches[0])
        return json.loads(content)
    except ControllerError as exc:
        raise ControllerError("phone_packaged_proof_corrupt", diagnostic=exc.code) from exc
    except (ValueError, UnicodeError) as exc:
        raise ControllerError("phone_packaged_proof_corrupt") from exc


def current_phone_load_bindings(route: Any) -> dict[str, Any]:
    """One validated binding per phone placement of ``route``; raises on refusal."""
    from mycelium_seed.coordinator import SeedCoordinatorError
    from mycelium_seed.phone_preparation import current_load_authority

    bindings: dict[str, Any] = {}
    ordered = [item["node_id"] for item in route._plan["nodes"]]
    placements = {p.node_id: p for stage in route._graph.stages for p in stage.placements}
    for node_id, peer in route._peers.items():
        if peer.process_transport != "external_iroh":
            continue
        seed = route._seed_authority
        if seed is None:
            raise ControllerError("external_seed_authority_required")
        index = ordered.index(node_id)
        placement = placements.get(node_id)
        if placement is None or ordered.count(node_id) != 1:
            raise ControllerError("phone_graph_placement_mismatch")
        configure = dict(route._plans_by_node[node_id]["configure"])
        controller = route._controller
        try:
            authority = current_load_authority(
                database=seed.database,
                seed_key_digest=seed.signer.verification_key_digest,
                revalidate=seed.state_root.revalidate,
                node_id=node_id,
                endpoint_id=route._endpoints[node_id]["endpoint_id"],
                incarnation=peer.boot_id,
                membership_generation=route._endpoints[node_id]["membership_generation"],
                staging_root=peer.staging_root,
                run_id=route._plan["run_id"],
                deployment_id=route._plan["deployment_id"],
                configure=configure,
                placement_assignment_id=placement.assignment_id,
                placement_load_proof_digest=placement.load_proof_digest,
                graph_manifest_digest=route._graph.manifest_digest,
                proof=_packaged_document(controller, f"control/node-{index}-load-proof.json"),
                assignment=_packaged_document(controller, f"control/node-{index}-assignment.json"),
                model_manifest=_packaged_document(controller, "control/model-manifest.json"),
                clock=time.time,
            )
        except ControllerError:
            raise
        except SeedCoordinatorError as exc:
            raise ControllerError(exc.code) from exc
        except (RuntimeError, ValueError, KeyError, TypeError) as exc:
            # PhoneEvidence and the provenance check name their refusal in the
            # message; a malformed proof has no better name than this one.
            code = str(exc) if isinstance(exc, (RuntimeError, ValueError)) and str(exc) else "phone_load_proof_invalid"
            raise ControllerError(code if code.replace("_", "").isalnum() else "phone_load_proof_invalid") from exc
        bindings[node_id] = {
            "endpoint_id": authority.endpoint_id,
            "process_id": authority.process_id,
            "member_key_digest": authority.member_key_digest,
            "configure": configure,
            "load_authority": authority,
        }
    return bindings


__all__ = ["current_phone_load_bindings"]
