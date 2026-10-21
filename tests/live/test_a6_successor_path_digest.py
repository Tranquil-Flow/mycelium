# SPDX-License-Identifier: AGPL-3.0-only
"""The A6 successor identity names the locked path document the route executes.

``PhysicalLiveRoute.infer`` recomputes ``path_manifest_digest`` from the locked
path manifest it is handed and refuses any other value
(``locked_path_identity_mismatch``). The admitted source identity already used
that document digest; the reserved successor used ``PathManifest.manifest_digest``
(the model representation digest), so every physical recovery was refused at
successor prefill.
"""
from __future__ import annotations

import hashlib
import json

from mycelium_m16_runtime import path_manifest_document_digest
from mycelium_router.serialization import path_manifest_to_dict
from tests.live.test_a6_m16_recovery import (
    _coordinator,
    _recovery_request,
    _source_and_exclusions,
)


def _route_side_digest(document) -> str:
    """Exactly what PhysicalLiveRoute.infer computes for a locked manifest."""

    return "sha256:" + hashlib.sha256(
        json.dumps(dict(document), sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def test_reserved_successor_identity_is_the_locked_path_document_digest() -> None:
    coordinator = _coordinator()
    request, source_manifest, source_identity, excluded = _source_and_exclusions(coordinator)
    # The admitted source already binds the document digest.
    assert source_identity["path_manifest_digest"] == _route_side_digest(
        coordinator.path_manifest(request.request_id))

    successor = coordinator.reserve_recovery_successor(
        request.request_id,
        recovery_request=_recovery_request(request),
        expected_source_identity=source_identity,
        excluded_placements=excluded,
    )
    identity = coordinator.recovery_successor_identity(request.request_id)
    document = path_manifest_to_dict(successor)

    assert identity["path_manifest_digest"] == path_manifest_document_digest(successor)
    assert identity["path_manifest_digest"] == _route_side_digest(document)
    # Never the representation digest the manifest merely carries.
    assert identity["path_manifest_digest"] != successor.manifest_digest
    assert source_manifest.manifest_digest == successor.manifest_digest
