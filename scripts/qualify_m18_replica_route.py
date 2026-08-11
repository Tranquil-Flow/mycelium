#!/usr/bin/env python3
"""Physically qualify two overlapping M18 replica tracks and throughput gain."""

# ruff: noqa: E402 -- direct execution bootstraps the repository import root.
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import sys
import time
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mycelium_live.route import PhysicalLiveRoute
from mycelium_live.supervisor import _validate_route_identity
from mycelium_qualification.contracts import route_qualification_to_dict
from mycelium_qualification.evidence import canonical_json_bytes, sha256_document
from mycelium_qualification.live import issue_live_route_qualification


class _DiscardSink:
    def emit(self, _token_index: int, _token_id: int) -> None:
        return None


def _write(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_json_bytes(document))


def build(args: argparse.Namespace) -> dict[str, Any]:
    route = PhysicalLiveRoute.from_operator_plan(
        args.operator_plan,
        seed_state_root=args.seed_state_root,
    )
    try:
        identity = route.open()
        _validate_route_identity(identity, route.execution_graph)
        startup_prompt, _startup_output = route.startup_challenge
        prompt = startup_prompt[-16:]
        max_new_tokens = 2
        gate_nonce = str(time.time_ns())

        serial_started = time.perf_counter()
        serial_request_id = f"m18-serial-{gate_nonce}"
        try:
            result = route.infer(
                prompt,
                max_new_tokens=max_new_tokens,
                request_id=serial_request_id,
                sink=_DiscardSink(),
            )
        except BaseException as exc:
            remote_code = getattr(exc, "remote_code", None)
            code = remote_code if isinstance(remote_code, str) else str(exc)
            stderr = route.diagnostic_stderr(route.execution_graph.stages[0].placements[0].node_id)
            raise RuntimeError(
                f"m18_primary_admission_rejected:{code}\n{stderr}"
            ) from exc
        expected_output = result.token_ids
        if len(expected_output) != max_new_tokens:
            raise RuntimeError("m18_serial_token_parity_failed")
        serial_elapsed_ms = (time.perf_counter() - serial_started) * 1_000.0
        primary_qualification = issue_live_route_qualification(
            route.live_attestation(request_id=serial_request_id),
            expected_prompt_token_ids=prompt,
            expected_output_token_ids=expected_output,
        )
        route.release_request(serial_request_id)

        concurrency = route.qualify_replica_concurrency(
            (prompt, prompt),
            max_new_tokens=max_new_tokens,
            request_id_prefix=f"m18-concurrent-{gate_nonce}",
        )
        track_qualifications = []
        for request in concurrency["requests"]:
            request_id = request["request_id"]
            qualification = issue_live_route_qualification(
                route.live_attestation(request_id=request_id),
                expected_prompt_token_ids=prompt,
                expected_output_token_ids=expected_output,
            )
            document = route_qualification_to_dict(qualification)
            track_qualifications.append(
                {
                    "request_id": request_id,
                    "entry_node_id": request["entry_node_id"],
                    "placement_ids": request["placement_ids"],
                    "qualification": document,
                    "qualification_digest": sha256_document(document),
                }
            )
            route.release_request(request_id)

        concurrent_elapsed_ms = float(concurrency["elapsed_ms"])
        measured_rates = {
            request["entry_node_id"]: 1_000.0
            / float(request["active_compute_elapsed_ms"])
            for request in concurrency["requests"]
        }
        saturation_request_count = 6
        total_rate = sum(measured_rates.values())
        raw_counts = {
            node_id: saturation_request_count * rate / total_rate
            for node_id, rate in measured_rates.items()
        }
        request_counts = {
            node_id: max(1, math.floor(raw_count))
            for node_id, raw_count in raw_counts.items()
        }
        while sum(request_counts.values()) < saturation_request_count:
            node_id = max(
                request_counts,
                key=lambda item: (
                    raw_counts[item] - request_counts[item],
                    measured_rates[item],
                    item,
                ),
            )
            request_counts[node_id] += 1
        while sum(request_counts.values()) > saturation_request_count:
            node_id = max(
                (item for item, count in request_counts.items() if count > 1),
                key=lambda item: (request_counts[item] - raw_counts[item], item),
            )
            request_counts[node_id] -= 1
        saturation = route.measure_replica_saturation(
            prompt,
            expected_output_token_ids=expected_output,
            request_counts_by_node=request_counts,
            request_id_prefix=f"m18-saturation-{gate_nonce}",
        )
        serial_rps = 1_000.0 / serial_elapsed_ms
        replicated_rps = float(saturation["throughput_rps"])
        gain_fraction = replicated_rps / serial_rps - 1.0
        threshold = float(args.minimum_gain_fraction)
        passed = (
            primary_qualification.route_ready
            and concurrency["overlapping_at_admission"] is True
            and concurrency["distinct_tracks"] is True
            and all(
                item["qualification"]["route_ready"] is True
                for item in track_qualifications
            )
            and gain_fraction >= threshold
        )
        evidence = {
            "protocol": "mycelium.m18_physical_throughput_gate.v1",
            "captured_at_unix_ms": int(time.time() * 1_000),
            "deployment_id": identity.deployment_id,
            "model_id": identity.model_id,
            "resolved_commit": identity.resolved_commit,
            "parallelism": "data_parallel_request_routing",
            "challenge": {
                "authority": "m17_qualified_primary_runtime_reference",
                "prompt_token_count": len(prompt),
                "output_token_count": len(expected_output),
                "output_digest": sha256_document(list(expected_output)),
            },
            "primary_qualification": route_qualification_to_dict(primary_qualification),
            "track_qualifications": track_qualifications,
            "concurrency": concurrency,
            "saturation": saturation,
            "baseline": {
                "mode": "primary_only_single_request_service_rate",
                "request_count": 1,
                "elapsed_ms": serial_elapsed_ms,
                "throughput_rps": serial_rps,
                "normalized_two_request_elapsed_ms": serial_elapsed_ms * 2.0,
            },
            "replicated": {
                "mode": "measured_service_rate_weighted_saturation",
                "request_count": saturation["request_count"],
                "elapsed_ms": saturation["elapsed_ms"],
                "throughput_rps": replicated_rps,
                "request_counts_by_node": saturation["request_counts_by_node"],
                "equal_split_probe": {
                    "request_count": 2,
                    "elapsed_ms": concurrent_elapsed_ms,
                    "throughput_rps": 2_000.0 / concurrent_elapsed_ms,
                },
            },
            "gain": {
                "minimum_required_fraction": threshold,
                "measured_fraction": gain_fraction,
                "passed": passed,
            },
            "claim_boundary": (
                "same-model physical two-request throughput and independently "
                "qualified immutable tracks; no M19 recovery claim"
            ),
            "route_ready": passed,
        }
        _write(args.output, evidence)
        if not passed:
            raise RuntimeError("m18_physical_throughput_gate_failed")
        return evidence
    finally:
        route.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--operator-plan", type=Path, required=True)
    parser.add_argument("--seed-state-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--minimum-gain-fraction", type=float, default=0.05)
    args = parser.parse_args()
    evidence = build(args)
    print(
        json.dumps(
            {
                "output": str(args.output),
                "distinct_tracks": evidence["concurrency"]["distinct_tracks"],
                "measured_gain_fraction": evidence["gain"]["measured_fraction"],
                "route_ready": evidence["route_ready"],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
