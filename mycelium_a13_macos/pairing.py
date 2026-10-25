# SPDX-License-Identifier: AGPL-3.0-only
"""Recipient-bound encrypted pairing contracts for the macOS demo."""

from __future__ import annotations

import base64
from collections.abc import Mapping
import hashlib
import json
import math
import os
import re
import secrets
import threading
import time
from typing import Any, Callable

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

PAIRING_REQUEST_PROTOCOL = "mycelium.pairing_request.v1"
ENCRYPTED_HANDOFF_PROTOCOL = "mycelium.encrypted_invitation_handoff.v1"
_HANDOFF_PAYLOAD_PROTOCOL = "mycelium.encrypted_invitation_payload.v1"
#: Closed allow-set of peer classes the owner module admits.  Adding a class
#: here is a deliberate cross-lane decision (see the pinned tests for the
#: Windows and Linux admissions); anything not in this set is refused, and
#: there is no brand/OS sniffing fallback that would admit an unlisted class
#: implicitly.
_SUPPORTED_PEER_CLASSES = frozenset(
    {"mac_mlx_iroh", "windows_native_iroh", "linux_native_iroh"}
)
_REQUEST_TTL_MAX = 600
_MAX_HANDOFF_BYTES = 64 * 1024
_MAX_QUOTA_BYTES = 1024**4
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


class PairingError(RuntimeError):
    """Fail-closed pairing error carrying no private material."""

    def __init__(self, code: str, *, parent: str | None = None) -> None:
        self.code = code
        #: The older, coarser code this one refines (``None`` when it is not a refinement).
        self.parent = parent
        super().__init__(code)


def _finite(value: Any) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
    ):
        raise PairingError("pairing_time_invalid")
    return float(value)


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64decode(value: Any, *, length: int | None = None) -> bytes:
    if not isinstance(value, str) or not value or "=" in value:
        raise PairingError("pairing_handoff_invalid")
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except Exception as exc:  # noqa: BLE001 - crypto boundary
        raise PairingError("pairing_handoff_invalid") from exc
    if _b64encode(raw) != value or (length is not None and len(raw) != length):
        raise PairingError("pairing_handoff_invalid")
    return raw


def _digest(document: Mapping[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(canonical_json_bytes(dict(document))).hexdigest()


def validate_quota(quota: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(quota, Mapping) or set(quota) != _QUOTA_FIELDS:
        raise PairingError("pairing_quota_invalid")
    result = dict(quota)
    for field in ("storage_bytes", "network_bytes_per_day"):
        value = result[field]
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or value < 0
            or value > _MAX_QUOTA_BYTES
        ):
            raise PairingError("pairing_quota_invalid")
    if type(result["background_allowed"]) is not bool:
        raise PairingError("pairing_quota_invalid")
    return result


def new_pairing_request(
    *,
    signer: Ed25519EvidenceSigner,
    node_id: str,
    peer_class: str,
    now: float,
    ttl_seconds: int = 300,
    nonce: str | None = None,
) -> tuple[dict[str, Any], bytes]:
    """Create one target-signed request and its recipient-only X25519 key."""

    if not isinstance(signer, Ed25519EvidenceSigner):
        raise PairingError("pairing_identity_invalid")
    if not isinstance(node_id, str) or _SEGMENT_RE.fullmatch(node_id) is None:
        raise PairingError("pairing_request_invalid")
    if peer_class not in _SUPPORTED_PEER_CLASSES:
        raise PairingError("pairing_peer_class_unsupported")
    if (
        isinstance(ttl_seconds, bool)
        or not isinstance(ttl_seconds, int)
        or not 1 <= ttl_seconds <= _REQUEST_TTL_MAX
    ):
        raise PairingError("pairing_request_ttl_invalid")
    issued_at = _finite(now)
    pairing_nonce = 'pair-' + _b64encode(os.urandom(24)) if nonce is None else nonce
    if not isinstance(pairing_nonce, str) or _SEGMENT_RE.fullmatch(pairing_nonce) is None:
        raise PairingError("pairing_request_invalid")
    private = X25519PrivateKey.generate()
    public_bytes = private.public_key().public_bytes_raw()
    statement = {
        "protocol": PAIRING_REQUEST_PROTOCOL,
        "node_id": node_id,
        "identity_endpoint_id": signer.endpoint_id,
        "identity_key_digest": signer.verification_key_digest,
        "identity_verification_key": signer.public_key_record(),
        "peer_class": peer_class,
        "invite_protocol": INVITE_BUNDLE_PROTOCOL,
        "recipient_encryption_key": _b64encode(public_bytes),
        "pairing_nonce": pairing_nonce,
        "issued_at": issued_at,
        "expires_at": issued_at + ttl_seconds,
    }
    return {**statement, "signature": signer.sign(statement)}, private.private_bytes_raw()


def verify_pairing_request(
    request: Mapping[str, Any],
    *,
    now: float,
) -> dict[str, Any]:
    """Verify the closed request, permanent identity signature, and lifetime."""

    if not isinstance(request, Mapping) or set(request) != _REQUEST_FIELDS:
        raise PairingError("pairing_request_invalid")
    value = dict(request)
    if value.get("protocol") != PAIRING_REQUEST_PROTOCOL:
        raise PairingError("pairing_request_invalid")
    for field in ("node_id", "identity_endpoint_id", "pairing_nonce"):
        if not isinstance(value.get(field), str) or _SEGMENT_RE.fullmatch(value[field]) is None:
            raise PairingError("pairing_request_invalid")
    if (
        not isinstance(value.get("identity_key_digest"), str)
        or _DIGEST_RE.fullmatch(value["identity_key_digest"]) is None
        or not isinstance(value.get("identity_verification_key"), Mapping)
        or value["identity_verification_key"].get("verification_key_digest")
        != value["identity_key_digest"]
    ):
        raise PairingError("pairing_request_invalid")
    _b64decode(value.get("recipient_encryption_key"), length=32)
    issued = _finite(value.get("issued_at"))
    expires = _finite(value.get("expires_at"))
    instant = _finite(now)
    if expires <= issued or expires - issued > _REQUEST_TTL_MAX:
        raise PairingError("pairing_request_invalid")
    signature = value.get("signature")
    statement = {key: item for key, item in value.items() if key != "signature"}
    try:
        verify = build_ed25519_verifier([dict(value["identity_verification_key"])])
    except Exception as exc:  # noqa: BLE001 - external record
        raise PairingError("pairing_request_signature_invalid") from exc
    if (
        not isinstance(signature, dict)
        or signature.get("signer_endpoint_id") != value["identity_endpoint_id"]
        or not verify(canonical_json_bytes(statement), signature)
    ):
        raise PairingError("pairing_request_signature_invalid")
    if (
        value.get("peer_class") not in _SUPPORTED_PEER_CLASSES
        or value.get("invite_protocol") != INVITE_BUNDLE_PROTOCOL
    ):
        raise PairingError("pairing_request_invalid")
    if instant > expires:
        raise PairingError("pairing_request_expired")
    return value


def _derive_key(
    *,
    shared: bytes,
    salt: bytes,
    request_digest: str,
) -> bytes:
    return HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        info=b"mycelium.a13.pairing.handoff.v1\0" + request_digest.encode("ascii"),
    ).derive(shared)


def approve_pairing_request(
    *,
    request: Mapping[str, Any],
    invite_bundle: Mapping[str, Any],
    quota: Mapping[str, Any],
    now: float,
) -> dict[str, Any]:
    """Verify owner inputs and encrypt one complete A8 bundle to the target."""

    instant = _finite(now)
    verified_request = verify_pairing_request(request, now=instant)
    approved_quota = validate_quota(quota)
    try:
        verified_invite = verify_invite_bundle(invite_bundle, now=instant)
    except InviteError as exc:
        raise PairingError("pairing_invite_binding_invalid") from exc
    invite = verified_invite["payload"]
    if (
        invite["nonce"] != verified_request["pairing_nonce"]
        or float(invite["expires_at"]) > float(verified_request["expires_at"])
    ):
        raise PairingError("pairing_invite_binding_invalid")
    request_digest = _digest(verified_request)
    recipient_bytes = _b64decode(
        verified_request["recipient_encryption_key"], length=32
    )
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
        raise PairingError("pairing_handoff_too_large")
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
        raise PairingError("pairing_handoff_invalid") from exc
    return value


def decrypt_invitation_handoff(
    *,
    request: Mapping[str, Any],
    handoff: Mapping[str, Any],
    recipient_private_key: bytes,
    now: float,
) -> dict[str, Any]:
    """Recipient-decrypt and verify every request, invite, quota, and pin binding."""

    instant = _finite(now)
    verified_request = verify_pairing_request(request, now=instant)
    if not isinstance(handoff, Mapping) or set(handoff) != _HANDOFF_FIELDS:
        raise PairingError("pairing_handoff_invalid")
    value = dict(handoff)
    request_digest = _digest(verified_request)
    if value.get("request_digest") != request_digest:
        raise PairingError("pairing_handoff_request_mismatch")
    if value.get("protocol") != ENCRYPTED_HANDOFF_PROTOCOL:
        raise PairingError("pairing_handoff_invalid")
    expires_at = _finite(value.get("expires_at"))
    if instant > expires_at:
        raise PairingError("pairing_handoff_expired")
    recipient_public = _b64decode(
        verified_request["recipient_encryption_key"], length=32
    )
    expected_recipient_digest = "sha256:" + hashlib.sha256(recipient_public).hexdigest()
    if value.get("recipient_key_digest") != expected_recipient_digest:
        raise PairingError("pairing_handoff_recipient_mismatch")
    if not isinstance(recipient_private_key, bytes) or len(recipient_private_key) != 32:
        raise PairingError("pairing_handoff_recipient_mismatch")
    try:
        private = X25519PrivateKey.from_private_bytes(recipient_private_key)
        if private.public_key().public_bytes_raw() != recipient_public:
            raise PairingError("pairing_handoff_recipient_mismatch")
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
    except PairingError:
        raise
    except (InvalidTag, ValueError, TypeError) as exc:
        raise PairingError("pairing_handoff_invalid") from exc
    payload = _strict_json_object(raw)
    if set(payload) != _PAYLOAD_FIELDS or payload.get("protocol") != _HANDOFF_PAYLOAD_PROTOCOL:
        raise PairingError("pairing_handoff_invalid")
    try:
        verified_invite = verify_invite_bundle(payload["invite_bundle"], now=instant)
    except (InviteError, KeyError, TypeError) as exc:
        raise PairingError("pairing_handoff_invalid") from exc
    invite = verified_invite["payload"]
    validate_quota(payload.get("quota"))
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
        raise PairingError("pairing_handoff_binding_invalid")
    return payload


# -- owner pairing window (A13 join flow) ---------------------------------------
#
# The owner opens a short-lived window; the app shows a join link and a short
# typed code.  The target posts its target-signed pairing request (plus its
# signed join capability/link report) to the window through the owner's public
# HTTPS relay, the owner approves it with one click, and the target collects
# the recipient-encrypted handoff by polling the same window.  Nothing is
# carried by hand, and the relay only ever sees the code, the public request
# and recipient ciphertext.  Windows are in-memory: an owner restart closes
# every open window (fail closed).

JOIN_LINK_PROTOCOL = "mycelium.a13_join_link.v1"
JOIN_LINK_SCHEME = "mycelium-join:"
PAIRING_WINDOW_PREFIX = "/pairing-window/v1/"
_SUBMISSION_PROTOCOL = "mycelium.a13_pairing_submission.v1"
_POLL_PROTOCOL = "mycelium.a13_pairing_poll.v1"
_WINDOW_TTL_MIN = 30
_WINDOW_TTL_MAX = 900
_MAX_WINDOWS = 8
_MAX_CODE_FAILURES = 5
_CODE_ALPHABET = "23456789ABCDEFGHJKMNPQRSTVWXYZ"
_WINDOW_ID_RE = re.compile(r"^[23456789ABCDEFGHJKMNPQRSTVWXYZ]{4}$")
_CODE_RE = re.compile(r"^[23456789ABCDEFGHJKMNPQRSTVWXYZ]{8}$")
_TUNNEL_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_ERROR_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{0,95}$")
#: Why the owner's window could not be reached; each refines ``pairing_window_unreachable``.
UNREACHABLE_CODE = "pairing_window_unreachable"
UNREACHABLE_CODES = ("pairing_window_dns_failed", "pairing_window_tls_failed",
                     "pairing_window_timeout")
#: A warning above this many seconds of HTTPS ``Date`` difference (the hard limit is 30 s).
CLOCK_SKEW_WARNING_SECONDS = 20


def _unreachable(exc: BaseException) -> PairingError:
    """Classify a transport failure (DNS / TLS / timeout / anything else)."""
    import socket
    import ssl

    seen: list[BaseException] = []
    current: BaseException | None = exc
    while current is not None and len(seen) < 4:
        seen.append(current)
        reason = getattr(current, "reason", None)
        current = reason if isinstance(reason, BaseException) else None
    for item in seen:
        text = str(item)
        if isinstance(item, socket.gaierror) or text.startswith("relay_dns_"):
            return PairingError(UNREACHABLE_CODES[0], parent=UNREACHABLE_CODE)
        if isinstance(item, ssl.SSLError) or text.startswith("relay_tls_"):
            return PairingError(UNREACHABLE_CODES[1], parent=UNREACHABLE_CODE)
        if isinstance(item, TimeoutError):
            return PairingError(UNREACHABLE_CODES[2], parent=UNREACHABLE_CODE)
    return PairingError(UNREACHABLE_CODE)
_WINDOW_STATUS = {
    "pairing_window_unknown": 404,
    "pairing_window_expired": 410,
    "pairing_window_revoked": 410,
    "pairing_window_locked": 410,
    "pairing_window_code_invalid": 403,
    "pairing_window_request_mismatch": 403,
    "pairing_window_claimed": 409,
}


def request_digest(request: Mapping[str, Any]) -> str:
    """Public digest of one exact pairing request (the window claim key)."""

    return _digest(request)


def identity_fingerprint(identity_key_digest: str) -> str:
    """Short grouped fingerprint both screens show before the owner approves."""

    if not isinstance(identity_key_digest, str) or _DIGEST_RE.fullmatch(identity_key_digest) is None:
        raise PairingError("pairing_request_invalid")
    raw = identity_key_digest.removeprefix("sha256:")[:12].upper()
    return "-".join(raw[i : i + 4] for i in range(0, 12, 4))


def _random_text(length: int) -> str:
    return "".join(secrets.choice(_CODE_ALPHABET) for _ in range(length))


def _window_origin(origin: Any) -> str:
    from mycelium_internet.bootstrap import canonical_https_origin

    try:
        canonical = canonical_https_origin(origin)
    except (TypeError, ValueError) as exc:
        raise PairingError("pairing_window_origin_invalid") from exc
    if canonical != origin:
        raise PairingError("pairing_window_origin_invalid")
    return canonical


def encode_join_link(document: Mapping[str, Any]) -> str:
    return JOIN_LINK_SCHEME + _b64encode(canonical_json_bytes(dict(document)))


def _normalized_code(text: str) -> tuple[str, str]:
    compact = re.sub(r"[\s-]", "", text).upper()
    window_id, code = compact[:4], compact[4:]
    if len(compact) != 12 or _WINDOW_ID_RE.fullmatch(window_id) is None or _CODE_RE.fullmatch(code) is None:
        raise PairingError("join_code_invalid")
    return window_id, code


def parse_join_text(text: Any, *, now: float) -> dict[str, Any]:
    """Parse a full join link or the short ``<tunnel-name> ABCD-EFGH-JKLM`` form.

    The full link pins the owner's seed key digest; the short typed form
    trusts the public HTTPS (Web-PKI) relay origin and returns no pin.
    """

    if not isinstance(text, str) or not text.strip() or len(text) > 4096:
        raise PairingError("join_code_invalid")
    value = text.strip()
    instant = _finite(now)
    if value.startswith(JOIN_LINK_SCHEME):
        try:
            document = json.loads(_b64decode(value[len(JOIN_LINK_SCHEME):]))
        except (PairingError, ValueError, UnicodeError) as exc:
            raise PairingError("join_code_invalid") from exc
        if (
            not isinstance(document, dict)
            or set(document) != {"protocol", "origin", "window_id", "code", "seed_key_digest", "expires_at"}
            or document["protocol"] != JOIN_LINK_PROTOCOL
            or not isinstance(document["window_id"], str)
            or _WINDOW_ID_RE.fullmatch(document["window_id"]) is None
            or not isinstance(document["code"], str)
            or _CODE_RE.fullmatch(document["code"]) is None
            or not isinstance(document["seed_key_digest"], str)
            or _DIGEST_RE.fullmatch(document["seed_key_digest"]) is None
        ):
            raise PairingError("join_code_invalid")
        try:
            origin = _window_origin(document["origin"])
        except PairingError as exc:
            raise PairingError("join_code_invalid") from exc
        expires_at = _finite(document["expires_at"])
        if instant > expires_at:
            raise PairingError("join_link_expired")
        return {"origin": origin, "window_id": document["window_id"], "code": document["code"],
                "seed_key_digest": document["seed_key_digest"], "expires_at": expires_at}
    host, _separator, code_text = value.partition(" ")
    host = host.strip().lower()
    if host.startswith("https://"):
        host = host[len("https://"):]
    host = host.removesuffix("/").removesuffix(".trycloudflare.com")
    if _TUNNEL_RE.fullmatch(host) is None or not code_text.strip():
        raise PairingError("join_code_invalid")
    window_id, code = _normalized_code(code_text)
    return {"origin": f"https://{host}.trycloudflare.com", "window_id": window_id, "code": code,
            "seed_key_digest": None, "expires_at": None}


def _public_json_object(raw: bytes) -> dict[str, Any]:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate")
            result[key] = value
        return result

    value = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs,
                       parse_constant=lambda _value: (_ for _ in ()).throw(ValueError("constant")))
    if not isinstance(value, dict):
        raise ValueError("object")
    return value


class _Window:
    __slots__ = ("window_id", "code", "origin", "seed_key_digest", "opened_at", "expires_at",
                 "state", "failures", "request", "report", "quota", "handoff")

    def __init__(self, *, window_id, code, origin, seed_key_digest, opened_at, expires_at):
        self.window_id = window_id
        self.code = code
        self.origin = origin
        self.seed_key_digest = seed_key_digest
        self.opened_at = opened_at
        self.expires_at = expires_at
        self.state = "open"
        self.failures = 0
        self.request = None
        self.report = None
        self.quota = None
        self.handoff = None


class PairingWindows:
    """Owner-side, in-memory registry of single-use pairing windows."""

    def __init__(self, *, clock: Callable[[], float] = time.time) -> None:
        if not callable(clock):
            raise ValueError("clock is invalid")
        self._clock = clock
        self._lock = threading.RLock()
        self._windows: dict[str, _Window] = {}

    def _now(self) -> float:
        return _finite(self._clock())

    def _refresh(self, window: _Window, now: float) -> None:
        if window.state in {"open", "claimed", "approved", "delivered"} and now > window.expires_at:
            window.state = "expired"
            window.request = window.report = window.quota = window.handoff = None

    def _live(self, window_id: Any, code: Any, now: float) -> _Window:
        window = self._windows.get(window_id) if isinstance(window_id, str) else None
        if window is None:
            raise PairingError("pairing_window_unknown")
        self._refresh(window, now)
        if window.state in {"expired", "revoked", "locked"}:
            raise PairingError(f"pairing_window_{window.state}")
        if not isinstance(code, str) or not secrets.compare_digest(code.encode(), window.code.encode()):
            window.failures += 1
            if window.failures >= _MAX_CODE_FAILURES:
                window.state = "locked"
                window.request = window.report = window.quota = window.handoff = None
                raise PairingError("pairing_window_locked")
            raise PairingError("pairing_window_code_invalid")
        return window

    def open(self, *, origin: str, seed_key_digest: str, ttl_seconds: int) -> dict[str, Any]:
        origin = _window_origin(origin)
        if not isinstance(seed_key_digest, str) or _DIGEST_RE.fullmatch(seed_key_digest) is None:
            raise PairingError("pairing_window_pin_invalid")
        if (isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int)
                or not _WINDOW_TTL_MIN <= ttl_seconds <= _WINDOW_TTL_MAX):
            raise PairingError("pairing_window_ttl_invalid")
        with self._lock:
            now = self._now()
            for window in self._windows.values():
                self._refresh(window, now)
            for key in [k for k, w in self._windows.items() if w.state in {"expired", "revoked", "locked"}]:
                if len(self._windows) >= _MAX_WINDOWS:
                    del self._windows[key]
            if sum(w.state in {"open", "claimed", "approved"} for w in self._windows.values()) >= _MAX_WINDOWS:
                raise PairingError("pairing_window_capacity")
            window_id = _random_text(4)
            while window_id in self._windows:
                window_id = _random_text(4)
            window = _Window(window_id=window_id, code=_random_text(8), origin=origin,
                             seed_key_digest=seed_key_digest, opened_at=now,
                             expires_at=now + ttl_seconds)
            self._windows[window_id] = window
            return self._owner_view(window, with_secret=True)

    def _owner_view(self, window: _Window, *, with_secret: bool = False) -> dict[str, Any]:
        view: dict[str, Any] = {"window_id": window.window_id, "state": window.state,
                                "opened_at": window.opened_at, "expires_at": window.expires_at}
        if with_secret or window.state == "open":
            code = window.code
            view["short_code"] = f"{window.window_id}-{code[:4]}-{code[4:]}"
            view["tunnel_name"] = window.origin.removeprefix("https://").removesuffix(".trycloudflare.com")
            view["join_link"] = encode_join_link({
                "protocol": JOIN_LINK_PROTOCOL, "origin": window.origin,
                "window_id": window.window_id, "code": code,
                "seed_key_digest": window.seed_key_digest, "expires_at": window.expires_at})
        if window.request is not None and window.state in {"claimed", "approved", "delivered"}:
            view["pending"] = {
                "node_id": window.request["node_id"],
                "peer_class": window.request["peer_class"],
                "fingerprint": identity_fingerprint(window.request["identity_key_digest"]),
                "request_digest": request_digest(window.request),
                "request_expires_at": window.request["expires_at"],
                "device": window.report["device"], "link": window.report["link"],
                "quota": dict(window.quota),
            }
        return view

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            now = self._now()
            for window in self._windows.values():
                self._refresh(window, now)
            return [self._owner_view(w) for w in sorted(self._windows.values(), key=lambda w: w.opened_at)]

    def revoke(self, window_id: str) -> dict[str, Any]:
        with self._lock:
            window = self._windows.get(window_id) if isinstance(window_id, str) else None
            if window is None:
                raise PairingError("pairing_window_unknown")
            window.state = "revoked"
            window.request = window.report = window.quota = window.handoff = None
            return self._owner_view(window)

    def submit(self, window_id: str, document: Any) -> dict[str, Any]:
        from .capability import verify_capability_link_report

        with self._lock:
            now = self._now()
            if not isinstance(document, Mapping) or set(document) != {"protocol", "code", "request", "report", "quota"} \
                    or document.get("protocol") != _SUBMISSION_PROTOCOL:
                # Unknown/closed windows answer first so a probe learns nothing more.
                self._live(window_id, document.get("code") if isinstance(document, Mapping) else None, now)
                raise PairingError("pairing_submission_invalid")
            window = self._live(window_id, document["code"], now)
            request = verify_pairing_request(document["request"], now=now)
            digest = request_digest(request)
            if window.state != "open":
                if window.request is not None and request_digest(window.request) == digest:
                    return self._waiting(window)
                stale = (window.state == "claimed" and window.request is not None
                         and window.request["identity_key_digest"] == request["identity_key_digest"]
                         and now > float(window.request["expires_at"]))
                if not stale:
                    raise PairingError("pairing_window_claimed")
            report = verify_capability_link_report(document["report"], now=now,
                expected_key_digest=request["identity_key_digest"], node_id=request["node_id"],
                purposes=frozenset({"join"}))
            quota = validate_quota(document["quota"])
            if quota["background_allowed"] is not False:
                raise PairingError("pairing_quota_invalid")
            window.request, window.report, window.quota = request, report, quota
            window.state = "claimed"
            return self._waiting(window)

    @staticmethod
    def _waiting(window: _Window) -> dict[str, Any]:
        return {"state": "waiting_for_owner",
                "fingerprint": identity_fingerprint(window.request["identity_key_digest"]),
                "request_digest": request_digest(window.request)}

    def approve(self, window_id: str, digest: str,
                issue: Callable[[dict[str, Any], dict[str, Any]], Mapping[str, Any]]) -> dict[str, Any]:
        """One owner click: mint the recipient-encrypted handoff for the claimant."""

        with self._lock:
            now = self._now()
            window = self._windows.get(window_id) if isinstance(window_id, str) else None
            if window is None:
                raise PairingError("pairing_window_unknown")
            self._refresh(window, now)
            if window.state in {"expired", "revoked", "locked"}:
                raise PairingError(f"pairing_window_{window.state}")
            if window.state == "open" or window.request is None:
                raise PairingError("pairing_window_not_claimed")
            if request_digest(window.request) != digest:
                raise PairingError("pairing_window_request_mismatch")
            if window.handoff is None:
                handoff = issue(dict(window.request), dict(window.quota))
                if not isinstance(handoff, Mapping):
                    raise PairingError("pairing_handoff_invalid")
                window.handoff = dict(handoff)
                window.state = "approved"
            return self._owner_view(window)

    def pending_request(self, window_id: str) -> dict[str, Any] | None:
        with self._lock:
            window = self._windows.get(window_id)
            return None if window is None or window.request is None else dict(window.request)

    def poll(self, window_id: str, document: Any) -> dict[str, Any]:
        with self._lock:
            now = self._now()
            code = document.get("code") if isinstance(document, Mapping) else None
            window = self._live(window_id, code, now)
            if (not isinstance(document, Mapping) or set(document) != {"protocol", "code", "request_digest"}
                    or document.get("protocol") != _POLL_PROTOCOL):
                raise PairingError("pairing_submission_invalid")
            if window.request is None or request_digest(window.request) != document["request_digest"]:
                raise PairingError("pairing_window_request_mismatch")
            if window.handoff is None:
                return {"state": "waiting_for_owner"}
            window.state = "delivered"
            return {"state": "approved", "handoff": dict(window.handoff)}

    def handle(self, method: str, path: str, body: bytes) -> tuple[int, bytes]:
        """Public HTTP surface reached through the owner's relay proxy."""

        def reply(status: int, value: Mapping[str, Any]) -> tuple[int, bytes]:
            return status, json.dumps(value, separators=(",", ":")).encode()

        rest = path[len(PAIRING_WINDOW_PREFIX):] if path.startswith(PAIRING_WINDOW_PREFIX) else ""
        window_id, _slash, action = rest.partition("/")
        if action not in {"request", "poll"}:
            return reply(404, {"error": "route_unknown"})
        if _WINDOW_ID_RE.fullmatch(window_id) is None:
            return reply(404, {"error": "pairing_window_unknown"})
        if method != "POST":
            return reply(405, {"error": "method_not_allowed"})
        try:
            document = _public_json_object(body)
        except (TypeError, ValueError, UnicodeError):
            return reply(400, {"error": "pairing_submission_invalid"})
        try:
            value = self.submit(window_id, document) if action == "request" else self.poll(window_id, document)
        except PairingError as exc:
            code = exc.code if _ERROR_CODE_RE.fullmatch(exc.code) else "pairing_submission_invalid"
            return reply(_WINDOW_STATUS.get(code, 400), {"error": code})
        except Exception:  # noqa: BLE001 - public surface: closed code only
            return reply(400, {"error": "pairing_submission_invalid"})
        return reply(200, value)


class JoinWindowClient:
    """Target-side HTTPS client for one owner's pairing window and link probe.

    Production: canonical ``https://`` origin, system Web-PKI trust, no
    redirects, Cloudflare DoH fallback for ``*.trycloudflare.com`` (the same
    opener the seed client uses).  ``_test_base_url`` is a deterministic
    local-test seam that sends the identical requests to a loopback relay.
    """

    def __init__(self, origin: str, *, timeout: float = 10.0, _test_base_url: str | None = None,
                 _clock: Callable[[], float] = time.time) -> None:
        self.origin = _window_origin(origin)
        self._timeout = float(timeout)
        self._clock = _clock
        #: Advisory only (never blocks a join): ``[{"code": "clock_skew", "skew_seconds": n}]``.
        self.warnings: list[dict[str, Any]] = []
        if _test_base_url is not None and not re.fullmatch(r"http://127\.0\.0\.1:[0-9]{1,5}", _test_base_url):
            raise ValueError("test base url is invalid")
        self._base = _test_base_url or self.origin
        if _test_base_url is None:
            from .public_https import public_opener

            self._opener = public_opener()
        else:
            from urllib.request import HTTPRedirectHandler, ProxyHandler, build_opener

            class _NoRedirect(HTTPRedirectHandler):
                def redirect_request(self, *args, **kwargs):  # noqa: ANN002, ANN003
                    return None

            self._opener = build_opener(ProxyHandler({}), _NoRedirect())

    def _note_clock(self, headers: Any) -> None:
        """Compare the server's HTTPS ``Date`` with this Mac's clock (advisory)."""
        try:
            from email.utils import parsedate_to_datetime

            value = headers.get("Date") if headers is not None else None
            skew = round(parsedate_to_datetime(value).timestamp() - self._clock())
        except Exception:  # noqa: BLE001 - missing/invalid Date is simply ignored
            return
        if abs(skew) > CLOCK_SKEW_WARNING_SECONDS:
            self.warnings = [{"code": "clock_skew", "skew_seconds": int(skew)}]
        else:
            self.warnings = []

    def post(self, path: str, body: bytes, *, content_type: str = "application/json") -> bytes:
        from urllib.error import HTTPError, URLError
        from urllib.request import Request

        from mycelium_http_identity import with_user_agent

        request = Request(self._base + path, data=body, method="POST",
                          headers=with_user_agent({"Content-Type": content_type}))
        try:
            with self._opener.open(request, timeout=self._timeout) as response:
                self._note_clock(getattr(response, "headers", None))
                return response.read(1024 * 1024 + 1)
        except HTTPError as exc:
            self._note_clock(getattr(exc, "headers", None))
            try:
                code = json.loads(exc.read(4096)).get("error")
                if isinstance(code, dict) and set(code) == {"code"}:
                    code = code['code']
            except Exception:  # noqa: BLE001 - remote body
                code = None
            finally:
                exc.close()
            if not isinstance(code, str) or _ERROR_CODE_RE.fullmatch(code) is None:
                code = "pairing_window_unreachable"
            raise PairingError(code) from None
        except (URLError, OSError, ValueError) as exc:
            raise _unreachable(exc) from exc

    def _json(self, path: str, document: Mapping[str, Any]) -> dict[str, Any]:
        raw = self.post(path, json.dumps(dict(document), separators=(",", ":")).encode())
        try:
            value = json.loads(raw)
        except ValueError as exc:
            raise PairingError("pairing_window_response_invalid") from exc
        if not isinstance(value, dict):
            raise PairingError("pairing_window_response_invalid")
        return value

    def submit(self, window_id: str, *, code: str, request: Mapping[str, Any],
               report: Mapping[str, Any], quota: Mapping[str, Any]) -> dict[str, Any]:
        return self._json(f"{PAIRING_WINDOW_PREFIX}{window_id}/request", {
            "protocol": _SUBMISSION_PROTOCOL, "code": code, "request": dict(request),
            "report": dict(report), "quota": dict(quota)})

    def poll(self, window_id: str, *, code: str, digest: str) -> dict[str, Any]:
        return self._json(f"{PAIRING_WINDOW_PREFIX}{window_id}/poll", {
            "protocol": _POLL_PROTOCOL, "code": code, "request_digest": digest})

    def probe(self, download_kib: int, upload: bytes) -> bytes:
        from .capability import LINK_PROBE_PATH

        return self.post(f"{LINK_PROBE_PATH}/{int(download_kib)}", upload,
                         content_type="application/octet-stream")

    def member_report(self, report: Mapping[str, Any]) -> dict[str, Any]:
        from .capability import MEMBER_REPORT_PATH

        return self._json(MEMBER_REPORT_PATH, report)


__all__ = [
    "CLOCK_SKEW_WARNING_SECONDS",
    "UNREACHABLE_CODES",
    "ENCRYPTED_HANDOFF_PROTOCOL",
    "JOIN_LINK_PROTOCOL",
    "JOIN_LINK_SCHEME",
    "JoinWindowClient",
    "PAIRING_REQUEST_PROTOCOL",
    "PAIRING_WINDOW_PREFIX",
    "PairingWindows",
    "PairingError",
    "approve_pairing_request",
    "decrypt_invitation_handoff",
    "encode_join_link",
    "identity_fingerprint",
    "parse_join_text",
    "request_digest",
    "new_pairing_request",
    "validate_quota",
    "verify_pairing_request",
]
