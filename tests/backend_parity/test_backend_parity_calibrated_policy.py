"""PV29 L1: the checked-in calibrated policy and calibration report are consistent and honest.

These tests activate once ``tolerances/backend-parity-qwen2.5-0.5b-instruct.v2.json`` and
``tolerances/backend-parity-calibration-mac-node-*.json`` are checked in (written by
``scripts/run_backend_parity.py calibrate``).  They are skipped, with that reason, before then.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mycelium_platform_neutral import backend_parity as bp

TOLERANCES = Path(__file__).resolve().parents[2] / "tolerances"
POLICY = TOLERANCES / "backend-parity-qwen2.5-0.5b-instruct.v2.json"
CORPUS = TOLERANCES / "backend-parity-corpus-qwen2.5-0.5b-instruct.v1.json"
REPORTS = sorted(TOLERANCES.glob("backend-parity-calibration-mac-node-*.json"))

pytestmark = pytest.mark.skipif(not POLICY.exists() or not REPORTS, reason="calibration artifacts not checked in yet")


def test_policy_is_valid_provisional_and_stamped_with_where_it_was_calibrated() -> None:
    policy = bp.load_policy(POLICY)
    assert policy["status"] == "provisional" and policy["frozen_at"] is None
    note = policy["calibration"]["note"]
    assert note.startswith("calibrated on mac-node 20") and note.endswith("provisional until the SB2 run")
    assert policy["wire"]["dtype"] == "float32"
    assert policy["model"]["revision"] == bp.load_corpus(CORPUS)["revision"]


def test_thresholds_are_never_looser_than_the_plan_prior_unless_flagged() -> None:
    policy = bp.load_policy(POLICY)
    prior = bp.validate_policy(bp.default_policy_v2())
    notes = policy["calibration"]["per_metric"]
    for gate in ("H", "Q0"):
        for metric, spec in policy["gates"][gate]["thresholds"].items():
            base = prior["gates"][gate]["thresholds"][metric]
            looser = spec["value"] > base["value"] if spec["op"] == "<=" else spec["value"] < base["value"]
            assert not looser or notes[gate][metric]["prior_exceeded"], (gate, metric)


def test_report_binds_this_policy_and_corpus_and_every_defect_failed() -> None:
    report = json.loads(REPORTS[-1].read_text(encoding="utf-8"))
    policy = bp.load_policy(POLICY)
    assert report["policy_digest"] == policy["policy_digest"]
    assert report["corpus_digest"] == bp.corpus_digest(bp.load_corpus(CORPUS))
    battery = report["battery"]
    assert battery["all_defects_fail"] and battery["all_controls_pass"] and not battery["problems"]
    assert battery["defects_total"] >= 20 and battery["controls_total"] >= 8
    assert report["label"].startswith("measured on mac-node") and "load_average_before" in report


def test_signed_primary_route_records_verify_under_the_policy() -> None:
    report = json.loads(REPORTS[-1].read_text(encoding="utf-8"))
    policy = bp.load_policy(POLICY)
    corpus = bp.load_corpus(CORPUS)
    assert len(report["primary_routes"]) >= 2
    for route in report["primary_routes"]:
        envelope = route["envelope"]
        record = bp.verify_route_record(
            envelope,
            policy=policy,
            now=envelope["record"]["created_at"] + 1.0,
            expected_key_digest=report["signer_key_digest"],
            corpus=corpus,
        )
        assert record["verdict"] == "partial_pass"
        assert record["gates"]["H"]["status"] == record["gates"]["Q0"]["status"] == record["gates"]["N"]["status"] == "pass"
        assert record["unevaluated_gates"] == ["G", "P"]
