"""Stable phone load identity, shared by builder and live qualification.

Callers must verify the signed configured observation against the enrolled key
before using this digest. This is not a Python/NumPy substitute load proof.
"""
from collections.abc import Mapping
from .evidence import is_sha256_ref, sha256_document


def phone_load_proof_digest(observation: Mapping) -> str:
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
