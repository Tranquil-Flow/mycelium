"""Strict, privacy-reduced contracts for assignment-local swarm artifacts.

The module is deliberately transport-neutral.  It validates the immutable objects
that a Provisioner may hand to an authenticated transfer implementation; it does not
discover peers, open network connections, download models, or qualify a route.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
import copy
import hashlib
import math
from pathlib import PurePosixPath
import re
from typing import Any

from mycelium_qualification.evidence import canonical_json_bytes
from mycelium_qualification.signing import Ed25519EvidenceSigner, SIGNATURE_FIELDS


MANIFEST_PROTOCOL = "mycelium.swarm_stage_pack_manifest.v1"
AVAILABILITY_PROTOCOL = "mycelium.swarm_artifact_availability.v1"
AVAILABILITY_BUNDLE_PROTOCOL = "mycelium.artifact_availability_bundle.v1"
GRANT_PROTOCOL = "mycelium.swarm_artifact_grant.v1"
POLICY_PROTOCOL = "mycelium.swarm_artifact_policy.v1"
ACQUISITION_PROTOCOL = "mycelium.swarm_artifact_acquisition.v1"
LEDGER_PROTOCOL = "mycelium.swarm_artifact_acquisition_ledger.v1"
CHUNK_REQUEST_PROTOCOL = "mycelium.swarm_artifact_chunk_request.v1"
CHUNK_RECEIPT_PROTOCOL = "mycelium.swarm_artifact_chunk_receipt.v1"

_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_REVISION = re.compile(r"[0-9a-f]{40}\Z")
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_SOURCE_REF = re.compile(r"source-[0-9a-f]{12}\Z")
_REASON = re.compile(r"[a-z][a-z0-9_]{0,127}\Z")
_COMPONENTS = frozenset(
    {
        "embedding",
        "transformer_layers",
        "final_norm",
        "lm_head",
        "tokenizer",
        "model_config",
    }
)
_HEALTH = frozenset({"healthy", "degraded", "paused", "unavailable"})
_SOURCE_STATES = frozenset({"eligible", "active", "rotated", "lost", "origin"})
_ACQUISITION_STATES = frozenset(
    {
        "pending",
        "reserving",
        "discovering_sources",
        "transferring",
        "verifying_chunks",
        "verifying_pack",
        "promoting",
        "ready",
        "cancelling",
        "cancelled",
        "failed",
    }
)
_TERMINAL_STATES = frozenset({"ready", "cancelled", "failed"})
_MANIFEST_FIELDS = frozenset(
    {
        "protocol",
        "manifest_id",
        "manifest_digest",
        "model_id",
        "model_revision",
        "model_artifact_digest",
        "source_quantization",
        "serving_dtype",
        "serving_quantization",
        "representation_digest",
        "owner_decision_digest",
        "feasibility_digest",
        "evidence_generation",
        "assignment_id",
        "assignment_digest",
        "graph_digest",
        "recipient_member_id",
        "recipient_membership_generation",
        "placement_id",
        "stage_id",
        "layer_start",
        "layer_end_exclusive",
        "component_scope",
        "tensor_scope_digest",
        "pack_format",
        "files",
        "stage_pack_digest",
        "chunk_size_bytes",
        "total_size_bytes",
        "merkle_root",
        "chunks",
        "issued_at_unix_ms",
        "expires_at_unix_ms",
        "owner_provenance",
    }
)
_FILE_FIELDS = frozenset(
    {"relative_path", "components", "offset_bytes", "size_bytes", "content_digest"}
)
_CHUNK_FIELDS = frozenset(
    {"index", "offset_bytes", "size_bytes", "content_digest", "merkle_proof"}
)
_PROOF_FIELDS = frozenset({"side", "digest"})
_AVAILABILITY_FIELDS = frozenset(
    {
        "protocol",
        "advertisement_id",
        "source_member_id",
        "membership_generation",
        "manifest_digest",
        "available_chunk_digests",
        "verified_bytes",
        "max_concurrent_streams",
        "max_bytes_per_second",
        "serving_priority",
        "transfer_health",
        "observed_at_unix_ms",
        "valid_until_unix_ms",
        "signature",
    }
)
_AVAILABILITY_BUNDLE_FIELDS = frozenset(
    {
        "protocol",
        "source_member_id",
        "membership_generation",
        "advertisements",
        "published_at_unix_ms",
    }
)
_GRANT_FIELDS = frozenset(
    {
        "protocol",
        "grant_id",
        "nonce",
        "provisioner_generation",
        "recipient_member_id",
        "recipient_membership_generation",
        "manifest_digest",
        "assignment_digest",
        "representation_digest",
        "feasibility_digest",
        "allowed_chunk_digests",
        "maximum_total_bytes",
        "maximum_concurrency",
        "maximum_bytes_per_second",
        "authorized_source_member_ids",
        "origin_fallback_allowed",
        "issued_at_unix_ms",
        "not_before_unix_ms",
        "expires_at_unix_ms",
        "signature",
    }
)
_CHUNK_REQUEST_FIELDS = frozenset(
    {
        "protocol",
        "request_id",
        "request_nonce",
        "grant",
        "source_member_id",
        "recipient_member_id",
        "recipient_membership_generation",
        "manifest_digest",
        "chunk_digest",
        "offset_bytes",
        "length_bytes",
        "issued_at_unix_ms",
        "expires_at_unix_ms",
        "signature",
    }
)
_CHUNK_RECEIPT_FIELDS = frozenset(
    {
        "protocol",
        "request_id",
        "source_member_id",
        "source_membership_generation",
        "recipient_member_id",
        "manifest_digest",
        "chunk_digest",
        "offset_bytes",
        "length_bytes",
        "range_content_digest",
        "advertisement_id",
        "responded_at_unix_ms",
        "signature",
    }
)
_POLICY_FIELDS = frozenset(
    {
        "protocol",
        "chunk_size_bytes",
        "maximum_sources",
        "per_source_concurrency",
        "aggregate_concurrency",
        "maximum_retries_per_chunk",
        "maximum_source_rotations",
        "partial_state_ttl_seconds",
        "disk_reserve_bytes",
        "per_source_bytes_per_second",
        "aggregate_bytes_per_second",
        "serving_traffic_reserve_ratio",
        "multi_source_threshold_bytes",
        "minimum_predicted_improvement_ratio",
        "allow_redundant_hedging",
        "thermal_classes_allowed",
        "power_classes_allowed",
    }
)
_STATUS_FIELDS = frozenset(
    {
        "protocol",
        "generation",
        "acquisition_id",
        "state",
        "phase",
        "model_id",
        "model_revision",
        "representation",
        "assignment_id",
        "placement_id",
        "stage_id",
        "layer_start",
        "layer_end_exclusive",
        "total_bytes",
        "cached_verified_bytes",
        "transferred_verified_bytes",
        "missing_bytes",
        "quarantined_bytes",
        "duplicate_bytes_prevented",
        "eligible_source_count",
        "active_source_count",
        "sources",
        "origin_bytes",
        "aggregate_bytes_per_second",
        "eta_seconds",
        "chunk_count",
        "verified_chunk_count",
        "resumed_chunk_count",
        "source_rotation_count",
        "manifest_digest",
        "assignment_digest",
        "representation_digest",
        "feasibility_digest",
        "evidence_generation",
        "promotion_digest",
        "reason_code",
        "retryable",
        "started_at_unix_ms",
        "updated_at_unix_ms",
        "terminal_at_unix_ms",
    }
)
_SOURCE_STATUS_FIELDS = frozenset({"source_ref", "state", "verified_bytes"})
_LEDGER_FIELDS = frozenset({"protocol", "generation", "current", "history"})


class SwarmArtifactContractError(ValueError):
    """Fail-closed contract error with a stable public code."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _fail(code: str) -> None:
    raise SwarmArtifactContractError(code)


def _closed(value: object, fields: frozenset[str], code: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != fields:
        _fail(code)
    return value


def _text(value: object, code: str, *, maximum: int = 256) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        _fail(code)
    return value


def _identifier(value: object, code: str) -> str:
    text = _text(value, code, maximum=128)
    if _IDENTIFIER.fullmatch(text) is None or "/" in text or "@" in text:
        _fail(code)
    return text


def _digest(value: object, code: str) -> str:
    text = _text(value, code, maximum=71)
    if _DIGEST.fullmatch(text) is None:
        _fail(code)
    return text


def _integer(
    value: object, code: str, *, minimum: int = 0, maximum: int = 2**63 - 1
) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        _fail(code)
    return value


def _number(
    value: object, code: str, *, minimum: float = 0.0, maximum: float = 1e18
) -> float:
    if (
        type(value) not in {int, float}
        or not math.isfinite(value)
        or not minimum <= value <= maximum
    ):
        _fail(code)
    return float(value)


def _sorted_unique_strings(
    value: object,
    code: str,
    *,
    validator: Callable[[object, str], str],
    allow_empty: bool = False,
    maximum: int = 100_000,
) -> list[str]:
    if (
        not isinstance(value, list)
        or (not value and not allow_empty)
        or len(value) > maximum
    ):
        _fail(code)
    items = [validator(item, code) for item in value]
    if items != sorted(set(items)):
        _fail(code)
    return items


def _unsigned(document: Mapping[str, Any], field: str) -> dict[str, Any]:
    return {
        key: copy.deepcopy(value) for key, value in document.items() if key != field
    }


def canonical_digest(value: object) -> str:
    return "sha256:" + hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _leaf(content_digest: str) -> bytes:
    return hashlib.sha256(b"\x00" + bytes.fromhex(content_digest[7:])).digest()


def _branch(left: bytes, right: bytes) -> bytes:
    return hashlib.sha256(b"\x01" + left + right).digest()


def merkle_root(content_digests: Sequence[str]) -> str:
    if not content_digests:
        _fail("stage_pack_chunks_invalid")
    level = [
        _leaf(_digest(value, "stage_pack_chunks_invalid")) for value in content_digests
    ]
    while len(level) > 1:
        if len(level) % 2:
            level.append(level[-1])
        level = [
            _branch(level[index], level[index + 1]) for index in range(0, len(level), 2)
        ]
    return "sha256:" + level[0].hex()


def merkle_proofs(
    content_digests: Sequence[str],
) -> tuple[tuple[dict[str, str], ...], ...]:
    if not content_digests:
        _fail("stage_pack_chunks_invalid")
    levels: list[list[bytes]] = [
        [
            _leaf(_digest(value, "stage_pack_chunks_invalid"))
            for value in content_digests
        ]
    ]
    while len(levels[-1]) > 1:
        level = list(levels[-1])
        if len(level) % 2:
            level.append(level[-1])
        levels.append(
            [
                _branch(level[index], level[index + 1])
                for index in range(0, len(level), 2)
            ]
        )
    proofs: list[tuple[dict[str, str], ...]] = []
    for original_index in range(len(content_digests)):
        index = original_index
        proof: list[dict[str, str]] = []
        for raw_level in levels[:-1]:
            level = list(raw_level)
            if len(level) % 2:
                level.append(level[-1])
            sibling_index = index - 1 if index % 2 else index + 1
            proof.append(
                {
                    "side": "left" if index % 2 else "right",
                    "digest": "sha256:" + level[sibling_index].hex(),
                }
            )
            index //= 2
        proofs.append(tuple(proof))
    return tuple(proofs)


def verify_merkle_proof(content_digest: str, proof: object, root: str) -> bool:
    try:
        current = _leaf(_digest(content_digest, "stage_pack_merkle_proof_invalid"))
        expected_root = _digest(root, "stage_pack_merkle_proof_invalid")
        if not isinstance(proof, list) or len(proof) > 64:
            return False
        for raw in proof:
            item = _closed(raw, _PROOF_FIELDS, "stage_pack_merkle_proof_invalid")
            side = item.get("side")
            sibling = bytes.fromhex(
                _digest(item.get("digest"), "stage_pack_merkle_proof_invalid")[7:]
            )
            if side == "left":
                current = _branch(sibling, current)
            elif side == "right":
                current = _branch(current, sibling)
            else:
                return False
        return "sha256:" + current.hex() == expected_root
    except SwarmArtifactContractError:
        return False


def validate_stage_pack_manifest(
    document: object,
    *,
    expected_binding: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    value = _closed(document, _MANIFEST_FIELDS, "stage_pack_manifest_invalid")
    if value.get("protocol") != MANIFEST_PROTOCOL:
        _fail("stage_pack_manifest_invalid")
    if value.get("pack_format") != "mycelium.stage_pack_stream.v1":
        _fail("stage_pack_format_invalid")
    for field in (
        "manifest_id",
        "assignment_id",
        "recipient_member_id",
        "placement_id",
        "stage_id",
    ):
        _identifier(value.get(field), "stage_pack_manifest_invalid")
    _text(value.get("model_id"), "stage_pack_manifest_invalid")
    if (
        not isinstance(value.get("model_revision"), str)
        or _REVISION.fullmatch(value["model_revision"]) is None
    ):
        _fail("stage_pack_manifest_invalid")
    for field in (
        "manifest_digest",
        "model_artifact_digest",
        "representation_digest",
        "owner_decision_digest",
        "feasibility_digest",
        "assignment_digest",
        "graph_digest",
        "tensor_scope_digest",
        "stage_pack_digest",
        "merkle_root",
    ):
        _digest(value.get(field), "stage_pack_manifest_invalid")
    for field in (
        "source_quantization",
        "serving_dtype",
        "serving_quantization",
        "owner_provenance",
    ):
        _text(value.get(field), "stage_pack_manifest_invalid", maximum=128)
    evidence_generation = _integer(
        value.get("evidence_generation"), "stage_pack_manifest_invalid", minimum=1
    )
    recipient_generation = _integer(
        value.get("recipient_membership_generation"),
        "stage_pack_manifest_invalid",
        minimum=1,
    )
    layer_start = _integer(value.get("layer_start"), "stage_pack_manifest_invalid")
    layer_end = _integer(
        value.get("layer_end_exclusive"), "stage_pack_manifest_invalid", minimum=1
    )
    chunk_size = _integer(
        value.get("chunk_size_bytes"),
        "stage_pack_manifest_invalid",
        minimum=1,
        maximum=64 * 1024 * 1024,
    )
    total_size = _integer(
        value.get("total_size_bytes"), "stage_pack_manifest_invalid", minimum=1
    )
    issued = _integer(
        value.get("issued_at_unix_ms"), "stage_pack_manifest_invalid", minimum=1
    )
    expires = _integer(
        value.get("expires_at_unix_ms"), "stage_pack_manifest_invalid", minimum=1
    )
    if (
        evidence_generation < 1
        or recipient_generation < 1
        or layer_end <= layer_start
        or expires <= issued
    ):
        _fail("stage_pack_manifest_invalid")
    scope = _sorted_unique_strings(
        value.get("component_scope"),
        "stage_pack_component_scope_invalid",
        validator=lambda item, code: _text(item, code, maximum=32),
    )
    if not set(scope) <= _COMPONENTS or "transformer_layers" not in scope:
        _fail("stage_pack_component_scope_invalid")
    files = value.get("files")
    if not isinstance(files, list) or not files or len(files) > 1_000_000:
        _fail("stage_pack_files_invalid")
    file_cursor = 0
    file_paths: set[str] = set()
    file_components: set[str] = set()
    for raw in files:
        file = _closed(raw, _FILE_FIELDS, "stage_pack_files_invalid")
        relative_path = _text(
            file.get("relative_path"), "stage_pack_files_invalid", maximum=1_024
        )
        path = PurePosixPath(relative_path)
        if (
            path.is_absolute()
            or relative_path != path.as_posix()
            or "\\" in relative_path
            or any(part in {"", ".", ".."} for part in path.parts)
            or relative_path in file_paths
        ):
            _fail("stage_pack_files_invalid")
        components = _sorted_unique_strings(
            file.get("components"),
            "stage_pack_files_invalid",
            validator=lambda item, code: _text(item, code, maximum=32),
        )
        size = _integer(file.get("size_bytes"), "stage_pack_files_invalid", minimum=1)
        if (
            not set(components) <= _COMPONENTS
            or file.get("offset_bytes") != file_cursor
        ):
            _fail("stage_pack_files_invalid")
        _digest(file.get("content_digest"), "stage_pack_files_invalid")
        file_paths.add(relative_path)
        file_components.update(components)
        file_cursor += size
    if file_cursor != total_size or scope != sorted(file_components):
        _fail("stage_pack_files_invalid")
    chunks = value.get("chunks")
    if not isinstance(chunks, list) or not chunks or len(chunks) > 1_000_000:
        _fail("stage_pack_chunks_invalid")
    cursor = 0
    digests: list[str] = []
    for index, raw in enumerate(chunks):
        chunk = _closed(raw, _CHUNK_FIELDS, "stage_pack_chunks_invalid")
        size = _integer(
            chunk.get("size_bytes"),
            "stage_pack_chunks_invalid",
            minimum=1,
            maximum=chunk_size,
        )
        if chunk.get("index") != index or chunk.get("offset_bytes") != cursor:
            _fail("stage_pack_chunks_invalid")
        if index < len(chunks) - 1 and size != chunk_size:
            _fail("stage_pack_chunks_invalid")
        digest = _digest(chunk.get("content_digest"), "stage_pack_chunks_invalid")
        if not verify_merkle_proof(
            digest, chunk.get("merkle_proof"), value["merkle_root"]
        ):
            _fail("stage_pack_merkle_proof_invalid")
        digests.append(digest)
        cursor += size
    if cursor != total_size or merkle_root(digests) != value["merkle_root"]:
        _fail("stage_pack_chunks_invalid")
    expected_digest = canonical_digest(_unsigned(value, "manifest_digest"))
    if value["manifest_digest"] != expected_digest:
        _fail("stage_pack_manifest_digest_mismatch")
    if expected_binding is not None:
        binding_fields = frozenset(
            {
                "model_id",
                "model_revision",
                "representation_digest",
                "owner_decision_digest",
                "feasibility_digest",
                "evidence_generation",
                "assignment_id",
                "assignment_digest",
                "graph_digest",
                "recipient_member_id",
                "recipient_membership_generation",
                "placement_id",
                "stage_id",
                "layer_start",
                "layer_end_exclusive",
                "component_scope",
                "tensor_scope_digest",
            }
        )
        if set(expected_binding) != binding_fields or any(
            value[field] != expected_binding[field] for field in binding_fields
        ):
            _fail("stage_pack_authorization_drift")
    return copy.deepcopy(dict(value))


def _validate_signature(
    document: Mapping[str, Any],
    *,
    verifier: Callable[[bytes, dict[str, Any]], bool],
    code: str,
) -> None:
    signature = document.get("signature")
    if not isinstance(signature, Mapping) or set(signature) != SIGNATURE_FIELDS:
        _fail(code)
    try:
        verified = verifier(
            canonical_json_bytes(_unsigned(document, "signature")), dict(signature)
        )
    except (TypeError, ValueError):
        _fail(code)
    if verified is not True:
        _fail(code)


def validate_availability(
    document: object,
    *,
    verifier: Callable[[bytes, dict[str, Any]], bool],
    now_unix_ms: int,
    expected_manifest_digest: str,
    expected_membership_generation: int | None = None,
) -> dict[str, Any]:
    value = _closed(document, _AVAILABILITY_FIELDS, "artifact_availability_invalid")
    if value.get("protocol") != AVAILABILITY_PROTOCOL:
        _fail("artifact_availability_invalid")
    _identifier(value.get("advertisement_id"), "artifact_availability_invalid")
    _identifier(value.get("source_member_id"), "artifact_availability_invalid")
    generation = _integer(
        value.get("membership_generation"), "artifact_availability_invalid", minimum=1
    )
    manifest_digest = _digest(
        value.get("manifest_digest"), "artifact_availability_invalid"
    )
    chunks = _sorted_unique_strings(
        value.get("available_chunk_digests"),
        "artifact_availability_invalid",
        validator=_digest,
        allow_empty=True,
    )
    verified = _integer(value.get("verified_bytes"), "artifact_availability_invalid")
    streams = _integer(
        value.get("max_concurrent_streams"),
        "artifact_availability_invalid",
        minimum=1,
        maximum=1_024,
    )
    rate = _integer(
        value.get("max_bytes_per_second"), "artifact_availability_invalid", minimum=1
    )
    priority = _integer(
        value.get("serving_priority"),
        "artifact_availability_invalid",
        maximum=1_000_000,
    )
    observed = _integer(
        value.get("observed_at_unix_ms"), "artifact_availability_invalid", minimum=1
    )
    valid_until = _integer(
        value.get("valid_until_unix_ms"), "artifact_availability_invalid", minimum=1
    )
    if (
        value.get("transfer_health") not in _HEALTH
        or valid_until <= observed
        or valid_until < now_unix_ms
        or observed > now_unix_ms
        or manifest_digest != expected_manifest_digest
        or (
            expected_membership_generation is not None
            and generation != expected_membership_generation
        )
        or (not chunks and verified != 0)
        or streams < 1
        or rate < 1
        or priority < 0
    ):
        _fail("artifact_availability_ineligible")
    _validate_signature(
        value, verifier=verifier, code="artifact_availability_signature_invalid"
    )
    return copy.deepcopy(dict(value))


def validate_availability_bundle(
    document: object,
    *,
    verifier: Callable[[bytes, dict[str, Any]], bool],
    now_unix_ms: int,
    expected_source_member_id: str,
    expected_membership_generation: int,
) -> dict[str, Any]:
    """Validate a member-bound collection of individually signed advertisements."""

    value = _closed(
        document,
        _AVAILABILITY_BUNDLE_FIELDS,
        "artifact_availability_bundle_invalid",
    )
    source = _identifier(
        value.get("source_member_id"), "artifact_availability_bundle_invalid"
    )
    generation = _integer(
        value.get("membership_generation"),
        "artifact_availability_bundle_invalid",
        minimum=1,
    )
    published = _integer(
        value.get("published_at_unix_ms"),
        "artifact_availability_bundle_invalid",
        minimum=1,
    )
    raw_advertisements = value.get("advertisements")
    if (
        value.get("protocol") != AVAILABILITY_BUNDLE_PROTOCOL
        or source != expected_source_member_id
        or generation != expected_membership_generation
        or published > now_unix_ms
        or not isinstance(raw_advertisements, list)
        or len(raw_advertisements) > 1_024
    ):
        _fail("artifact_availability_bundle_invalid")
    advertisements = []
    for raw in raw_advertisements:
        if not isinstance(raw, Mapping):
            _fail("artifact_availability_bundle_invalid")
        manifest_digest = _digest(
            raw.get("manifest_digest"), "artifact_availability_bundle_invalid"
        )
        advertisement = validate_availability(
            raw,
            verifier=verifier,
            now_unix_ms=now_unix_ms,
            expected_manifest_digest=manifest_digest,
            expected_membership_generation=generation,
        )
        if (
            advertisement["source_member_id"] != source
            or advertisement["observed_at_unix_ms"] > published
        ):
            _fail("artifact_availability_bundle_invalid")
        advertisements.append(advertisement)
    if [item["manifest_digest"] for item in advertisements] != sorted(
        {item["manifest_digest"] for item in advertisements}
    ):
        _fail("artifact_availability_bundle_invalid")
    return {
        "protocol": AVAILABILITY_BUNDLE_PROTOCOL,
        "source_member_id": source,
        "membership_generation": generation,
        "advertisements": advertisements,
        "published_at_unix_ms": published,
    }


def validate_grant(
    document: object,
    *,
    verifier: Callable[[bytes, dict[str, Any]], bool],
    now_unix_ms: int,
    expected_recipient_member_id: str,
    expected_recipient_membership_generation: int,
    expected_provisioner_generation: int,
    expected_manifest_digest: str,
    expected_assignment_digest: str,
    expected_representation_digest: str,
    expected_feasibility_digest: str,
) -> dict[str, Any]:
    value = _closed(document, _GRANT_FIELDS, "artifact_grant_invalid")
    if value.get("protocol") != GRANT_PROTOCOL:
        _fail("artifact_grant_invalid")
    for field in ("grant_id", "nonce", "recipient_member_id"):
        _identifier(value.get(field), "artifact_grant_invalid")
    provisioner_generation = _integer(
        value.get("provisioner_generation"), "artifact_grant_invalid", minimum=1
    )
    recipient_generation = _integer(
        value.get("recipient_membership_generation"),
        "artifact_grant_invalid",
        minimum=1,
    )
    for field in (
        "manifest_digest",
        "assignment_digest",
        "representation_digest",
        "feasibility_digest",
    ):
        _digest(value.get(field), "artifact_grant_invalid")
    _sorted_unique_strings(
        value.get("allowed_chunk_digests"), "artifact_grant_invalid", validator=_digest
    )
    _sorted_unique_strings(
        value.get("authorized_source_member_ids"),
        "artifact_grant_invalid",
        validator=_identifier,
    )
    maximum_bytes = _integer(
        value.get("maximum_total_bytes"), "artifact_grant_invalid", minimum=1
    )
    maximum_concurrency = _integer(
        value.get("maximum_concurrency"),
        "artifact_grant_invalid",
        minimum=1,
        maximum=1_024,
    )
    maximum_rate = _integer(
        value.get("maximum_bytes_per_second"), "artifact_grant_invalid", minimum=1
    )
    issued = _integer(
        value.get("issued_at_unix_ms"), "artifact_grant_invalid", minimum=1
    )
    not_before = _integer(
        value.get("not_before_unix_ms"), "artifact_grant_invalid", minimum=1
    )
    expires = _integer(
        value.get("expires_at_unix_ms"), "artifact_grant_invalid", minimum=1
    )
    if (
        type(value.get("origin_fallback_allowed")) is not bool
        or issued > not_before
        or expires <= not_before
        or not not_before <= now_unix_ms <= expires
        or value["recipient_member_id"] != expected_recipient_member_id
        or recipient_generation != expected_recipient_membership_generation
        or provisioner_generation != expected_provisioner_generation
        or value["manifest_digest"] != expected_manifest_digest
        or value["assignment_digest"] != expected_assignment_digest
        or value["representation_digest"] != expected_representation_digest
        or value["feasibility_digest"] != expected_feasibility_digest
        or maximum_bytes < 1
        or maximum_concurrency < 1
        or maximum_rate < 1
    ):
        _fail("artifact_grant_ineligible")
    _validate_signature(
        value, verifier=verifier, code="artifact_grant_signature_invalid"
    )
    return copy.deepcopy(dict(value))


def validate_chunk_request(
    document: object,
    *,
    provisioner_verifier: Callable[[bytes, dict[str, Any]], bool],
    recipient_verifier: Callable[[bytes, dict[str, Any]], bool],
    now_unix_ms: int,
    expected_source_member_id: str,
    expected_manifest: Mapping[str, Any],
    expected_provisioner_generation: int,
) -> dict[str, Any]:
    value = _closed(document, _CHUNK_REQUEST_FIELDS, "artifact_chunk_request_invalid")
    if value.get("protocol") != CHUNK_REQUEST_PROTOCOL:
        _fail("artifact_chunk_request_invalid")
    for field in (
        "request_id",
        "request_nonce",
        "source_member_id",
        "recipient_member_id",
    ):
        _identifier(value.get(field), "artifact_chunk_request_invalid")
    generation = _integer(
        value.get("recipient_membership_generation"),
        "artifact_chunk_request_invalid",
        minimum=1,
    )
    manifest_digest = _digest(
        value.get("manifest_digest"), "artifact_chunk_request_invalid"
    )
    chunk_digest = _digest(value.get("chunk_digest"), "artifact_chunk_request_invalid")
    offset = _integer(value.get("offset_bytes"), "artifact_chunk_request_invalid")
    length = _integer(
        value.get("length_bytes"), "artifact_chunk_request_invalid", minimum=1
    )
    issued = _integer(
        value.get("issued_at_unix_ms"),
        "artifact_chunk_request_invalid",
        minimum=1,
    )
    expires = _integer(
        value.get("expires_at_unix_ms"),
        "artifact_chunk_request_invalid",
        minimum=1,
    )
    checked_manifest = validate_stage_pack_manifest(expected_manifest)
    chunk_records = {
        item["content_digest"]: item for item in checked_manifest["chunks"]
    }
    chunk = chunk_records.get(chunk_digest)
    if (
        value["source_member_id"] != expected_source_member_id
        or value["recipient_member_id"] != checked_manifest["recipient_member_id"]
        or generation != checked_manifest["recipient_membership_generation"]
        or manifest_digest != checked_manifest["manifest_digest"]
        or chunk is None
        or offset + length > chunk["size_bytes"]
        or issued > now_unix_ms
        or expires < now_unix_ms
        or expires <= issued
    ):
        _fail("artifact_chunk_request_ineligible")
    checked_grant = validate_grant(
        value.get("grant"),
        verifier=provisioner_verifier,
        now_unix_ms=now_unix_ms,
        expected_recipient_member_id=checked_manifest["recipient_member_id"],
        expected_recipient_membership_generation=checked_manifest[
            "recipient_membership_generation"
        ],
        expected_provisioner_generation=expected_provisioner_generation,
        expected_manifest_digest=checked_manifest["manifest_digest"],
        expected_assignment_digest=checked_manifest["assignment_digest"],
        expected_representation_digest=checked_manifest["representation_digest"],
        expected_feasibility_digest=checked_manifest["feasibility_digest"],
    )
    if (
        chunk_digest not in checked_grant["allowed_chunk_digests"]
        or expected_source_member_id
        not in checked_grant["authorized_source_member_ids"]
        or expires > checked_grant["expires_at_unix_ms"]
    ):
        _fail("artifact_chunk_request_unauthorized")
    _validate_signature(
        value,
        verifier=recipient_verifier,
        code="artifact_chunk_request_signature_invalid",
    )
    return copy.deepcopy(dict(value))


def validate_chunk_receipt(
    document: object,
    *,
    source_verifier: Callable[[bytes, dict[str, Any]], bool],
    request: Mapping[str, Any],
    availability: Mapping[str, Any],
    returned_bytes: bytes,
) -> dict[str, Any]:
    value = _closed(document, _CHUNK_RECEIPT_FIELDS, "artifact_chunk_receipt_invalid")
    if value.get("protocol") != CHUNK_RECEIPT_PROTOCOL:
        _fail("artifact_chunk_receipt_invalid")
    _identifier(value.get("request_id"), "artifact_chunk_receipt_invalid")
    _identifier(value.get("source_member_id"), "artifact_chunk_receipt_invalid")
    _identifier(value.get("recipient_member_id"), "artifact_chunk_receipt_invalid")
    _identifier(value.get("advertisement_id"), "artifact_chunk_receipt_invalid")
    generation = _integer(
        value.get("source_membership_generation"),
        "artifact_chunk_receipt_invalid",
        minimum=1,
    )
    for field in (
        "manifest_digest",
        "chunk_digest",
        "range_content_digest",
    ):
        _digest(value.get(field), "artifact_chunk_receipt_invalid")
    offset = _integer(value.get("offset_bytes"), "artifact_chunk_receipt_invalid")
    length = _integer(
        value.get("length_bytes"), "artifact_chunk_receipt_invalid", minimum=1
    )
    responded = _integer(
        value.get("responded_at_unix_ms"),
        "artifact_chunk_receipt_invalid",
        minimum=1,
    )
    if not isinstance(returned_bytes, bytes) or len(returned_bytes) != length:
        _fail("artifact_chunk_receipt_bytes_invalid")
    if (
        value["request_id"] != request.get("request_id")
        or value["source_member_id"] != request.get("source_member_id")
        or value["recipient_member_id"] != request.get("recipient_member_id")
        or value["manifest_digest"] != request.get("manifest_digest")
        or value["chunk_digest"] != request.get("chunk_digest")
        or offset != request.get("offset_bytes")
        or length != request.get("length_bytes")
        or value["range_content_digest"]
        != "sha256:" + hashlib.sha256(returned_bytes).hexdigest()
        or value["advertisement_id"] != availability.get("advertisement_id")
        or generation != availability.get("membership_generation")
        or value["source_member_id"] != availability.get("source_member_id")
        or value["manifest_digest"] != availability.get("manifest_digest")
        or value["chunk_digest"] not in availability.get("available_chunk_digests", [])
        or responded < request.get("issued_at_unix_ms", 0)
        or responded > request.get("expires_at_unix_ms", 0)
    ):
        _fail("artifact_chunk_receipt_mismatch")
    _validate_signature(
        value,
        verifier=source_verifier,
        code="artifact_chunk_receipt_signature_invalid",
    )
    return copy.deepcopy(dict(value))


def validate_policy(document: object) -> dict[str, Any]:
    value = _closed(document, _POLICY_FIELDS, "artifact_policy_invalid")
    if value.get("protocol") != POLICY_PROTOCOL:
        _fail("artifact_policy_invalid")
    chunk_size = _integer(
        value.get("chunk_size_bytes"),
        "artifact_policy_invalid",
        minimum=64 * 1024,
        maximum=64 * 1024 * 1024,
    )
    maximum_sources = _integer(
        value.get("maximum_sources"), "artifact_policy_invalid", minimum=1, maximum=64
    )
    per_source = _integer(
        value.get("per_source_concurrency"),
        "artifact_policy_invalid",
        minimum=1,
        maximum=64,
    )
    aggregate = _integer(
        value.get("aggregate_concurrency"),
        "artifact_policy_invalid",
        minimum=1,
        maximum=4_096,
    )
    retries = _integer(
        value.get("maximum_retries_per_chunk"), "artifact_policy_invalid", maximum=64
    )
    rotations = _integer(
        value.get("maximum_source_rotations"), "artifact_policy_invalid", maximum=1_024
    )
    ttl = _integer(
        value.get("partial_state_ttl_seconds"),
        "artifact_policy_invalid",
        minimum=1,
        maximum=31_536_000,
    )
    reserve = _integer(value.get("disk_reserve_bytes"), "artifact_policy_invalid")
    per_source_rate = _integer(
        value.get("per_source_bytes_per_second"), "artifact_policy_invalid", minimum=1
    )
    aggregate_rate = _integer(
        value.get("aggregate_bytes_per_second"), "artifact_policy_invalid", minimum=1
    )
    serving_reserve = _number(
        value.get("serving_traffic_reserve_ratio"),
        "artifact_policy_invalid",
        maximum=1.0,
    )
    threshold = _integer(
        value.get("multi_source_threshold_bytes"), "artifact_policy_invalid", minimum=1
    )
    improvement = _number(
        value.get("minimum_predicted_improvement_ratio"),
        "artifact_policy_invalid",
        maximum=1.0,
    )
    if type(value.get("allow_redundant_hedging")) is not bool:
        _fail("artifact_policy_invalid")
    thermal = _sorted_unique_strings(
        value.get("thermal_classes_allowed"),
        "artifact_policy_invalid",
        validator=lambda item, code: _text(item, code, maximum=32),
    )
    power = _sorted_unique_strings(
        value.get("power_classes_allowed"),
        "artifact_policy_invalid",
        validator=lambda item, code: _text(item, code, maximum=32),
    )
    if (
        aggregate < per_source
        or aggregate > maximum_sources * per_source
        or aggregate_rate < per_source_rate
        or chunk_size > threshold
        or retries > 64
        or rotations > 1_024
        or ttl < 1
        or reserve < 0
        or serving_reserve >= 1.0
        or improvement <= 0.0
        or not thermal
        or not power
    ):
        _fail("artifact_policy_invalid")
    return copy.deepcopy(dict(value))


def validate_acquisition_status(document: object) -> dict[str, Any]:
    value = _closed(document, _STATUS_FIELDS, "artifact_acquisition_status_invalid")
    if (
        value.get("protocol") != ACQUISITION_PROTOCOL
        or value.get("state") not in _ACQUISITION_STATES
    ):
        _fail("artifact_acquisition_status_invalid")
    _integer(value.get("generation"), "artifact_acquisition_status_invalid", minimum=1)
    for field in ("acquisition_id", "assignment_id", "placement_id", "stage_id"):
        _identifier(value.get(field), "artifact_acquisition_status_invalid")
    _text(value.get("model_id"), "artifact_acquisition_status_invalid")
    if (
        not isinstance(value.get("model_revision"), str)
        or _REVISION.fullmatch(value["model_revision"]) is None
    ):
        _fail("artifact_acquisition_status_invalid")
    _text(
        value.get("representation"), "artifact_acquisition_status_invalid", maximum=128
    )
    start = _integer(value.get("layer_start"), "artifact_acquisition_status_invalid")
    end = _integer(
        value.get("layer_end_exclusive"),
        "artifact_acquisition_status_invalid",
        minimum=1,
    )
    if end <= start:
        _fail("artifact_acquisition_status_invalid")
    byte_fields = (
        "total_bytes",
        "cached_verified_bytes",
        "transferred_verified_bytes",
        "missing_bytes",
        "quarantined_bytes",
        "duplicate_bytes_prevented",
        "origin_bytes",
    )
    for field in byte_fields:
        _integer(value.get(field), "artifact_acquisition_status_invalid")
    count_fields = (
        "eligible_source_count",
        "active_source_count",
        "chunk_count",
        "verified_chunk_count",
        "resumed_chunk_count",
        "source_rotation_count",
        "evidence_generation",
    )
    for field in count_fields:
        _integer(value.get(field), "artifact_acquisition_status_invalid")
    _number(
        value.get("aggregate_bytes_per_second"), "artifact_acquisition_status_invalid"
    )
    eta = value.get("eta_seconds")
    if eta is not None:
        _number(eta, "artifact_acquisition_status_invalid")
    for field in (
        "manifest_digest",
        "assignment_digest",
        "representation_digest",
        "feasibility_digest",
    ):
        _digest(value.get(field), "artifact_acquisition_status_invalid")
    promotion = value.get("promotion_digest")
    if promotion is not None:
        _digest(promotion, "artifact_acquisition_status_invalid")
    reason = value.get("reason_code")
    if reason is not None and (
        not isinstance(reason, str) or _REASON.fullmatch(reason) is None
    ):
        _fail("artifact_acquisition_status_invalid")
    if type(value.get("retryable")) is not bool:
        _fail("artifact_acquisition_status_invalid")
    started = _integer(
        value.get("started_at_unix_ms"),
        "artifact_acquisition_status_invalid",
        minimum=1,
    )
    updated = _integer(
        value.get("updated_at_unix_ms"),
        "artifact_acquisition_status_invalid",
        minimum=started,
    )
    terminal = value.get("terminal_at_unix_ms")
    if terminal is not None:
        _integer(terminal, "artifact_acquisition_status_invalid", minimum=updated)
    phase = value.get("phase")
    if phase is not None and (
        not isinstance(phase, str) or phase not in _ACQUISITION_STATES
    ):
        _fail("artifact_acquisition_status_invalid")
    sources = value.get("sources")
    if not isinstance(sources, list) or len(sources) > 64:
        _fail("artifact_acquisition_status_invalid")
    source_refs: list[str] = []
    source_bytes = 0
    for raw in sources:
        source = _closed(
            raw, _SOURCE_STATUS_FIELDS, "artifact_acquisition_status_invalid"
        )
        source_ref = source.get("source_ref")
        if (
            not isinstance(source_ref, str)
            or _SOURCE_REF.fullmatch(source_ref) is None
            or source.get("state") not in _SOURCE_STATES
        ):
            _fail("artifact_acquisition_status_invalid")
        source_refs.append(source_ref)
        source_bytes += _integer(
            source.get("verified_bytes"), "artifact_acquisition_status_invalid"
        )
    if source_refs != sorted(set(source_refs)):
        _fail("artifact_acquisition_status_invalid")
    if (
        value["cached_verified_bytes"]
        + value["transferred_verified_bytes"]
        + value["missing_bytes"]
        != value["total_bytes"]
        or value["verified_chunk_count"] > value["chunk_count"]
        or value["resumed_chunk_count"] > value["verified_chunk_count"]
        or value["active_source_count"] > value["eligible_source_count"]
        or value["active_source_count"] > len(sources)
        or source_bytes + value["origin_bytes"] != value["transferred_verified_bytes"]
        or (value["state"] in _TERMINAL_STATES) != (terminal is not None)
        or (value["state"] == "ready") != (promotion is not None)
        or (value["state"] == "failed") != (reason is not None)
        or (value["state"] in _TERMINAL_STATES and phase is not None)
    ):
        _fail("artifact_acquisition_status_invalid")
    return copy.deepcopy(dict(value))


def validate_acquisition_ledger(document: object) -> dict[str, Any]:
    value = _closed(document, _LEDGER_FIELDS, "artifact_acquisition_ledger_invalid")
    if value.get("protocol") != LEDGER_PROTOCOL:
        _fail("artifact_acquisition_ledger_invalid")
    generation = _integer(
        value.get("generation"), "artifact_acquisition_ledger_invalid"
    )
    current_raw = value.get("current")
    current = None if current_raw is None else validate_acquisition_status(current_raw)
    history_raw = value.get("history")
    if not isinstance(history_raw, list) or len(history_raw) > 256:
        _fail("artifact_acquisition_ledger_invalid")
    history = [validate_acquisition_status(item) for item in history_raw]
    all_statuses = history + ([] if current is None else [current])
    if (
        (current is not None and current["state"] in _TERMINAL_STATES)
        or any(item["state"] not in _TERMINAL_STATES for item in history)
        or [item["generation"] for item in history]
        != sorted({item["generation"] for item in history})
        or len({item["acquisition_id"] for item in all_statuses}) != len(all_statuses)
        or generation != max([0] + [item["generation"] for item in all_statuses])
    ):
        _fail("artifact_acquisition_ledger_invalid")
    return {
        "protocol": LEDGER_PROTOCOL,
        "generation": generation,
        "current": current,
        "history": history,
    }


def sign_availability(
    statement: Mapping[str, Any], signer: Ed25519EvidenceSigner
) -> dict[str, Any]:
    unsigned = dict(statement)
    if set(unsigned) != _AVAILABILITY_FIELDS - {"signature"}:
        _fail("artifact_availability_invalid")
    return {**copy.deepcopy(unsigned), "signature": signer.sign(unsigned)}


def sign_grant(
    statement: Mapping[str, Any], signer: Ed25519EvidenceSigner
) -> dict[str, Any]:
    unsigned = dict(statement)
    if set(unsigned) != _GRANT_FIELDS - {"signature"}:
        _fail("artifact_grant_invalid")
    return {**copy.deepcopy(unsigned), "signature": signer.sign(unsigned)}


def sign_chunk_request(
    statement: Mapping[str, Any], signer: Ed25519EvidenceSigner
) -> dict[str, Any]:
    unsigned = dict(statement)
    if set(unsigned) != _CHUNK_REQUEST_FIELDS - {"signature"}:
        _fail("artifact_chunk_request_invalid")
    return {**copy.deepcopy(unsigned), "signature": signer.sign(unsigned)}


def sign_chunk_receipt(
    statement: Mapping[str, Any], signer: Ed25519EvidenceSigner
) -> dict[str, Any]:
    unsigned = dict(statement)
    if set(unsigned) != _CHUNK_RECEIPT_FIELDS - {"signature"}:
        _fail("artifact_chunk_receipt_invalid")
    return {**copy.deepcopy(unsigned), "signature": signer.sign(unsigned)}


def source_ref(member_id: str, acquisition_id: str) -> str:
    _identifier(member_id, "artifact_source_identity_invalid")
    _identifier(acquisition_id, "artifact_source_identity_invalid")
    digest = hashlib.sha256(f"{acquisition_id}\0{member_id}".encode()).hexdigest()
    return "source-" + digest[:12]


def select_transfer_sources(
    *,
    missing_chunk_digests: Sequence[str],
    advertisements: Sequence[Mapping[str, Any]],
    policy: Mapping[str, Any],
    predicted_improvement_ratio: float,
    serving_reserve_satisfied: bool,
) -> dict[str, tuple[str, ...]]:
    """Choose bounded sources deterministically without widening chunk scope."""

    validated_policy = validate_policy(policy)
    missing = tuple(
        sorted(
            {
                _digest(item, "artifact_source_selection_invalid")
                for item in missing_chunk_digests
            }
        )
    )
    if not missing:
        return {}
    missing_bytes = len(missing) * validated_policy["chunk_size_bytes"]
    candidates: list[tuple[int, int, str, set[str]]] = []
    for advertisement in advertisements:
        if advertisement.get("transfer_health") not in {"healthy", "degraded"}:
            continue
        source = _identifier(
            advertisement.get("source_member_id"), "artifact_source_selection_invalid"
        )
        available_raw = advertisement.get("available_chunk_digests")
        if not isinstance(available_raw, list):
            _fail("artifact_source_selection_invalid")
        available = {
            _digest(item, "artifact_source_selection_invalid") for item in available_raw
        } & set(missing)
        if available:
            candidates.append(
                (
                    int(advertisement.get("serving_priority", 0)),
                    int(advertisement.get("max_bytes_per_second", 0)),
                    source,
                    available,
                )
            )
    candidates.sort(key=lambda item: (-item[0], -item[1], item[2]))
    multi_source = (
        len(candidates) >= 2
        and missing_bytes >= validated_policy["multi_source_threshold_bytes"]
        and predicted_improvement_ratio
        >= validated_policy["minimum_predicted_improvement_ratio"]
        and serving_reserve_satisfied
    )
    source_limit = validated_policy["maximum_sources"] if multi_source else 1
    selected: list[tuple[int, int, str, set[str]]] = []
    uncovered = set(missing)
    remaining = list(candidates)
    while remaining and len(selected) < source_limit and uncovered:
        chosen = min(
            remaining,
            key=lambda item: (
                -len(item[3] & uncovered),
                -item[0],
                -item[1],
                item[2],
            ),
        )
        if not (chosen[3] & uncovered):
            break
        selected.append(chosen)
        uncovered -= chosen[3]
        remaining.remove(chosen)
    if multi_source:
        minimum_source_count = min(source_limit, 2)
        while remaining and len(selected) < minimum_source_count:
            chosen = min(
                remaining,
                key=lambda item: (-len(item[3]), -item[0], -item[1], item[2]),
            )
            selected.append(chosen)
            remaining.remove(chosen)
    assignments: dict[str, list[str]] = {source: [] for _, _, source, _ in selected}
    covered: set[str] = set()
    for digest in missing:
        eligible = [item for item in selected if digest in item[3]]
        if not eligible:
            continue
        chosen = min(
            eligible,
            key=lambda item: (len(assignments[item[2]]), -item[0], -item[1], item[2]),
        )
        assignments[chosen[2]].append(digest)
        covered.add(digest)
    return {
        source: tuple(chunks)
        for source, chunks in sorted(assignments.items())
        if chunks
    }


__all__ = [
    "ACQUISITION_PROTOCOL",
    "AVAILABILITY_PROTOCOL",
    "AVAILABILITY_BUNDLE_PROTOCOL",
    "CHUNK_RECEIPT_PROTOCOL",
    "CHUNK_REQUEST_PROTOCOL",
    "GRANT_PROTOCOL",
    "LEDGER_PROTOCOL",
    "MANIFEST_PROTOCOL",
    "POLICY_PROTOCOL",
    "SwarmArtifactContractError",
    "canonical_digest",
    "merkle_proofs",
    "merkle_root",
    "select_transfer_sources",
    "sign_availability",
    "sign_chunk_receipt",
    "sign_chunk_request",
    "sign_grant",
    "source_ref",
    "validate_acquisition_status",
    "validate_acquisition_ledger",
    "validate_availability",
    "validate_availability_bundle",
    "validate_chunk_receipt",
    "validate_chunk_request",
    "validate_grant",
    "validate_policy",
    "validate_stage_pack_manifest",
    "verify_merkle_proof",
]
