# SPDX-License-Identifier: AGPL-3.0-only
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


def _gateway(kind, code=None, count=1, committed=5, terminal_attempt=None):
    return {"terminal_count": count, "terminal_kind": kind, "terminal_code": code,
            "committed_count": committed, "expected_token_index": committed,
            "duplicated_token_count": 0, "late_source_publication_count": 0,
            "subscribed": True, "cleanup_notice_provenance": None,
            "terminal_attempt": terminal_attempt}


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
                   _settled(completed_view, _gateway("completed", terminal_attempt=2),
                            qualification=qualified)),
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
                                         "attempts_exhausted": True,
                                         "successor_nodes_failed": [["node-2"], ["node-3"]]},
                                        _settled({**_FAILED_VIEW, "recovery_attempts": 2},
                                                 _gateway("failed", "recovery_failed"),
                                                 qualification={
                                                     "successor_path_attempt": 2})),
        # Real fleet behaviour (owner ruling): the fenced source's late
        # publication is refused and the request completes on its successor.
        "stale_source_output_after_fencing": ({"source_failure_observed": True,
                                               "source_fenced": True,
                                               "late_publication_attempted": True,
                                               "late_publication_rejected": True},
                                              _settled(dict(completed_view),
                                                       _gateway("completed",
                                                                terminal_attempt=2))),
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


@pytest.mark.parametrize("mutation", ["no_cutover", "stale_reservation", "duplicate", "late",
                                      "gateway_attempt_missing", "gateway_attempt_foreign"])
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
    elif mutation == "late":
        settled.gateway = {**settled.gateway, "late_source_publication_count": 1}
    elif mutation == "gateway_attempt_missing":
        settled.gateway = {**settled.gateway, "terminal_attempt": None}
    else:
        settled.gateway = {**settled.gateway, "terminal_attempt": 1}
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


def test_a_refused_observation_is_logged_with_its_reason(plan: Path, caplog):
    import logging

    authority = observation_authority(operator_plan=plan, repo_root=ROOT)
    recorder = CaseObservationRecorder(authority=authority, target=SimpleNamespace())
    recorder._record(SimpleNamespace(case_id="no_successor", request_id="request-x"))
    status = recorder.status()
    assert "no_successor" in status["refused"]
    logged = [json.loads(record.getMessage()) for record in caplog.records
              if record.name == "mycelium_a6_case_observations"
              and record.levelno == logging.WARNING]
    assert logged == [{"case": "no_successor", "error_type": "AttributeError",
                       "event": "a6_case_observation_refused", "reason":
                       status["refused"]["no_successor"], "request_id": "request-x"}]


STALE = "stale_source_output_after_fencing"


def test_stale_source_output_completes_only_when_the_late_publication_is_refused(plan: Path):
    authority = observation_authority(operator_plan=plan, repo_root=ROOT)
    recorder = CaseObservationRecorder(authority=authority, target=SimpleNamespace())
    post, settled = _product_outcomes()[STALE]
    observation = derive_observation(STALE, _executed(STALE, post), settled)
    assert observation["terminal_state"] == "completed"
    assert observation["fail_closed"] is True and observation["rejection_observed"] is True
    for mutation in ("accepted", "late", "unattempted"):
        changed_post, changed = dict(post), _product_outcomes()[STALE][1]
        if mutation == "accepted":
            changed_post["late_publication_rejected"] = False
        elif mutation == "late":
            changed.gateway = {**changed.gateway, "late_source_publication_count": 1}
        else:
            changed_post["late_publication_attempted"] = False
        with pytest.raises(ObservationRefused, match="unsatisfied"):
            recorder.write(STALE, _executed(STALE, changed_post), changed)


@pytest.mark.parametrize("case_id", [case for case in runner.REQUIRED_CASES if case not in (
    POSITIVE, STALE, "cleanup_reservation_release", "cleanup_authority_failure")])
def test_every_other_negative_case_still_refuses_a_completed_terminal(case_id):
    _post, settled = _product_outcomes()[case_id]
    observation = {**derive_observation(case_id, _executed(case_id, _post), settled),
                   "terminal_state": "completed"}
    with pytest.raises(ValueError, match="physical_observation_invalid"):
        runner.validate_case_observation(case_id, observation)


class _CutoverReleasedTarget:
    """The 50 ms poller only ever saw the source identity: cutover, COMPLETED
    and the router release happened between two polls."""

    def __init__(self, committed):
        self.service = SimpleNamespace()
        self.qualifier = SimpleNamespace(issued_qualification_facts=lambda _rid: {
            "controlled_fault": None, "physical_challenge": "passed",
            "compatibility_matches_source": True})
        self.calls = 0
        self._committed = committed

    def view(self, _request_id):
        self.calls += 1
        if self.calls < 3:
            return {"retained": True, "terminal_status": None, "recovery_required": True,
                    "recovery_phase": "recovery_prefill", "recovery_attempts": 1,
                    "route_identity": {"request_attempt": 1, "path_id": "path-source",
                                       "path_attempt": 0}}
        return {"retained": False, "terminal_status": "COMPLETED", "recovery_attempts": 1,
                "recovery_failure_reason": None, "source_failure_observed": True}

    def committed_route_identity(self, _request_id):
        return self._committed

    def coordinator_terminal(self, _request_id):
        return "completed" if self.calls >= 3 else None

    def gateway_terminal_count(self, _request_id):
        return 1 if self.calls >= 3 else 0

    def reservation_count(self, _request_id):
        return 0


@pytest.mark.parametrize("committed_attempt", [2, 1])
def test_positive_cutover_is_read_from_the_coordinator_not_the_last_view(
        plan: Path, monkeypatch, committed_attempt):
    import mycelium_a6_case_observations as module

    authority = observation_authority(operator_plan=plan, repo_root=ROOT)
    target = _CutoverReleasedTarget({"request_id": "request-1",
                                     "request_attempt": committed_attempt,
                                     "path_id": "path-successor" if committed_attempt == 2
                                     else "path-source",
                                     "path_attempt": committed_attempt - 1})
    recorder = CaseObservationRecorder(authority=authority, target=target, settle_seconds=5)
    monkeypatch.setattr(module, "gateway_facts", lambda *_args, **_kwargs: _gateway(
        "completed", terminal_attempt=2))
    recorder.observe(_executed(POSITIVE, {"source_failure_observed": True,
                                          "recovery_required": True}))
    recorder.join(timeout=10)
    status = recorder.status()
    if committed_attempt == 2:
        assert status["refused"] == {} and POSITIVE in status["written"]
        envelope = json.loads((authority.evidence_output_dir / "a6-production-observations"
                               / f"{POSITIVE}.json").read_text())
        assert envelope["bindings"]["successor_attempt"] == 2
        assert envelope["observation"]["publication_generation_cas_count"] == 1
    else:
        # No cutover committed: the coordinator never names a successor.
        assert status["written"] == {} and POSITIVE in status["refused"]


def test_refused_observation_logs_the_measured_field_values(plan: Path, caplog):
    import logging

    authority = observation_authority(operator_plan=plan, repo_root=ROOT)
    recorder = CaseObservationRecorder(authority=authority, target=SimpleNamespace())
    post, settled = _product_outcomes()["no_successor"]
    executed = _executed("no_successor", {**post, "truthful_abort_required": False})
    with pytest.raises(ObservationRefused) as refused:
        recorder.write("no_successor", executed, settled)
    recorder._refuse("no_successor", executed, refused.value)
    logged = [json.loads(record.getMessage()) for record in caplog.records
              if record.name == "mycelium_a6_case_observations"
              and record.levelno == logging.WARNING]
    detail = logged[-1]["detail"]
    assert detail["observation.rejection_observed"] is False
    assert detail["observation.terminal_state"] == "aborted"
    assert detail["bindings.request_id"] == "request-1"
    assert not any(key.endswith("_digest") for key in detail)


def test_cleanup_authority_failure_records_only_the_v2_contract(plan: Path):
    """The v1 shape is unsatisfiable on the real product; only v2 is attempted."""

    authority = observation_authority(operator_plan=plan, repo_root=ROOT)
    target = SimpleNamespace(
        service=SimpleNamespace(), qualifier=None,
        view=lambda _rid: {"retained": False},
        coordinator_terminal=lambda _rid: "failed",
        reservation_count=lambda _rid: 0,
        gateway_terminal_count=lambda _rid: 1,
        router=SimpleNamespace(a6_command_authority=lambda _rid: None))
    recorder = CaseObservationRecorder(authority=authority, target=target, settle_seconds=1)
    recorder._record(_executed("cleanup_authority_failure",
                               {"cleanup_authority_refused": True,
                                "unpublishable_suffix_discarded": True}))
    status = recorder.status()
    assert "cleanup_authority_failure" not in status["refused"]
    assert status["refused"]["cleanup_authority_failure.v2"] == (
        "a6_cleanup_v2_authority_unavailable")


EXHAUSTED = "exhausted_recovery_attempts"


@pytest.mark.parametrize("mutation", ["one_successor", "same_node", "no_second_issue"])
def test_exhausted_attempts_needs_evidence_of_a_second_real_successor(mutation):
    post, settled = _product_outcomes()[EXHAUSTED]
    post = dict(post)
    if mutation == "one_successor":
        post.update(attempts_used=1, successor_nodes_failed=[["node-2"]])
    elif mutation == "same_node":
        post["successor_nodes_failed"] = [["node-2"], ["node-2"]]
    else:
        # Two placements: the counter reached 2 but the qualifier only ever
        # issued a record for the first successor.
        settled.qualification = {"successor_path_attempt": 1}
    observation = derive_observation(EXHAUSTED, _executed(EXHAUSTED, post), settled)
    assert observation["rejection_observed"] is False
    with pytest.raises(ValueError, match="physical_observation_invalid"):
        runner.validate_case_observation(EXHAUSTED, observation)


def test_a_blocked_request_is_measured_after_its_late_cleanup_proof(plan: Path, monkeypatch):
    import mycelium_a6_case_observations as module

    class _LateCleanup:
        service, qualifier, calls = SimpleNamespace(), None, 0

        def view(self, _rid):
            self.calls += 1
            return {"retained": True, "terminal_blocked_reason": "command_cleanup_receipt_missing",
                    "recovery_failure_reason": "recovery_timeout", "recovery_attempts": 1}

        def coordinator_terminal(self, _rid):
            self.calls += 1  # the late proof lands after a few polls
            return "failed" if self.calls >= 8 else None

        def reservation_count(self, _rid):
            return 0 if self.calls >= 8 else 2

        def gateway_terminal_count(self, _rid):
            return 1

    authority = observation_authority(operator_plan=plan, repo_root=ROOT)
    recorder = CaseObservationRecorder(authority=authority, target=_LateCleanup(),
                                       settle_seconds=5)
    monkeypatch.setattr(module, "gateway_facts", lambda *_a, **_k: _gateway(
        "failed", "request_cleanup_unproven"))
    recorder.observe(_executed("timeout_during_recovery", {
        "source_failure_observed": True, "recovery_deadline_exceeded": True}))
    recorder.join(timeout=10)
    status = recorder.status()
    assert status["refused"] == {} and "timeout_during_recovery" in status["written"]


def test_gateway_facts_keep_committed_count_after_worker_releases_tokens():
    """A finished worker clears its token material; the committed count survives."""
    from mycelium_a6_case_observations import gateway_facts
    from mycelium_request_gateway.service import RequestGatewayService
    from tests.request_conformance.support import (
        CountingBackend, MutableQualificationSource, drain, submission,
        synthetic_qualification)

    qualification = synthetic_qualification()

    def script(_backend, _request_id, _submission, emit_token, _is_cancelled):
        for index, text in enumerate(("alpha", "beta", "gamma")):
            emit_token(index, text)
        return "completed"

    service = RequestGatewayService(
        qualification_source=MutableQualificationSource(qualification),
        backend=CountingBackend(script),
        request_id_source=lambda: "request-released-count",
        max_buffered_events=8,
    )
    try:
        request_id = service.submit(submission(qualification))
        events = drain(service, request_id)
        assert events[-1].kind == "completed"
        session = service._get_session(request_id)
        deadline = time.monotonic() + 5
        while not session.worker_done and time.monotonic() < deadline:
            time.sleep(0.01)
        assert session.worker_done and session.token_digests == []

        facts = gateway_facts(service, request_id, failure_sequence=None,
                              committed_at_failure=None)
        assert facts["committed_count"] == facts["expected_token_index"] == 3
    finally:
        service.close()
