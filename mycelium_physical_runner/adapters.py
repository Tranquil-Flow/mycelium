"""Production adapters for immutable sealing, sole qualification, and publication."""
from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Protocol

from mycelium_qualification import (
    QualificationAuthority,
    route_qualification_to_dict,
    seal_physical_evidence,
)
from mycelium_qualification.sealer import (
    REQUIRED_AUTHORITY_DOCUMENTS as _REQUIRED_AUTHORITY_DOCUMENTS,
    _read_sealed_evidence,
)

from .errors import RunnerError

REQUIRED_AUTHORITY_DOCUMENTS = tuple(sorted(_REQUIRED_AUTHORITY_DOCUMENTS))
QUALIFIED_BY = "mycelium_qualification.qualifier:RouteQualificationV1"
SealAdapter = Callable[..., Mapping[str, Any]]


class Publisher(Protocol):
    def __call__(self, sealed: Mapping[str, Any]) -> Mapping[str, Any]: ...

    def revoke(self, qualification_id: str) -> bool: ...


def _documents(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != set(REQUIRED_AUTHORITY_DOCUMENTS):
        raise RunnerError("authority_documents_invalid")
    return {path: value[path] for path in REQUIRED_AUTHORITY_DOCUMENTS}


def build_seal_adapter(
    *,
    output_dir: str | Path,
    document_builder: Callable[[Mapping[str, Any]], Mapping[str, Any]],
    seal_physical_evidence_fn: Callable[..., Any] = seal_physical_evidence,
) -> SealAdapter:
    if not callable(document_builder):
        raise RunnerError("authority_documents_builder_missing")
    output_root = Path(output_dir)

    def adapter(*, run_id: str, evidence: Mapping[str, Any]) -> Mapping[str, Any]:
        if not isinstance(run_id, str) or not run_id:
            raise RunnerError("seal_input_invalid", "run_id")
        if not isinstance(evidence, Mapping):
            raise RunnerError("seal_input_invalid", "evidence")
        try:
            documents = _documents(document_builder(evidence))
            sealed = seal_physical_evidence_fn(
                output_dir=output_root,
                run_id=run_id,
                documents=documents,
            )
        except RunnerError:
            raise
        except Exception as exc:
            raise RunnerError("seal_failed") from exc
        return {
            "run_id": run_id,
            "manifest_path": str(Path(sealed.root) / "evidence-manifest.json"),
            "manifest_digest": str(sealed.manifest_digest),
        }

    return adapter


def build_authority_publisher(
    *,
    authority: QualificationAuthority,
    verify_gossip_signature: Callable[[bytes, Mapping[str, Any]], bool],
    verify_load_proof_signature: Callable[[bytes, Mapping[str, Any]], bool],
) -> Publisher:
    class AuthorityPublisher:
        def __call__(self, sealed: Mapping[str, Any]) -> Mapping[str, Any]:
            manifest_path = sealed.get("manifest_path")
            if not isinstance(manifest_path, str):
                raise RunnerError("sealed_descriptor_invalid")
            previous = authority.current()
            previous_id = previous.qualification_id if previous is not None else None
            published_id: str | None = None
            try:
                files, manifest = _read_sealed_evidence(Path(manifest_path).parent)
                record = authority.qualify_and_publish(
                    evidence_files=files,
                    evidence_manifest=manifest,
                    verify_gossip_signature=verify_gossip_signature,
                    verify_load_proof_signature=verify_load_proof_signature,
                )
                published_id = record.qualification_id
                published = route_qualification_to_dict(record)
            except Exception as exc:
                if published_id is not None and published_id != previous_id:
                    authority.drop(expected_qualification_id=published_id)
                raise RunnerError("authority_publish_failed") from exc
            sealed_digest = sealed.get("manifest_digest")
            identity_matches = (
                isinstance(published_id, str)
                and bool(published_id)
                and isinstance(sealed_digest, str)
                and published.get("evidence_manifest_digest") == sealed_digest
                and published.get("evidence_class") == "physical_qualification"
                and published.get("route_ready") is True
                and published.get("reason_codes") == []
                and published.get("qualified_by") == QUALIFIED_BY
            )
            if not identity_matches:
                if published_id is not None and published_id != previous_id:
                    authority.drop(expected_qualification_id=published_id)
                raise RunnerError("authority_publication_mismatch")
            return published

        def revoke(self, qualification_id: str) -> bool:
            try:
                return authority.drop(expected_qualification_id=qualification_id)
            except Exception as exc:
                raise RunnerError("authority_revoke_failed") from exc

    return AuthorityPublisher()


__all__ = [
    "QUALIFIED_BY",
    "REQUIRED_AUTHORITY_DOCUMENTS",
    "Publisher",
    "SealAdapter",
    "build_authority_publisher",
    "build_seal_adapter",
]
