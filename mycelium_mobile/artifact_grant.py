# SPDX-License-Identifier: AGPL-3.0-only
"""A2 artifact authority for phone stage objects, carried over iroh.

The phone acquires the per-tensor objects its seed-signed v2 stage manifest
pins (``qwen_stage_manifest.py``). The authority model is A2's
(``mycelium_swarm_artifacts.py``): an assignment-scoped grant signed by the
provisioner, a recipient-signed chunk request that embeds the grant, and a
source-signed receipt carrying the digest of the bytes served. Three things
differ, because the A2 originals cannot serve an off-tailnet phone:

* the provisioner is the seed, and the grant is delivered inside the
  seed-signed route-assignment answer (no SSH or Python on the phone);
* the grant binds the recipient's node id, iroh endpoint id and enrolled key
  digest, not its membership generation (which changes on every resume);
* objects are addressed by the SHA-256 the signed manifest pins, so no
  stream manifest or Merkle proof is needed.

All three documents are ``mycelium.membership.signed_message.v1`` envelopes
(the seed/member signing scheme both Python and Rust already verify), with
integer millisecond timestamps (no floats) and a 30 s clock-skew allowance.
The carrier is the iroh ALPN ``mycelium.artifact_chunk.v1``
(``native/mobile_core/src/artifact_net.rs``).
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, Mapping, NoReturn, Sequence

SIGNED_MESSAGE_PROTOCOL = "mycelium.membership.signed_message.v1"
GRANT_PROTOCOL = "mycelium.mobile_artifact_grant.v1"
CHUNK_REQUEST_PROTOCOL = "mycelium.mobile_artifact_chunk_request.v1"
CHUNK_RECEIPT_PROTOCOL = "mycelium.mobile_artifact_chunk_receipt.v1"
ARTIFACT_CHUNK_ALPN = "mycelium.artifact_chunk.v1"
#: A2's member-transport grant lifetime (``MEMBER_TRANSPORT_GRANT_TTL_MS``).
DEFAULT_GRANT_TTL_MS = 900_000
MAX_GRANT_TTL_MS = 6 * 3_600_000
MAX_REQUEST_TTL_MS = 120_000
CLOCK_SKEW_MS = 30_000
#: One range per request; below the 16 MiB iroh frame cap.
MAX_CHUNK_BYTES = 4 << 20
MAX_OBJECTS = 4096
MAX_SOURCES = 16
MAX_SAFE_INTEGER = (1 << 53) - 1

_ENVELOPE_FIELDS = frozenset({"protocol", "message", "signature", "verification_key"})
_GRANT_FIELDS = frozenset(
    {
        "protocol",
        "grant_id",
        "swarm_id",
        "seed_node_id",
        "recipient_node_id",
        "recipient_endpoint_id",
        "recipient_key_digest",
        "assignment_id",
        "manifest_pack_digest",
        "allowed_object_digests",
        "authorized_sources",
        "maximum_total_bytes",
        "maximum_bytes_per_second",
        "maximum_chunk_bytes",
        "issued_at_unix_ms",
        "not_before_unix_ms",
        "expires_at_unix_ms",
    }
)
_SOURCE_FIELDS = frozenset({"endpoint_id", "verification_key_digest", "direct_addrs"})
#: Direct-address hints per source (``ip:port``; iroh still authenticates the
#: endpoint id, so a wrong hint only fails to connect).
MAX_DIRECT_ADDRS = 8
_REQUEST_FIELDS = frozenset(
    {
        "protocol",
        "request_id",
        "request_nonce",
        "grant",
        "source_endpoint_id",
        "object_digest",
        "offset_bytes",
        "length_bytes",
        "issued_at_unix_ms",
        "expires_at_unix_ms",
    }
)
_RECEIPT_FIELDS = frozenset(
    {
        "protocol",
        "request_id",
        "grant_id",
        "source_endpoint_id",
        "recipient_node_id",
        "object_digest",
        "offset_bytes",
        "length_bytes",
        "range_content_digest",
        "responded_at_unix_ms",
    }
)
_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_ENDPOINT_RE = re.compile(r"[0-9a-f]{64}\Z")
_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")


class ArtifactGrantError(ValueError):
    """Fail-closed refusal with a stable public code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _fail(code: str) -> NoReturn:
    raise ArtifactGrantError(code)


def _closed(value: Any, fields: frozenset[str], code: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != fields:
        _fail(code)
    return value


def _match(value: Any, pattern: re.Pattern[str], code: str) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        _fail(code)
    return value


def _int(value: Any, code: str, *, minimum: int = 0, maximum: int = MAX_SAFE_INTEGER) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        _fail(code)
    return value


def _sorted_unique(values: Any, code: str, *, maximum: int) -> list[Any]:
    if not isinstance(values, list) or not values or len(values) > maximum:
        _fail(code)
    return values


def _envelope(signer: Any, message: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "protocol": SIGNED_MESSAGE_PROTOCOL,
        "message": dict(message),
        "signature": signer.sign(dict(message)),
        "verification_key": signer.public_key_record(),
    }


def _open(envelope: Any, protocol: str, code: str) -> tuple[Mapping[str, Any], Mapping[str, Any], Mapping[str, Any]]:
    value = _closed(envelope, _ENVELOPE_FIELDS, code)
    if value["protocol"] != SIGNED_MESSAGE_PROTOCOL:
        _fail(code)
    message = value["message"]
    if not isinstance(message, Mapping) or message.get("protocol") != protocol:
        _fail(code)
    if not isinstance(value["signature"], Mapping) or not isinstance(value["verification_key"], Mapping):
        _fail(code)
    return message, value["signature"], value["verification_key"]


def _verify_signature(message: Mapping[str, Any], signature: Mapping[str, Any], key: Mapping[str, Any], code: str) -> None:
    from mycelium_qualification.evidence import canonical_json_bytes  # noqa: PLC0415
    from mycelium_qualification.signing import build_ed25519_verifier  # noqa: PLC0415

    try:
        valid = build_ed25519_verifier([dict(key)])(canonical_json_bytes(dict(message)), dict(signature))
    except Exception:  # noqa: BLE001 - any verifier failure is a refusal
        valid = False
    if not valid:
        _fail(code)


def canonical_socket_addr(value: Any) -> str | None:
    """``ip:port`` / ``[ipv6]:port`` in Rust's ``SocketAddr`` display form."""

    import ipaddress  # noqa: PLC0415

    if not isinstance(value, str) or ":" not in value:
        return None
    host, _, port = value.rpartition(":")
    if not port.isdigit() or not 0 < int(port) < 65536 or str(int(port)) != port:
        return None
    try:
        ip = ipaddress.ip_address(host[1:-1] if host.startswith("[") and host.endswith("]") else host)
    except ValueError:
        return None
    return f"[{ip}]:{port}" if ip.version == 6 else f"{ip}:{port}"


def range_digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def issue_grant(
    seed_signer: Any,
    *,
    grant_id: str,
    swarm_id: str,
    seed_node_id: str,
    recipient_node_id: str,
    recipient_endpoint_id: str,
    recipient_key_digest: str,
    assignment_id: str,
    manifest: Mapping[str, Any],
    sources: Sequence[Mapping[str, str]],
    now_unix_ms: int,
    ttl_ms: int = DEFAULT_GRANT_TTL_MS,
    maximum_bytes_per_second: int = 32 << 20,
    maximum_chunk_bytes: int = MAX_CHUNK_BYTES,
) -> dict[str, Any]:
    """Seed-sign a grant for exactly the objects one verified v2 manifest pins."""

    from mycelium_mobile import qwen_stage_manifest as qm  # noqa: PLC0415

    verified = qm.verify_stage_manifest(manifest)
    digests = sorted({t["sha256"] for t in verified["tensors"]})
    message = {
        "protocol": GRANT_PROTOCOL,
        "grant_id": grant_id,
        "swarm_id": swarm_id,
        "seed_node_id": seed_node_id,
        "recipient_node_id": recipient_node_id,
        "recipient_endpoint_id": recipient_endpoint_id,
        "recipient_key_digest": recipient_key_digest,
        "assignment_id": assignment_id,
        "manifest_pack_digest": verified["pack_digest"],
        "allowed_object_digests": digests,
        "authorized_sources": sorted(
            ({"direct_addrs": [], **dict(s)} for s in sources), key=lambda s: s["endpoint_id"]
        ),
        # Each object once; a resumed range is not counted twice by an honest
        # source, so the exact sum is the budget.
        "maximum_total_bytes": sum({t["sha256"]: t["size_bytes"] for t in verified["tensors"]}.values()),
        "maximum_bytes_per_second": maximum_bytes_per_second,
        "maximum_chunk_bytes": maximum_chunk_bytes,
        "issued_at_unix_ms": now_unix_ms,
        "not_before_unix_ms": now_unix_ms,
        "expires_at_unix_ms": now_unix_ms + ttl_ms,
    }
    _check_grant_shape(message)
    return _envelope(seed_signer, message)


def _check_grant_shape(g: Mapping[str, Any]) -> None:
    code = "artifact_grant_invalid"
    _closed(g, _GRANT_FIELDS, code)
    for field in ("grant_id", "swarm_id", "seed_node_id", "recipient_node_id", "assignment_id"):
        _match(g[field], _ID_RE, code)
    # The member's enrolled endpoint id (an iroh id on a phone; any seed
    # segment in general); sources are iroh endpoints this node dials.
    _match(g["recipient_endpoint_id"], _ID_RE, code)
    _match(g["recipient_key_digest"], _DIGEST_RE, code)
    _match(g["manifest_pack_digest"], _DIGEST_RE, code)
    objects = _sorted_unique(g["allowed_object_digests"], code, maximum=MAX_OBJECTS)
    for digest in objects:
        _match(digest, _DIGEST_RE, code)
    if objects != sorted(set(objects)):
        _fail(code)
    sources = _sorted_unique(g["authorized_sources"], code, maximum=MAX_SOURCES)
    for source in sources:
        _closed(source, _SOURCE_FIELDS, code)
        _match(source["endpoint_id"], _ENDPOINT_RE, code)
        _match(source["verification_key_digest"], _DIGEST_RE, code)
        addrs = source["direct_addrs"]
        if not isinstance(addrs, list) or len(addrs) > MAX_DIRECT_ADDRS:
            _fail(code)
        for addr in addrs:
            if canonical_socket_addr(addr) != addr:
                _fail(code)
    ids = [s["endpoint_id"] for s in sources]
    if ids != sorted(set(ids)) or g["recipient_endpoint_id"] in ids:
        _fail(code)
    _int(g["maximum_total_bytes"], code, minimum=1)
    _int(g["maximum_bytes_per_second"], code, minimum=1)
    _int(g["maximum_chunk_bytes"], code, minimum=1, maximum=MAX_CHUNK_BYTES)
    issued = _int(g["issued_at_unix_ms"], code, minimum=1)
    not_before = _int(g["not_before_unix_ms"], code, minimum=1)
    expires = _int(g["expires_at_unix_ms"], code, minimum=1)
    if issued > not_before or not not_before < expires <= not_before + MAX_GRANT_TTL_MS:
        _fail(code)


def verify_grant(envelope: Any, *, seed_key_record: Mapping[str, Any], now_unix_ms: int) -> dict[str, Any]:
    """The grant must be signed by the pinned seed key and be within its window."""

    message, signature, key = _open(envelope, GRANT_PROTOCOL, "artifact_grant_invalid")
    _check_grant_shape(message)
    if dict(key) != dict(seed_key_record):
        _fail("artifact_grant_signer_mismatch")
    _verify_signature(message, signature, key, "artifact_grant_signature_invalid")
    if not message["not_before_unix_ms"] - CLOCK_SKEW_MS <= now_unix_ms <= message["expires_at_unix_ms"] + CLOCK_SKEW_MS:
        _fail("artifact_grant_ineligible")
    return dict(message)


def sign_chunk_request(
    member_signer: Any,
    *,
    grant: Mapping[str, Any],
    request_id: str,
    request_nonce: str,
    source_endpoint_id: str,
    object_digest: str,
    offset_bytes: int,
    length_bytes: int,
    now_unix_ms: int,
    ttl_ms: int = 60_000,
) -> dict[str, Any]:
    return _envelope(
        member_signer,
        {
            "protocol": CHUNK_REQUEST_PROTOCOL,
            "request_id": request_id,
            "request_nonce": request_nonce,
            "grant": dict(grant),
            "source_endpoint_id": source_endpoint_id,
            "object_digest": object_digest,
            "offset_bytes": offset_bytes,
            "length_bytes": length_bytes,
            "issued_at_unix_ms": now_unix_ms,
            "expires_at_unix_ms": now_unix_ms + ttl_ms,
        },
    )


def verify_chunk_request(
    envelope: Any,
    *,
    seed_key_record: Mapping[str, Any],
    source_endpoint_id: str,
    source_key_digest: str,
    object_sizes: Mapping[str, int],
    now_unix_ms: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Source side: the request is the grant's recipient asking this source,
    within both windows, for an allowed object range the source holds."""

    code = "artifact_chunk_request_invalid"
    message, signature, key = _open(envelope, CHUNK_REQUEST_PROTOCOL, code)
    _closed(message, _REQUEST_FIELDS, code)
    _match(message["request_id"], _ID_RE, code)
    _match(message["request_nonce"], _ID_RE, code)
    _match(message["source_endpoint_id"], _ENDPOINT_RE, code)
    digest = _match(message["object_digest"], _DIGEST_RE, code)
    offset = _int(message["offset_bytes"], code)
    length = _int(message["length_bytes"], code, minimum=1, maximum=MAX_CHUNK_BYTES)
    issued = _int(message["issued_at_unix_ms"], code, minimum=1)
    expires = _int(message["expires_at_unix_ms"], code, minimum=1)
    grant = verify_grant(message["grant"], seed_key_record=seed_key_record, now_unix_ms=now_unix_ms)
    # The request is signed by exactly the recipient the grant names.
    if key.get("verification_key_digest") != grant["recipient_key_digest"] or signature.get("signer_endpoint_id") != grant["recipient_endpoint_id"]:
        _fail("artifact_chunk_request_unauthorized")
    _verify_signature(message, signature, key, "artifact_chunk_request_signature_invalid")
    if (
        expires <= issued
        or expires - issued > MAX_REQUEST_TTL_MS
        or issued > now_unix_ms + CLOCK_SKEW_MS
        or expires < now_unix_ms - CLOCK_SKEW_MS
    ):
        _fail("artifact_chunk_request_ineligible")
    sources = {s["endpoint_id"]: s["verification_key_digest"] for s in grant["authorized_sources"]}
    if (
        message["source_endpoint_id"] != source_endpoint_id
        or sources.get(source_endpoint_id) != source_key_digest
        or digest not in grant["allowed_object_digests"]
        or length > grant["maximum_chunk_bytes"]
    ):
        _fail("artifact_chunk_request_unauthorized")
    size = object_sizes.get(digest)
    if size is None:
        _fail("artifact_object_unavailable")
    if offset + length > size:
        _fail("artifact_chunk_range_invalid")
    return dict(message), grant


def sign_chunk_receipt(source_signer: Any, *, request: Mapping[str, Any], grant: Mapping[str, Any], data: bytes, now_unix_ms: int) -> dict[str, Any]:
    return _envelope(
        source_signer,
        {
            "protocol": CHUNK_RECEIPT_PROTOCOL,
            "request_id": request["request_id"],
            "grant_id": grant["grant_id"],
            "source_endpoint_id": request["source_endpoint_id"],
            "recipient_node_id": grant["recipient_node_id"],
            "object_digest": request["object_digest"],
            "offset_bytes": request["offset_bytes"],
            "length_bytes": len(data),
            "range_content_digest": range_digest(data),
            "responded_at_unix_ms": now_unix_ms,
        },
    )


def verify_chunk_receipt(envelope: Any, *, request: Mapping[str, Any], grant: Mapping[str, Any], data: bytes) -> dict[str, Any]:
    """Recipient side: signed by the authorized source key, bound to this
    request, and digesting exactly the bytes received."""

    code = "artifact_chunk_receipt_invalid"
    message, signature, key = _open(envelope, CHUNK_RECEIPT_PROTOCOL, code)
    _closed(message, _RECEIPT_FIELDS, code)
    sources = {s["endpoint_id"]: s["verification_key_digest"] for s in grant["authorized_sources"]}
    if sources.get(request["source_endpoint_id"]) != key.get("verification_key_digest"):
        _fail("artifact_chunk_receipt_signer_mismatch")
    _verify_signature(message, signature, key, "artifact_chunk_receipt_signature_invalid")
    if (
        message["request_id"] != request["request_id"]
        or message["grant_id"] != grant["grant_id"]
        or message["source_endpoint_id"] != request["source_endpoint_id"]
        or message["recipient_node_id"] != grant["recipient_node_id"]
        or message["object_digest"] != request["object_digest"]
        or message["offset_bytes"] != request["offset_bytes"]
        or message["length_bytes"] != len(data)
        or not 0 < len(data) <= request["length_bytes"]
        or message["range_content_digest"] != range_digest(data)
    ):
        _fail("artifact_chunk_receipt_mismatch")
    return dict(message)
