"""Stable phone load identity, shared by builder and live qualification.

Callers must verify the signed configured observation against the enrolled key
before using this digest. Promotion is currently unsupported: the native
producer identifies manifest/load generation as controller claims. Raw signed
observations remain usable, but no digest may launder them into load authority.
"""
from collections.abc import Mapping
from .evidence import is_sha256_ref, sha256_document


LOAD_IDENTITY_PROVENANCE = dict(
    applied_fields=['assignment_id', 'stage_id', 'phone_stage_pack_digest',
                    'runtime_backend', 'runtime_build_digest', 'golden_output_digest'],
    controller_claim_fields=['manifest_digest', 'load_generation'],
    limitation='native_manifest_load_generation_not_independently_observed')


class PhoneLoadProvenanceError(ValueError):
    """Authenticated controller claims are not supported load authority."""


def validate_phone_load_provenance(observation: Mapping) -> None:
    """Recognize the closed native descriptor, without promoting its claims."""
    details = observation.get("details")
    if (not isinstance(details, Mapping)
            or details.get("load_identity_provenance") != LOAD_IDENTITY_PROVENANCE):
        raise PhoneLoadProvenanceError("phone_load_provenance_unsupported")


def require_phone_load_provenance(observation: Mapping) -> None:
    """No independently bound phone generation authority is supported yet.

    Seed route generation and signed configure echoes cannot supply that
    missing policy. Keep observations intact, but never publish a load proof.
    """
    validate_phone_load_provenance(observation)
    raise PhoneLoadProvenanceError("phone_load_provenance_unsupported")


def phone_load_proof_digest(observation: Mapping) -> str:
    require_phone_load_provenance(observation)
    details = observation.get("details")
    if not isinstance(details, Mapping):
        raise ValueError("phone_load_proof_invalid")
    identity = {key: observation.get(key) for key in
                ("node_id", "host_id", "process_id", "endpoint_id")}
    if (any(not isinstance(v, str) or not v for v in identity.values())
            or identity["host_id"] != identity["node_id"]
            or not identity["process_id"].startswith("inc-")
            or len(identity["process_id"]) <= 4
            or any(not isinstance(details.get(k), str) or not details[k] for k in ("assignment_id", "stage_id"))
            or any(not is_sha256_ref(details.get(k)) for k in ("manifest_digest", "phone_stage_pack_digest"))
            or type(details.get("load_generation")) is not int or details["load_generation"] <= 0
            or details.get("runtime_mode") != "complete_context_replay"):
        raise ValueError("phone_load_proof_invalid")
    return sha256_document({"protocol": "mycelium.phone_load_identity.v1", **identity,
        **{k: details[k] for k in ("assignment_id", "stage_id", "manifest_digest",
            "phone_stage_pack_digest", "load_generation", "runtime_mode")}})
