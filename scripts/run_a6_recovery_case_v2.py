#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Explicit prospective case-v2 reader for ``cleanup_authority_failure``.

Two modes:

``--observation ABSOLUTE_PATH`` is the historical shape reader. A valid file
cannot itself establish an executed product observation or physical
qualification, so this mode never emits pass evidence.

``--operator-plan PLAN`` is the gate mode admitted under owner ruling R6. Like
the v1 trusted runner it takes nothing but the operator plan: it reads the
product's source-owned v2 envelope below that plan's evidence directory, checks
the closed shape, the run and operator-plan bindings, and the owner-amended
count-layer clause (``reconcile_count_layers`` must authorize the envelope; since
the 2026-10-06 ruling only the pre-cutover refusal is authorized), and only then
derives the privacy-reduced v2 case record the gate consumes.
Authorization is checked before any plan or evidence access.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mycelium_a6_cleanup_observation_v2 import (
    CASE,
    reconcile_count_layers,
    validate_observation,
)
from scripts.run_a6_recovery_case import (
    EXACT_FLEET_GRANT,
    _canonical,
    _operator_plan_digest,
    _read_private_json,
    _sha256,
    _write_private_json,
)

RECORD_PROTOCOL = "mycelium.a6_physical_recovery_case.v2"
OBSERVATION_FILENAME = f"{CASE}.v2.json"


def _gate_record(operator_plan: Path) -> dict:
    from mycelium_physical_runner import load_operator_plan

    config = load_operator_plan(operator_plan)
    source = (Path(config.evidence_output_dir) / "a6-production-observations"
              / OBSERVATION_FILENAME)
    document = _read_private_json(source)
    observation = validate_observation(document)
    bindings = document["bindings"]
    if (bindings["operator_plan_digest"] != _operator_plan_digest(operator_plan)
            or bindings["run_id"] != config.run_id):
        raise ValueError("cleanup_v2_plan_binding_mismatch")
    layers = reconcile_count_layers(
        command_authority=observation["command_authority"],
        gateway_authority=observation["gateway_authority"],
        cleanup_authority={
            "request_id": bindings["request_id"],
            "request_attempt": bindings["request_attempt"],
            "stale_reservation_count": observation["stale_reservation_count"],
            "process_cleanup_complete": observation["process_cleanup_complete"],
        },
        cutover_committed=observation["cutover_committed"],
    )
    if layers["v2_envelope_authorized"] is not True:
        raise ValueError("cleanup_v2_count_layers_unauthorized")
    return {
        "protocol": RECORD_PROTOCOL,
        "contract_version": "v2",
        "case_id": CASE,
        "executed": True,
        "result": "passed",
        "source_digest": bindings["source_digest"],
        "candidate_digest": bindings["candidate_digest"],
        "spec_digest": bindings["spec_digest"],
        "contract_digest": bindings["contract_digest"],
        "run_id": bindings["run_id"],
        "deployment_id": bindings["deployment_id"],
        "request_id": bindings["request_id"],
        "request_attempt": bindings["request_attempt"],
        "count_layer_state": layers["layer_state"],
        "observation_digest": _sha256(_canonical(observation)),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", choices=(CASE,), required=True)
    parser.add_argument("--contract-version", choices=("v2",), required=True)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--observation", type=Path)
    source.add_argument("--operator-plan", type=Path)
    args = parser.parse_args(argv)
    if args.operator_plan is not None:
        # Authorization is checked before plan or evidence access.
        if os.environ.get("MYCELIUM_A6_FLEET_GRANT") != EXACT_FLEET_GRANT:
            print("explicit_fleet_grant_required", file=sys.stderr)
            return 2
        output_raw = os.environ.get("MYCELIUM_A6_CASE_EVIDENCE_FILE")
        try:
            if not output_raw or not Path(output_raw).is_absolute():
                raise ValueError("physical_case_evidence_path_required")
            record = _gate_record(args.operator_plan)
            _write_private_json(Path(output_raw), record)
        except (ValueError, OSError, TypeError, KeyError) as exc:
            print(f"cleanup_v2_invalid:{exc}", file=sys.stderr)
            return 1
        print(json.dumps(record, indent=2, sort_keys=True))
        return 0
    try:
        if not args.observation.is_absolute():
            raise ValueError("absolute_observation_path_required")
        value = _read_private_json(args.observation)
        validate_observation(value)
    except (ValueError, OSError, TypeError) as exc:
        parser.exit(2, f"cleanup_v2_invalid:{exc}\n")
    print(json.dumps({"protocol": "mycelium.a6_cleanup_case_shape_check.v2",
                      "case_id": CASE, "shape_validated": True,
                      "product_proof": False, "physical_qualified": False}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
