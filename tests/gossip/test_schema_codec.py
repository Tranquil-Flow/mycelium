from __future__ import annotations

import json

import pytest

from mycelium_gossip.codec import DecodeError, decode_record, encode_record
from mycelium_gossip.schema import (
    RecordKind,
    SchemaError,
    build_record,
    canonical_payload_hash,
    logical_record_key,
)


def profile_payload() -> dict:
    return {
        "protocol": "mycelium.device_profile.v2",
        "node_id": "node-a",
        "software_version": "0.1.0",
        "protocol_versions": ["mycelium.gossip.record.v1"],
        "platform": "darwin",
        "architecture": "arm64",
        "memory_domains": [
            {
                "memory_domain_id": "unified-0",
                "kind": "unified",
                "total_bytes": 48 * 1024**3,
            }
        ],
        "endpoints": [
            {
                "endpoint_id": "tailscale-http",
                "transport": "http",
                "host": "100.64.0.1",
                "port": 9000,
                "scope": "overlay",
                "inbound": True,
            }
        ],
        "policy": {"available_for_swarm": True},
    }


def offering_payload() -> dict:
    return {
        "protocol": "mycelium.runtime_offering.v1",
        "deployment_id": "deploy-1",
        "deployment_epoch": 4,
        "assignment_id": "assignment-a",
        "manifest_digest": "sha256:" + "1" * 64,
        "resolved_commit": "a" * 40,
        "model_id": "model-a",
        "start_layer": 8,
        "end_layer_exclusive": 16,
        "runtime_instance_id": "runtime-1",
        "load_generation": 7,
        "readiness_state": "loaded_and_probed",
        "proof_digest": "sha256:" + "2" * 64,
        "inference_endpoint_id": "tailscale-http",
    }


def test_canonical_hash_does_not_depend_on_mapping_order() -> None:
    left = {"b": 2, "a": {"y": 2, "x": 1}}
    right = {"a": {"x": 1, "y": 2}, "b": 2}
    assert canonical_payload_hash(left) == canonical_payload_hash(right)


def test_profile_round_trip_preserves_validated_record() -> None:
    record = build_record(
        swarm_id="swarm-a",
        kind=RecordKind.PROFILE,
        origin_node_id="node-a",
        incarnation=3,
        sequence=9,
        boot_id="boot-a",
        generated_at_unix_ms=1_000,
        ttl_ms=60_000,
        payload=profile_payload(),
    )

    encoded = encode_record(record)
    decoded = decode_record(encoded)

    assert decoded == record
    assert decoded.payload_hash == canonical_payload_hash(profile_payload())


def test_profile_rejects_secret_bearing_fields() -> None:
    payload = profile_payload()
    payload["auth_key"] = "must-not-leak"

    with pytest.raises(SchemaError, match="unknown field|forbidden"):
        build_record(
            swarm_id="swarm-a",
            kind=RecordKind.PROFILE,
            origin_node_id="node-a",
            incarnation=1,
            sequence=1,
            boot_id="boot-a",
            generated_at_unix_ms=1,
            ttl_ms=1_000,
            payload=payload,
        )


def test_ids_must_be_safe_zenoh_key_segments() -> None:
    with pytest.raises(SchemaError, match="swarm_id"):
        build_record(
            swarm_id="swarm/**",
            kind=RecordKind.PROFILE,
            origin_node_id="node-a",
            incarnation=1,
            sequence=1,
            boot_id="boot-a",
            generated_at_unix_ms=1,
            ttl_ms=1_000,
            payload=profile_payload(),
        )


def test_link_logical_key_distinguishes_endpoint_pairs() -> None:
    def link(src_endpoint: str, dst_endpoint: str):
        return build_record(
            swarm_id="swarm-a",
            kind=RecordKind.LINK,
            origin_node_id="node-a",
            incarnation=1,
            sequence=1,
            boot_id="boot-a",
            generated_at_unix_ms=1,
            ttl_ms=5_000,
            payload={
                "protocol": "mycelium.link_state.v1",
                "src_node_id": "node-a",
                "dst_node_id": "node-b",
                "src_endpoint_id": src_endpoint,
                "dst_endpoint_id": dst_endpoint,
                "reachable": True,
                "connect_rtt_ema_ms": 4.0,
                "rtt_p95_ms": 6.0,
                "jitter_ms": 0.5,
                "loss_ratio": 0.0,
                "goodput_mbps": 200.0,
                "sample_count": 4,
                "measurement_method": "http-health",
            },
        )

    lan = link("lan-http", "lan-http")
    overlay = link("tailscale-http", "tailscale-http")

    assert logical_record_key(lan) != logical_record_key(overlay)
    assert logical_record_key(lan)[-3:] == ("lan-http", "node-b", "lan-http")


def test_offering_requires_assignment_bound_readiness() -> None:
    payload = offering_payload()
    del payload["proof_digest"]

    with pytest.raises(SchemaError, match="proof_digest"):
        build_record(
            swarm_id="swarm-a",
            kind=RecordKind.OFFERING,
            origin_node_id="node-a",
            incarnation=1,
            sequence=1,
            boot_id="boot-a",
            generated_at_unix_ms=1,
            ttl_ms=5_000,
            payload=payload,
        )


def test_offering_rejects_empty_half_open_range() -> None:
    payload = offering_payload()
    payload["end_layer_exclusive"] = payload["start_layer"]

    with pytest.raises(SchemaError, match="half-open"):
        build_record(
            swarm_id="swarm-a",
            kind=RecordKind.OFFERING,
            origin_node_id="node-a",
            incarnation=1,
            sequence=1,
            boot_id="boot-a",
            generated_at_unix_ms=1,
            ttl_ms=5_000,
            payload=payload,
        )


def test_decode_rejects_payload_hash_tampering() -> None:
    record = build_record(
        swarm_id="swarm-a",
        kind=RecordKind.PROFILE,
        origin_node_id="node-a",
        incarnation=1,
        sequence=1,
        boot_id="boot-a",
        generated_at_unix_ms=1,
        ttl_ms=1_000,
        payload=profile_payload(),
    )
    raw = json.loads(encode_record(record))
    raw["payload"]["architecture"] = "x86_64"

    with pytest.raises(DecodeError, match="payload hash"):
        decode_record(json.dumps(raw).encode())


def test_decode_rejects_duplicate_json_keys() -> None:
    with pytest.raises(DecodeError, match="duplicate"):
        decode_record(b'{"protocol":"x","protocol":"y"}')


def test_decode_rejects_oversized_record_before_parsing() -> None:
    with pytest.raises(DecodeError, match="too large"):
        decode_record(b"{" + b" " * 70_000 + b"}")
