# SPDX-License-Identifier: AGPL-3.0-only
"""Android/Pixel target-side mobile onboarding and invite flow for A12.

SOURCE TRANCH.  This module is the Android half of the A12 generic
mobile-activation source: the onboarding + invitation flow the signed native
application runs on the device, sitting behind the existing mobile scaffolding
(``mycelium_mobile``, ``tests/a12_acceptance/inventory.v1.json`` and the A12
rung machine source ``mycelium_mobile/a12_activation.py``).

The Android target speaks the SAME wire protocols the committed macOS slice and
the A13 Linux tranche froze:

    mycelium.pairing_request.v1
    mycelium.encrypted_invitation_handoff.v1

so a request and a handoff mean the same thing on every platform.  What differs
is the peer class (``android_native_iroh``) and the carrier: an Android request
may only travel over the ordinary product path
(``signed_native_application``).  Termux, ADB and SSH are the inventory's
excluded development paths, so a request that claims one of them as its carrier
is refused with ``android_onboarding_carrier_excluded`` — the deviation the A13
Linux tranche left open is closed here by construction instead of by comment.

Behaviour implemented here (all of it in-process, none of it on a device):

* a closed, target-signed pairing request and its recipient-only X25519 key;
* owner-side approval that encrypts one complete A8 invite bundle to the
  device's ephemeral recipient key (X25519 + HKDF-SHA256 + ChaCha20-Poly1305);
* recipient-side decryption with every request, invite, quota, pin and
  generation binding re-verified;
* exactly one atomic local join into the application-private state root, with a
  bounded consumed-nonce ledger, strictly fenced member generations and
  ``route_ready=false``;
* a bounded closed consent quota.

NOT implemented, and not implied by any passing test: enrollment on a real
device, a signed application, native evidence, capability eligibility, an
artifact, an operation qualification, a physical gate, and any Android
two-family claim.  ``route_ready`` never becomes true here and no rung is
authored: joining is not eligibility.  No private key material is ever written
into a record, a return value or a log.
"""

from __future__ import annotations

import base64
from collections.abc import Mapping
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey,
    X25519PublicKey,
)
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from mycelium_invite import INVITE_BUNDLE_PROTOCOL, InviteError, verify_invite_bundle
from mycelium_qualification.evidence import canonical_json_bytes
from mycelium_qualification.signing import (
    Ed25519EvidenceSigner,
    build_ed25519_verifier,
)

from .a12_activation import (
    EXCLUDED_NORMAL_PRODUCT_PATHS,
    require_route_ready_is_false,
    require_undivided_product_path,
)

PAIRING_REQUEST_PROTOCOL = "mycelium.pairing_request.v1"
ENCRYPTED_HANDOFF_PROTOCOL = "mycelium.encrypted_invitation_handoff.v1"
_HANDOFF_PAYLOAD_PROTOCOL = "mycelium.encrypted_invitation_payload.v1"
JOIN_PROTOCOL = "mycelium.a12_android_local_member.v1"

#: Registered native Android profiles. The producer default remains reference;
#: accepting an explicitly signed Qwen profile never relabels a legacy request.
ANDROID_PEER_CLASS = "android_native_iroh"
ANDROID_QWEN2_PEER_CLASS = "android_native_qwen2_iroh"
SUPPORTED_PEER_CLASSES: frozenset[str] = frozenset({ANDROID_PEER_CLASS, ANDROID_QWEN2_PEER_CLASS})

#: The only carrier an Android onboarding request may travel over: the ordinary
#: signed native application path of inventory decision
#: ``native_product_path_only``.
NATIVE_CARRIER = "signed_native_application"
SUPPORTED_CARRIERS: tuple[str, ...] = (NATIVE_CARRIER,)

ONBOARDING_STEPS: tuple[str, ...] = (
    "pairing_requested",
    "invitation_approved",
    "invitation_decrypted",
    "identity_verified",
    "member_joined",
)

MEMBER_STATE_NAME = "android-member.json"
REPLAY_LEDGER_LIMIT = 64

_MEMBER_AUTHORITY = "a12_android_onboarding_source"
_MEMBER_GRANT: tuple[str, ...] = ()
_MEMBER_RUNG_EFFECT = "none"

_REQUEST_TTL_MAX = 600
_MAX_HANDOFF_BYTES = 64 * 1024
_MAX_QUOTA_BYTES = 1024**4
_MAX_THERMAL_CEILING_C = 80
_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

_REQUEST_FIELDS = frozenset(
    {
        "protocol",
        "device_ref",
        "identity_endpoint_id",
        "identity_key_digest",
        "identity_verification_key",
        "peer_class",
        "carrier",
        "invite_protocol",
        "recipient_encryption_key",
        "pairing_nonce",
        "issued_at",
        "expires_at",
        "signature",
    }
)
_HANDOFF_FIELDS = frozenset(
    {
        "protocol",
        "request_digest",
        "recipient_key_digest",
        "sender_ephemeral_key",
        "kdf_salt",
        "aead_nonce",
        "expires_at",
        "ciphertext",
    }
)
_PAYLOAD_FIELDS = frozenset(
    {
        "protocol",
        "request_digest",
        "device_ref",
        "identity_key_digest",
        "pairing_nonce",
        "peer_class",
        "carrier",
        "expires_at",
        "seed_key_digest",
        "quota",
        "invite_bundle",
    }
)
_QUOTA_FIELDS = frozenset(
    {
        "storage_bytes",
        "network_bytes_per_day",
        "background_allowed",
        "battery_floor_percent",
        "thermal_ceiling_c",
    }
)
_MEMBER_FIELDS = frozenset(
    {
        "protocol",
        "peer_class",
        "device_ref",
        "identity_key_digest",
        "seed_key_digest",
        "member_generation",
        "carrier",
        "joined_at",
        "route_ready",
        "consumed_pairing_nonces",
    }
)


class AndroidOnboardingError(RuntimeError):
    """Fail-closed onboarding error carrying a stable code and no secret."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _finite(value: Any) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
    ):
        raise AndroidOnboardingError("android_onboarding_time_invalid")
    return float(value)


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64decode(value: Any, *, length: int | None = None) -> bytes:
    if not isinstance(value, str) or not value or "=" in value:
        raise AndroidOnboardingError("android_onboarding_handoff_invalid")
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except Exception as exc:  # noqa: BLE001 - crypto boundary
        raise AndroidOnboardingError("android_onboarding_handoff_invalid") from exc
    if _b64encode(raw) != value or (length is not None and len(raw) != length):
        raise AndroidOnboardingError("android_onboarding_handoff_invalid")
    return raw


def _digest(document: Mapping[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(canonical_json_bytes(dict(document))).hexdigest()


def validate_android_quota(quota: object) -> dict[str, Any]:
    """Bounded, closed consent quota carried inside the encrypted invitation.

    Missing or denied consent is a refusal, never zero or an inferred default
    (specification section 8: both sides may only narrow each other's bound).
    """

    if not isinstance(quota, Mapping) or set(quota) != _QUOTA_FIELDS:
        raise AndroidOnboardingError("android_onboarding_quota_invalid")
    result = dict(quota)
    for field in ("storage_bytes", "network_bytes_per_day"):
        value = result[field]
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or value < 0
            or value > _MAX_QUOTA_BYTES
        ):
            raise AndroidOnboardingError("android_onboarding_quota_invalid")
    if type(result["background_allowed"]) is not bool:
        raise AndroidOnboardingError("android_onboarding_quota_invalid")
    floor = result["battery_floor_percent"]
    if isinstance(floor, bool) or not isinstance(floor, int) or not 0 <= floor <= 100:
        raise AndroidOnboardingError("android_onboarding_quota_invalid")
    ceiling = result["thermal_ceiling_c"]
    if (
        isinstance(ceiling, bool)
        or not isinstance(ceiling, (int, float))
        or not math.isfinite(float(ceiling))
        or not 0 < float(ceiling) <= _MAX_THERMAL_CEILING_C
    ):
        raise AndroidOnboardingError("android_onboarding_quota_invalid")
    return result


def _require_carrier(carrier: object) -> str:
    if carrier in EXCLUDED_NORMAL_PRODUCT_PATHS:
        raise AndroidOnboardingError("android_onboarding_carrier_excluded")
    if carrier not in SUPPORTED_CARRIERS:
        raise AndroidOnboardingError("android_onboarding_carrier_invalid")
    return str(carrier)


def new_android_pairing_request(
    *,
    signer: Ed25519EvidenceSigner,
    device_ref: str,
    now: float,
    ttl_seconds: int = 300,
    nonce: str | None = None,
) -> tuple[dict[str, Any], bytes]:
    """Create one target-signed request and its recipient-only X25519 key."""

    if not isinstance(signer, Ed25519EvidenceSigner):
        raise AndroidOnboardingError("android_onboarding_identity_invalid")
    if not isinstance(device_ref, str) or _SEGMENT_RE.fullmatch(device_ref) is None:
        raise AndroidOnboardingError("android_onboarding_request_invalid")
    if (
        isinstance(ttl_seconds, bool)
        or not isinstance(ttl_seconds, int)
        or not 1 <= ttl_seconds <= _REQUEST_TTL_MAX
    ):
        raise AndroidOnboardingError("android_onboarding_request_ttl_invalid")
    issued_at = _finite(now)
    pairing_nonce = "pair-" + _b64encode(os.urandom(24)) if nonce is None else nonce
    if not isinstance(pairing_nonce, str) or _SEGMENT_RE.fullmatch(pairing_nonce) is None:
        raise AndroidOnboardingError("android_onboarding_request_invalid")

    private = X25519PrivateKey.generate()
    statement = {
        "protocol": PAIRING_REQUEST_PROTOCOL,
        "device_ref": device_ref,
        "identity_endpoint_id": signer.endpoint_id,
        "identity_key_digest": signer.verification_key_digest,
        "identity_verification_key": signer.public_key_record(),
        "peer_class": ANDROID_PEER_CLASS,
        "carrier": NATIVE_CARRIER,
        "invite_protocol": INVITE_BUNDLE_PROTOCOL,
        "recipient_encryption_key": _b64encode(private.public_key().public_bytes_raw()),
        "pairing_nonce": pairing_nonce,
        "issued_at": issued_at,
        "expires_at": issued_at + ttl_seconds,
    }
    return {**statement, "signature": signer.sign(statement)}, private.private_bytes_raw()


def verify_android_pairing_request(request: object, *, now: float) -> dict[str, Any]:
    """Verify the closed request, its permanent identity signature and its life."""

    if not isinstance(request, Mapping) or set(request) != _REQUEST_FIELDS:
        raise AndroidOnboardingError("android_onboarding_request_invalid")
    value = dict(request)
    if value.get("protocol") != PAIRING_REQUEST_PROTOCOL:
        raise AndroidOnboardingError("android_onboarding_request_invalid")
    for field in ("device_ref", "identity_endpoint_id", "pairing_nonce"):
        if (
            not isinstance(value.get(field), str)
            or _SEGMENT_RE.fullmatch(value[field]) is None
        ):
            raise AndroidOnboardingError("android_onboarding_request_invalid")
    if (
        not isinstance(value.get("identity_key_digest"), str)
        or _DIGEST_RE.fullmatch(value["identity_key_digest"]) is None
        or not isinstance(value.get("identity_verification_key"), Mapping)
        or value["identity_verification_key"].get("verification_key_digest")
        != value["identity_key_digest"]
    ):
        raise AndroidOnboardingError("android_onboarding_request_invalid")
    _b64decode(value.get("recipient_encryption_key"), length=32)

    issued = _finite(value.get("issued_at"))
    expires = _finite(value.get("expires_at"))
    instant = _finite(now)
    if expires <= issued or expires - issued > _REQUEST_TTL_MAX:
        raise AndroidOnboardingError("android_onboarding_request_invalid")

    signature = value.get("signature")
    statement = {key: item for key, item in value.items() if key != "signature"}
    try:
        verify = build_ed25519_verifier([dict(value["identity_verification_key"])])
    except Exception as exc:  # noqa: BLE001 - external record
        raise AndroidOnboardingError("android_onboarding_request_signature_invalid") from exc
    if (
        not isinstance(signature, Mapping)
        or signature.get("signer_endpoint_id") != value["identity_endpoint_id"]
        or not verify(canonical_json_bytes(statement), dict(signature))
    ):
        raise AndroidOnboardingError("android_onboarding_request_signature_invalid")

    if (
        not isinstance(value.get("peer_class"), str)
        or value["peer_class"] not in SUPPORTED_PEER_CLASSES
    ):
        raise AndroidOnboardingError("android_onboarding_peer_class_unsupported")
    if value.get("invite_protocol") != INVITE_BUNDLE_PROTOCOL:
        raise AndroidOnboardingError("android_onboarding_request_invalid")
    _require_carrier(value.get("carrier"))
    if instant > expires:
        raise AndroidOnboardingError("android_onboarding_request_expired")
    return value


def _derive_key(*, shared: bytes, salt: bytes, request_digest: str) -> bytes:
    return HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        info=b"mycelium.a12.pairing.handoff.v1\0" + request_digest.encode("ascii"),
    ).derive(shared)


def approve_android_pairing_request(
    *,
    request: Mapping[str, Any],
    invite_bundle: Mapping[str, Any],
    quota: Mapping[str, Any],
    now: float,
) -> dict[str, Any]:
    """Owner-side approval: encrypt one complete A8 bundle to the Android target.

    The invitation is bound to the request's pairing nonce and may not outlive
    it.  The owner's quota is validated before anything is encrypted, so a
    refused consent never reaches the device.
    """

    instant = _finite(now)
    verified_request = verify_android_pairing_request(request, now=instant)
    approved_quota = validate_android_quota(quota)
    try:
        verified_invite = verify_invite_bundle(invite_bundle, now=instant)
    except InviteError as exc:
        raise AndroidOnboardingError("android_onboarding_invite_binding_invalid") from exc
    invite = verified_invite["payload"]
    if (
        invite["nonce"] != verified_request["pairing_nonce"]
        or float(invite["expires_at"]) > float(verified_request["expires_at"])
    ):
        raise AndroidOnboardingError("android_onboarding_invite_binding_invalid")

    request_digest = _digest(verified_request)
    recipient_bytes = _b64decode(verified_request["recipient_encryption_key"], length=32)
    recipient = X25519PublicKey.from_public_bytes(recipient_bytes)
    sender_private = X25519PrivateKey.generate()
    sender_public = sender_private.public_key().public_bytes_raw()
    salt = os.urandom(32)
    aead_nonce = os.urandom(12)
    expires_at = float(invite["expires_at"])
    header = {
        "protocol": ENCRYPTED_HANDOFF_PROTOCOL,
        "request_digest": request_digest,
        "recipient_key_digest": "sha256:" + hashlib.sha256(recipient_bytes).hexdigest(),
        "sender_ephemeral_key": _b64encode(sender_public),
        "kdf_salt": _b64encode(salt),
        "aead_nonce": _b64encode(aead_nonce),
        "expires_at": expires_at,
    }
    payload = {
        "protocol": _HANDOFF_PAYLOAD_PROTOCOL,
        "request_digest": request_digest,
        "device_ref": verified_request["device_ref"],
        "identity_key_digest": verified_request["identity_key_digest"],
        "pairing_nonce": verified_request["pairing_nonce"],
        "peer_class": verified_request["peer_class"],
        "carrier": verified_request["carrier"],
        "expires_at": expires_at,
        "seed_key_digest": verified_invite["seed_key_digest"],
        "quota": approved_quota,
        "invite_bundle": dict(invite_bundle),
    }
    key = _derive_key(
        shared=sender_private.exchange(recipient),
        salt=salt,
        request_digest=request_digest,
    )
    ciphertext = ChaCha20Poly1305(key).encrypt(
        aead_nonce,
        canonical_json_bytes(payload),
        canonical_json_bytes(header),
    )
    handoff = {**header, "ciphertext": _b64encode(ciphertext)}
    if len(canonical_json_bytes(handoff)) > _MAX_HANDOFF_BYTES:
        raise AndroidOnboardingError("android_onboarding_handoff_too_large")
    return handoff


def _strict_json_object(raw: bytes) -> dict[str, Any]:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in items:
            if key in result:
                raise ValueError("duplicate")
            result[key] = item
        return result

    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=pairs,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError("constant")),
        )
        if not isinstance(value, dict) or canonical_json_bytes(value) != raw:
            raise ValueError("noncanonical")
    except Exception as exc:  # noqa: BLE001 - decrypted attacker-controlled bytes
        raise AndroidOnboardingError("android_onboarding_handoff_invalid") from exc
    return value


def decrypt_android_invitation_handoff(
    *,
    request: Mapping[str, Any],
    handoff: Mapping[str, Any],
    recipient_private_key: bytes,
    now: float,
) -> dict[str, Any]:
    """Recipient-decrypt and verify every request, invite, quota and pin binding."""

    instant = _finite(now)
    verified_request = verify_android_pairing_request(request, now=instant)
    if not isinstance(handoff, Mapping) or set(handoff) != _HANDOFF_FIELDS:
        raise AndroidOnboardingError("android_onboarding_handoff_invalid")
    value = dict(handoff)
    request_digest = _digest(verified_request)
    if value.get("request_digest") != request_digest:
        raise AndroidOnboardingError("android_onboarding_request_mismatch")
    if value.get("protocol") != ENCRYPTED_HANDOFF_PROTOCOL:
        raise AndroidOnboardingError("android_onboarding_handoff_invalid")
    expires_at = _finite(value.get("expires_at"))
    if instant > expires_at:
        raise AndroidOnboardingError("android_onboarding_handoff_expired")

    recipient_public = _b64decode(verified_request["recipient_encryption_key"], length=32)
    expected_recipient_digest = "sha256:" + hashlib.sha256(recipient_public).hexdigest()
    if value.get("recipient_key_digest") != expected_recipient_digest:
        raise AndroidOnboardingError("android_onboarding_recipient_mismatch")
    if not isinstance(recipient_private_key, bytes) or len(recipient_private_key) != 32:
        raise AndroidOnboardingError("android_onboarding_recipient_mismatch")
    try:
        private = X25519PrivateKey.from_private_bytes(recipient_private_key)
        if private.public_key().public_bytes_raw() != recipient_public:
            raise AndroidOnboardingError("android_onboarding_recipient_mismatch")
        sender_public = X25519PublicKey.from_public_bytes(
            _b64decode(value.get("sender_ephemeral_key"), length=32)
        )
        salt = _b64decode(value.get("kdf_salt"), length=32)
        nonce = _b64decode(value.get("aead_nonce"), length=12)
        ciphertext = _b64decode(value.get("ciphertext"))
        header = {key: item for key, item in value.items() if key != "ciphertext"}
        key = _derive_key(
            shared=private.exchange(sender_public),
            salt=salt,
            request_digest=request_digest,
        )
        raw = ChaCha20Poly1305(key).decrypt(
            nonce, ciphertext, canonical_json_bytes(header)
        )
    except AndroidOnboardingError:
        raise
    except (InvalidTag, ValueError, TypeError) as exc:
        raise AndroidOnboardingError("android_onboarding_handoff_invalid") from exc

    payload = _strict_json_object(raw)
    if set(payload) != _PAYLOAD_FIELDS or payload.get("protocol") != _HANDOFF_PAYLOAD_PROTOCOL:
        raise AndroidOnboardingError("android_onboarding_handoff_invalid")
    try:
        verified_invite = verify_invite_bundle(payload["invite_bundle"], now=instant)
    except (InviteError, KeyError, TypeError) as exc:
        raise AndroidOnboardingError("android_onboarding_handoff_invalid") from exc
    invite = verified_invite["payload"]
    validate_android_quota(payload.get("quota"))
    _require_carrier(payload.get("carrier"))
    if not (
        payload.get("request_digest") == request_digest
        and payload.get("device_ref") == verified_request["device_ref"]
        and payload.get("identity_key_digest") == verified_request["identity_key_digest"]
        and payload.get("pairing_nonce") == verified_request["pairing_nonce"]
        and payload.get("peer_class") == verified_request["peer_class"]
        and payload.get("carrier") == verified_request["carrier"]
        and payload.get("expires_at") == expires_at
        and float(invite["expires_at"]) == expires_at
        and invite["nonce"] == verified_request["pairing_nonce"]
        and payload.get("seed_key_digest") == verified_invite["seed_key_digest"]
    ):
        raise AndroidOnboardingError("android_onboarding_handoff_binding_invalid")
    return payload


# ------------------------------------------------------------------ local join


def _private_directory(path: Path) -> Path:
    path = Path(path)
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink() or not path.is_dir():
        raise AndroidOnboardingError("android_onboarding_state_root_unsafe")
    info = path.stat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise AndroidOnboardingError("android_onboarding_state_root_unsafe")
    return path


def _checked_identity(identity_path: Path) -> Path:
    """Stat the durable device identity; never read, copy or create it."""

    path = Path(identity_path)
    if path.is_symlink() or not path.exists() or not path.is_file():
        raise AndroidOnboardingError("android_onboarding_identity_missing")
    info = path.stat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
        raise AndroidOnboardingError("android_onboarding_identity_unsafe")
    return path


def _atomic_write(path: Path, raw: bytes) -> None:
    temporary = path.with_name("." + path.name + ".joining")
    descriptor = os.open(
        temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _read_member(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        document = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError) as exc:
        raise AndroidOnboardingError("android_onboarding_member_state_invalid") from exc
    if (
        not isinstance(document, dict)
        or set(document) != _MEMBER_FIELDS
        or document.get("protocol") != JOIN_PROTOCOL
        or not isinstance(document.get("member_generation"), int)
        or document.get("route_ready") is not False
        or not isinstance(document.get("consumed_pairing_nonces"), list)
    ):
        raise AndroidOnboardingError("android_onboarding_member_state_invalid")
    return document


def join_android_member(
    *,
    state_root: Path,
    identity_path: Path,
    request: Mapping[str, Any],
    handoff: Mapping[str, Any],
    recipient_private_key: bytes,
    seed_key_digest_pin: str,
    member_generation: int,
    now: float,
    route_ready: object = False,
) -> dict[str, Any]:
    """Perform exactly one atomic Android local join into app-private state.

    The durable identity is only stat-checked.  ``route_ready`` must be the
    literal ``False`` and anything else is refused with
    ``route_ready_must_start_false``: joining alters no Router state, model,
    placement, topology, artifact grant or qualification.  A consumed pairing
    nonce is replayed exactly once, and a later join must strictly fence the
    generation it replaces.
    """

    instant = _finite(now)
    require_route_ready_is_false(route_ready)
    require_undivided_product_path(NATIVE_CARRIER)
    verified_request = verify_android_pairing_request(request, now=instant)
    if (
        isinstance(member_generation, bool)
        or not isinstance(member_generation, int)
        or member_generation < 1
    ):
        raise AndroidOnboardingError("android_onboarding_generation_invalid")
    if not isinstance(seed_key_digest_pin, str) or _DIGEST_RE.fullmatch(
        seed_key_digest_pin
    ) is None:
        raise AndroidOnboardingError("android_onboarding_seed_pin_mismatch")

    payload = decrypt_android_invitation_handoff(
        request=verified_request,
        handoff=handoff,
        recipient_private_key=recipient_private_key,
        now=instant,
    )
    if payload["seed_key_digest"] != seed_key_digest_pin:
        raise AndroidOnboardingError("android_onboarding_seed_pin_mismatch")

    root = _private_directory(Path(state_root))
    _checked_identity(identity_path)
    state_path = root / MEMBER_STATE_NAME
    existing = _read_member(state_path)
    consumed: list[str] = [] if existing is None else list(existing["consumed_pairing_nonces"])
    nonce = str(payload["pairing_nonce"])
    if nonce in consumed:
        raise AndroidOnboardingError("android_onboarding_replay_rejected")
    if existing is not None and member_generation <= int(existing["member_generation"]):
        raise AndroidOnboardingError("android_onboarding_generation_not_fenced")
    consumed.append(nonce)
    del consumed[:-REPLAY_LEDGER_LIMIT]

    state = {
        "protocol": JOIN_PROTOCOL,
        "peer_class": payload["peer_class"],
        "device_ref": payload["device_ref"],
        "identity_key_digest": payload["identity_key_digest"],
        "seed_key_digest": payload["seed_key_digest"],
        "member_generation": member_generation,
        "carrier": payload["carrier"],
        "joined_at": instant,
        "route_ready": False,
        "consumed_pairing_nonces": consumed,
    }
    _atomic_write(state_path, canonical_json_bytes(state))
    return {
        **state,
        "authority": _MEMBER_AUTHORITY,
        "step": "member_joined",
        "steps_completed": list(ONBOARDING_STEPS),
        "product_path_kind": NATIVE_CARRIER,
        "granted": _MEMBER_GRANT,
        "rung_effect": _MEMBER_RUNG_EFFECT,
    }
