# SPDX-License-Identifier: AGPL-3.0-only
"""A2 artifact authority for phone stage objects (grant, chunk request, receipt)."""

from __future__ import annotations

import copy
import json
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import pytest

from mycelium_mobile import artifact_grant as ag
from mycelium_qualification.signing import Ed25519EvidenceSigner

ROOT = Path(__file__).resolve().parents[2]
V2 = json.loads((ROOT / "native/mobile_core/tests/fixtures/qwen2_stage_manifest_v2.json").read_text())
NOW = 1_790_000_000_000
PHONE = "a" * 64
SOURCE = "c" * 64


def _signer(byte: int, endpoint: str) -> Ed25519EvidenceSigner:
    key = Ed25519PrivateKey.from_private_bytes(bytes([byte]) * 32)
    return Ed25519EvidenceSigner(endpoint_id=endpoint, _private_key=key, _public_key_bytes=key.public_key().public_bytes_raw())


SEED = _signer(0x22, "seed-endpoint-1")
MEMBER = _signer(0x11, PHONE)
SOURCE_KEY = _signer(0x33, SOURCE)
SIZES = {t["sha256"]: t["size_bytes"] for t in V2["manifest"]["tensors"]}
FIRST = V2["manifest"]["tensors"][0]


def _grant(**overrides):
    args = dict(
        grant_id="grant-1", swarm_id="swarm-fixture", seed_node_id="seed-node-1",
        recipient_node_id="node-phone-1", recipient_endpoint_id=PHONE,
        recipient_key_digest=MEMBER.verification_key_digest, assignment_id="route-fixture-1",
        manifest=V2["manifest"],
        sources=[{"endpoint_id": SOURCE, "verification_key_digest": SOURCE_KEY.verification_key_digest}],
        now_unix_ms=NOW,
    )
    args.update(overrides)
    return ag.issue_grant(SEED, **args)


def _request(grant, signer=MEMBER, **overrides):
    args = dict(grant=grant, request_id="req-1", request_nonce="nonce-1", source_endpoint_id=SOURCE,
                object_digest=FIRST["sha256"], offset_bytes=0, length_bytes=FIRST["size_bytes"], now_unix_ms=NOW + 1000)
    args.update(overrides)
    return ag.sign_chunk_request(signer, **args)


def _source_verify(request, *, now=NOW + 1000, sizes=SIZES, key_digest=SOURCE_KEY.verification_key_digest):
    return ag.verify_chunk_request(request, seed_key_record=SEED.public_key_record(), source_endpoint_id=SOURCE,
                                   source_key_digest=key_digest, object_sizes=sizes, now_unix_ms=now)


def _code(fn, *args, **kwargs) -> str:
    with pytest.raises(ag.ArtifactGrantError) as caught:
        fn(*args, **kwargs)
    return caught.value.code


def test_the_grant_covers_exactly_the_manifest_objects_and_its_window() -> None:
    grant = _grant()
    message = ag.verify_grant(grant, seed_key_record=SEED.public_key_record(), now_unix_ms=NOW)
    assert message["allowed_object_digests"] == sorted(SIZES)
    assert message["manifest_pack_digest"] == V2["manifest"]["pack_digest"]
    assert message["maximum_total_bytes"] == sum(SIZES.values())
    assert message["expires_at_unix_ms"] == NOW + ag.DEFAULT_GRANT_TTL_MS
    seed = SEED.public_key_record()
    assert _code(ag.verify_grant, grant, seed_key_record=_signer(0x44, "x").public_key_record(), now_unix_ms=NOW) == "artifact_grant_signer_mismatch"
    late = NOW + ag.DEFAULT_GRANT_TTL_MS + ag.CLOCK_SKEW_MS + 1
    assert _code(ag.verify_grant, grant, seed_key_record=seed, now_unix_ms=late) == "artifact_grant_ineligible"
    assert ag.verify_grant(grant, seed_key_record=seed, now_unix_ms=NOW - ag.CLOCK_SKEW_MS)  # phone clock behind
    tampered = copy.deepcopy(grant)
    tampered["message"]["allowed_object_digests"].append("sha256:" + "f" * 64)
    assert _code(ag.verify_grant, tampered, seed_key_record=seed, now_unix_ms=NOW) == "artifact_grant_signature_invalid"
    for bad in [dict(ttl_ms=0), dict(ttl_ms=ag.MAX_GRANT_TTL_MS + 1), dict(recipient_endpoint_id="phone id"),
                dict(sources=[{"endpoint_id": PHONE, "verification_key_digest": SOURCE_KEY.verification_key_digest}]),
                dict(maximum_chunk_bytes=ag.MAX_CHUNK_BYTES + 1),
                dict(sources=[{"endpoint_id": SOURCE, "verification_key_digest": SOURCE_KEY.verification_key_digest,
                               "direct_addrs": ["127.0.0.01:80"]}])]:
        assert _code(_grant, **bad) == "artifact_grant_invalid", bad


def test_direct_address_hints_are_canonical_socket_addresses() -> None:
    assert ag.canonical_socket_addr("10.0.0.2:4433") == "10.0.0.2:4433"
    assert ag.canonical_socket_addr("[fe80::1]:9") == "[fe80::1]:9"
    for bad in ["10.0.0.2", "host:1", "10.0.0.2:0", "10.0.0.2:65536", "10.0.0.2:080", "fe80::1:9", 7]:
        assert ag.canonical_socket_addr(bad) != bad, bad  # not a canonical hint
    grant = _grant(sources=[{"endpoint_id": SOURCE, "verification_key_digest": SOURCE_KEY.verification_key_digest,
                             "direct_addrs": ["10.0.0.2:4433"]}])
    assert ag.verify_grant(grant, seed_key_record=SEED.public_key_record(), now_unix_ms=NOW)["authorized_sources"][0]["direct_addrs"] == ["10.0.0.2:4433"]


def test_a_source_serves_only_the_granted_recipient_and_ranges() -> None:
    grant = _grant()
    request, message = _source_verify(_request(grant))
    assert request["object_digest"] == FIRST["sha256"] and message["grant_id"] == "grant-1"
    # Signed by another member key (even claiming the phone endpoint).
    assert _code(_source_verify, _request(grant, signer=_signer(0x55, PHONE))) == "artifact_chunk_request_unauthorized"
    # Re-signed after tampering with the range.
    forged = _request(grant)
    forged["message"]["length_bytes"] = 1
    assert _code(_source_verify, forged) == "artifact_chunk_request_signature_invalid"
    # Another source, an object outside the grant, a range past the object end,
    # a source not holding the object, an over-long chunk, a stale request.
    assert _code(_source_verify, _request(grant), key_digest="sha256:" + "0" * 64) == "artifact_chunk_request_unauthorized"
    assert _code(_source_verify, _request(grant, object_digest="sha256:" + "e" * 64)) == "artifact_chunk_request_unauthorized"
    assert _code(_source_verify, _request(grant, offset_bytes=1)) == "artifact_chunk_range_invalid"
    assert _code(_source_verify, _request(grant), sizes={}) == "artifact_object_unavailable"
    assert _code(_source_verify, _request(grant, length_bytes=ag.MAX_CHUNK_BYTES + 1)) == "artifact_chunk_request_invalid"
    assert _code(_source_verify, _request(grant, now_unix_ms=NOW - 200_000)) == "artifact_chunk_request_ineligible"
    # A grant from an impostor seed embedded in an otherwise valid request.
    impostor = ag.issue_grant(_signer(0x66, "seed-endpoint-1"), **{
        "grant_id": "grant-1", "swarm_id": "swarm-fixture", "seed_node_id": "seed-node-1",
        "recipient_node_id": "node-phone-1", "recipient_endpoint_id": PHONE,
        "recipient_key_digest": MEMBER.verification_key_digest, "assignment_id": "route-fixture-1",
        "manifest": V2["manifest"],
        "sources": [{"endpoint_id": SOURCE, "verification_key_digest": SOURCE_KEY.verification_key_digest}],
        "now_unix_ms": NOW})
    assert _code(_source_verify, _request(impostor)) == "artifact_grant_signer_mismatch"


def test_the_receipt_binds_the_request_and_the_exact_bytes() -> None:
    grant = _grant()
    request, message = _source_verify(_request(grant, length_bytes=16))
    data = bytes(range(16))
    receipt = ag.sign_chunk_receipt(SOURCE_KEY, request=request, grant=message, data=data, now_unix_ms=NOW + 2000)
    assert ag.verify_chunk_receipt(receipt, request=request, grant=message, data=data)["range_content_digest"] == ag.range_digest(data)
    assert _code(ag.verify_chunk_receipt, receipt, request=request, grant=message, data=data[:-1] + b"\xff") == "artifact_chunk_receipt_mismatch"
    other = ag.sign_chunk_receipt(_signer(0x77, SOURCE), request=request, grant=message, data=data, now_unix_ms=NOW)
    assert _code(ag.verify_chunk_receipt, other, request=request, grant=message, data=data) == "artifact_chunk_receipt_signer_mismatch"
    short = ag.sign_chunk_receipt(SOURCE_KEY, request=request, grant=message, data=data[:8], now_unix_ms=NOW)
    assert ag.verify_chunk_receipt(short, request=request, grant=message, data=data[:8])["length_bytes"] == 8
