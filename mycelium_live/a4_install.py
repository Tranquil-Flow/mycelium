"""A4 product concurrency-liveness qualification install (serve-side).

The A4 document is built IN-SESSION: qualification_digest binds to the serve's
own just-issued live qualification (the digest covers the random startup
request_id, so no pre-sealed file can ever match a fresh serve). The evidence
digest binds the owner-supplied physical gate artifacts.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from mycelium_live.a4_contracts import validate_product_qualification
from mycelium_qualification.evidence import canonical_json_bytes

_MAX_ARTIFACT_BYTES = 4 * 1024 * 1024

PROTOCOLS = {
    "positive": "mycelium.a4_product_positive_observation.v1",
    "data_plane": "mycelium.a4_product_negative_data_plane.v1",
    "qualification": "mycelium.a4_product_negative_qualification_observation.v1",
    "shutdown": "mycelium.a4_product_negative_shutdown_observation.v1",
}


class A4EvidenceError(ValueError):
    """An owner-supplied A4 gate artifact failed validation."""


def _read_artifact(path: Path, kind: str) -> dict[str, Any]:
    if (
        path.is_symlink()
        or not path.is_file()
        or path.stat().st_size > _MAX_ARTIFACT_BYTES
    ):
        raise A4EvidenceError(f"a4_artifact_unsafe:{kind}:{path}")
    try:
        raw = json.loads(path.read_text("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise A4EvidenceError(f"a4_artifact_invalid:{kind}:{path}") from exc
    if not isinstance(raw, Mapping) or raw.get("protocol") != PROTOCOLS[kind]:
        raise A4EvidenceError(f"a4_artifact_protocol_invalid:{kind}:{path}")
    return dict(raw)


def _validate_positive(
    document: Mapping[str, Any],
    *,
    graph_digest: str | None,
    manifest_digest: str | None,
    path: Path,
) -> None:
    if document.get("qualification_claim") is not False:
        raise A4EvidenceError(f"a4_positive_qualification_claim:{path}")
    if document.get("promotion_authorized") is not False:
        raise A4EvidenceError(f"a4_positive_promotion_claim:{path}")
    if document.get("simulated") is True:
        raise A4EvidenceError(f"a4_positive_simulated:{path}")
    streams = document.get("streams")
    if isinstance(streams, Mapping):
        entries = list(streams.values())
    elif isinstance(streams, list):
        entries = list(streams)
    else:
        entries = []
    if not entries:
        raise A4EvidenceError(f"a4_positive_streams_missing:{path}")
    terminals = {
        stream.get("terminal")
        for stream in entries
        if isinstance(stream, Mapping)
    }
    if "completed" not in terminals or "cancelled" not in terminals:
        raise A4EvidenceError(f"a4_positive_terminals_incomplete:{path}")
    cancellation = document.get("cancellation")
    if (
        not isinstance(cancellation, Mapping)
        or cancellation.get("within_total_bound") is not True
    ):
        raise A4EvidenceError(f"a4_positive_bound_exceeded:{path}")
    for field, expected in (
        ("graph_digest", graph_digest),
        ("manifest_digest", manifest_digest),
    ):
        observed = document.get(field)
        if expected is not None and observed != expected:
            raise A4EvidenceError(f"a4_positive_identity_mismatch:{field}:{path}")


def _validate_passed(document: Mapping[str, Any], *, kind: str, path: Path) -> None:
    if document.get("passed") is not True:
        raise A4EvidenceError(f"a4_negative_not_passed:{kind}:{path}")
    if document.get("simulated") is True:
        raise A4EvidenceError(f"a4_negative_simulated:{kind}:{path}")


def build_a4_qualification(
    *,
    positive_observations: tuple[Mapping[str, Any], ...],
    data_plane_observations: tuple[Mapping[str, Any], ...],
    qualification_observation: Mapping[str, Any],
    shutdown_observation: Mapping[str, Any],
    qualification_digest: str,
    graph_digest: str | None,
    manifest_digest: str | None,
    positive_sources: tuple[Path, ...] = (),
    data_plane_sources: tuple[Path, ...] = (),
    qualification_source: Path | None = None,
    shutdown_source: Path | None = None,
) -> dict[str, Any]:
    """Validate artifacts and build the in-session A4 qualification document."""

    if not positive_observations:
        raise A4EvidenceError("a4_positive_missing")
    if not data_plane_observations:
        raise A4EvidenceError("a4_data_plane_missing")
    for document, path in zip(
        positive_observations,
        positive_sources or (Path("?"),) * len(positive_observations),
        strict=False,
    ):
        _validate_positive(
            document,
            graph_digest=graph_digest,
            manifest_digest=manifest_digest,
            path=path,
        )
    for document, path in zip(
        data_plane_observations,
        data_plane_sources or (Path("?"),) * len(data_plane_observations),
        strict=False,
    ):
        _validate_passed(document, kind="data_plane", path=path)
    _validate_passed(
        qualification_observation,
        kind="qualification",
        path=qualification_source or Path("?"),
    )
    _validate_passed(
        shutdown_observation,
        kind="shutdown",
        path=shutdown_source or Path("?"),
    )
    evidence_digest = "sha256:" + hashlib.sha256(
        canonical_json_bytes(
            {
                "data_plane_observations": list(data_plane_observations),
                "positive_observations": list(positive_observations),
                "qualification_observation": qualification_observation,
                "shutdown_observation": shutdown_observation,
            }
        )
    ).hexdigest()
    document = {
        "protocol": "mycelium.product_concurrency_liveness_qualification.v1",
        "deployment_id": positive_observations[0]["deployment_id"],
        "qualification_digest": qualification_digest,
        "maximum_concurrent_requests": 4,
        "cancellation_and_cleanup_bound_ms": 2000,
        "cooperative_interruption_proven": True,
        "request_scoped_cleanup_proven": True,
        "shared_process_termination_used": False,
        "publisher_generation_fencing_proven": True,
        "scoped_liveness_proven": True,
        "eligible": True,
        "evidence_digest": evidence_digest,
    }
    return validate_product_qualification(document)


def load_a4_evidence_files(
    *,
    positive: Sequence[Path] | None = None,
    data_plane: Sequence[Path] | None = None,
    qualification: Path | None = None,
    shutdown: Path | None = None,
) -> dict[str, Any]:
    """Load owner-supplied gate artifacts from disk into validated documents."""

    positives = tuple(_read_artifact(path, "positive") for path in positive or ())
    data_planes = tuple(_read_artifact(path, "data_plane") for path in data_plane or ())
    qualification_document = (
        _read_artifact(qualification, "qualification")
        if qualification is not None
        else None
    )
    shutdown_document = (
        _read_artifact(shutdown, "shutdown") if shutdown is not None else None
    )
    if qualification_document is None or shutdown_document is None:
        raise A4EvidenceError("a4_evidence_incomplete")
    return {
        "positive_observations": positives,
        "data_plane_observations": data_planes,
        "qualification_observation": qualification_document,
        "shutdown_observation": shutdown_document,
        "positive_sources": tuple(positive or ()),
        "data_plane_sources": tuple(data_plane or ()),
        "qualification_source": qualification,
        "shutdown_source": shutdown,
    }
