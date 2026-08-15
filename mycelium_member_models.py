"""Signed, expiring, privacy-reduced model discovery from current swarm members."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
import copy
import hashlib
import json
import re
from typing import Any

from mycelium_qualification.evidence import canonical_json_bytes
from mycelium_qualification.signing import (
    Ed25519EvidenceSigner,
    SIGNATURE_FIELDS,
    build_ed25519_verifier,
)


MEMBER_MODEL_INVENTORY_PROTOCOL = "mycelium.member_model_inventory.v1"
MEMBER_MODEL_INVENTORY_STATEMENT_PROTOCOL = (
    "mycelium.member_model_inventory_statement.v1"
)
_ENVELOPE_FIELDS = frozenset(
    {"protocol", "statement", "signature", "verification_key"}
)
_STATEMENT_FIELDS = frozenset(
    {
        "protocol",
        "inventory_id",
        "member_id",
        "membership_generation",
        "observed_at_unix_ms",
        "valid_until_unix_ms",
        "entries",
    }
)
_ENTRY_FIELDS = frozenset(
    {
        "model_id",
        "revision",
        "artifact_digest",
        "state",
        "architecture",
        "adapter_id",
        "checkpoint_format",
        "quantization",
        "num_layers",
        "weight_bytes",
        "exact_tensor_accounting",
        "required_file_count",
        "present_file_count",
        "reasons",
        "serving_representations",
    }
)
_REPRESENTATION_FIELDS = frozenset(
    {
        "quantization",
        "quantizer",
        "runtime_dtype",
        "resident_weight_bytes",
        "load_peak_weight_bytes",
        "preparation_required",
        "representation_digest",
    }
)
_AUTHORITY_FIELDS = frozenset(
    {"member_id", "membership_generation", "verification_key"}
)
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_REVISION = re.compile(r"[0-9a-f]{40}\Z")
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}\Z")
_REASON = re.compile(r"[a-z][a-z0-9_:>.=-]{0,127}\Z")
_MAX_ENTRIES = 256
_MAX_REASONS = 32
_MAX_REPRESENTATIONS = 8
_MAX_TTL_MS = 86_400_000
_MAX_FUTURE_SKEW_MS = 5_000
_MAX_SAFE_INTEGER = 2**53 - 1


class MemberModelInventoryError(ValueError):
    """Stable validation error for private member inventory material."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _fail(condition: bool, code: str) -> None:
    if not condition:
        raise MemberModelInventoryError(code)


def _integer(value: object, *, minimum: int = 0) -> bool:
    return (
        type(value) is int
        and minimum <= value <= _MAX_SAFE_INTEGER
    )


def _text(value: object, pattern: re.Pattern[str]) -> bool:
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def _safe_reason(value: object) -> str:
    text = str(value).strip().lower()
    normalized = re.sub(r"[^a-z0-9_:>.=-]+", "_", text).strip("_")
    if not normalized or not normalized[0].islower():
        normalized = f"member_{normalized or 'unspecified'}"
    return normalized[:128].rstrip("_") or "member_unspecified"


def _entry(value: object) -> dict[str, Any]:
    _fail(isinstance(value, Mapping) and set(value) == _ENTRY_FIELDS, "member_model_inventory_entry_invalid")
    item = dict(value)
    _fail(_text(item["model_id"], _IDENTIFIER), "member_model_inventory_entry_invalid")
    _fail(_text(item["revision"], _REVISION), "member_model_inventory_entry_invalid")
    _fail(_text(item["artifact_digest"], _DIGEST), "member_model_inventory_entry_invalid")
    _fail(item["state"] in {"incomplete", "discovered", "compatible"}, "member_model_inventory_entry_invalid")
    for field in ("architecture", "checkpoint_format", "quantization"):
        _fail(_text(item[field], _IDENTIFIER), "member_model_inventory_entry_invalid")
    _fail(item["adapter_id"] is None or _text(item["adapter_id"], _IDENTIFIER), "member_model_inventory_entry_invalid")
    _fail(item["num_layers"] is None or _integer(item["num_layers"], minimum=1), "member_model_inventory_entry_invalid")
    for field in ("weight_bytes", "required_file_count", "present_file_count"):
        _fail(_integer(item[field]), "member_model_inventory_entry_invalid")
    _fail(item["present_file_count"] <= item["required_file_count"], "member_model_inventory_entry_invalid")
    _fail(type(item["exact_tensor_accounting"]) is bool, "member_model_inventory_entry_invalid")
    reasons = item["reasons"]
    _fail(
        isinstance(reasons, list)
        and len(reasons) <= _MAX_REASONS
        and reasons == sorted(set(reasons))
        and all(_text(reason, _REASON) for reason in reasons),
        "member_model_inventory_entry_invalid",
    )
    representations = item["serving_representations"]
    _fail(
        isinstance(representations, list)
        and len(representations) <= _MAX_REPRESENTATIONS,
        "member_model_inventory_entry_invalid",
    )
    seen: set[str] = set()
    for representation in representations:
        _fail(
            isinstance(representation, Mapping)
            and set(representation) == _REPRESENTATION_FIELDS,
            "member_model_inventory_representation_invalid",
        )
        record = dict(representation)
        for field in ("quantization", "quantizer", "runtime_dtype"):
            _fail(_text(record[field], _IDENTIFIER), "member_model_inventory_representation_invalid")
        for field in ("resident_weight_bytes", "load_peak_weight_bytes"):
            _fail(_integer(record[field]), "member_model_inventory_representation_invalid")
        _fail(type(record["preparation_required"]) is bool, "member_model_inventory_representation_invalid")
        digest = record["representation_digest"]
        _fail(_text(digest, _DIGEST) and digest not in seen, "member_model_inventory_representation_invalid")
        seen.add(digest)
    _fail(
        representations
        == sorted(representations, key=lambda record: record["representation_digest"]),
        "member_model_inventory_entry_invalid",
    )
    return json.loads(json.dumps(item, allow_nan=False))


def inventory_entry_from_catalog_projection(
    projection: Mapping[str, Any],
) -> dict[str, Any]:
    """Reduce one local catalog projection to the signed discovery contract."""

    candidate = {
        "model_id": projection.get("model_id"),
        "revision": projection.get("revision"),
        "artifact_digest": projection.get("artifact_digest"),
        "state": projection.get("state"),
        "architecture": projection.get("architecture"),
        "adapter_id": projection.get("adapter_id"),
        "checkpoint_format": projection.get("checkpoint_format"),
        "quantization": projection.get("quantization"),
        "num_layers": projection.get("num_layers"),
        "weight_bytes": projection.get("weight_bytes"),
        "exact_tensor_accounting": projection.get("exact_tensor_accounting"),
        "required_file_count": projection.get("required_file_count"),
        "present_file_count": projection.get("present_file_count"),
        "reasons": sorted(
            {
                _safe_reason(reason)
                for reason in projection.get("reasons", [])
            }
        ),
        "serving_representations": sorted(
            copy.deepcopy(projection.get("serving_representations", [])),
            key=lambda record: record.get("representation_digest", ""),
        ),
    }
    return _entry(candidate)


def create_member_model_inventory(
    *,
    member_id: str,
    membership_generation: int,
    entries: Iterable[Mapping[str, Any]],
    observed_at_unix_ms: int,
    valid_until_unix_ms: int,
    signer: Ed25519EvidenceSigner,
) -> dict[str, Any]:
    """Create a deterministic, signed member inventory without moving model bytes."""

    checked = sorted(
        (_entry(item) for item in entries),
        key=lambda item: (item["model_id"], item["revision"]),
    )
    _fail(len(checked) <= _MAX_ENTRIES, "member_model_inventory_limit_exceeded")
    _fail(
        len({(item["model_id"], item["revision"]) for item in checked})
        == len(checked),
        "member_model_inventory_duplicate_identity",
    )
    statement = {
        "protocol": MEMBER_MODEL_INVENTORY_STATEMENT_PROTOCOL,
        "inventory_id": "inventory-"
        + hashlib.sha256(
            canonical_json_bytes(
                {
                    "member_id": member_id,
                    "membership_generation": membership_generation,
                    "observed_at_unix_ms": observed_at_unix_ms,
                    "entries": checked,
                }
            )
        ).hexdigest()[:32],
        "member_id": member_id,
        "membership_generation": membership_generation,
        "observed_at_unix_ms": observed_at_unix_ms,
        "valid_until_unix_ms": valid_until_unix_ms,
        "entries": checked,
    }
    _validate_statement(statement)
    return {
        "protocol": MEMBER_MODEL_INVENTORY_PROTOCOL,
        "statement": statement,
        "signature": signer.sign(statement),
        "verification_key": signer.public_key_record(),
    }


def _validate_statement(value: object) -> dict[str, Any]:
    _fail(isinstance(value, Mapping) and set(value) == _STATEMENT_FIELDS, "member_model_inventory_statement_invalid")
    statement = dict(value)
    _fail(statement["protocol"] == MEMBER_MODEL_INVENTORY_STATEMENT_PROTOCOL, "member_model_inventory_statement_invalid")
    _fail(_text(statement["inventory_id"], _IDENTIFIER), "member_model_inventory_statement_invalid")
    _fail(_text(statement["member_id"], _IDENTIFIER), "member_model_inventory_statement_invalid")
    _fail(_integer(statement["membership_generation"], minimum=1), "member_model_inventory_generation_invalid")
    _fail(_integer(statement["observed_at_unix_ms"], minimum=1), "member_model_inventory_time_invalid")
    _fail(_integer(statement["valid_until_unix_ms"], minimum=1), "member_model_inventory_time_invalid")
    _fail(
        statement["observed_at_unix_ms"] < statement["valid_until_unix_ms"]
        and statement["valid_until_unix_ms"] - statement["observed_at_unix_ms"]
        <= _MAX_TTL_MS,
        "member_model_inventory_time_invalid",
    )
    entries = statement["entries"]
    _fail(isinstance(entries, list) and len(entries) <= _MAX_ENTRIES, "member_model_inventory_limit_exceeded")
    checked = [_entry(item) for item in entries]
    _fail(
        checked == sorted(checked, key=lambda item: (item["model_id"], item["revision"])),
        "member_model_inventory_entries_unsorted",
    )
    _fail(
        len({(item["model_id"], item["revision"]) for item in checked})
        == len(checked),
        "member_model_inventory_duplicate_identity",
    )
    statement["entries"] = checked
    return json.loads(json.dumps(statement, allow_nan=False))


def validate_member_model_inventory(
    envelope: Mapping[str, Any],
    *,
    authority: Mapping[str, Any],
    now_unix_ms: int,
) -> dict[str, Any]:
    """Validate signature, current member key/generation, and bounded currency."""

    _fail(isinstance(authority, Mapping) and set(authority) == _AUTHORITY_FIELDS, "member_model_inventory_authority_invalid")
    _fail(isinstance(envelope, Mapping) and set(envelope) == _ENVELOPE_FIELDS, "member_model_inventory_envelope_invalid")
    _fail(envelope.get("protocol") == MEMBER_MODEL_INVENTORY_PROTOCOL, "member_model_inventory_envelope_invalid")
    statement = _validate_statement(envelope.get("statement"))
    _fail(statement["member_id"] == authority["member_id"], "member_model_inventory_member_mismatch")
    _fail(
        statement["membership_generation"] == authority["membership_generation"],
        "member_model_inventory_generation_mismatch",
    )
    _fail(_integer(now_unix_ms, minimum=1), "member_model_inventory_clock_invalid")
    _fail(
        statement["observed_at_unix_ms"] <= now_unix_ms + _MAX_FUTURE_SKEW_MS
        and now_unix_ms < statement["valid_until_unix_ms"],
        "member_model_inventory_stale",
    )
    key = envelope.get("verification_key")
    signature = envelope.get("signature")
    expected_key = authority.get("verification_key")
    _fail(isinstance(key, Mapping) and key == expected_key, "member_model_inventory_key_mismatch")
    _fail(isinstance(signature, Mapping) and set(signature) == SIGNATURE_FIELDS, "member_model_inventory_signature_invalid")
    try:
        verifier = build_ed25519_verifier([key])
    except ValueError as exc:
        raise MemberModelInventoryError("member_model_inventory_authority_invalid") from exc
    _fail(
        verifier(canonical_json_bytes(statement), dict(signature)),
        "member_model_inventory_signature_invalid",
    )
    return {
        "protocol": MEMBER_MODEL_INVENTORY_PROTOCOL,
        "statement": statement,
        "signature": copy.deepcopy(dict(signature)),
        "verification_key": copy.deepcopy(dict(key)),
    }


def validate_member_authorities(
    values: Sequence[Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for value in values:
        _fail(isinstance(value, Mapping) and set(value) == _AUTHORITY_FIELDS, "member_model_inventory_authority_invalid")
        member_id = value.get("member_id")
        generation = value.get("membership_generation")
        key = value.get("verification_key")
        _fail(_text(member_id, _IDENTIFIER) and member_id not in result, "member_model_inventory_authority_invalid")
        _fail(_integer(generation, minimum=1) and isinstance(key, Mapping), "member_model_inventory_authority_invalid")
        try:
            build_ed25519_verifier([key])
        except ValueError as exc:
            raise MemberModelInventoryError("member_model_inventory_authority_invalid") from exc
        result[str(member_id)] = copy.deepcopy(dict(value))
    return result


def _representation_identity(entry: Mapping[str, Any]) -> tuple[str, ...]:
    return tuple(
        str(record["representation_digest"])
        for record in entry["serving_representations"]
    )


def _remote_catalog_projection(
    entry: Mapping[str, Any],
    *,
    member_count: int,
) -> dict[str, Any]:
    reasons = sorted(
        {
            *(str(reason) for reason in entry["reasons"]),
            "owner_metadata_reconciliation_required",
        }
    )[:_MAX_REASONS]
    return {
        "model_id": entry["model_id"],
        "revision": entry["revision"],
        "state": "discovered",
        "architecture": entry["architecture"],
        "adapter_id": entry["adapter_id"],
        "checkpoint_format": entry["checkpoint_format"],
        "quantization": entry["quantization"],
        "num_layers": entry["num_layers"],
        "weight_bytes": entry["weight_bytes"],
        "exact_tensor_accounting": entry["exact_tensor_accounting"],
        "required_file_count": entry["required_file_count"],
        "present_file_count": entry["present_file_count"],
        "serving_representations": copy.deepcopy(entry["serving_representations"]),
        "reasons": reasons,
        "artifact_digest": entry["artifact_digest"],
        "route_ready": False,
        "qualification_evaluated": False,
        "discovery_scope": ["member_inventory"],
        "discovered_member_count": member_count,
        "metadata_reconciled": False,
        "discovery_blockers": ["owner_metadata_reconciliation_required"],
    }


def reconcile_member_model_catalog(
    *,
    local_entries: Sequence[Any],
    inventories: Sequence[Mapping[str, Any]],
    authorities: Sequence[Mapping[str, Any]],
    now_unix_ms: int,
    generation: int,
) -> dict[str, object]:
    """Merge verified member discovery without making remote bytes selectable."""

    from mycelium_model_catalog import catalog_document

    authority_by_member = validate_member_authorities(authorities)
    candidates_by_member: dict[str, list[Mapping[str, Any]]] = {}
    invalid_without_member = 0
    for inventory in inventories:
        statement = inventory.get("statement") if isinstance(inventory, Mapping) else None
        member_id = statement.get("member_id") if isinstance(statement, Mapping) else None
        if not isinstance(member_id, str):
            invalid_without_member += 1
            continue
        candidates_by_member.setdefault(member_id, []).append(inventory)

    accepted: dict[str, dict[str, Any]] = {}
    blockers: set[str] = set()
    rejected = invalid_without_member
    if invalid_without_member:
        blockers.add("member_inventory_invalid")
    for member_id, candidates in sorted(candidates_by_member.items()):
        authority = authority_by_member.get(member_id)
        if authority is None:
            rejected += 1
            blockers.add("member_inventory_unknown_member")
            continue
        if len(candidates) != 1:
            rejected += 1
            blockers.add("member_inventory_duplicate_member")
            continue
        try:
            accepted[member_id] = validate_member_model_inventory(
                candidates[0],
                authority=authority,
                now_unix_ms=now_unix_ms,
            )
        except MemberModelInventoryError as exc:
            rejected += 1
            blockers.add(exc.code)

    remote_by_identity: dict[tuple[str, str], list[tuple[str, dict[str, Any]]]] = {}
    for member_id, inventory in accepted.items():
        for entry in inventory["statement"]["entries"]:
            identity = (str(entry["model_id"]), str(entry["revision"]))
            remote_by_identity.setdefault(identity, []).append((member_id, entry))

    local_by_identity = {
        (str(entry.model_id), str(entry.revision)): entry for entry in local_entries
    }
    local_metadata: dict[tuple[str, str], dict[str, object]] = {}
    discovered: list[dict[str, Any]] = []
    for identity, local in local_by_identity.items():
        peers = remote_by_identity.pop(identity, [])
        local_projection = local.projection()
        conflicts = [
            record
            for _member, record in peers
            if record["artifact_digest"] != local_projection["artifact_digest"]
            or _representation_identity(record)
            != _representation_identity(local_projection)
        ]
        entry_blockers = ["member_inventory_identity_conflict"] if conflicts else []
        if conflicts:
            blockers.add("member_inventory_identity_conflict")
        local_metadata[identity] = {
            "discovery_scope": ["coordinator"]
            + (["member_inventory"] if peers else []),
            "discovered_member_count": len(peers),
            "metadata_reconciled": not conflicts,
            "discovery_blockers": entry_blockers,
        }

    for identity, peers in sorted(remote_by_identity.items()):
        first = peers[0][1]
        if any(
            record["artifact_digest"] != first["artifact_digest"]
            or _representation_identity(record) != _representation_identity(first)
            for _member, record in peers[1:]
        ):
            blockers.add("member_inventory_identity_conflict")
            continue
        discovered.append(
            _remote_catalog_projection(first, member_count=len(peers))
        )

    discovery = {
        "scope": "coordinator_and_members" if accepted else "coordinator_only",
        "accepted_member_count": len(accepted),
        "rejected_member_count": rejected,
        "blockers": sorted(blockers),
    }
    return catalog_document(
        local_entries,
        generation=generation,
        discovered_entries=discovered,
        discovery=discovery,
        entry_discovery=local_metadata,
    )


__all__ = [
    "MEMBER_MODEL_INVENTORY_PROTOCOL",
    "MEMBER_MODEL_INVENTORY_STATEMENT_PROTOCOL",
    "MemberModelInventoryError",
    "create_member_model_inventory",
    "inventory_entry_from_catalog_projection",
    "reconcile_member_model_catalog",
    "validate_member_authorities",
    "validate_member_model_inventory",
]
