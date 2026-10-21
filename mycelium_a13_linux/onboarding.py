# SPDX-License-Identifier: AGPL-3.0-only
"""Linux target-side invitation onboarding for A13 (source tranche).

The Linux target (any admitted Linux member) creates its own pairing request, decrypts
an owner-approved handoff with a recipient-only X25519 key, checks the seed key
the target was pinned to, and performs exactly one atomic local join.  It speaks
the SAME wire protocols the committed macOS slice froze:

    mycelium.pairing_request.v1
    mycelium.encrypted_invitation_handoff.v1

so the request and handoff field sets are identical to the macOS slice's.  The
peer class differs (``linux_native_iroh``).  The macOS owner module's closed
peer-class allow-set now names ``linux_native_iroh`` explicitly, so owner-side
approval of a Linux pairing request is served by that module, and the
membership registry declares the class so a seed admits its join.  The class is
joinable but activation-refused (``linux_native_member_runtime_unbound``) until
the Linux product package is bound to a member stage runtime; this module never
widens either check on its own side.

This module never writes a private key, never copies identity material into a
record or a return value, never stores plaintext handoff state, and never claims
serving capability, qualification, or inference eligibility.
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

from .progress import LADDER, advance, new_progress

PAIRING_REQUEST_PROTOCOL = "mycelium.pairing_request.v1"
ENCRYPTED_HANDOFF_PROTOCOL = "mycelium.encrypted_invitation_handoff.v1"
_HANDOFF_PAYLOAD_PROTOCOL = "mycelium.encrypted_invitation_payload.v1"
LINUX_PEER_CLASS = "linux_native_iroh"
SUPPORTED_PEER_CLASSES: frozenset[str] = frozenset({LINUX_PEER_CLASS})
_MEMBER_PROTOCOL = "mycelium.a13_linux_local_member.v1"
_LEDGER_PROTOCOL = "mycelium.a13_linux_replay_ledger.v1"

_REQUEST_TTL_MAX = 600
_MAX_HANDOFF_BYTES = 64 * 1024
_MAX_QUOTA_BYTES = 1024**4
_REPLAY_LEDGER_LIMIT = 64
_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

_REQUEST_FIELDS = frozenset(
    {
        "protocol",
        "node_id",
        "identity_endpoint_id",
        "identity_key_digest",
        "identity_verification_key",
        "peer_class",
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
        "node_id",
        "identity_key_digest",
        "pairing_nonce",
        "peer_class",
        "expires_at",
        "seed_key_digest",
        "quota",
        "invite_bundle",
    }
)
_QUOTA_FIELDS = frozenset(
    {"storage_bytes", "network_bytes_per_day", "background_allowed"}
)


class OnboardingError(RuntimeError):
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
        raise OnboardingError("onboarding_time_invalid")
    return float(value)


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64decode(value: Any, *, length: int | None = None) -> bytes:
    if not isinstance(value, str) or not value or "=" in value:
        raise OnboardingError("onboarding_handoff_invalid")
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except Exception as exc:  # noqa: BLE001 - crypto boundary
        raise OnboardingError("onboarding_handoff_invalid") from exc
    if _b64encode(raw) != value or (length is not None and len(raw) != length):
        raise OnboardingError("onboarding_handoff_invalid")
    return raw


def _digest(document: Mapping[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(canonical_json_bytes(dict(document))).hexdigest()


def validate_linux_quota(quota: object) -> dict[str, Any]:
    """Bounded consent quota carried inside the encrypted invitation."""

    if not isinstance(quota, Mapping) or set(quota) != _QUOTA_FIELDS:
        raise OnboardingError("onboarding_quota_invalid")
    result = dict(quota)
    for field in ("storage_bytes", "network_bytes_per_day"):
        value = result[field]
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or value < 0
            or value > _MAX_QUOTA_BYTES
        ):
            raise OnboardingError("onboarding_quota_invalid")
    if type(result["background_allowed"]) is not bool:
        raise OnboardingError("onboarding_quota_invalid")
    return result


def new_linux_pairing_request(
    *,
    signer: Ed25519EvidenceSigner,
    node_id: str,
    now: float,
    ttl_seconds: int = 300,
    nonce: str | None = None,
) -> tuple[dict[str, Any], bytes]:
    """Create one target-signed request and its recipient-only X25519 key."""

    if not isinstance(signer, Ed25519EvidenceSigner):
        raise OnboardingError("onboarding_identity_invalid")
    if not isinstance(node_id, str) or _SEGMENT_RE.fullmatch(node_id) is None:
        raise OnboardingError("onboarding_request_invalid")
    if (
        isinstance(ttl_seconds, bool)
        or not isinstance(ttl_seconds, int)
        or not 1 <= ttl_seconds <= _REQUEST_TTL_MAX
    ):
        raise OnboardingError("onboarding_request_ttl_invalid")
    issued_at = _finite(now)
    pairing_nonce = "pair-" + _b64encode(os.urandom(24)) if nonce is None else nonce
    if not isinstance(pairing_nonce, str) or _SEGMENT_RE.fullmatch(pairing_nonce) is None:
        raise OnboardingError("onboarding_request_invalid")
    private = X25519PrivateKey.generate()
    public_bytes = private.public_key().public_bytes_raw()
    statement = {
        "protocol": PAIRING_REQUEST_PROTOCOL,
        "node_id": node_id,
        "identity_endpoint_id": signer.endpoint_id,
        "identity_key_digest": signer.verification_key_digest,
        "identity_verification_key": signer.public_key_record(),
        "peer_class": LINUX_PEER_CLASS,
        "invite_protocol": INVITE_BUNDLE_PROTOCOL,
        "recipient_encryption_key": _b64encode(public_bytes),
        "pairing_nonce": pairing_nonce,
        "issued_at": issued_at,
        "expires_at": issued_at + ttl_seconds,
    }
    return {**statement, "signature": signer.sign(statement)}, private.private_bytes_raw()


def verify_linux_pairing_request(request: object, *, now: float) -> dict[str, Any]:
    """Verify the closed request, permanent identity signature, and lifetime."""

    if not isinstance(request, Mapping) or set(request) != _REQUEST_FIELDS:
        raise OnboardingError("onboarding_request_invalid")
    value = dict(request)
    if value.get("protocol") != PAIRING_REQUEST_PROTOCOL:
        raise OnboardingError("onboarding_request_invalid")
    for field in ("node_id", "identity_endpoint_id", "pairing_nonce"):
        if not isinstance(value.get(field), str) or _SEGMENT_RE.fullmatch(value[field]) is None:
            raise OnboardingError("onboarding_request_invalid")
    if (
        not isinstance(value.get("identity_key_digest"), str)
        or _DIGEST_RE.fullmatch(value["identity_key_digest"]) is None
        or not isinstance(value.get("identity_verification_key"), Mapping)
        or value["identity_verification_key"].get("verification_key_digest")
        != value["identity_key_digest"]
    ):
        raise OnboardingError("onboarding_request_invalid")
    _b64decode(value.get("recipient_encryption_key"), length=32)
    issued = _finite(value.get("issued_at"))
    expires = _finite(value.get("expires_at"))
    instant = _finite(now)
    if expires <= issued or expires - issued > _REQUEST_TTL_MAX:
        raise OnboardingError("onboarding_request_invalid")
    signature = value.get("signature")
    statement = {key: item for key, item in value.items() if key != "signature"}
    try:
        verify = build_ed25519_verifier([dict(value["identity_verification_key"])])
    except Exception as exc:  # noqa: BLE001 - external record
        raise OnboardingError("onboarding_request_signature_invalid") from exc
    if (
        not isinstance(signature, dict)
        or signature.get("signer_endpoint_id") != value["identity_endpoint_id"]
        or not verify(canonical_json_bytes(statement), signature)
    ):
        raise OnboardingError("onboarding_request_signature_invalid")
    if value.get("peer_class") not in SUPPORTED_PEER_CLASSES:
        raise OnboardingError("onboarding_peer_class_unsupported")
    if value.get("invite_protocol") != INVITE_BUNDLE_PROTOCOL:
        raise OnboardingError("onboarding_request_invalid")
    if instant > expires:
        raise OnboardingError("onboarding_request_expired")
    return value


def _derive_key(*, shared: bytes, salt: bytes, request_digest: str) -> bytes:
    return HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        info=b"mycelium.a13.pairing.handoff.v1\0" + request_digest.encode("ascii"),
    ).derive(shared)


def approve_linux_pairing_request(
    *,
    request: Mapping[str, Any],
    invite_bundle: Mapping[str, Any],
    quota: Mapping[str, Any],
    now: float,
) -> dict[str, Any]:
    """Owner-side approval: encrypt one complete A8 bundle to the Linux target."""

    instant = _finite(now)
    verified_request = verify_linux_pairing_request(request, now=instant)
    approved_quota = validate_linux_quota(quota)
    try:
        verified_invite = verify_invite_bundle(invite_bundle, now=instant)
    except InviteError as exc:
        raise OnboardingError("onboarding_invite_binding_invalid") from exc
    invite = verified_invite["payload"]
    if (
        invite["nonce"] != verified_request["pairing_nonce"]
        or float(invite["expires_at"]) > float(verified_request["expires_at"])
    ):
        raise OnboardingError("onboarding_invite_binding_invalid")
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
        "node_id": verified_request["node_id"],
        "identity_key_digest": verified_request["identity_key_digest"],
        "pairing_nonce": verified_request["pairing_nonce"],
        "peer_class": verified_request["peer_class"],
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
        raise OnboardingError("onboarding_handoff_too_large")
    return handoff


def _strict_json_object(raw: bytes) -> dict[str, Any]:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate")
            result[key] = value
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
        raise OnboardingError("onboarding_handoff_invalid") from exc
    return value


def decrypt_linux_invitation_handoff(
    *,
    request: Mapping[str, Any],
    handoff: Mapping[str, Any],
    recipient_private_key: bytes,
    now: float,
) -> dict[str, Any]:
    """Recipient-decrypt and verify every request, invite, quota, and pin binding."""

    instant = _finite(now)
    verified_request = verify_linux_pairing_request(request, now=instant)
    if not isinstance(handoff, Mapping) or set(handoff) != _HANDOFF_FIELDS:
        raise OnboardingError("onboarding_handoff_invalid")
    value = dict(handoff)
    request_digest = _digest(verified_request)
    if value.get("request_digest") != request_digest:
        raise OnboardingError("onboarding_request_mismatch")
    if value.get("protocol") != ENCRYPTED_HANDOFF_PROTOCOL:
        raise OnboardingError("onboarding_handoff_invalid")
    expires_at = _finite(value.get("expires_at"))
    if instant > expires_at:
        raise OnboardingError("onboarding_handoff_expired")
    recipient_public = _b64decode(verified_request["recipient_encryption_key"], length=32)
    expected_recipient_digest = "sha256:" + hashlib.sha256(recipient_public).hexdigest()
    if value.get("recipient_key_digest") != expected_recipient_digest:
        raise OnboardingError("onboarding_recipient_mismatch")
    if not isinstance(recipient_private_key, bytes) or len(recipient_private_key) != 32:
        raise OnboardingError("onboarding_recipient_mismatch")
    try:
        private = X25519PrivateKey.from_private_bytes(recipient_private_key)
        if private.public_key().public_bytes_raw() != recipient_public:
            raise OnboardingError("onboarding_recipient_mismatch")
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
            nonce,
            ciphertext,
            canonical_json_bytes(header),
        )
    except OnboardingError:
        raise
    except (InvalidTag, ValueError, TypeError) as exc:
        raise OnboardingError("onboarding_handoff_invalid") from exc
    payload = _strict_json_object(raw)
    if set(payload) != _PAYLOAD_FIELDS or payload.get("protocol") != _HANDOFF_PAYLOAD_PROTOCOL:
        raise OnboardingError("onboarding_handoff_invalid")
    try:
        verified_invite = verify_invite_bundle(payload["invite_bundle"], now=instant)
    except (InviteError, KeyError, TypeError) as exc:
        raise OnboardingError("onboarding_handoff_invalid") from exc
    invite = verified_invite["payload"]
    validate_linux_quota(payload.get("quota"))
    expected = (
        payload.get("request_digest") == request_digest
        and payload.get("node_id") == verified_request["node_id"]
        and payload.get("identity_key_digest") == verified_request["identity_key_digest"]
        and payload.get("pairing_nonce") == verified_request["pairing_nonce"]
        and payload.get("peer_class") == verified_request["peer_class"]
        and payload.get("expires_at") == expires_at
        and float(invite["expires_at"]) == expires_at
        and invite["nonce"] == verified_request["pairing_nonce"]
        and payload.get("seed_key_digest") == verified_invite["seed_key_digest"]
    )
    if not expected:
        raise OnboardingError("onboarding_handoff_binding_invalid")
    return payload


# ---------------------------------------------------------------- local join


def _private_directory(path: Path, *, create: bool) -> Path:
    path = Path(path)
    if create:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink() or not path.is_dir():
        raise OnboardingError("onboarding_state_root_unsafe")
    info = path.stat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise OnboardingError("onboarding_state_root_unsafe")
    return path


def _checked_identity(identity_path: Path) -> Path:
    """Stat the durable target identity; never read, copy, or create it."""

    path = Path(identity_path)
    if path.is_symlink() or not path.exists() or not path.is_file():
        raise OnboardingError("onboarding_identity_missing")
    info = path.stat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
        raise OnboardingError("onboarding_identity_unsafe")
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
        raise OnboardingError("onboarding_member_state_invalid") from exc
    if (
        not isinstance(document, dict)
        or document.get("protocol") != _MEMBER_PROTOCOL
        or not isinstance(document.get("member_generation"), int)
    ):
        raise OnboardingError("onboarding_member_state_invalid")
    return document


def _read_ledger(path: Path) -> list[str]:
    if not path.exists():
        return []
    try:
        document = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError) as exc:
        raise OnboardingError("onboarding_replay_state_invalid") from exc
    if (
        not isinstance(document, dict)
        or document.get("protocol") != _LEDGER_PROTOCOL
        or not isinstance(document.get("nonces"), list)
        or any(not isinstance(item, str) for item in document["nonces"])
    ):
        raise OnboardingError("onboarding_replay_state_invalid")
    return list(document["nonces"])


def _initial_progress(member_generation: int, now: float) -> dict[str, Any]:
    document = new_progress(
        member_generation=member_generation,
        authority_provenance="local_join",
        now=now,
    )
    for index, rung in enumerate(LADDER[:6]):
        document = advance(document, rung=rung, state="satisfied", now=now + index)
    return document


def complete_local_join(
    *,
    state_root: Path,
    identity_path: Path,
    request: Mapping[str, Any],
    handoff: Mapping[str, Any],
    recipient_private_key: bytes,
    seed_key_digest_pin: str,
    member_generation: int,
    now: float,
    plaintext_handoff_file: Path | None = None,
) -> dict[str, Any]:
    """Verify, decrypt, check the seed pin, and persist one accepted member."""

    root = _private_directory(Path(state_root), create=False)
    _checked_identity(Path(identity_path))
    if (
        isinstance(member_generation, bool)
        or not isinstance(member_generation, int)
        or member_generation <= 0
    ):
        raise OnboardingError("onboarding_member_generation_invalid")
    if not isinstance(seed_key_digest_pin, str) or _DIGEST_RE.fullmatch(
        seed_key_digest_pin
    ) is None:
        raise OnboardingError("onboarding_seed_pin_invalid")
    instant = _finite(now)
    payload = decrypt_linux_invitation_handoff(
        request=request,
        handoff=handoff,
        recipient_private_key=recipient_private_key,
        now=instant,
    )
    if payload["seed_key_digest"] != seed_key_digest_pin:
        raise OnboardingError("onboarding_seed_pin_mismatch")

    onboarding = _private_directory(root / "onboarding", create=True)
    member_path = onboarding / "member.json"
    ledger_path = onboarding / "consumed-handoffs.json"
    nonce = payload["pairing_nonce"]
    ledger = _read_ledger(ledger_path)
    if nonce in ledger:
        raise OnboardingError("onboarding_invitation_replayed")
    if len(ledger) >= _REPLAY_LEDGER_LIMIT:
        # Bounded tombstones: fail closed instead of evicting a replay record.
        raise OnboardingError("onboarding_replay_ledger_full")
    existing = _read_member(member_path)
    if existing is not None and member_generation <= existing["member_generation"]:
        raise OnboardingError("onboarding_member_generation_stale")

    record = {
        "protocol": _MEMBER_PROTOCOL,
        "member_generation": member_generation,
        "pairing_nonce": nonce,
        "identity_key_digest": payload["identity_key_digest"],
        "seed_key_digest": payload["seed_key_digest"],
        "joined_at": instant,
        "eligibility": "member_only",
        "route_ready": False,
    }
    _atomic_write(member_path, canonical_json_bytes(record))
    _atomic_write(
        ledger_path,
        canonical_json_bytes(
            {"protocol": _LEDGER_PROTOCOL, "nonces": sorted([*ledger, nonce])}
        ),
    )

    erased = False
    if plaintext_handoff_file is not None:
        try:
            Path(plaintext_handoff_file).unlink()
        except OSError as exc:
            raise OnboardingError("onboarding_plaintext_erasure_failed") from exc
        erased = True
    return {
        "joined": True,
        "member_generation": member_generation,
        "pairing_nonce": nonce,
        "identity_key_digest": payload["identity_key_digest"],
        "eligibility": "member_only",
        "route_ready": False,
        "plaintext_erased": erased,
        "progress": _initial_progress(member_generation, instant),
    }


__all__ = [
    "ENCRYPTED_HANDOFF_PROTOCOL",
    "LINUX_PEER_CLASS",
    "PAIRING_REQUEST_PROTOCOL",
    "SUPPORTED_PEER_CLASSES",
    "OnboardingError",
    "approve_linux_pairing_request",
    "complete_local_join",
    "decrypt_linux_invitation_handoff",
    "new_linux_pairing_request",
    "validate_linux_quota",
    "verify_linux_pairing_request",
]
