# SPDX-License-Identifier: AGPL-3.0-only
"""A15 intake admits the A5 benchmark evidence v2 envelope only when it is bound
to the pre-registered calibrated-load protocol v2.

v1 stays admitted for historical r33 evidence; any other benchmark envelope is
still refused as not allowlisted.  The envelopes here are real bytes written by
the gate's own sealing function from a synthetic run fixture.
"""

from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

import mycelium_a15_run_intake as intake
import scripts.run_a5_benchmark_gate as benchmark_gate
from tests.a5_acceptance.materiality_harness import evaluate_benchmark_v2, protocol_v2_digest
from tests.a5_acceptance.test_materiality_protocol_v2 import _run_v2

NOW = datetime(2026, 9, 28, tzinfo=timezone.utc)
V1 = "mycelium.a5_benchmark_evidence.v1"
V2 = "mycelium.a5_benchmark_evidence.v2"
UNBOUND = intake.F_A5_V2_UNBOUND


def _envelope() -> dict:
    fixture = _run_v2([0.2] * 16, baseline_completed={2: 40})
    return benchmark_gate.seal_benchmark_evidence(
        fixture=fixture,
        decision=evaluate_benchmark_v2(fixture),
        deployment_id="deployment-intake-test",
        route_generation=7,
        provenance={
            "software_digest": "sha256:" + "d" * 64,
            "configuration_digest": "sha256:" + "c" * 64,
            "instrumentation_digest": "sha256:" + "e" * 64,
        },
        started_at_unix_ms=1_000,
        finished_at_unix_ms=2_000,
        observed={"arrival_rate_per_second": 1.0},
    )


def _intake(tmp_path: Path, payload: object, protocol: str | None) -> tuple[dict, set[str]]:
    path = tmp_path / "a5-benchmark.json"
    data = json.dumps(payload, sort_keys=True).encode("utf-8")
    path.write_bytes(data)
    artifact = {
        "artifact_id": "a5_benchmark",
        "path": path.name,
        "declared_digest": "sha256:" + hashlib.sha256(data).hexdigest(),
        "protocol": protocol,
        "provenance": "live",
        "required": True,
        "fresh_until": None,
    }
    findings: set[str] = set()
    entry = intake._intake_artifact(artifact, tmp_path, findings, NOW)
    return entry, findings


def test_pinned_digest_is_the_registered_v2_protocol_digest() -> None:
    assert intake.A5_BENCHMARK_PROTOCOL_V2_DIGEST == protocol_v2_digest()
    assert intake.A5_BENCHMARK_EVIDENCE_V2_PROTOCOL == V2 == benchmark_gate.EVIDENCE_PROTOCOL_V2


def test_allowlist_admits_v1_and_v2_benchmark_envelopes_only() -> None:
    admitted = {
        protocol
        for protocol in intake.ALLOWED_ARTIFACT_PROTOCOLS
        if protocol.startswith("mycelium.a5_benchmark_evidence.")
    }
    assert admitted == {V1, V2}


def test_bound_v2_envelope_is_admitted_without_findings(tmp_path: Path) -> None:
    entry, findings = _intake(tmp_path, _envelope(), V2)
    assert findings == set()
    assert entry["verified"] is True
    assert entry["observed_protocol"] == V2


def test_v1_envelope_stays_admitted_for_history(tmp_path: Path) -> None:
    historical = {
        "protocol": V1,
        "qualification_claim": False,
        "promotion_authorized": False,
        "decision": {"decision": "inconclusive"},
    }
    entry, findings = _intake(tmp_path, historical, V1)
    assert findings == set()
    assert entry["verified"] is True


@pytest.mark.parametrize(
    "protocol",
    ["mycelium.a5_benchmark_evidence.v3", "mycelium.a5_benchmark_evidence"],
)
def test_other_benchmark_envelopes_are_still_refused(tmp_path: Path, protocol: str) -> None:
    payload = dict(_envelope(), protocol=protocol)
    _, findings = _intake(tmp_path, payload, protocol)
    assert intake.F_PROTO_UNKNOWN in findings


def _tampered(mutate) -> dict:
    envelope = copy.deepcopy(_envelope())
    mutate(envelope)
    return envelope


OTHER_DIGEST = "sha256:" + "5" * 64


@pytest.mark.parametrize(
    "mutate",
    [
        lambda e: e.__setitem__("benchmark_protocol_digest", OTHER_DIGEST),
        lambda e: e["run_fixture"].__setitem__("benchmark_protocol_digest", OTHER_DIGEST),
        lambda e: e["replica_benchmark"].__setitem__(
            "benchmark_protocol_digest", OTHER_DIGEST
        ),
        lambda e: e.__setitem__("benchmark_protocol", "mycelium.a5_benchmark_acceptance.v1"),
        lambda e: e.pop("replica_benchmark"),
        lambda e: e["replica_benchmark"].__setitem__("decision", "not_material"),
        lambda e: e["replica_benchmark"].__setitem__("valid_pair_ids", list(range(16))),
        lambda e: e.__setitem__("promotion_authorized", True),
    ],
    ids=[
        "envelope_digest",
        "run_fixture_digest",
        "record_digest",
        "acceptance_protocol",
        "record_missing",
        "record_decision_forged",
        "excluded_pair_rescored",
        "promotion_claimed",
    ],
)
def test_v2_envelope_not_bound_to_the_registered_protocol_is_a_finding(
    tmp_path: Path, mutate
) -> None:
    entry, findings = _intake(tmp_path, _tampered(mutate), V2)
    assert UNBOUND in findings
    assert entry["finding"] == UNBOUND


def test_v2_declared_over_unreadable_bytes_is_a_finding(tmp_path: Path) -> None:
    path = tmp_path / "a5-benchmark.json"
    path.write_bytes(b"{not json")
    artifact = {
        "artifact_id": "a5_benchmark",
        "path": path.name,
        "declared_digest": "sha256:" + hashlib.sha256(b"{not json").hexdigest(),
        "protocol": V2,
        "provenance": "live",
        "required": True,
        "fresh_until": None,
    }
    findings: set[str] = set()
    intake._intake_artifact(artifact, tmp_path, findings, NOW)
    assert UNBOUND in findings


def test_v2_bytes_declared_as_v1_are_still_checked(tmp_path: Path) -> None:
    envelope = _tampered(
        lambda e: e.__setitem__("benchmark_protocol_digest", OTHER_DIGEST)
    )
    _, findings = _intake(tmp_path, envelope, V1)
    assert intake.F_PROTO_MISMATCH in findings
    assert UNBOUND in findings
