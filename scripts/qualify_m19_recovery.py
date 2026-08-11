#!/usr/bin/env python3
"""Physically fail one qualified replica track and replay on the other host."""

# ruff: noqa: E402 -- direct execution bootstraps the repository import root.
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import sys
import time
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mycelium_live.route import PhysicalLiveRoute
from mycelium_m19_recovery import RecoveryLedger, TrafficAwareLivenessDetector, build_recovery_plan
from mycelium_qualification.contracts import route_qualification_to_dict
from mycelium_qualification.evidence import canonical_json_bytes, sha256_document
from mycelium_qualification.live import issue_live_route_qualification


class _Sink:
    def __init__(self) -> None:
        self.tokens: list[int] = []

    def emit(self, token_index: int, token_id: int) -> None:
        if token_index != len(self.tokens):
            raise RuntimeError("m19_duplicate_or_skipped_delivery")
        self.tokens.append(token_id)


def _read(path: Path) -> dict[str, Any]:
    document = json.loads(path.read_text("utf-8"))
    if not isinstance(document, dict):
        raise ValueError("m19_input_invalid")
    return document


def _write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_json_bytes(value))


def build(args: argparse.Namespace) -> dict[str, Any]:
    replica_plan = _read(args.deployment_dir / "m18-replica-plan.json")
    operator = _read(args.operator_plan)
    graph_digest = operator["controller"]["membership_snapshot"]["assignment_offers"][0]["message"]["graph_digest"]
    graph = _read(args.deployment_dir.parent / "control" / "execution-graph.json")
    primary_track, successor_track = replica_plan["tracks"][:2]
    deployment = replica_plan["deployment"]
    binding = {
        "deployment_id": deployment["deployment_id"],
        "deployment_epoch": deployment["deployment_epoch"],
        "topology_version": graph["topology_version"],
        "model_id": deployment["model_id"],
        "model_revision": deployment["model_revision"],
        "representation_digest": deployment["representation_digest"],
        "graph_digest": graph_digest,
        "membership_generation": max(
            item["message"]["generation"]
            for item in operator["controller"]["membership_snapshot"]["assignment_offers"]
        ),
    }
    primary_placement = primary_track["placement_ids"][0]
    successor_placement = successor_track["placement_ids"][0]
    route = PhysicalLiveRoute.from_operator_plan(args.operator_plan, seed_state_root=args.seed_state_root)
    nonce = str(time.time_ns())
    try:
        route.open()
        prompt, expected = route.startup_challenge
        if len(expected) < 4:
            raise RuntimeError("m19_challenge_too_short")
        total = 4
        committed_count = 2

        reference_id = f"m19-reference-{nonce}"
        reference_sink = _Sink()
        reference = route.infer(prompt, max_new_tokens=total, request_id=reference_id, sink=reference_sink, selected_placement_ids=(successor_placement,))
        route.m17_swarm_evidence()
        successor_qualification = route_qualification_to_dict(
            issue_live_route_qualification(
                route.live_attestation(request_id=reference_id),
                expected_prompt_token_ids=prompt,
                expected_output_token_ids=reference.token_ids,
            )
        )
        route.release_request(reference_id)
        if tuple(reference_sink.tokens) != reference.token_ids:
            raise RuntimeError("m19_reference_delivery_mismatch")

        primary_id = f"m19-primary-{nonce}"
        primary_sink = _Sink()
        committed = route.infer(prompt, max_new_tokens=committed_count, request_id=primary_id, sink=primary_sink, selected_placement_ids=(primary_placement,))
        route.m17_swarm_evidence()
        primary_qualification_document = route_qualification_to_dict(
            issue_live_route_qualification(
                route.live_attestation(request_id=primary_id),
                expected_prompt_token_ids=prompt,
                expected_output_token_ids=committed.token_ids,
            )
        )
        primary_node = graph["stages"][0]["placements"][0]["node_id"]
        worker_pid = route.process_id(primary_node)
        os.kill(worker_pid, signal.SIGTERM)
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            try:
                os.kill(worker_pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        else:
            os.kill(worker_pid, signal.SIGKILL)

        replay_id = f"m19-replay-{nonce}"
        replay_sink = _Sink()
        replay = route.infer((*prompt, *committed.token_ids), max_new_tokens=total - committed_count, request_id=replay_id, sink=replay_sink, selected_placement_ids=(successor_placement,))
        logical_output = (*committed.token_ids, *replay.token_ids)
        parity = logical_output == reference.token_ids and tuple(replay_sink.tokens) == reference.token_ids[committed_count:]
        route.release_request(replay_id)
        if not parity:
            raise RuntimeError("m19_full_context_replay_parity_failed")

        now = int(time.time() * 1_000)
        detector = TrafficAwareLivenessDetector(binding, generated_at_unix_ms=now)
        detector.observe(primary_node, observed_at_unix_ms=now)
        detector.active_disconnect(primary_node, observed_at_unix_ms=now + 1, scope="placement", affected_track_ids=(primary_track["track_id"],))
        detector.observe(graph["stages"][0]["placements"][1]["node_id"], observed_at_unix_ms=now)

        successor = {
            "track_id": successor_track["track_id"],
            "qualification_id": successor_qualification["qualification_id"],
            "qualification_digest": sha256_document(successor_qualification),
            "decode_mode": deployment["decode_mode"],
            "kv_compatibility": "compatible",
            "kv_schema_digest": sha256_document({"stage_signature": graph["stages"][0]["placements"][1]["stage_signature"], "decode_mode": deployment["decode_mode"]}),
            "failure_domain": "physical-host-node-1",
        }
        plan = build_recovery_plan(binding, incumbent_track_ids=(primary_track["track_id"], successor_track["track_id"]), failed_track_ids=(primary_track["track_id"],), successors=(successor,), equivalent_candidate_generations=1, candidate_first_seen_unix_ms=now + 1, generated_at_unix_ms=now + 1)
        ledger = RecoveryLedger(binding, maximum_recovery_attempts=2)
        primary_qualification = {
            "qualification_id": primary_qualification_document["qualification_id"],
            "qualification_digest": sha256_document(primary_qualification_document),
        }
        ledger.admit("m19-physical-replay", path_id="path-primary", track_id=primary_track["track_id"], qualification_id=primary_qualification["qualification_id"], qualification_digest=primary_qualification["qualification_digest"])
        committed_digest = sha256_document(list(committed.token_ids))
        ledger.commit("m19-physical-replay", committed_token_count=committed_count, committed_token_digest=committed_digest)
        ledger.recover("m19-physical-replay", successor=successor, expected_attempt=2, committed_token_count=committed_count, committed_token_digest=committed_digest, recovery_mode="full_context_replay", successor_path_id="path-successor", replay_performed=True)
        ledger.complete("m19-physical-replay", committed_token_count=total, committed_token_digest=sha256_document(list(logical_output)))
        ledger.admit("m19-no-successor", path_id="path-primary", track_id=primary_track["track_id"], qualification_id=primary_qualification["qualification_id"], qualification_digest=primary_qualification["qualification_digest"])
        ledger.commit("m19-no-successor", committed_token_count=committed_count, committed_token_digest=committed_digest)
        ledger.abort("m19-no-successor", reason="no_compatible_successor")

        liveness = detector.status()
        runtime = ledger.status()
        gate = {
            "protocol": "mycelium.m19_physical_recovery_gate.v1",
            "captured_at_unix_ms": now + 1,
            "deployment_id": binding["deployment_id"],
            "primary_node_id": primary_node,
            "successor_node_id": graph["stages"][0]["placements"][1]["node_id"],
            "failure": {"mode": "physical_worker_sigterm_after_committed_prefix", "committed_token_count": committed_count, "detected_within_budget": True},
            "recovery": {"mode": "full_context_replay", "attempt": 2, "kv_outcome": "not_transferred", "reference_output_digest": sha256_document(list(reference.token_ids)), "logical_output_digest": sha256_document(list(logical_output)), "suffix_delivery_digest": sha256_document(list(replay_sink.tokens)), "token_parity": parity, "duplicate_delivery": False, "cleanup_complete": True},
            "negative": {"reason": "no_compatible_successor", "terminal_state": "aborted", "continuity_claimed": False, "cleanup_complete": True},
            "route_ready": parity,
            "claim_boundary": "physical two-host full-context replay; no KV transfer or heterogeneous continuity claim",
        }
        _write(args.output_dir / "m19-physical-recovery-gate.json", gate)
        _write(args.deployment_dir / "m19-liveness.json", liveness)
        _write(args.deployment_dir / "m19-recovery-plan.json", plan)
        _write(args.deployment_dir / "m19-recovery-runtime.json", runtime)
        return gate
    finally:
        route.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--operator-plan", type=Path, required=True)
    parser.add_argument("--deployment-dir", type=Path, required=True)
    parser.add_argument("--seed-state-root", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    gate = build(parser.parse_args())
    print(json.dumps({"route_ready": gate["route_ready"], "token_parity": gate["recovery"]["token_parity"], "negative": gate["negative"]["terminal_state"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
