"""Frozen public contracts for the separately authenticated request gateway."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import re
from typing import Any, Mapping

from mycelium_qualification.contracts import (
    RouteQualificationV1,
    route_qualification_to_dict,
)
from mycelium_qualification.evidence import canonical_json_bytes, is_sha256_ref, sha256_bytes

REQUEST_GATEWAY_PROTOCOL = "mycelium.request_gateway.v1"
REQUEST_EVENT_PROTOCOL = "mycelium.request_event.v1"
MAX_PROMPT_UTF8_BYTES = 131_072
MAX_NEW_TOKENS = 4_096
_REQUEST_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._~-]{0,127}")
_ERROR_CODE_RE = re.compile(r"[a-z][a-z0-9_]{0,63}")
_PUBLIC_MODEL_ID_RE = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9._:~-]{0,63}"
    r"(?:/[A-Za-z0-9][A-Za-z0-9._:~-]{0,63})?"
)


def is_valid_request_id(value: object) -> bool:
    return isinstance(value, str) and _REQUEST_ID_RE.fullmatch(value) is not None


def is_safe_error_code(value: object) -> bool:
    return isinstance(value, str) and _ERROR_CODE_RE.fullmatch(value) is not None


def is_public_model_id(value: object) -> bool:
    """Return whether a model id is safe for the browser-visible contract."""
    return isinstance(value, str) and _PUBLIC_MODEL_ID_RE.fullmatch(value) is not None


class AdmissionError(ValueError):
    """Stable fail-closed admission error with no private request material."""

    def __init__(self, code: str) -> None:
        if not is_safe_error_code(code):
            raise ValueError("invalid_admission_error_code")
        self.code = code
        super().__init__(code)


def _require(condition: bool, code: str) -> None:
    if not condition:
        raise AdmissionError(code)


def qualification_digest(qualification: RouteQualificationV1) -> str:
    """Digest the entire canonical authority record for exact-match admission."""
    return sha256_bytes(canonical_json_bytes(route_qualification_to_dict(qualification)))


def _public_model_id(model_id: str) -> str:
    if is_public_model_id(model_id):
        return model_id
    return "model-sha256-" + hashlib.sha256(model_id.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class QualificationBinding:
    """Client-safe exact admission identity derived from RouteQualificationV1."""

    qualification_id: str
    qualification_digest: str
    deployment_id: str
    deployment_epoch: int
    topology_version: int
    model_id: str
    resolved_commit: str
    manifest_digest: str
    path_manifest_digest: str
    stage_load_proof_digests: tuple[str, ...]

    def __post_init__(self) -> None:
        for value in (
            self.qualification_id,
            self.deployment_id,
            self.model_id,
            self.resolved_commit,
        ):
            _require(isinstance(value, str) and bool(value), "invalid_qualification_binding")
        for value in (
            self.qualification_digest,
            self.manifest_digest,
            self.path_manifest_digest,
            *self.stage_load_proof_digests,
        ):
            _require(isinstance(value, str) and is_sha256_ref(value), "invalid_qualification_binding")
        _require(
            isinstance(self.deployment_epoch, int)
            and not isinstance(self.deployment_epoch, bool)
            and self.deployment_epoch >= 0,
            "invalid_qualification_binding",
        )
        _require(
            isinstance(self.topology_version, int)
            and not isinstance(self.topology_version, bool)
            and self.topology_version >= 0,
            "invalid_qualification_binding",
        )
        _require(
            len(set(self.stage_load_proof_digests)) == len(self.stage_load_proof_digests),
            "invalid_qualification_binding",
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "qualification_id": self.qualification_id,
            "qualification_digest": self.qualification_digest,
            "deployment_id": self.deployment_id,
            "deployment_epoch": self.deployment_epoch,
            "topology_version": self.topology_version,
            "model_id": self.model_id,
            "resolved_commit": self.resolved_commit,
            "manifest_digest": self.manifest_digest,
            "path_manifest_digest": self.path_manifest_digest,
            "stage_load_proof_digests": list(self.stage_load_proof_digests),
        }

    @classmethod
    def from_dict(cls, document: Mapping[str, Any]) -> "QualificationBinding":
        expected = {
            "qualification_id",
            "qualification_digest",
            "deployment_id",
            "deployment_epoch",
            "topology_version",
            "model_id",
            "resolved_commit",
            "manifest_digest",
            "path_manifest_digest",
            "stage_load_proof_digests",
        }
        _require(isinstance(document, Mapping) and set(document) == expected, "invalid_qualification_binding")
        stage_digests = document["stage_load_proof_digests"]
        _require(isinstance(stage_digests, (list, tuple)), "invalid_qualification_binding")
        return cls(
            qualification_id=document["qualification_id"],
            qualification_digest=document["qualification_digest"],
            deployment_id=document["deployment_id"],
            deployment_epoch=document["deployment_epoch"],
            topology_version=document["topology_version"],
            model_id=document["model_id"],
            resolved_commit=document["resolved_commit"],
            manifest_digest=document["manifest_digest"],
            path_manifest_digest=document["path_manifest_digest"],
            stage_load_proof_digests=tuple(stage_digests),
        )


def qualification_binding(qualification: RouteQualificationV1) -> QualificationBinding:
    """Project an authority record into the exact client admission binding."""
    model_id = _public_model_id(qualification.model_id)
    return QualificationBinding(
        qualification_id=qualification.qualification_id,
        qualification_digest=qualification_digest(qualification),
        deployment_id=qualification.deployment_id,
        deployment_epoch=qualification.deployment_epoch,
        topology_version=qualification.topology_version,
        model_id=model_id,
        resolved_commit=qualification.resolved_commit,
        manifest_digest=qualification.manifest_digest,
        path_manifest_digest=qualification.path_manifest_digest,
        stage_load_proof_digests=tuple(
            sorted(binding.load_proof_digest for binding in qualification.stage_bindings)
        ),
    )


def safe_qualification_projection(qualification: RouteQualificationV1) -> dict[str, Any]:
    """Allowlisted projection; never exposes endpoints, processes, or reservations."""
    return {
        "protocol": REQUEST_GATEWAY_PROTOCOL,
        "issued_at_unix_ms": qualification.issued_at_unix_ms,
        "evidence_class": qualification.evidence_class,
        "route_ready": qualification.route_ready,
        "reason_codes": list(qualification.reason_codes),
        "binding": qualification_binding(qualification).to_dict(),
    }


@dataclass(frozen=True, slots=True)
class InferenceSubmission:
    prompt: str
    max_new_tokens: int
    qualification: QualificationBinding
    protocol: str = REQUEST_GATEWAY_PROTOCOL

    def __post_init__(self) -> None:
        _require(self.protocol == REQUEST_GATEWAY_PROTOCOL, "unsupported_request_protocol")
        _require(isinstance(self.prompt, str) and bool(self.prompt), "invalid_prompt")
        _require(
            len(self.prompt.encode("utf-8")) <= MAX_PROMPT_UTF8_BYTES,
            "prompt_too_large",
        )
        _require(
            isinstance(self.max_new_tokens, int)
            and not isinstance(self.max_new_tokens, bool)
            and 1 <= self.max_new_tokens <= MAX_NEW_TOKENS,
            "invalid_max_new_tokens",
        )
        _require(isinstance(self.qualification, QualificationBinding), "invalid_qualification_binding")

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol": self.protocol,
            "prompt": self.prompt,
            "max_new_tokens": self.max_new_tokens,
            "qualification": self.qualification.to_dict(),
        }

    @classmethod
    def from_dict(cls, document: Mapping[str, Any]) -> "InferenceSubmission":
        _require(
            isinstance(document, Mapping)
            and set(document) == {"protocol", "prompt", "max_new_tokens", "qualification"},
            "invalid_submission",
        )
        _require(document["protocol"] == REQUEST_GATEWAY_PROTOCOL, "unsupported_request_protocol")
        qualification = document["qualification"]
        _require(isinstance(qualification, Mapping), "invalid_qualification_binding")
        return cls(
            prompt=document["prompt"],
            max_new_tokens=document["max_new_tokens"],
            qualification=QualificationBinding.from_dict(qualification),
            protocol=document["protocol"],
        )


@dataclass(frozen=True, slots=True)
class StreamEvent:
    """Deterministically ordered client event; token content exists only here."""

    request_id: str
    sequence: int
    kind: str
    token_index: int | None = None
    text: str | None = None
    code: str | None = None
    protocol: str = REQUEST_EVENT_PROTOCOL

    def __post_init__(self) -> None:
        _require(
            isinstance(self.request_id, str) and bool(self.request_id),
            "invalid_stream_event",
        )
        _require(
            isinstance(self.sequence, int)
            and not isinstance(self.sequence, bool)
            and self.sequence >= 0,
            "invalid_stream_event",
        )
        _require(
            self.kind in {"accepted", "token", "completed", "cancelled", "failed"},
            "invalid_stream_event",
        )
        if self.kind == "token":
            _require(
                isinstance(self.token_index, int)
                and not isinstance(self.token_index, bool)
                and self.token_index >= 0
                and isinstance(self.text, str)
                and self.code is None,
                "invalid_stream_event",
            )
        elif self.kind == "failed":
            _require(
                self.token_index is None
                and self.text is None
                and is_safe_error_code(self.code),
                "invalid_stream_event",
            )
        else:
            _require(
                self.token_index is None and self.text is None and self.code is None,
                "invalid_stream_event",
            )

    @property
    def terminal(self) -> bool:
        return self.kind in {"completed", "cancelled", "failed"}

    def to_dict(self) -> dict[str, Any]:
        document: dict[str, Any] = {
            "protocol": self.protocol,
            "request_id": self.request_id,
            "sequence": self.sequence,
            "type": self.kind,
        }
        if self.kind == "token":
            document["token_index"] = self.token_index
            document["text"] = self.text
        elif self.kind == "failed":
            document["code"] = self.code
        return document

    @classmethod
    def from_dict(cls, document: Mapping[str, Any]) -> "StreamEvent":
        _require(isinstance(document, Mapping), "invalid_stream_event")
        allowed = {"protocol", "request_id", "sequence", "type", "token_index", "text", "code"}
        _require(set(document).issubset(allowed), "invalid_stream_event")
        _require(document.get("protocol") == REQUEST_EVENT_PROTOCOL, "invalid_stream_event")
        kind_value = document.get("type")
        _require(isinstance(kind_value, str), "invalid_stream_event")
        kind = kind_value
        required = {"protocol", "request_id", "sequence", "type"}
        if kind == "token":
            required.update({"token_index", "text"})
        elif kind == "failed":
            required.add("code")
        _require(set(document) == required, "invalid_stream_event")
        return cls(
            request_id=document["request_id"],
            sequence=document["sequence"],
            kind=kind,
            token_index=document.get("token_index"),
            text=document.get("text"),
            code=document.get("code"),
            protocol=document["protocol"],
        )
