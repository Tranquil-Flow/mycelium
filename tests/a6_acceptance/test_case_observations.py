# SPDX-License-Identifier: AGPL-3.0-or-later
"""Source-owned A6 observations round-trip through the trusted case runner."""
from __future__ import annotations

import json
import os
import shutil
import stat
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import tests.physical_runner.conftest as runner_plans
from mycelium_a6_case_observations import (
    CLEANUP_FILENAME,
    CaseObservationRecorder,
    ObservationRefused,
    SettledFacts,
    derive_observation,
    observation_authority,
    write_cleanup_observation,
)
from mycelium_a6_fault_control import EXACT_FLEET_GRANT
from mycelium_a6_product_fault_seams import ExecutedCase
from scripts import run_a6_recovery_case as runner

ROOT = Path(__file__).resolve().parents[2]
POSITIVE = "ordinary_browser_source_failure_recovery"


@pytest.fixture
def plan(tmp_path: Path):
    base = Path.home() / ".cache" / "mycelium-physical-runner-tests"
    base.mkdir(mode=0o700, parents=True, exist_ok=True)
    identity_root = base / f"a6-obs-{os.getpid()}-{tmp_path.name}"
    identity_root.mkdir(mode=0o700)
    runner_plans._SECURE_IDENTITY_ROOT = identity_root
    try:
        (tmp_path / "src").mkdir()
        path = runner_plans.write_operator_plan(
            tmp_path / "operator-plan.json", runner_plans.operator_plan_payload(tmp_path))
        yield path
    finally:
        runner_plans._SECURE_IDENTITY_ROOT = None
        shutil.rmtree(identity_root, ignore_errors=True)


def _binding(**overrides):
    value = {"deployment_id": "deployment-g4", "deployment_epoch": 3,
             "qualification_digest": "sha256:" + "c" * 64, "request_id": "request-1",
             "request_attempt": 1, "path_id": "path-source", "path_attempt": 0,
             "path_digest": "sha256:" + "d" * 64, "topology_generation": 4,
             "command_id": "request:request-1:attempt:1", "publisher_generation": 1,
             "cancellation_generation": 0, "source_node_id": "node-a", "source_attempt": 1,
             "absolute_deadline_ms": 900_000, "path_manifest_digest": "sha256:" + "d" * 64}
    value.update(overrides)
    return value


def _executed(case_id, post_state, *, committed=3, reservations=2):
    record = ExecutedCase(case_id=case_id, request_id="request-1", binding=_binding(),
                          source_node_id="node-a", armed_reservation_count=reservations,
                          deadline_unix_ms=1_800_000_060_000)
    record.post_state = dict(post_state)
    record.failure_marker = None if committed is None else (7, committed)
    return record


def _gateway(kind, code=None, count=1, committed=5):
    return {"terminal_count": count, "terminal_kind": kind, "terminal_code": code,
            "committed_count": committed, "expected_token_index": committed,
            "duplicated_token_count": 0, "late_source_publication_count": 0,
            "subscribed": True, "cleanup_notice_provenance": None}


def _settled(view, gateway, *, reservations=0, qualification=None):
    return SettledFacts(view=view, gateway=gateway, reservation_count=reservations,
                        coordinator_terminal="completed" if reservations == 0 else None,
                        qualification=qualification, deadline_unix_ms=1_800_000_060_000)


_REFUSED = {"source_failure_observed": True, "successor_candidate_reserved": True,
            "recovery_refused": True}
# A reserved successor advanced the command attempt; no cutover replaced the
# request's route identity.
_FAILED_VIEW = {"retained": False, "request_attempt": 2, "path_id": "path-successor",
                "path_attempt": 1, "terminal_status": "FAILED", "recovery_attempts": 1,
                "route_identity": {"request_attempt": 1, "path_id": "path-source",
                                   "path_attempt": 0}}


def _product_outcomes():
    """One settled product outcome per frozen case, as its authorities report it."""
    completed_view = {"retained": True, "request_attempt": 2, "path_id": "path-successor",
                      "path_attempt": 1, "terminal_status": "COMPLETED", "recovery_attempts": 1,
                      "recovery_phase": "recovered_decode",
                      "route_identity": {"request_attempt": 2, "path_id": "path-successor",
                                         "path_attempt": 1}}
    qualified = {"controlled_fault": None, "physical_challenge": "passed",
                 "compatibility_matches_source": True}
    aborted = _settled(_FAILED_VIEW, _gateway("failed", "recovery_failed"))
    outcomes = {
        POSITIVE: ({"source_failure_observed": True, "recovery_required": True},
                   _settled(completed_view, _gateway("completed"), qualification=qualified)),
        "no_successor": ({"source_failure_observed": True, "candidate_available": False,
                          "truthful_abort_required": True},
                         _settled({"retained": False, "request_attempt": 1, "path_id": "path-source",
                                   "path_attempt": 0, "terminal_status": "FAILED",
                                   "recovery_failure_reason": "recovery_successor_unavailable"},
                                  _gateway("failed", "recovery_failed"))),
        "successor_prefill_failure": ({"source_failure_observed": True,
                                       "successor_attempt_started": True,
                                       "successor_failure_injected": True}, aborted),
        "cancellation_before_recovery": ({"source_failure_observed": True,
                                          "cancellation_observed_before_recovery": True},
                                         _settled(_FAILED_VIEW, _gateway("cancelled"))),
        "cancellation_during_prefill": ({"source_failure_observed": True,
                                         "cancellation_observed_during_prefill": True},
                                        _settled(_FAILED_VIEW, _gateway("cancelled"))),
        "timeout_during_recovery": ({"source_failure_observed": True,
                                     "recovery_deadline_exceeded": True},
                                    _settled({**_FAILED_VIEW,
                                              "recovery_failure_reason": "recovery_timeout"},
                                             _gateway("failed", "deadline_exceeded"))),
        "exhausted_recovery_attempts": ({"source_failure_observed": True, "attempts_used": 2,
                                         "attempts_exhausted": True},
                                        _settled({**_FAILED_VIEW, "recovery_attempts": 2},
                                                 _gateway("failed", "recovery_failed"))),
        "stale_source_output_after_fencing": ({"source_failure_observed": True,
                                               "source_fenced": True,
                                               "late_publication_attempted": True,
                                               "late_publication_rejected": True}, aborted),
        "cleanup_reservation_release": ({"request_terminal": True, "release_observed": True,
                                         "release_count_positive": True},
                                        _settled({"retained": False, "request_attempt": 1,
                                                  "terminal_status": "COMPLETED"},
                                                 _gateway("completed"))),
        # Frozen v1 requires zero gateway terminal events for this case; this
        # outcome exercises the writer/reader mechanics under that contract.
        "cleanup_authority_failure": ({"cleanup_authority_refused": True,
                                       "unpublishable_suffix_discarded": True},
                                      _settled({"retained": True, "request_attempt": 1,
                                                "terminal_blocked_reason": "cleanup_unproven"},
                                               _gateway(None, count=0))),
        "shutdown_during_recovery": ({"source_failure_observed": True, "shutdown_observed": True},
                                     _settled(_FAILED_VIEW, _gateway("failed", "shutdown"))),
    }
    for case_id in ("incompatible_successor", "stale_qualification", "foreign_qualification",
                    "unexecuted_qualification", "mismatched_path_attempt"):
        outcomes[case_id] = (_REFUSED, aborted)
    return outcomes


def _run_case(case_id: str, operator_plan: Path, records: Path, monkeypatch) -> int:
    monkeypatch.setenv("MYCELIUM_A6_FLEET_GRANT", EXACT_FLEET_GRANT)
    monkeypatch.setenv("MYCELIUM_A6_CASE_EVIDENCE_FILE", str(records / f"{case_id}.json"))
    return runner.main(["--case", case_id, "--operator-plan", str(operator_plan)])


def test_all_sixteen_cases_and_cleanup_round_trip_through_the_trusted_runner(
        plan: Path, tmp_path: Path, monkeypatch):
    authority = observation_authority(operator_plan=plan, repo_root=ROOT)
    recorder = CaseObservationRecorder(authority=authority, target=SimpleNamespace())
    outcomes = _product_outcomes()
    assert sorted(outcomes) == sorted(runner.REQUIRED_CASES)
    for case_id in runner.REQUIRED_CASES:
        post, settled = outcomes[case_id]
        committed = None if case_id in ("cleanup_reservation_release",
                                        "cleanup_authority_failure") else 3
        recorder.write(case_id, _executed(case_id, post, committed=committed), settled)
    root = authority.evidence_output_dir / "a6-production-observations"
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    assert all(stat.S_IMODE(item.stat().st_mode) == 0o600 for item in root.iterdir())
    write_cleanup_observation(authority=authority, product_port=_free_port(),
                              physical_process_count=0, reservation_count=0)
    records = tmp_path / "records"
    records.mkdir(mode=0o700)
    for case_id in runner.REQUIRED_CASES:
        assert _run_case(case_id, plan, records, monkeypatch) == 0, case_id
        record = json.loads((records / f"{case_id}.json").read_text())
        assert record["result"] == "passed" and record["executed"] is True
        assert record["source_digest"] == authority.source_digest
        assert record["spec_digest"] == authority.spec_digest
        assert record["contract_digest"] == authority.contract_digest
        assert record["run_id"] == "run-0001"
    for check in runner.REQUIRED_CLEANUP_CHECKS:
        assert runner.main(["--cleanup-check", check, "--operator-plan", str(plan)]) == 0
    assert set(recorder.status()["written"]) == set(runner.REQUIRED_CASES)


def _free_port() -> int:
    import socket
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_zero_prefix_positive_fault_is_measured_but_never_written(plan: Path):
    authority = observation_authority(operator_plan=plan, repo_root=ROOT)
    recorder = CaseObservationRecorder(authority=authority, target=SimpleNamespace())
    post, settled = _product_outcomes()[POSITIVE]
    with pytest.raises(ObservationRefused, match="unsatisfied"):
        recorder.write(POSITIVE, _executed(POSITIVE, post, committed=0), settled)
    assert not (authority.evidence_output_dir / "a6-production-observations"
                / f"{POSITIVE}.json").exists()


@pytest.mark.parametrize("mutation", ["no_cutover", "stale_reservation", "duplicate", "late"])
def test_positive_observation_is_derived_from_authorities_not_asserted(plan: Path, mutation):
    authority = observation_authority(operator_plan=plan, repo_root=ROOT)
    recorder = CaseObservationRecorder(authority=authority, target=SimpleNamespace())
    post, settled = _product_outcomes()[POSITIVE]
    if mutation == "no_cutover":
        settled.view = {**settled.view, "route_identity": {
            "request_attempt": 1, "path_id": "path-source", "path_attempt": 0}}
    elif mutation == "stale_reservation":
        settled.reservation_count, settled.coordinator_terminal = 1, None
    elif mutation == "duplicate":
        settled.gateway = {**settled.gateway, "duplicated_token_count": 1}
    else:
        settled.gateway = {**settled.gateway, "late_source_publication_count": 1}
    with pytest.raises(ObservationRefused):
        recorder.write(POSITIVE, _executed(POSITIVE, post), settled)


def test_real_product_cleanup_notice_does_not_satisfy_the_frozen_v1_case(plan: Path):
    """The gateway publishes one request_cleanup_unproven notice; v1 wants zero."""
    authority = observation_authority(operator_plan=plan, repo_root=ROOT)
    recorder = CaseObservationRecorder(authority=authority, target=SimpleNamespace())
    post, settled = _product_outcomes()["cleanup_authority_failure"]
    settled.gateway = _gateway("failed", "request_cleanup_unproven", count=1)
    observation = derive_observation("cleanup_authority_failure",
                                     _executed("cleanup_authority_failure", post), settled)
    assert observation["terminal_state"] == "TERMINAL_BLOCKED"
    assert observation["terminal_event_count"] == 1
    with pytest.raises(ObservationRefused, match="unsatisfied"):
        recorder.write("cleanup_authority_failure",
                       _executed("cleanup_authority_failure", post), settled)


def test_observations_are_written_once_and_never_replaced(plan: Path):
    authority = observation_authority(operator_plan=plan, repo_root=ROOT)
    recorder = CaseObservationRecorder(authority=authority, target=SimpleNamespace())
    post, settled = _product_outcomes()["no_successor"]
    path = recorder.write("no_successor", _executed("no_successor", post), settled)
    before = path.read_bytes()
    with pytest.raises(ObservationRefused, match="exists"):
        recorder.write("no_successor", _executed("no_successor", post), settled)
    assert path.read_bytes() == before


def test_cleanup_observation_measures_the_real_listener(plan: Path, monkeypatch, tmp_path):
    import socket
    authority = observation_authority(operator_plan=plan, repo_root=ROOT)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        write_cleanup_observation(authority=authority, product_port=port,
                                  physical_process_count=0, reservation_count=0)
    document = json.loads((authority.evidence_output_dir / "a6-production-observations"
                           / CLEANUP_FILENAME).read_text())
    assert document["local_product_port_listener_count"] == 1
    monkeypatch.setenv("MYCELIUM_A6_FLEET_GRANT", EXACT_FLEET_GRANT)
    assert runner.main(["--cleanup-check", "local_product_port",
                        "--operator-plan", str(plan)]) == 1
    assert runner.main(["--cleanup-check", "reservations", "--operator-plan", str(plan)]) == 0
    with pytest.raises(ObservationRefused):
        write_cleanup_observation(authority=authority, product_port=port,
                                  physical_process_count=-1, reservation_count=0)


class _SettlingTarget:
    """Product reads for one request that settles after the fault."""

    def __init__(self):
        self.service = SimpleNamespace()
        self.qualifier = None
        self.calls = 0

    def view(self, _request_id):
        self.calls += 1
        if self.calls < 3:
            return {"retained": True, "request_attempt": 1, "recovery_phase": "successor_pending"}
        return {"retained": False, "recovery_failure_reason": "recovery_successor_unavailable"}

    def coordinator_terminal(self, _request_id):
        return "failed" if self.calls >= 3 else None

    def gateway_terminal_count(self, _request_id):
        return 1 if self.calls >= 3 else 0

    def reservation_count(self, _request_id):
        return 0


def test_recorder_settles_the_request_before_writing(plan: Path, monkeypatch):
    authority = observation_authority(operator_plan=plan, repo_root=ROOT)
    target = _SettlingTarget()
    recorder = CaseObservationRecorder(authority=authority, target=target, settle_seconds=5)
    import mycelium_a6_case_observations as module
    monkeypatch.setattr(module, "gateway_facts",
                        lambda *_args, **_kwargs: _gateway("failed", "recovery_failed"))
    post = {"source_failure_observed": True, "candidate_available": False,
            "truthful_abort_required": True}
    recorder.observe(_executed("no_successor", post))
    recorder.join(timeout=10)
    status = recorder.status()
    assert status["refused"] == {} and "no_successor" in status["written"]
    envelope = json.loads((authority.evidence_output_dir / "a6-production-observations"
                           / "no_successor.json").read_text())
    assert envelope["observation"]["terminal_state"] == "aborted"
    assert envelope["bindings"]["successor_attempt"] is None
    assert time.monotonic() > 0


def test_real_product_fault_is_observed_end_to_end_and_refused_without_a_prefix(
        plan: Path, monkeypatch, tmp_path):
    """Ordinary gateway/router/route composition: a real peer kill with no
    committed prefix is recorded as refused, never written as a pass."""
    import signal as signals

    from mycelium_a6_product_fault_control import ProductFaultControl
    from mycelium_a6_product_fault_seams import LiveProductFaultTarget, build_product_case_seams
    from tests.a6_acceptance.a6_measured_stack import build_measured_stack, settle
    from tests.a6_acceptance.test_product_fault_seams import _KillableSession, _post, _RealPeer

    authority = observation_authority(operator_plan=plan, repo_root=ROOT)
    with build_measured_stack(monkeypatch, tmp_path / "stack") as stack:
        source = stack.binding["source_node_id"]
        peer = _RealPeer(on_signal=lambda signum: (
            peer.child.wait(timeout=5), stack.hold.set()) if signum == signals.SIGKILL else None)
        try:
            stack.route._sessions[source] = _KillableSession(peer)
            target = LiveProductFaultTarget(router=stack.router, route=stack.route,
                                            service=stack.service, wait_seconds=10)
            recorder = CaseObservationRecorder(authority=authority, target=target,
                                               settle_seconds=20)
            target.add_listener(recorder.observe)
            with ProductFaultControl(grant=EXACT_FLEET_GRANT,
                                     seams=build_product_case_seams(target),
                                     product_port=8787) as control:
                assert _post(control.port, POSITIVE) == 200
            assert settle(stack) is True
            recorder.join(timeout=30)
        finally:
            stack.hold.set()
            peer.close()
    status = recorder.status()
    assert status["written"] == {}
    assert status["refused"][POSITIVE].startswith("a6_observation_unsatisfied")
    assert not (authority.evidence_output_dir / "a6-production-observations"
                / f"{POSITIVE}.json").exists()


def test_only_the_final_shutdown_of_a_run_measures_cleanup(plan: Path):
    from mycelium_live.supervisor import _A6FaultControlMount

    authority = observation_authority(operator_plan=plan, repo_root=ROOT)
    cleanup = authority.evidence_output_dir / "a6-production-observations" / CLEANUP_FILENAME
    router = SimpleNamespace(_coordinator=SimpleNamespace(status=lambda: {"requests": []}))
    for executed, expected in (((POSITIVE,), False), ((POSITIVE, "shutdown_during_recovery"), True)):
        control = SimpleNamespace(executed_case_ids=executed)
        recorder = CaseObservationRecorder(authority=authority, target=SimpleNamespace())
        _A6FaultControlMount(control, recorder, authority).finalize(
            product_port=_free_port(), processes=(), router=router)
        assert cleanup.exists() is expected
    document = json.loads(cleanup.read_text())
    assert (document["physical_process_count"], document["reservation_count"]) == (0, 0)
