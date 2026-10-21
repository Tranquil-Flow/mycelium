# SPDX-License-Identifier: AGPL-3.0-only
"""Prospective v2 contract; frozen v1 remains a separate oracle."""
import copy
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.run_a6_recovery_case import validate_case_observation as old_reader
from mycelium_a6_cleanup_observation_v2 import validate_observation, collect_local_authorities


def sample():
    request, attempt = "request-1", 2
    bindings = {"request_id": request, "request_attempt": attempt,
                "source_digest": "sha256:" + "a" * 64,
                "candidate_digest": "sha256:" + "b" * 64,
                "spec_digest": "sha256:" + "c" * 64,
                "contract_digest": "sha256:" + "d" * 64,
                "operator_plan_digest": "sha256:" + "e" * 64,
                "run_id": "run-1", "deployment_id": "deployment-1",
                "deployment_epoch": 1, "source_path_id": "source-1",
                "source_attempt": 1, "source_path_attempt": 0,
                "source_path_generation": 1,
                "successor_path_id": "successor-1", "successor_attempt": 2,
                "successor_path_attempt": 1, "successor_path_generation": 2}
    observation = {
        "case_id": "cleanup_authority_failure", "executed": True,
        "ordinary_product_path": True, "private_material_exposed": False,
        "rejection_observed": True, "fail_closed": True,
        "terminal_state": "TERMINAL_BLOCKED", "unpublishable_suffix_discarded": True,
        "process_cleanup_complete": True, "stale_reservation_count": 0,
        # Owner ruling 2026-10-06: refused before cutover. The request keeps the
        # source's route identity, the pre-cutover abort ended the coordinator
        # request, nothing was published twice, and the refused successor's
        # command terminal is never committed (zero CAS).
        "cutover_committed": False, "committed_route_attempt": 1,
        "coordinator_terminal": "failed", "duplicated_token_count": 0,
        "command_authority": {"request_id": request, "request_attempt": attempt,
                              "registered_command_count": 1, "terminal_cas_count": 0},
        "gateway_authority": {"request_id": request, "request_attempt": attempt,
                              "terminal_event_count": 1, "terminal_kind": "failed",
                              "terminal_code": "request_cleanup_unproven"},
    }
    body = {"protocol": "mycelium.a6_cleanup_authority_observation.v3",
            "authority": "command_controller_and_request_gateway",
            "case_id": "cleanup_authority_failure", "bindings": bindings,
            "observation": observation}
    return {**body, "observation_digest": "sha256:" + hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()}


def resign(doc):
    doc = copy.deepcopy(doc)
    body = {k: v for k, v in doc.items() if k != "observation_digest"}
    doc["observation_digest"] = "sha256:" + hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
    return doc


def test_v2_accepts_distinct_bound_authorities_and_v1_remains_rejecting():
    doc = sample()
    # Owner ruling 2026-10-06: the refused successor commits no terminal CAS.
    assert validate_observation(doc)["command_authority"]["terminal_cas_count"] == 0
    old = {k: v for k, v in doc["observation"].items() if k not in ("command_authority", "gateway_authority")}
    old["terminal_event_count"] = 1
    with pytest.raises(ValueError, match="physical_observation_invalid"):
        old_reader("cleanup_authority_failure", old)


@pytest.mark.parametrize("path,value", [
    (("gateway_authority", "terminal_event_count"), 0),
    (("gateway_authority", "terminal_event_count"), 2),
    (("gateway_authority", "terminal_kind"), "completed"),
    (("gateway_authority", "terminal_code"), "other"),
    (("gateway_authority", "request_id"), "foreign"),
    (("gateway_authority", "request_attempt"), 1),
    (("command_authority", "request_id"), "foreign"),
    (("command_authority", "request_attempt"), 3),
    # 2026-10-06 ruling: the refused successor's terminal is never committed;
    # the cardinality is exact (zero), so one or more committed CAS is refused.
    (("command_authority", "terminal_cas_count"), 1),
    (("command_authority", "terminal_cas_count"), 2),
    (("command_authority", "terminal_cas_count"), None),
    (("command_authority", "registered_command_count"), 0),
    (("command_authority", "registered_command_count"), None),
    (("bindings", "request_attempt"), None),
    (("observation", "rejection_observed"), False),
    (("observation", "unpublishable_suffix_discarded"), False),
    (("observation", "process_cleanup_complete"), False),
    # A committed cutover, or any route identity but the source's, fails.
    (("observation", "cutover_committed"), True),
    (("observation", "cutover_committed"), None),
    (("observation", "committed_route_attempt"), 2),
    (("observation", "committed_route_attempt"), None),
    # Anything published twice, or a coordinator not ended failed, fails.
    (("observation", "duplicated_token_count"), 1),
    (("observation", "duplicated_token_count"), None),
    (("observation", "coordinator_terminal"), None),
    (("observation", "coordinator_terminal"), "completed"),
    (("observation", "terminal_state"), "FAILED"),
    (("bindings", "private_key"), "secret"),
    (("observation", "checkpoint_path"), "/secret"),
    (("command_authority", "encoded_prompt_token_ids"), [1]),
])
def test_invalid_even_with_valid_digest(path, value):
    doc = sample()
    target = doc["observation"] if path[0] in ("command_authority", "gateway_authority", "observation") else doc
    if path[0] == "observation":
        target[path[1]] = value
    else:
        target[path[0]][path[1]] = value
    with pytest.raises(ValueError):
        validate_observation(resign(doc))


def test_missing_and_extra_receipt_rejected():
    doc = sample()
    del doc["observation"]["gateway_authority"]["terminal_code"]
    with pytest.raises(ValueError):
        validate_observation(resign(doc))
    doc = sample()
    doc["observation"]["command_authority"]["receipt"] = {"secret": "x"}
    with pytest.raises(ValueError):
        validate_observation(resign(doc))


def test_producer_unknown_not_zero():
    with pytest.raises(ValueError, match="command_authority_unavailable"):
        collect_local_authorities(None, None, "request-1", 2)


def test_explicit_case_v2_cli_shape_only(tmp_path):
    root = Path(__file__).resolve().parents[2]
    script = root / "scripts" / "run_a6_recovery_case_v2.py"
    tmp_path.chmod(0o700)
    observation = tmp_path / "observation.json"
    observation.write_text(json.dumps(sample()))
    observation.chmod(0o600)
    args = [sys.executable, str(script), "--case", "cleanup_authority_failure",
            "--contract-version", "v2", "--observation", str(observation)]
    good = subprocess.run(args, cwd=root, capture_output=True, text=True, check=False)
    assert good.returncode == 0, good.stderr
    result = json.loads(good.stdout)
    assert result["shape_validated"] is True
    assert result["physical_qualified"] is False
    assert result["product_proof"] is False
    assert "passed" not in result
    bad = subprocess.run(args[:5] + ["v1"] + args[6:], cwd=root,
                         capture_output=True, text=True, check=False)
    assert bad.returncode != 0
