# SPDX-License-Identifier: AGPL-3.0-only
"""Stable phone load identity, shared by builder and live qualification.

Two closed native descriptors exist. The v1 producer marked BOTH the manifest
digest and the load generation as controller claims; nothing here promotes it.
The v2 producer counts its own stage loads, refuses a configure whose
generation differs, and signs that count as an applied fact bound to its
process incarnation. The full-model manifest digest is still a controller
claim in v2: it is bound externally, through the seed's current applied
assignment and the packaged model bytes, never by this digest alone.

Callers must verify the signed configured observation against the enrolled key
before using this digest. A digest is an identity, not an authority: the final
qualifier additionally requires a :class:`PhoneLoadAuthority`.
"""
from collections.abc import Mapping
from dataclasses import dataclass, field
from .evidence import is_sha256_ref, sha256_document

PHONE_LOAD_IDENTITY_PROTOCOL = "mycelium.phone_load_identity.v2"
PHONE_RUNTIME_BACKEND = "mobile-rust-qwen2"
PHONE_RUNTIME_MODE = "complete_context_replay"

#: The retired producer. Recognised only so it is refused by name, never promoted.
LOAD_IDENTITY_PROVENANCE_V1 = dict(
    applied_fields=['assignment_id', 'stage_id', 'phone_stage_pack_digest',
                    'runtime_backend', 'runtime_build_digest', 'golden_output_digest'],
    controller_claim_fields=['manifest_digest', 'load_generation'],
    limitation='native_manifest_load_generation_not_independently_observed')

#: The native-observed load. ``load_generation`` is the phone's own stage-load
#: sequence within one process incarnation; ``manifest_digest`` is not observed.
LOAD_IDENTITY_PROVENANCE_V2 = dict(
    applied_fields=['assignment_id', 'stage_id', 'phone_stage_pack_digest',
                    'runtime_backend', 'runtime_build_digest', 'golden_output_digest',
                    'load_generation'],
    controller_claim_fields=['manifest_digest'],
    limitation='native_full_model_manifest_digest_not_independently_observed',
    load_generation_basis='native_stage_load_sequence_within_process_incarnation')

_SNAPSHOT_LOAD_FIELDS = ('assignment_id', 'stage_id', 'phone_stage_pack_digest', 'load_generation')


class PhoneLoadProvenanceError(ValueError):
    """The signed record does not carry the supported native load descriptor."""


def validate_phone_load_provenance(observation: Mapping) -> None:
    """Require the exact v2 descriptor; everything else is unsupported.

    The v1 descriptor, a missing or malformed one, an unknown limitation, an
    altered field list and any extra marker (such as a caller-added
    ``verified`` flag) all fail with the same stable code.
    """
    details = observation.get("details") if isinstance(observation, Mapping) else None
    if (not isinstance(details, Mapping)
            or details.get("load_identity_provenance") != LOAD_IDENTITY_PROVENANCE_V2):
        raise PhoneLoadProvenanceError("phone_load_provenance_unsupported")


def _positive_int(value) -> bool:
    return type(value) is int and value > 0


def phone_load_proof_digest(observation: Mapping) -> str:
    """``mycelium.phone_load_identity.v2`` of one verified configured record.

    Binds the member identity and process incarnation, the applied assignment,
    stage, pack, runtime backend/build/golden, the native load generation, the
    runtime mode, the claimed manifest digest and the provenance semantics.
    Per-session fields (run, clock, peer generation) are deliberately absent,
    so the same applied load hashes the same at preparation and at route open.
    """
    validate_phone_load_provenance(observation)
    details = observation["details"]
    identity = {key: observation.get(key) for key in
                ("node_id", "host_id", "process_id", "endpoint_id")}
    if (any(not isinstance(v, str) or not v for v in identity.values())
            or identity["host_id"] != identity["node_id"]
            or not identity["process_id"].startswith("inc-")
            or len(identity["process_id"]) <= 4
            or any(not isinstance(details.get(k), str) or not details[k] for k in ("assignment_id", "stage_id"))
            or any(not is_sha256_ref(details.get(k)) for k in (
                "manifest_digest", "phone_stage_pack_digest", "runtime_build_digest", "golden_output_digest"))
            or details.get("runtime_backend") != PHONE_RUNTIME_BACKEND
            or not _positive_int(details.get("load_generation"))
            or details.get("runtime_mode") != PHONE_RUNTIME_MODE):
        raise ValueError("phone_load_proof_invalid")
    return sha256_document({"protocol": PHONE_LOAD_IDENTITY_PROTOCOL, **identity,
        **{k: details[k] for k in ("assignment_id", "stage_id", "phone_stage_pack_digest",
            "runtime_backend", "runtime_build_digest", "golden_output_digest",
            "load_generation", "runtime_mode", "manifest_digest")},
        "load_identity_provenance": LOAD_IDENTITY_PROVENANCE_V2})


def require_current_phone_load(configured: Mapping, snapshot: Mapping) -> None:
    """The signed snapshot must still name the load the configure was signed for.

    A reload or a replaced assignment advances the phone's own counter; the
    snapshot then reports another applied load (or none) and the configured
    record is stale, whatever its signature says.
    """
    details, current = configured.get("details"), snapshot.get("details")
    if not isinstance(details, Mapping) or not isinstance(current, Mapping):
        raise ValueError("phone_load_stale")
    applied = current.get("load_identity")
    if (not isinstance(applied, Mapping) or set(applied) != set(_SNAPSHOT_LOAD_FIELDS)
            or not _positive_int(applied.get("load_generation"))
            or any(type(applied[k]) is not type(details.get(k)) or applied[k] != details.get(k)
                   for k in _SNAPSHOT_LOAD_FIELDS)
            or type(current.get("configured_load_generation")) is not int
            or current["configured_load_generation"] != details["load_generation"]
            or any(configured.get(k) != snapshot.get(k)
                   for k in ("node_id", "host_id", "process_id", "endpoint_id"))):
        raise ValueError("phone_load_stale")


_AUTHORITY_FIELDS = (
    "node_id", "endpoint_id", "process_id", "member_key_digest", "membership_generation",
    "seed_key_digest", "assignment_id", "assignment_digest", "stage_id", "pack_digest",
    "manifest_digest", "load_generation", "runtime_backend", "runtime_build_digest",
    "golden_output_digest", "load_proof_digest", "checked_at")
# Process-local and never serialized: an authority does not survive a pickle,
# a JSON round trip or another process.
_AUTHORITY_KEY = __import__("secrets").token_bytes(32)


def _authority_seal(fields: Mapping) -> str:
    import hashlib
    import hmac
    from .evidence import canonical_json_bytes
    document = {key: fields[key] for key in _AUTHORITY_FIELDS}
    return hmac.new(_AUTHORITY_KEY, canonical_json_bytes(document), hashlib.sha256).hexdigest()


@dataclass(frozen=True, slots=True)
class PhoneLoadAuthority:
    """Current seed and packaged authority for one phone load, already checked.

    Issued only by ``mycelium_seed.phone_preparation.current_load_authority``
    from the pinned seed's coherent member/applied-route read and the verified
    packaged proof. It is trusted consumer context, produced outside the
    attestation it is compared with; a mapping, a flag or a copy of the plan's
    configure is not one.

    The seal binds the content: it is a keyed digest over every field, so an
    instance built directly, copied with ``dataclasses.replace`` and changed,
    or assembled field by field does not verify. The qualifier calls
    :meth:`verify_seal` before it reads anything.

    Limit, stated plainly: this stops accidents and casual construction, not
    code running in the same process. Such code can read this module's key or
    call its sealing function, exactly as it could replace the qualifier. The
    qualifier already trusts its caller for the whole binding (the member key
    digest included); the seal does not change that trust boundary.
    """
    node_id: str
    endpoint_id: str
    process_id: str
    member_key_digest: str
    membership_generation: int
    seed_key_digest: str
    assignment_id: str
    assignment_digest: str
    stage_id: str
    pack_digest: str
    manifest_digest: str
    load_generation: int
    runtime_backend: str
    runtime_build_digest: str
    golden_output_digest: str
    load_proof_digest: str
    checked_at: float
    _seal: str | None = field(default=None, repr=False, compare=False)

    def __post_init__(self):
        self.verify_seal()

    def verify_seal(self) -> None:
        """Raise unless the seal is this module's digest of exactly these fields."""
        import hmac
        try:
            sealed = isinstance(self._seal, str) and hmac.compare_digest(
                self._seal, _authority_seal({key: getattr(self, key) for key in _AUTHORITY_FIELDS}))
        except (AttributeError, TypeError, ValueError):
            sealed = False
        if not sealed:
            raise TypeError("phone_load_authority_unsealed")

    def expected_details(self) -> dict:
        return dict(assignment_id=self.assignment_id, stage_id=self.stage_id,
            phone_stage_pack_digest=self.pack_digest, manifest_digest=self.manifest_digest,
            load_generation=self.load_generation, runtime_backend=self.runtime_backend,
            runtime_build_digest=self.runtime_build_digest,
            golden_output_digest=self.golden_output_digest, runtime_mode=PHONE_RUNTIME_MODE)


def _seal_phone_load_authority(**fields) -> PhoneLoadAuthority:
    """Module-private constructor for the one validating issuer."""
    return PhoneLoadAuthority(**fields, _seal=_authority_seal(fields))
