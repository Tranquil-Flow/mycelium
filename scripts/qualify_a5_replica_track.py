#!/usr/bin/env python3
"""Physically prequalify one complete multi-stage A5 replica track.

This is an offline qualifier, not the final A5 product gate. It opens the real
physical route, renews the incumbent deployment qualification, then executes
the incumbent and replica tracks through the production M16 LiveRouterPort
with exact per-track exclusions. Only measured parity, signed snapshots,
transport movement, and zero-resource cleanup may yield route_ready=true.
The emitted replica qualification can then be installed into the ordinary
product gateway for the required positive and negative A5 gates.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mycelium_live.router_port import LiveRouterPort  # noqa: E402
from mycelium_live.supervisor import _qualified_runtime  # noqa: E402
from mycelium_m16_runtime import build_live_m16_runtime  # noqa: E402
from mycelium_qualification.contracts import (  # noqa: E402
    route_qualification_to_dict,
)
from mycelium_qualification.live import issue_live_route_qualification  # noqa: E402
from mycelium_qualification.replica import (  # noqa: E402
    ReplicaQualificationInput,
    qualify_replica_track,
)
from mycelium_request_gateway.contracts import qualification_binding  # noqa: E402
from mycelium_router.contracts import RequestContext  # noqa: E402


@dataclass(frozen=True)
class TrackPair:
    incumbent_placement_ids: tuple[str, ...]
    replica_placement_ids: tuple[str, ...]
    replica_placement_id: str
    replica_group_id: str
    replica_node_ids: frozenset[str]
    replica_load_proof_digest: str


class _CaptureSink:
    def __init__(self) -> None:
        self.tokens: list[int] = []

    def emit(self, token_index: int, token_id: int) -> None:
        if token_index != len(self.tokens):
            raise RuntimeError("physical_output_index_invalid")
        self.tokens.append(token_id)


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _digest(value: object) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value)).hexdigest()


def _atomic_json(path: Path, document: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_bytes(_canonical(document))
    temporary.replace(path)


def derive_track_pair(graph: Any) -> TrackPair:
    """Derive one incumbent/replica pair spanning every ordered stage."""

    stages = tuple(getattr(graph, "stages", ()))
    if len(stages) < 2:
        raise ValueError("a5_requires_multistage_graph")
    replicated = [
        (index, stage)
        for index, stage in enumerate(stages)
        if len(tuple(getattr(stage, "placements", ()))) >= 2
    ]
    if len(replicated) != 1:
        raise ValueError("a5_prequalifier_requires_one_replicated_stage_range")
    replica_stage_index, replica_stage = replicated[0]
    replica_stage_placements = tuple(replica_stage.placements)
    if len(replica_stage_placements) != 2:
        raise ValueError("a5_prequalifier_requires_exact_replica_pair")
    incumbent_replica, candidate_replica = replica_stage_placements
    if (
        incumbent_replica.replica_group_id != candidate_replica.replica_group_id
        or incumbent_replica.stage_signature != candidate_replica.stage_signature
        or incumbent_replica.node_id == candidate_replica.node_id
    ):
        raise ValueError("replica_stage_identity_mismatch")

    incumbent = tuple(stage.placements[0].placement_id for stage in stages)
    candidate = list(incumbent)
    candidate[replica_stage_index] = candidate_replica.placement_id
    replica = tuple(candidate)
    if incumbent == replica or len(set(replica)) != len(replica):
        raise ValueError("replica_complete_track_invalid")
    placement_by_id = {
        placement.placement_id: placement
        for stage in stages
        for placement in stage.placements
    }
    return TrackPair(
        incumbent_placement_ids=incumbent,
        replica_placement_ids=replica,
        replica_placement_id=candidate_replica.placement_id,
        replica_group_id=candidate_replica.replica_group_id,
        replica_node_ids=frozenset(
            placement_by_id[placement_id].node_id for placement_id in replica
        ),
        replica_load_proof_digest=candidate_replica.load_proof_digest,
    )


def _latest_snapshots(attestation: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    snapshots: dict[str, Mapping[str, Any]] = {}
    envelopes = attestation.get("signed_observations")
    if not isinstance(envelopes, list):
        return snapshots
    for envelope in envelopes:
        if not isinstance(envelope, Mapping):
            continue
        observation = envelope.get("observation")
        if (
            isinstance(observation, Mapping)
            and observation.get("event") == "snapshot"
            and isinstance(observation.get("node_id"), str)
        ):
            snapshots[str(observation["node_id"])] = observation
    return snapshots


def signed_memory_safe(
    attestation: Mapping[str, Any], selected_node_ids: frozenset[str]
) -> bool:
    """Fail closed unless every selected node signed positive memory headroom."""

    snapshots = _latest_snapshots(attestation)
    if not selected_node_ids or not selected_node_ids <= set(snapshots):
        return False
    for node_id in selected_node_ids:
        details = snapshots[node_id].get("details")
        resources = details.get("host_resources") if isinstance(details, Mapping) else None
        if (
            not isinstance(resources, Mapping)
            or type(resources.get("available_memory_bytes")) is not int
            or resources["available_memory_bytes"] <= 0
            or type(resources.get("rss_bytes")) is not int
            or resources["rss_bytes"] <= 0
        ):
            return False
    return True


def _peer_map(status: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    peers = status.get("peers")
    if not isinstance(peers, list):
        return {}
    return {
        str(peer["node_id"]): peer
        for peer in peers
        if isinstance(peer, Mapping) and isinstance(peer.get("node_id"), str)
    }


def directed_link_qualified(
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    selected_node_ids: frozenset[str],
) -> bool:
    """Require bidirectional frame and applied-work movement on every track node."""

    before_peers = _peer_map(before)
    after_peers = _peer_map(after)
    if not selected_node_ids or not selected_node_ids <= set(before_peers) & set(after_peers):
        return False
    fields = ("frames_sent", "frames_received", "applied_operation_count")
    for node_id in selected_node_ids:
        prior = before_peers[node_id]
        current = after_peers[node_id]
        if any(
            type(prior.get(field)) is not int
            or type(current.get(field)) is not int
            or current[field] <= prior[field]
            for field in fields
        ):
            return False
    return True


def runtime_clean(
    runtime_status: Mapping[str, Any],
    live_status: Mapping[str, Any],
    placement_ids: Sequence[str],
    selected_node_ids: frozenset[str],
) -> bool:
    """Require exact post-terminal zero reservations, queue, and stage-local KV."""

    queue = runtime_status.get("queue")
    placements = runtime_status.get("placements")
    placement_map = {
        item.get("placement_id"): item
        for item in placements
        if isinstance(item, Mapping)
    } if isinstance(placements, list) else {}
    peers = _peer_map(live_status)
    return bool(
        live_status.get("route_alive") is True
        and isinstance(queue, Mapping)
        and queue.get("depth") == 0
        and queue.get("active_request_ids") == []
        and set(placement_ids) <= set(placement_map)
        and all(
            placement_map[placement_id].get("active_reservations") == 0
            for placement_id in placement_ids
        )
        and selected_node_ids <= set(peers)
        and all(
            peers[node_id].get("active_kv_state_count") == 0
            and peers[node_id].get("transport_fatal") is False
            for node_id in selected_node_ids
        )
    )


def _request_record(status: Mapping[str, Any], request_id: str) -> Mapping[str, Any]:
    requests = status.get("requests")
    if not isinstance(requests, list):
        raise TypeError("runtime_request_projection_missing")
    matches = [
        item
        for item in requests
        if isinstance(item, Mapping) and item.get("request_id") == request_id
    ]
    if len(matches) != 1:
        raise RuntimeError("runtime_request_projection_missing")
    return matches[0]


def _execute_track(
    *,
    router: LiveRouterPort,
    route: Any,
    deployment_qualification: Any,
    prompt: tuple[int, ...],
    maximum_new_tokens: int,
    request_id: str,
    selected_placement_ids: tuple[str, ...],
    all_placement_ids: frozenset[str],
    timeout_seconds: float,
) -> dict[str, Any]:
    before = route.public_status()
    sink = _CaptureSink()
    config = {
        "max_new_tokens": maximum_new_tokens,
        "sampling_seed": 17,
    }
    request = RequestContext(
        request_id=request_id,
        prompt_token_ids=prompt,
        max_new_tokens=maximum_new_tokens,
        expected_new_tokens=maximum_new_tokens,
        qos_class="interactive",
        admitted_at=time.monotonic(),
        target_ttft_ms=120_000.0,
        target_tpot_ms=120_000.0,
        target_tokens_per_second=0.001,
        sampling_seed=17,
        generation_config_digest=_digest(config),
    )
    excluded = all_placement_ids - frozenset(selected_placement_ids)
    router.admit(
        request,
        sink,
        workload_profile_id="interactive_chat_v1",
        excluded_placements=frozenset(excluded),
        qualification_binding=qualification_binding(deployment_qualification),
    )
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        advanced = router.poll_one(request_id)
        status = router.request_status(request_id)
        if status in {"COMPLETED", "CANCELLED", "FAILED", "TERMINAL_BLOCKED"}:
            break
        if not advanced:
            time.sleep(0.01)
    else:
        cancellation_requested = router.cancel_with_deadline(
            request_id,
            deadline_monotonic_s=time.monotonic() + 2.0,
        )
        raise RuntimeError(
            "a5_track_execution_timeout:"
            + json.dumps(
                {
                    "request_id": request_id,
                    "timeout_seconds": timeout_seconds,
                    "cancellation_requested": cancellation_requested,
                    "router_error_code": router.request_error_code(request_id),
                },
                sort_keys=True,
            )
        )
    runtime_status = router.runtime_status()
    if not isinstance(runtime_status, Mapping):
        raise TypeError("runtime_status_unavailable")
    record = _request_record(runtime_status, request_id)
    if status != "COMPLETED":
        incidents = [
            {
                "kind": incident.get("kind"),
                "scope": incident.get("scope"),
                "state": incident.get("state"),
            }
            for incident in runtime_status.get("incidents", [])
            if isinstance(incident, Mapping)
            and incident.get("request_id") == request_id
        ]
        live_fatal = route.public_status().get("counters", {}).get("fatal")
        diagnostic = {
            "terminal_status": status,
            "terminal_state": record.get("terminal_state"),
            "router_error_code": router.request_error_code(request_id),
            "incidents": incidents,
            "live_fatal": live_fatal,
        }
        raise RuntimeError(
            "a5_track_terminal_failed:" + json.dumps(diagnostic, sort_keys=True)
        )

    observed_placement_ids = tuple(record.get("placement_ids", ()))
    attestation = route.live_attestation(request_id=request_id)
    track_qualification = issue_live_route_qualification(
        attestation,
        expected_prompt_token_ids=prompt,
        expected_output_token_ids=tuple(sink.tokens),
    )
    after = route.public_status()
    selected_node_ids = frozenset(
        peer["node_id"]
        for peer in after.get("peers", [])
        if isinstance(peer, Mapping)
        and any(
            placement.get("placement_id") in selected_placement_ids
            for placement in peer.get("placements", [])
            if isinstance(placement, Mapping)
        )
    )
    result = {
        "request_id": request_id,
        "requested_placement_ids": list(selected_placement_ids),
        "observed_placement_ids": list(observed_placement_ids),
        "terminal": status.lower(),
        "output_token_count": len(sink.tokens),
        "output_digest": _digest(sink.tokens),
        "track_exact": observed_placement_ids == selected_placement_ids,
        "memory_within_bounds": signed_memory_safe(attestation, selected_node_ids),
        "cleanup_within_bounds": runtime_clean(
            runtime_status,
            after,
            selected_placement_ids,
            selected_node_ids,
        ),
        "directed_link_qualified": directed_link_qualified(
            before,
            after,
            selected_node_ids,
        ),
        "runtime_status": runtime_status,
        "live_status_before": before,
        "live_status_after": after,
        "attestation": attestation,
        "route_qualification": route_qualification_to_dict(track_qualification),
    }
    router.release_request(request_id)
    if not router.is_idle():
        raise RuntimeError("router_release_incomplete")
    return result


def _configured_details(
    attestation: Mapping[str, Any], *, replica_node_id: str, placement_id: str
) -> Mapping[str, Any]:
    matches: list[Mapping[str, Any]] = []
    for signed in attestation.get("signed_observations", []):
        if not isinstance(signed, Mapping):
            continue
        observation = signed.get("observation")
        if not isinstance(observation, Mapping):
            continue
        details = observation.get("details")
        if (
            observation.get("node_id") == replica_node_id
            and observation.get("event") == "configured"
            and isinstance(details, Mapping)
            and details.get("placement_id") == placement_id
        ):
            matches.append(details)
    if len(matches) != 1:
        raise RuntimeError("replica_configured_observation_missing")
    return matches[0]


def _artifact_verification_digest(
    attestation: Mapping[str, Any], *, replica_node_id: str, placement_id: str
) -> str:
    details = _configured_details(
        attestation,
        replica_node_id=replica_node_id,
        placement_id=placement_id,
    )
    digest = details.get("stage_pack_verification_digest")
    if not isinstance(digest, str) or not digest.startswith("sha256:"):
        raise RuntimeError("replica_artifact_verification_missing")
    return digest


def _assignment_digest(
    plan: Mapping[str, Any], *, replica_node_id: str
) -> str:
    run_nodes = plan["controller"]["run_plan"]["nodes"]
    node = next(item for item in run_nodes if item["node_id"] == replica_node_id)
    assignment_file = node["configure"]["assignment_file"]
    rows = plan["controller"]["transfer_manifest"]["files"]
    matches = [row for row in rows if row["path"] == assignment_file]
    if len(matches) != 1:
        raise RuntimeError("replica_assignment_digest_missing")
    return str(matches[0]["content_digest"])


def build(args: argparse.Namespace) -> tuple[dict[str, Any], dict[str, Any]]:
    plan = json.loads(args.operator_plan.read_text(encoding="utf-8"))
    runtime = _qualified_runtime(
        args.operator_plan,
        seed_state_root=args.seed_state_root,
    )
    route = runtime.route
    router: LiveRouterPort | None = None
    try:
        pair = derive_track_pair(runtime.graph)
        all_placements = frozenset(
            placement.placement_id
            for stage in runtime.graph.stages
            for placement in stage.placements
        )
        coordinator = build_live_m16_runtime(
            runtime.graph,
            placement_projection=runtime.placement_projection,
            workload_comparison=runtime.workload_comparison,
        )
        if runtime.m16_performance_budget is not None:
            coordinator.attach_performance_budget(runtime.m16_performance_budget)
        router = LiveRouterPort(
            route=route,
            execution_graph=runtime.graph,
            runtime_coordinator=coordinator,
        )
        route.set_m16_runtime_source(router.runtime_status)
        startup_prompt, _startup_output = route.startup_challenge
        prompt = tuple(startup_prompt[-16:])
        maximum_new_tokens = 2
        nonce = str(time.time_ns())
        incumbent = _execute_track(
            router=router,
            route=route,
            deployment_qualification=runtime.qualification,
            prompt=prompt,
            maximum_new_tokens=maximum_new_tokens,
            request_id=f"a5-prequal-incumbent-{nonce}",
            selected_placement_ids=pair.incumbent_placement_ids,
            all_placement_ids=all_placements,
            timeout_seconds=args.track_timeout_seconds,
        )
        replica = _execute_track(
            router=router,
            route=route,
            deployment_qualification=runtime.qualification,
            prompt=prompt,
            maximum_new_tokens=maximum_new_tokens,
            request_id=f"a5-prequal-replica-{nonce}",
            selected_placement_ids=pair.replica_placement_ids,
            all_placement_ids=all_placements,
            timeout_seconds=args.track_timeout_seconds,
        )
        parity_verified = (
            incumbent["output_digest"] == replica["output_digest"]
            and incumbent["output_token_count"] == maximum_new_tokens
            and replica["output_token_count"] == maximum_new_tokens
        )
        workload_envelope = {
            "protocol": "mycelium.a5_replica_prequalification_workload.v1",
            "prompt_token_count": len(prompt),
            "maximum_new_tokens": maximum_new_tokens,
            "workload_profile_id": "interactive_chat_v1",
            "qos_class": "interactive",
        }
        replica_node_id = next(
            placement.node_id
            for stage in runtime.graph.stages
            for placement in stage.placements
            if placement.placement_id == pair.replica_placement_id
        )
        replica_startup_passed = (
            replica["terminal"] == "completed"
            and replica["track_exact"] is True
            and replica["output_token_count"] == maximum_new_tokens
        )
        evidence = {
            "protocol": "mycelium.a5_replica_track_physical_prequalification.v1",
            "captured_at_unix_ms": int(time.time() * 1_000),
            "deployment_id": runtime.graph.deployment_id,
            "deployment_epoch": runtime.graph.deployment_epoch,
            "topology_version": runtime.graph.topology_version,
            "stage_count": len(runtime.graph.stages),
            "incumbent_placement_ids": list(pair.incumbent_placement_ids),
            "replica_placement_ids": list(pair.replica_placement_ids),
            "replica_group_id": pair.replica_group_id,
            "replica_placement_id": pair.replica_placement_id,
            "workload_envelope": workload_envelope,
            "startup_challenge_passed": replica_startup_passed,
            "parity_verified": parity_verified,
            "incumbent": incumbent,
            "replica": replica,
        }
        evidence_digest = _digest(evidence)
        issued_at = int(time.time() * 1_000)
        track_id = _digest(
            {
                "protocol": "mycelium.a5_complete_track_identity.v1",
                "deployment_id": runtime.graph.deployment_id,
                "deployment_epoch": runtime.graph.deployment_epoch,
                "placement_ids": list(pair.replica_placement_ids),
            }
        )
        extra_rejections = []
        if not incumbent["track_exact"] or not replica["track_exact"]:
            extra_rejections.append("complete_track_mismatch")
        qualification = qualify_replica_track(
            ReplicaQualificationInput(
                deployment_id=runtime.graph.deployment_id,
                deployment_epoch=runtime.graph.deployment_epoch,
                replica_group_id=pair.replica_group_id,
                placement_id=pair.replica_placement_id,
                placement_ids=pair.replica_placement_ids,
                track_id=track_id,
                traffic_fraction=0.5,
                qualifier_generation=runtime.graph.topology_version,
                issued_at_unix_ms=issued_at,
                expires_at_unix_ms=issued_at + args.validity_seconds * 1_000,
                evidence_bundle_digest=evidence_digest,
                load_proof_digest=pair.replica_load_proof_digest,
                assignment_digest=_assignment_digest(
                    plan,
                    replica_node_id=replica_node_id,
                ),
                artifact_verification_digest=_artifact_verification_digest(
                    replica["attestation"],
                    replica_node_id=replica_node_id,
                    placement_id=pair.replica_placement_id,
                ),
                parity_verified=parity_verified,
                startup_challenge_passed=replica_startup_passed,
                memory_within_bounds=bool(replica["memory_within_bounds"]),
                cleanup_within_bounds=bool(replica["cleanup_within_bounds"]),
                directed_link_qualified=bool(replica["directed_link_qualified"]),
                workload_envelope_digest=_digest(workload_envelope),
            ),
            extra_rejections=extra_rejections,
        )
        bundle = {
            "protocol": "mycelium.a5_replica_track_prequalification_bundle.v1",
            "evidence_digest": evidence_digest,
            "evidence": evidence,
            "qualification": qualification,
        }
        return bundle, qualification
    finally:
        if router is not None:
            router.close()
        route.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--operator-plan", type=Path, required=True)
    parser.add_argument("--seed-state-root", type=Path, required=True)
    parser.add_argument("--evidence-output", type=Path, required=True)
    parser.add_argument("--qualification-output", type=Path, required=True)
    parser.add_argument("--validity-seconds", type=int, default=86_400)
    parser.add_argument("--track-timeout-seconds", type=float, default=600.0)
    args = parser.parse_args()
    if not 300 <= args.validity_seconds <= 604_800:
        raise SystemExit("--validity-seconds must be in [300, 604800]")
    if not 1.0 <= args.track_timeout_seconds <= 900.0:
        raise SystemExit("--track-timeout-seconds must be in [1, 900]")
    bundle, qualification = build(args)
    _atomic_json(args.evidence_output.resolve(), bundle)
    _atomic_json(args.qualification_output.resolve(), qualification)
    print(json.dumps({
        "evidence_output": str(args.evidence_output),
        "qualification_output": str(args.qualification_output),
        "route_ready": qualification["route_ready"],
        "rejected_reasons": qualification["rejected_reasons"],
        "placement_ids": qualification["placement_ids"],
    }, sort_keys=True))
    return 0 if qualification["route_ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
