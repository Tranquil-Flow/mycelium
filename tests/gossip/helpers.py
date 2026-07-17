from __future__ import annotations

from typing import Any, Dict, Optional

from mycelium_gossip.schema import RecordKind, build_record


def profile_payload(node_id: str) -> Dict[str, Any]:
    return {
        "protocol": "mycelium.device_profile.v2",
        "node_id": node_id,
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
                "endpoint_id": "http-overlay",
                "transport": "http",
                "host": "100.64.0.1",
                "port": 9000,
                "scope": "overlay",
                "inbound": True,
            }
        ],
        "policy": {"available_for_swarm": True},
    }


def status_payload(node_id: str, lifecycle: str = "ready", free_bytes: int = 32 * 1024**3) -> Dict[str, Any]:
    return {
        "protocol": "mycelium.device_status.v1",
        "node_id": node_id,
        "lifecycle": lifecycle,
        "memory_domains": [
            {
                "memory_domain_id": "unified-0",
                "kind": "unified",
                "total_bytes": 48 * 1024**3,
                "allocatable_after_reservations_bytes": free_bytes,
                "committed_bytes": 8 * 1024**3,
                "reclaimable_bytes": 2 * 1024**3,
                "reservation_generation": 1,
            }
        ],
        "queue_depth": 0,
        "in_flight": 0,
        "concurrency_limit": 2,
    }


def link_payload(
    src: str,
    dst: str,
    src_endpoint: str = "http-overlay",
    dst_endpoint: str = "http-overlay",
    reachable: bool = True,
) -> Dict[str, Any]:
    return {
        "protocol": "mycelium.link_state.v1",
        "src_node_id": src,
        "dst_node_id": dst,
        "src_endpoint_id": src_endpoint,
        "dst_endpoint_id": dst_endpoint,
        "reachable": reachable,
        "connect_rtt_ema_ms": 4.0,
        "rtt_p95_ms": 6.0,
        "jitter_ms": 0.5,
        "loss_ratio": 0.0,
        "goodput_mbps": 200.0,
        "sample_count": 4,
        "measurement_method": "http-health",
    }


def offering_payload(node_id: str, assignment_id: str = "assignment-a") -> Dict[str, Any]:
    return {
        "protocol": "mycelium.runtime_offering.v1",
        "deployment_id": "deploy-1",
        "deployment_epoch": 1,
        "assignment_id": assignment_id,
        "manifest_digest": "sha256:" + "1" * 64,
        "resolved_commit": "a" * 40,
        "model_id": "model-a",
        "start_layer": 0,
        "end_layer_exclusive": 8,
        "runtime_instance_id": f"runtime-{node_id}",
        "load_generation": 1,
        "readiness_state": "loaded_and_probed",
        "proof_digest": "sha256:" + "2" * 64,
        "inference_endpoint_id": "http-overlay",
    }


def membership_payload(reporter: str, subject: str, state: str = "alive", incarnation: int = 1) -> Dict[str, Any]:
    return {
        "protocol": "mycelium.membership.v1",
        "subject_node_id": subject,
        "subject_incarnation": incarnation,
        "state": state,
        "reporter_node_id": reporter,
        "reason": "test",
    }


def make_record(
    kind: RecordKind,
    *,
    node_id: str = "node-a",
    sequence: int = 1,
    incarnation: int = 1,
    boot_id: Optional[str] = None,
    ttl_ms: int = 1_000,
    payload: Optional[Dict[str, Any]] = None,
    swarm_id: str = "swarm-a",
):
    if payload is None:
        if kind is RecordKind.PROFILE:
            payload = profile_payload(node_id)
        elif kind is RecordKind.STATUS:
            payload = status_payload(node_id)
        elif kind is RecordKind.LINK:
            payload = link_payload(node_id, "node-b")
        elif kind is RecordKind.OFFERING:
            payload = offering_payload(node_id)
        else:
            payload = membership_payload(node_id, node_id, incarnation=incarnation)
    return build_record(
        swarm_id=swarm_id,
        kind=kind,
        origin_node_id=node_id,
        incarnation=incarnation,
        sequence=sequence,
        boot_id=boot_id or f"boot-{node_id}-{incarnation}",
        generated_at_unix_ms=sequence * 100,
        ttl_ms=ttl_ms,
        payload=payload,
    )
