from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parents[2]
RECOVERY = ROOT / "scripts/reconcile_a5_interrupted_attempt.py"
COMMIT = "2" * 40
TREE = "1" * 40
CYCLE = "a5-controlled-recovery-cycle"
MANIFEST_SHA = "sha256:" + "3" * 64


def _sha_bytes(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _sha_file(path: Path) -> str:
    return _sha_bytes(path.read_bytes())


def _write_json(path: Path, document: object) -> None:
    path.write_text(
        json.dumps(document, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def _artifact_input(path: Path) -> dict[str, Any]:
    return {
        "path": str(path),
        "sha256": _sha_file(path),
        "size_bytes": path.stat().st_size,
    }


def _identity(pid: int) -> dict[str, Any]:
    return {
        "pid": pid,
        "parent_pid": 1,
        "session_id": pid,
        "start_identity": f"controlled-{pid}",
        "start_identity_source": "os_ps_lstart",
        "observed_at_unix_ns": 1_000_000 + pid,
    }


def _case(
    tmp_path: Path,
    *,
    with_start: bool = False,
    later_statuses: dict[str, str] | None = None,
) -> tuple[Path, Path, dict[str, Any], dict[str, bytes]]:
    retained = tmp_path / "retained-evidence"
    retained.mkdir()
    frozen = retained / "frozen-input.json"
    frozen.write_text('{"frozen":true}\n', encoding="utf-8")
    frozen_mode = stat.S_IMODE(frozen.stat().st_mode)

    authorization = tmp_path / "authorization.json"
    authorization_document = {
        "protocol": "mycelium.integration_requalification_cycle_authorization.v23",
        "cycle_id": CYCLE,
        "candidate_commit": COMMIT,
        "candidate_tree": TREE,
        "source_manifest": {"sha256": MANIFEST_SHA},
        "qualification_claim": False,
        "promotion_authorized": False,
    }
    _write_json(authorization, authorization_document)
    authorization_ref = _artifact_input(authorization)

    start_observation: dict[str, Any] | None = None
    if with_start:
        claim = tmp_path / "attempt-claim.json"
        claim_binding = {
            "authorization_sha256": authorization_ref["sha256"],
            "candidate_tree": TREE,
            "child_argv_digest": "sha256:" + "5" * 64,
            "cycle_id": CYCLE,
            "expected_source_manifest_digest": MANIFEST_SHA,
            "source_manifest_digest": MANIFEST_SHA,
            "terminal_path_binding_digest": "sha256:" + "6" * 64,
        }
        claim_document = {
            "protocol": "mycelium.a5_benchmark_attempt_claim.v1",
            "state": "claimed",
            "attempt_id": _sha_bytes(
                json.dumps(
                    claim_binding,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                ).encode("utf-8")
            ),
            "attempt_binding": claim_binding,
            "claim_owner_identity": _identity(101),
            "claimed_at_unix_ms": 8_000,
            "detached_requested": True,
            "qualification_claim": False,
            "promotion_authorized": False,
        }
        _write_json(claim, claim_document)
        started = tmp_path / "attempt-started.json"
        started_document = {
            "protocol": "mycelium.a5_benchmark_attempt_started.v1",
            "state": "workload_started",
            "attempt_id": claim_document["attempt_id"],
            "attempt_claim_sha256": _sha_file(claim),
            "supervisor_identity": _identity(102),
            "child_identity": _identity(103),
            "started_at_unix_ms": 8_100,
            "qualification_claim": False,
            "promotion_authorized": False,
        }
        _write_json(started, started_document)
        start_observation = {
            "claim": _artifact_input(claim),
            "started": _artifact_input(started),
        }

    later: dict[str, Any] | None = None
    if later_statuses is not None:
        collector = tmp_path / "controlled-collector.py"
        collector.write_text("# controlled local collector fixture\n", encoding="utf-8")
        hosts = []
        for index, host_id in enumerate(sorted(later_statuses)):
            status = later_statuses[host_id]
            if status == "observed":
                ack = tmp_path / f"cleanup-{host_id}.json"
                _write_json(
                    ack,
                    {
                        "protocol": "mycelium.controller_remote_cleanup_ack.v1",
                        "node_id": host_id,
                        "staging_root": f"/controlled/{host_id}/stage",
                        "removed": True,
                    },
                )
                hosts.append(
                    {
                        "host_id": host_id,
                        "status": "observed",
                        "sampled_at_unix_ms": 9_000 + index,
                        "ownership_marker": {
                            "matched": True,
                            "sha256": "sha256:" + f"{index + 7:x}" * 64,
                        },
                        "cleanup_acknowledgment": _artifact_input(ack),
                        "post_cleanup": {
                            "process_count": 0,
                            "listener_count": 0,
                            "socket_count": 0,
                            "runtime_stage_root_exists": False,
                        },
                    }
                )
            else:
                hosts.append(
                    {
                        "host_id": host_id,
                        "status": status,
                        "sampled_at_unix_ms": None,
                        "ownership_marker": None,
                        "cleanup_acknowledgment": None,
                        "post_cleanup": None,
                    }
                )
        later = {
            "observed_at_unix_ms": 9_100,
            "maximum_age_ms": 1_000,
            "expected_host_ids": sorted(later_statuses),
            "collector": {
                "tool_commit": "7" * 40,
                "tool_tree": "8" * 40,
                "source": _artifact_input(collector),
            },
            "hosts": hosts,
        }

    terminals = {
        "success": str(tmp_path / "absent-success.json"),
        "failure": str(tmp_path / "absent-failure.json"),
        "supervisor_receipt": str(tmp_path / "absent-supervisor.json"),
        "sentinel": str(tmp_path / "absent-sentinel.json"),
    }
    recovery_input = {
        "protocol": "mycelium.a5_interrupted_attempt_recovery_operator_input.v1",
        "historical_candidate": {
            "cycle_id": CYCLE,
            "candidate_commit": COMMIT,
            "candidate_tree": TREE,
            "source_manifest_sha256": MANIFEST_SHA,
        },
        "authorization": authorization_ref,
        "start_observation": start_observation,
        "terminal_artifact_paths": terminals,
        "frozen_inputs": [_artifact_input(frozen)],
        "retained_evidence_roots": [str(retained)],
        "removable_runtime_stage_roots": [str(tmp_path / "runtime-stage")],
        "historical_unknowns": {
            "exit_code": None,
            "exit_time_utc": None,
            "signal": None,
            "cause": None,
            "historical_signed_request_cleanup": None,
        },
        "later_resource_release_observation": later,
    }
    input_path = tmp_path / "recovery-input.json"
    _write_json(input_path, recovery_input)
    output = tmp_path / "reconciliation.json"
    preserved = {
        str(frozen): frozen.read_bytes(),
        str(authorization): authorization.read_bytes(),
    }
    assert stat.S_IMODE(frozen.stat().st_mode) == frozen_mode
    return input_path, output, recovery_input, preserved


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    for name in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"):
        environment.pop(name, None)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run(
        [sys.executable, str(RECOVERY), *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        env=environment,
    )


def _produce(input_path: Path, output: Path) -> subprocess.CompletedProcess[str]:
    return _run(
        "produce",
        "--input",
        str(input_path),
        "--output",
        str(output),
        "--recorded-at-unix-ms",
        "10000",
    )


def test_recovery_producer_preserves_unknowns_and_never_claims_success(tmp_path) -> None:
    input_path, output, _, preserved = _case(tmp_path)

    produced = _produce(input_path, output)

    assert produced.returncode == 0, produced.stderr
    document = json.loads(output.read_text("utf-8"))
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert document["protocol"] == "mycelium.a5_interrupted_attempt_reconciliation.v1"
    assert document["authority"] == {
        "authenticated_live_measurement": False,
        "signed": False,
        "trust_boundary": "unsigned_operator_recovery_accounting_only",
    }
    assert document["historical_candidate"] == {
        "cycle_id": CYCLE,
        "candidate_commit": COMMIT,
        "candidate_tree": TREE,
        "source_manifest_sha256": MANIFEST_SHA,
    }
    assert document["historical_observation"] == {
        "workload_started": "unknown",
        "exit_code": None,
        "exit_time_utc": None,
        "signal": None,
        "cause": None,
        "historical_signed_request_cleanup": None,
    }
    assert document["disposition"] == {
        "outcome": "invalid_no_result",
        "attempt_consumed": True,
        "resumable": False,
        "retry_authorized": False,
        "qualification_claim": False,
        "benchmark_pass": False,
        "promotion_authorized": False,
        "fresh_run_authorized": False,
    }
    assert all(
        artifact == {"exists": False, "path": artifact["path"], "sha256": None, "size_bytes": 0}
        for artifact in document["terminal_artifacts"].values()
    )
    assert document["later_resource_release"] is None
    assert document["producer"]["commit"] != COMMIT
    assert document["producer"]["tree"] != TREE
    assert document["record_sha256"].startswith("sha256:")
    for path, content in preserved.items():
        assert Path(path).read_bytes() == content

    verified = _run("verify", "--record", str(output))
    assert verified.returncode == 0, verified.stderr
    assert json.loads(verified.stdout) == {
        "protocol": "mycelium.a5_interrupted_attempt_reconciliation_verification.v1",
        "record_sha256": document["record_sha256"],
        "valid": True,
    }


def test_matching_claim_and_started_record_are_required_for_started_true(tmp_path) -> None:
    input_path, output, _, _ = _case(tmp_path, with_start=True)

    produced = _produce(input_path, output)

    assert produced.returncode == 0, produced.stderr
    document = json.loads(output.read_text("utf-8"))
    assert document["historical_observation"]["workload_started"] is True
    assert document["start_observation"]["status"] == "source_bound_observed"
    assert document["start_observation"]["claim"]["exists"] is True
    assert document["start_observation"]["started"]["exists"] is True


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        ("candidate_drift", "authorization_candidate_mismatch"),
        ("partial_start", "start_observation_partial"),
        ("terminal_present", "historical_terminal_artifact_present"),
        ("retained_runtime_overlap", "evidence_runtime_root_overlap"),
    ],
)
def test_recovery_producer_rejects_drift_partial_or_retrofit(
    tmp_path, mutation: str, reason: str
) -> None:
    input_path, output, recovery_input, _ = _case(tmp_path, with_start=True)
    if mutation == "candidate_drift":
        recovery_input["historical_candidate"]["candidate_tree"] = "9" * 40
    elif mutation == "partial_start":
        recovery_input["start_observation"]["started"] = None
    elif mutation == "terminal_present":
        terminal = Path(recovery_input["terminal_artifact_paths"]["success"])
        terminal.write_text("{}\n", encoding="utf-8")
    elif mutation == "retained_runtime_overlap":
        recovery_input["removable_runtime_stage_roots"] = recovery_input[
            "retained_evidence_roots"
        ]
    _write_json(input_path, recovery_input)

    produced = _produce(input_path, output)

    assert produced.returncode == 2
    assert json.loads(produced.stdout)["reason_code"] == reason
    assert not output.exists()


def test_recovery_output_name_is_exclusive_for_concurrent_or_restarted_callers(
    tmp_path,
) -> None:
    input_path, output, _, _ = _case(tmp_path)
    command = [
        sys.executable,
        str(RECOVERY),
        "produce",
        "--input",
        str(input_path),
        "--output",
        str(output),
        "--recorded-at-unix-ms",
        "10000",
    ]
    environment = os.environ.copy()
    for name in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"):
        environment.pop(name, None)
    contenders = [
        subprocess.Popen(
            command,
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=environment,
        )
        for _ in range(2)
    ]
    results = []
    for contender in contenders:
        stdout, stderr = contender.communicate(timeout=30)
        results.append((contender.returncode, stdout, stderr))

    assert sorted(result[0] for result in results) == [0, 2]
    rejected = next(result for result in results if result[0] == 2)
    assert json.loads(rejected[1])["reason_code"] == "output_exists"
    assert _run("verify", "--record", str(output)).returncode == 0


@pytest.mark.parametrize(
    "mutation",
    [
        "qualification_claim",
        "benchmark_pass",
        "unknown_cause",
        "candidate_producer_conflation",
        "terminal_fabrication",
        "fleet_release_claim",
        "record_digest",
    ],
)
def test_verifier_rejects_forbidden_claims_and_tampering(tmp_path, mutation: str) -> None:
    input_path, output, _, _ = _case(tmp_path)
    assert _produce(input_path, output).returncode == 0
    document = json.loads(output.read_text("utf-8"))
    if mutation == "qualification_claim":
        document["disposition"]["qualification_claim"] = True
    elif mutation == "benchmark_pass":
        document["disposition"]["benchmark_pass"] = True
    elif mutation == "unknown_cause":
        document["historical_observation"]["cause"] = "inferred"
    elif mutation == "candidate_producer_conflation":
        document["producer"]["commit"] = document["historical_candidate"][
            "candidate_commit"
        ]
        document["producer"]["tree"] = document["historical_candidate"][
            "candidate_tree"
        ]
    elif mutation == "terminal_fabrication":
        document["terminal_artifacts"]["success"]["exists"] = True
    elif mutation == "fleet_release_claim":
        document["later_resource_release"] = {"fleet_released": True}
    elif mutation == "record_digest":
        document["record_sha256"] = "sha256:" + "0" * 64
    _write_json(output, document)

    verified = _run("verify", "--record", str(output))

    assert verified.returncode == 2
    assert json.loads(verified.stdout)["valid"] is False


def test_verifier_rejects_frozen_input_drift_and_missing_inputs(tmp_path) -> None:
    input_path, output, recovery_input, _ = _case(tmp_path)
    assert _produce(input_path, output).returncode == 0
    frozen = Path(recovery_input["frozen_inputs"][0]["path"])
    frozen.write_text('{"frozen":false}\n', encoding="utf-8")

    drifted = _run("verify", "--record", str(output))

    assert drifted.returncode == 2
    assert json.loads(drifted.stdout)["reason_code"] == "bound_artifact_drift"
    frozen.unlink()
    missing = _run("verify", "--record", str(output))
    assert missing.returncode == 2
    assert json.loads(missing.stdout)["reason_code"] == "bound_artifact_missing"


def test_verifier_rejects_nonrestrictive_record_mode(tmp_path) -> None:
    input_path, output, _, _ = _case(tmp_path)
    assert _produce(input_path, output).returncode == 0
    output.chmod(0o644)

    verified = _run("verify", "--record", str(output))

    assert verified.returncode == 2
    assert json.loads(verified.stdout)["reason_code"] == "record_mode_invalid"


def test_recovery_reader_rejects_symlink_and_duplicate_json_keys(tmp_path) -> None:
    input_path, output, _, _ = _case(tmp_path)
    alias = tmp_path / "input-alias.json"
    alias.symlink_to(input_path)

    symlinked = _produce(alias, output)

    assert symlinked.returncode == 2
    assert json.loads(symlinked.stdout)["reason_code"] == "input_not_regular"
    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text(
        input_path.read_text("utf-8").replace(
            "{\n", '{\n  "protocol": "duplicate",\n', 1
        ),
        encoding="utf-8",
    )
    duplicated = _produce(duplicate, output)
    assert duplicated.returncode == 2
    assert json.loads(duplicated.stdout)["reason_code"] == "duplicate_json_key"


def test_later_release_observation_is_separate_unsigned_and_fail_closed(tmp_path) -> None:
    input_path, output, _, _ = _case(
        tmp_path,
        later_statuses={"node-0": "observed", "node-2": "missing"},
    )

    produced = _produce(input_path, output)

    assert produced.returncode == 0, produced.stderr
    later = json.loads(output.read_text("utf-8"))["later_resource_release"]
    assert later["evidence_class"] == "unsigned_operator_observation"
    assert later["authenticated_live_measurement"] is False
    assert later["observation_complete"] is False
    assert later["fleet_released"] is False
    missing = next(host for host in later["hosts"] if host["host_id"] == "node-2")
    assert missing["resource_state"] is None
    assert missing["status"] == "missing"


def test_all_zero_later_observations_still_do_not_create_release_authority(
    tmp_path,
) -> None:
    input_path, output, _, _ = _case(
        tmp_path,
        later_statuses={"node-0": "observed", "node-2": "observed"},
    )

    produced = _produce(input_path, output)

    assert produced.returncode == 0, produced.stderr
    later = json.loads(output.read_text("utf-8"))["later_resource_release"]
    assert later["observation_complete"] is True
    assert later["fleet_released"] is False
    assert later["historical_signed_request_cleanup_established"] is False
    assert _run("verify", "--record", str(output)).returncode == 0


def test_preexisting_partial_output_is_never_replaced_or_reused(tmp_path) -> None:
    input_path, output, _, _ = _case(tmp_path)
    partial = b'{"protocol":"partial"'
    output.write_bytes(partial)

    produced = _produce(input_path, output)

    assert produced.returncode == 2
    assert json.loads(produced.stdout)["reason_code"] == "output_exists"
    assert output.read_bytes() == partial
    verified = _run("verify", "--record", str(output))
    assert verified.returncode == 2
    assert json.loads(verified.stdout)["reason_code"] == "json_invalid"


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        ("omit_host", "later_host_roster_incomplete"),
        ("stale_observed", "later_census_stale"),
        ("coerce_missing_to_zero", "unknown_resource_state_coerced"),
        ("collector_drift", "bound_artifact_drift"),
    ],
)
def test_later_observation_rejects_partial_stale_coerced_or_drifted_inputs(
    tmp_path, mutation: str, reason: str
) -> None:
    input_path, output, recovery_input, _ = _case(
        tmp_path,
        later_statuses={"node-0": "observed", "node-2": "missing"},
    )
    later = recovery_input["later_resource_release_observation"]
    if mutation == "omit_host":
        later["hosts"] = later["hosts"][:-1]
    elif mutation == "stale_observed":
        observed = next(host for host in later["hosts"] if host["status"] == "observed")
        observed["sampled_at_unix_ms"] = 1
    elif mutation == "coerce_missing_to_zero":
        missing = next(host for host in later["hosts"] if host["status"] == "missing")
        missing["post_cleanup"] = {
            "process_count": 0,
            "listener_count": 0,
            "socket_count": 0,
            "runtime_stage_root_exists": False,
        }
    elif mutation == "collector_drift":
        Path(later["collector"]["source"]["path"]).write_text(
            "# drifted controlled collector\n", encoding="utf-8"
        )
    _write_json(input_path, recovery_input)

    produced = _produce(input_path, output)

    assert produced.returncode == 2
    assert json.loads(produced.stdout)["reason_code"] == reason
    assert not output.exists()
