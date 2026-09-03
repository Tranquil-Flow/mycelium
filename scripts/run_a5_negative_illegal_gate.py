#!/usr/bin/env python3
"""Execute the A5 illegal-state negative gate against a live product serve.

Proves (spec §6, §10; plan 6.4) that an invalid or rejected replica
candidate is rejected without poisoning admission and that the incumbent
selector is preserved:

1. Qualifier leaf (deterministic, local): a corrupted qualification input
   yields route_ready=false with explicit rejection reasons.
2. Product path, tampered document: a replica_qualification.v1 whose
   assignment_digest was corrupted without qualifier resealing fails
   self-binding validation,
   so the operator install endpoint answers 400 and the live set is UNCHANGED.
3. Product path, closed-but-rejected document: a validly-shaped
   qualification with parity_verified=false (route_ready=false,
   rejected_reasons=["parity_mismatch"]) installs, but the selector skips
   non-route-ready tracks — every new request runs the incumbent A4 default
   path and completes. No admission poisoned.
4. Restoration: reinstalling the true documents restores candidate
   selection; a follow-up request lands on the replica track again, proving
   the incumbent selector was never replaced or poisoned.

The output is a bounded privacy-reduced observation
(mycelium.a5_negative_illegal_observation.v1): no prompt, decoded text,
token IDs, cookies, CSRF values, hostnames, addresses, paths, command lines,
or exception strings.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.run_a5_product_gate import (  # noqa: E402
    GateError,
    ProductSession,
    _request_map,
    _zero_live_resources,
    atomic_json,
    public_json,
    wait_for,
)


def _load_true_documents(paths: list[Path]) -> list[dict[str, Any]]:
    from mycelium_replica_contracts import validate_replica_qualification

    documents: list[dict[str, Any]] = []
    for path in paths:
        candidate = Path(path)
        if (
            candidate.is_symlink()
            or not candidate.is_file()
            or candidate.stat().st_size > 4 * 1024 * 1024
        ):
            raise GateError("replica_qualification_unsafe")
        try:
            document = json.loads(candidate.read_text("utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError, RecursionError) as exc:
            raise GateError("replica_qualification_invalid") from exc
        documents.append(validate_replica_qualification(document))
    return documents


def _install(base_url: str, documents: list[dict[str, Any]]) -> tuple[int, dict]:
    """POST the install endpoint; returns (status, body). Never raises."""

    operator_token = os.environ.get("MYCELIUM_A5_OPERATOR_TOKEN")
    if not isinstance(operator_token, str) or len(operator_token) < 32:
        raise GateError("operator_authorization_missing")
    body = json.dumps({"documents": documents}, sort_keys=True).encode("utf-8")
    request = urllib.request.Request(
        base_url.rstrip("/") + "/__mycelium/replica-qualification/install",
        data=body,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Origin": base_url.rstrip("/"),
            "Authorization": f"Bearer {operator_token}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10.0) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        try:
            document = json.load(error)
        except (ValueError, json.JSONDecodeError):
            document = {}
        return error.code, document
    except (OSError, TimeoutError) as error:
        raise GateError("qualification_install_unavailable") from error


def _tamper_assignment_digest(document: dict[str, Any]) -> dict[str, Any]:
    """Corrupt a valid assignment identity without qualifier resealing."""

    tampered = dict(document)
    digest = tampered["assignment_digest"]
    replacement = "0" if digest[-1] != "0" else "1"
    tampered["assignment_digest"] = digest[:-1] + replacement
    return tampered


def _rejected_document(document: dict[str, Any]) -> dict[str, Any]:
    """Closed-but-rejected: same shape, parity failed, route_ready=false."""

    from mycelium_qualification.replica import (
        ReplicaQualificationInput,
        qualify_replica_track,
    )

    data = ReplicaQualificationInput(
        deployment_id=document["deployment_id"],
        deployment_epoch=document["deployment_epoch"],
        replica_group_id=document["replica_group_id"],
        placement_id=document["placement_id"],
        placement_ids=tuple(document["placement_ids"]),
        track_id=document["track_id"],
        traffic_fraction=document["traffic_fraction"],
        qualifier_generation=document["qualifier_generation"],
        issued_at_unix_ms=document["issued_at_unix_ms"],
        expires_at_unix_ms=document["expires_at_unix_ms"],
        evidence_bundle_digest=document["evidence_bundle_digest"],
        load_proof_digest=document["load_proof_digest"],
        assignment_digest=document["assignment_digest"],
        artifact_verification_digest=document["artifact_verification_digest"],
        parity_verified=False,
        startup_challenge_passed=document["startup_challenge_passed"],
        memory_within_bounds=document["memory_within_bounds"],
        cleanup_within_bounds=document["cleanup_within_bounds"],
        directed_link_qualified=document["directed_link_qualified"],
        workload_envelope_digest=document["workload_envelope_digest"],
    )
    return qualify_replica_track(data)


def _qualifier_leaf_evidence(document: dict[str, Any]) -> dict[str, Any]:
    """Deterministic leaf checks on corrupted inputs (real local output)."""

    from mycelium_qualification.replica import (
        ReplicaQualificationInput,
        qualify_replica_track,
    )

    def build(**overrides: Any) -> ReplicaQualificationInput:
        values = {
            "deployment_id": document["deployment_id"],
            "deployment_epoch": document["deployment_epoch"],
            "replica_group_id": document["replica_group_id"],
            "placement_id": document["placement_id"],
            "placement_ids": tuple(document["placement_ids"]),
            "track_id": document["track_id"],
            "traffic_fraction": document["traffic_fraction"],
            "qualifier_generation": document["qualifier_generation"],
            "issued_at_unix_ms": document["issued_at_unix_ms"],
            "expires_at_unix_ms": document["expires_at_unix_ms"],
            "evidence_bundle_digest": document["evidence_bundle_digest"],
            "load_proof_digest": document["load_proof_digest"],
            "assignment_digest": document["assignment_digest"],
            "artifact_verification_digest": document["artifact_verification_digest"],
            "parity_verified": document["parity_verified"],
            "startup_challenge_passed": document["startup_challenge_passed"],
            "memory_within_bounds": document["memory_within_bounds"],
            "cleanup_within_bounds": document["cleanup_within_bounds"],
            "directed_link_qualified": document["directed_link_qualified"],
            "workload_envelope_digest": document["workload_envelope_digest"],
        }
        values.update(overrides)
        return ReplicaQualificationInput(**values)

    parity_fail = qualify_replica_track(build(parity_verified=False))
    stale = qualify_replica_track(build(qualifier_generation=0))
    memory_fail = qualify_replica_track(build(memory_within_bounds=False))
    return {
        "parity_fail": {
            "route_ready": parity_fail["route_ready"],
            "rejected_reasons": parity_fail["rejected_reasons"],
        },
        "stale_generation": {
            "route_ready": stale["route_ready"],
            "rejected_reasons": stale["rejected_reasons"],
        },
        "memory_fail": {
            "route_ready": memory_fail["route_ready"],
            "rejected_reasons": memory_fail["rejected_reasons"],
        },
    }


def _installed_placements(status: dict[str, Any]) -> list[str]:
    return [
        document["placement_id"]
        for document in status.get("replica_track_qualification") or []
    ]


def run_gate(base_url: str, qualification_paths: list[Path]) -> dict[str, Any]:
    true_documents = _load_true_documents(qualification_paths)
    if not true_documents:
        raise GateError("replica_qualification_missing")
    true_placement_ids = sorted(doc["placement_id"] for doc in true_documents)
    leaf = _qualifier_leaf_evidence(true_documents[0])
    if leaf["parity_fail"]["route_ready"] is not False:
        raise GateError("qualifier_leaf_did_not_reject")

    session = ProductSession(base_url)
    before = public_json(base_url, "/__mycelium/live-status")
    if before.get("route_alive") is not True:
        raise GateError("route_not_alive")
    before_set = sorted(_installed_placements(before))
    before_runtime = public_json(base_url, "/__mycelium/runtime/admission-status")
    if not _zero_live_resources(before_runtime):
        raise GateError("route_not_clean_and_alive")

    # Step 2: tampered document -> closed-shape validation fails -> 400,
    # live set unchanged.
    tampered = _tamper_assignment_digest(true_documents[0])
    status, body = _install(base_url, [tampered])
    if status != 400:
        raise GateError("tampered_document_not_rejected")
    mid = public_json(base_url, "/__mycelium/live-status")
    if sorted(_installed_placements(mid)) != before_set:
        raise GateError("tampered_document_mutated_live_set")

    # Step 3: closed-but-rejected document installs but is unselectable;
    # every new request runs the incumbent path and completes.
    rejected = _rejected_document(true_documents[0])
    if rejected["route_ready"] is not False or rejected["rejected_reasons"] != [
        "parity_mismatch"
    ]:
        raise GateError("rejected_document_shape_unexpected")
    status, body = _install(base_url, [rejected])
    if status != 200 or body != {"installed": 1}:
        raise GateError("rejected_document_install_failed")
    rejected_status = public_json(base_url, "/__mycelium/live-status")
    if sorted(_installed_placements(rejected_status)) != true_placement_ids:
        raise GateError("rejected_document_not_projected")
    incumbent = session.submit(
        label="incumbent-under-rejection", maximum_new_tokens=32
    )
    incumbent_summary = session.stream_summary(incumbent)
    if incumbent_summary["terminal"] != "completed":
        raise GateError("incumbent_under_rejection_terminal_invalid")
    incumbent_placements = wait_for(
        lambda: (
            record
            if (
                record := _request_map(
                    public_json(base_url, "/__mycelium/runtime/admission-status")
                ).get(incumbent["request_id"])
            )
            is not None
            and record["terminal_state"] is not None
            else None
        ),
        timeout=30.0,
    )
    if true_placement_ids[0] in incumbent_placements["placement_ids"]:
        raise GateError("rejected_track_still_admitted")

    # Step 4: restoration recovers candidate selection; two follow-up
    # requests alternate incumbent/replica regardless of the serve-global
    # rotation cursor, so at least one must carry the replica placement
    # (selector never poisoned).
    status, body = _install(base_url, true_documents)
    if status != 200 or body != {"installed": len(true_documents)}:
        raise GateError("restore_install_failed")
    restored = public_json(base_url, "/__mycelium/live-status")
    recovered_requests: list[dict[str, Any]] = []
    for label in ("candidate-restored-a", "candidate-restored-b"):
        accepted = session.submit(label=label, maximum_new_tokens=32)
        summary = session.stream_summary(accepted)
        if summary["terminal"] != "completed":
            raise GateError("recovered_candidate_terminal_invalid")
        record = wait_for(
            lambda: (
                item
                if (
                    item := _request_map(
                        public_json(
                            base_url, "/__mycelium/runtime/admission-status"
                        )
                    ).get(accepted["request_id"])
                )
                is not None
                and item["terminal_state"] is not None
                else None
            ),
            timeout=30.0,
        )
        recovered_requests.append(
            {
                "request_id": accepted["request_id"],
                "terminal": summary["terminal"],
                "placement_ids": list(record["placement_ids"]),
            }
        )
    if not any(
        true_placement_ids[0] in item["placement_ids"]
        for item in recovered_requests
    ):
        raise GateError("candidate_selection_not_restored")

    after_runtime = wait_for(
        lambda: (
            status_doc
            if _zero_live_resources(
                status_doc := public_json(
                    base_url, "/__mycelium/runtime/admission-status"
                )
            )
            else None
        ),
        timeout=30.0,
    )
    return {
        "protocol": "mycelium.a5_negative_illegal_observation.v1",
        "qualification_claim": False,
        "promotion_authorized": False,
        "deployment_id": after_runtime["deployment_id"],
        "qualifier_leaf": leaf,
        "tampered_document": {
            "install_status": status,
            "install_body": body,
            "live_set_unchanged": sorted(_installed_placements(mid)) == before_set,
        },
        "rejected_document": {
            "route_ready": rejected["route_ready"],
            "rejected_reasons": rejected["rejected_reasons"],
            "install_status": 200,
            "incumbent_request": {
                "request_id": incumbent["request_id"],
                "terminal": incumbent_summary["terminal"],
                "placement_ids": list(incumbent_placements["placement_ids"]),
            },
        },
        "restoration": {
            "install_status": 200,
            "recovered_requests": recovered_requests,
            "final_qualified_placements": sorted(
                _installed_placements(restored)
            ),
        },
        "no_admission_poisoned": True,
        "final_queue": after_runtime["queue"],
        "final_placement_reservations": {
            item["placement_id"]: item["active_reservations"]
            for item in after_runtime["placements"]
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8791")
    parser.add_argument(
        "--replica-qualification",
        type=Path,
        action="append",
        default=[],
        help="the TRUE validated replica_qualification.v1 documents",
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not args.replica_qualification:
        raise SystemExit("--replica-qualification is required")
    report = run_gate(args.base_url, list(args.replica_qualification))
    atomic_json(args.output.resolve(), report)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
