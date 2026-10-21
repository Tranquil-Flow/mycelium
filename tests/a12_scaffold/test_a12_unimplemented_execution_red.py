# SPDX-License-Identifier: AGPL-3.0-only
"""WEAK-RED tests: A12 execution behaviour this scaffold deliberately does not have.

Every test in this file is RED in this lane and fails with ``A12NotImplementedError``
raised by this lane's OWN boundary functions in ``mycelium_mobile/a12_activation.py``.
That is WEAK evidence of absence: the failure is produced by the scaffold, not by a
demonstrated gap at the frozen base ``c573e0f``.  It is recorded here so the lane's
incompleteness is machine-visible, and so these tests can be driven to GREEN only by
landing the real execution path.

Do not cite a failure in this file as proof that the baseline lacks the behaviour.
Cite ``test_a12_strong_red_absent_behaviour.py`` (baseline absence, no scaffold
imported) and the measured facts in the A12 implementation contract for that.

Payloads here are deliberately plausible so that shape validation passes and the
refusal is the only thing standing between the caller and a result.

Run with:

    env -u PYTHONPATH python3.14 -m pytest tests/a12_scaffold -q

``PYTHONPATH`` is cleared because this host sets it to a Python 3.11 virtualenv whose
pytest plugins cannot load under Python 3.14; the flag changes nothing about the
code under test.
"""

from __future__ import annotations

from pathlib import Path
from typing import NoReturn

import pytest

import mycelium_mobile.a12_activation as a12

OBSERVATION: dict[str, object] = {
    "protocol": "mycelium.mobile_runtime_observation.v1",
    "platform_family": "android",
    "runtime_build_digest": "sha256:" + "1" * 64,
    "profile_generation": 3,
    "evidence_generation": 4,
    "source_kind": "native_device_observation",
    "observed_at_unix_ms": 1_754_000_000_000.0,
    "measurement_duration_ms": 600_000.0,
    "sample_count": 60,
    "authority": "a9_role_class_qualifier",
    "metrics": {"sustainable_memory_bytes": 1.5e9},
}

WEAK_RED = (
    "WEAK RED (raised by this lane's own scaffold, not by a demonstrated baseline gap)"
)


def _weak_red(entry_point: str, error: BaseException, demanded: str) -> NoReturn:
    pytest.fail(
        f"{WEAK_RED}: {entry_point} refused with {str(error)!r}.  The behaviour this "
        f"test demands ({demanded}) does not exist because this lane deliberately "
        "did not implement it.  Cite "
        "tests/a12_scaffold/test_a12_strong_red_absent_behaviour.py and the A12 "
        "implementation contract for baseline absence, not this failure.",
        pytrace=False,
    )


def test_enrollment_grants_signed_membership_and_keeps_route_ready_false() -> None:
    try:
        enrollment = a12.enroll_mobile_member(
            member_ref="member:1",
            platform_family="android",
            source_kind="native_device_observation",
            route_ready=False,
        )
    except a12.A12NotImplementedError as error:
        _weak_red(
            "enroll_mobile_member",
            error,
            "an A9 signed-membership grant binding a durable device key, consent "
            "and a fresh incarnation with route_ready=false",
        )

    assert enrollment["rung_id"] == "signed_membership"
    assert enrollment["route_ready"] is False
    assert enrollment["grants"] == ["current_signed_membership"]


def test_capability_eligibility_qualifies_one_exact_role_only() -> None:
    try:
        eligibility = a12.qualify_capability_eligibility(
            member_ref="member:1",
            platform_family="android",
            role="probe_contributor",
            observation=OBSERVATION,
        )
    except a12.A12NotImplementedError as error:
        _weak_red(
            "qualify_capability_eligibility",
            error,
            "an exact role eligibility decision built from fresh native "
            "observations, granting no artifact, operation or placement",
        )

    assert eligibility["rung_id"] == "capability_eligibility"
    assert eligibility["decision"] in a12.RUNG_DECISIONS
    assert eligibility["role"] == "probe_contributor"
    assert "artifact_readiness" in eligibility["cannot_grant"]


def test_artifact_readiness_promotes_only_the_assigned_artifact() -> None:
    try:
        readiness = a12.promote_assignment_artifact(
            member_ref="member:1",
            assignment_ref="assignment:1",
            artifact_digest="sha256:" + "0" * 64,
        )
    except a12.A12NotImplementedError as error:
        _weak_red(
            "promote_assignment_artifact",
            error,
            "an assignment-scoped artifact grant with integrity, quota and atomic "
            "promotion evidence",
        )

    assert readiness["rung_id"] == "artifact_readiness"
    assert readiness["decision"] == "eligible"
    assert readiness["assignment_ref"] == "assignment:1"


def test_operation_qualification_binds_one_exact_platform_runtime_build_and_role() -> None:
    try:
        qualification = a12.qualify_mobile_operation(
            member_ref="member:1",
            role="qualified_model_stage",
            platform_family="android",
            runtime_build_digest="sha256:" + "1" * 64,
            prior_rung_decisions={
                "signed_membership": "eligible",
                "capability_eligibility": "eligible",
                "artifact_readiness": "eligible",
            },
        )
    except a12.A12NotImplementedError as error:
        _weak_red(
            "qualify_mobile_operation",
            error,
            "a physical positive/negative qualification of one exact native "
            "platform/runtime/build operation",
        )

    assert qualification["rung_id"] == "operation_qualification"
    assert qualification["decision"] == "eligible"
    assert qualification["platform_family"] == "android"


def test_draft_binding_qualifies_only_against_a_physically_closed_a11() -> None:
    try:
        binding = a12.bind_speculative_draft_worker(
            member_ref="member:1",
            a11_binding_state="physically_closed",
            a11_binding_digest="sha256:" + "0" * 64,
        )
    except a12.A12NotImplementedError as error:
        _weak_red(
            "bind_speculative_draft_worker",
            error,
            "an optional draft-role binding proven against the exact physically "
            "closed A11 target-authoritative decision",
        )

    assert binding["claim_id"] == "speculative_draft_worker"
    assert binding["decision"] == "eligible"
    assert binding["a11_binding_state"] == "physically_closed"


def test_runtime_observation_is_produced_by_the_runtime_not_the_caller() -> None:
    try:
        observation = a12.observe_mobile_runtime(
            member_ref="member:1",
            platform_family="android",
            runtime_build_digest="sha256:" + "1" * 64,
        )
    except a12.A12NotImplementedError as error:
        _weak_red(
            "observe_mobile_runtime",
            error,
            "a runtime-owned mycelium.mobile_runtime_observation.v1 record produced "
            "by the device runtime itself",
        )

    assert a12.validate_mobile_runtime_observation(observation) == observation


def test_projection_reconstructs_the_rung_ladder_without_private_material() -> None:
    try:
        projection = a12.project_mobile_rungs(workspace="device_lab", member_ref="member:1")
    except a12.A12NotImplementedError as error:
        _weak_red(
            "project_mobile_rungs",
            error,
            "a privacy-reduced all-eight projection of the four rungs, roles, source "
            "kinds, freshness and blockers",
        )

    assert projection["workspace"] == "device_lab"
    assert tuple(projection["rungs"]) == a12.RUNG_IDS
    assert "endpoint_id" not in projection


def test_physical_gate_executes_positive_and_negative_cases() -> None:
    try:
        gate = a12.run_mobile_physical_gate(
            platform_family="android", roles=("probe_contributor",)
        )
    except a12.A12NotImplementedError as error:
        _weak_red(
            "run_mobile_physical_gate",
            error,
            "an owner-private executed physical positive and negative gate bundle "
            "for the exact platform and role",
        )

    assert gate["executed"] is True
    assert gate["platform_family"] == "android"
    assert gate["positive_cases"] >= 1
    assert gate["negative_cases"] >= 1


def test_guard_weak_red_failures_are_raised_by_this_lane_s_own_scaffold() -> None:
    """The failures in this file come from the scaffold, and this says so."""

    with pytest.raises(a12.A12NotImplementedError) as error:
        a12.project_mobile_rungs(workspace="nodes", member_ref="member:1")

    frames = [
        Path(entry.path).name
        for entry in (error.traceback or [])
        if getattr(entry, "path", None)
    ]
    assert a12.__file__.endswith("mycelium_mobile/a12_activation.py")
    assert "a12_activation.py" in frames
    assert error.value.code == "not_implemented:mobile_projection_absent"
