# SPDX-License-Identifier: AGPL-3.0-only
"""A11 Wave D: speculative rounds over the router route with real MLX stage runtimes.

Two Router instances (node-a: entry + stage 0, node-b: final stage 1) run over the
synchronous ``InProcessMesh``.  Target-only output (``decode_one_distributed``) is the
authority; a speculative request on the same route must produce exactly the same
tokens while proposals are accepted, rejected and corrected, and must leave no stage
KV or speculative window behind.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from mycelium_a11_route_speculation import (
    RouteSpeculationController,
    RouteSpeculationError,
    RouteSpeculationPolicy,
    arbitrate_round,
)
from mycelium_qualification.physical_deployment import build_physical_device_states
from mycelium_router.contracts import RequestContext, RouterConfig
from mycelium_router.fakes import (
    FakeCapacityPort,
    FakeDeviceStateProvider,
    FakeTopologyProvider,
    InMemoryClientSink,
    InProcessMesh,
    ManualClock,
    SequenceIdSource,
)
from mycelium_router.router import Router
from mycelium_speculative_decode_execution import DraftProposal
from a11_route_fixtures import fresh_ports, varied_two_stage_case

PROMPT = (6, 5, 4, 3, 2)
INCARNATION = "sha256:" + "1" * 64


# ---------------------------------------------------------------------------
# Arbitration over one returned VERIFY (A10 arithmetic)
# ---------------------------------------------------------------------------


def test_all_match_commits_every_candidate_and_no_correction():
    result = arbitrate_round(context=(1, 2), candidates=(3, 4, 5), decisions=(3, 4, 5, 9),
                             request_ref="r", runtime_incarnation=INCARNATION)
    assert result.committed == (3, 4, 5) and result.correction is None
    assert result.outcome == {"proposed": 3, "verified": 3, "accepted": 3, "corrected": 0, "bonus": 0,
                              "committed": 3, "commit_state": "committed"}
    assert result.bonus is None


def test_all_match_with_the_bonus_allowed_also_commits_the_target_next_decision():
    result = arbitrate_round(context=(1, 2), candidates=(3, 4, 5), decisions=(3, 4, 5, 9),
                             request_ref="r", runtime_incarnation=INCARNATION, bonus_allowed=True)
    assert result.committed == (3, 4, 5, 9) and result.bonus == 9 and result.correction is None
    assert result.outcome["committed"] == result.outcome["accepted"] + result.outcome["corrected"] + result.outcome["bonus"]
    assert result.outcome["bonus"] == 1


@pytest.mark.parametrize("mismatch", [0, 1, 2])
def test_a_mismatch_never_takes_a_bonus(mismatch):
    decisions = [3, 4, 5, 9]
    decisions[mismatch] = 8
    result = arbitrate_round(context=(1, 2), candidates=(3, 4, 5), decisions=decisions,
                             request_ref="r", runtime_incarnation=INCARNATION, bonus_allowed=True)
    assert result.bonus is None and result.outcome["bonus"] == 0
    assert result.committed == (3, 4, 5)[:mismatch] + (8,)


@pytest.mark.parametrize("mismatch", [0, 1, 2])
def test_first_mismatch_commits_the_prefix_plus_the_target_correction(mismatch):
    candidates = [3, 4, 5]
    decisions = [3, 4, 5, 9]
    decisions[mismatch] = 8
    result = arbitrate_round(context=(1, 2), candidates=candidates, decisions=decisions,
                             request_ref="r", runtime_incarnation=INCARNATION)
    assert result.accepted == tuple(candidates[:mismatch])
    assert result.correction == 8
    assert result.committed == tuple(candidates[:mismatch]) + (8,)
    outcome = result.outcome
    assert outcome["accepted"] + (outcome["verified"] - outcome["accepted"]) <= outcome["verified"] <= outcome["proposed"]
    assert outcome["committed"] == outcome["accepted"] + outcome["corrected"]


def test_a_later_agreement_after_a_mismatch_is_never_accepted():
    result = arbitrate_round(context=(1,), candidates=(3, 4, 5), decisions=(3, 7, 5, 6),
                             request_ref="r", runtime_incarnation=INCARNATION)
    assert result.committed == (3, 7)


def test_decision_count_must_be_width_plus_one():
    with pytest.raises(RouteSpeculationError, match="staged_candidate_width_mismatch"):
        arbitrate_round(context=(1,), candidates=(3, 4), decisions=(3, 4),
                        request_ref="r", runtime_incarnation=INCARNATION)


# ---------------------------------------------------------------------------
# Admission
# ---------------------------------------------------------------------------


def _policy(**overrides):
    values = {"width": 4, "warm_target_tpot_p95_ms": 40.0, "warm_target_tpot_p50_ms": 30.0}
    values.update(overrides)
    return RouteSpeculationPolicy(**values)


def _admit(controller, request_id="r1", **overrides):
    values = {"backends": ("mlx", "mlx"), "kv_successor_covered": False, "remaining_deadline_ms": 10_000.0}
    values.update(overrides)
    return controller.admit(request_id, **values)


@pytest.mark.parametrize(
    "setup, expected",
    [
        (lambda c: None, "preference_target_only"),
        (lambda c: c.set_preference("r1", "speculative_requested") or c.revoke(), "qualification_revoked"),
    ],
)
def test_admission_refuses_with_closed_reasons(setup, expected):
    controller = RouteSpeculationController(policy=_policy(), draft_factory=lambda rid: None, runtime_incarnation=INCARNATION)
    setup(controller)
    record = _admit(controller)
    assert record["mode"] == "target_only" and record["fallback_reason"] == expected


@pytest.mark.parametrize(
    "kwargs, policy, factory, expected",
    [
        ({}, {}, None, "no_authorized_local_draft"),
        ({"backends": ("mlx", "numpy")}, {}, "ok", "target_verifier_unavailable"),
        ({"kv_successor_covered": True}, {}, "ok", "kv_successor_coverage_active"),
        ({}, {"warm_target_tpot_p95_ms": None}, "ok", "warm_target_only_p95_tpot_unknown"),
        ({"remaining_deadline_ms": None}, {}, "ok", "request_deadline_unavailable"),
        ({}, {"generation_policy": "sampled"}, "ok", "unsupported_generation_policy"),
    ],
)
def test_admission_requires_draft_verifier_policy_and_measured_baseline(kwargs, policy, factory, expected):
    controller = RouteSpeculationController(
        policy=_policy(**policy),
        draft_factory=None if factory is None else (lambda rid: object()),
        runtime_incarnation=INCARNATION,
    )
    controller.set_preference("r1", "speculative_requested")
    record = _admit(controller, **kwargs)
    assert record["mode"] == "target_only" and record["fallback_reason"] == expected
    assert not controller.active("r1")


def test_invalid_width_and_preference_are_refused():
    with pytest.raises(RouteSpeculationError, match="invalid_speculation_width"):
        _policy(width=17)
    controller = RouteSpeculationController(policy=_policy(), draft_factory=None, runtime_incarnation=INCARNATION)
    with pytest.raises(RouteSpeculationError, match="invalid_speculation_preference"):
        controller.set_preference("r1", "always")


# ---------------------------------------------------------------------------
# Route fixture
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def case():
    temporary, value = varied_two_stage_case()
    yield value
    temporary.cleanup()


class Mesh:
    def __init__(self, case):
        self.case = case
        clock = ManualClock(1.0)
        self.ports = fresh_ports(case, clock=clock.now)
        self.mesh = InProcessMesh()
        states = build_physical_device_states(case.graph)
        self.routers = {}
        capacity = FakeCapacityPort()
        for assignment, port in zip(case.assignments, self.ports):
            node_id = assignment["node_id"]
            router = Router(
                node_id=node_id,
                topology=FakeTopologyProvider(case.graph),
                device_states=FakeDeviceStateProvider(states),
                capacity=capacity,
                runtime=port,
                transport=self.mesh.transport_for(node_id),
                clock=clock,
                id_source=SequenceIdSource(),
                config=RouterConfig(prefill_chunk_size_tokens=0),
            )
            self.mesh.register_router(node_id, router)
            self.routers[node_id] = router
        self.entry = self.routers[case.assignments[0]["node_id"]]

    def start(self, request_id, max_new_tokens):
        request = RequestContext(
            request_id=request_id,
            prompt_token_ids=PROMPT,
            max_new_tokens=max_new_tokens,
            expected_new_tokens=max_new_tokens,
            qos_class="interactive",
            admitted_at=1.0,
            target_ttft_ms=10_000.0,
            target_tpot_ms=10_000.0,
            target_tokens_per_second=1.0,
            sampling_seed=0,
            generation_config_digest="sha256:" + "c" * 64,
        )
        sink = InMemoryClientSink()
        self.entry.start_distributed_prefill(request, sink)
        assert self.entry.request_status(request_id) == "DECODING"
        return sink

    def run(self, request_id, step, *, limit=200):
        for _ in range(limit):
            if self.entry.request_status(request_id) != "DECODING":
                break
            step(request_id)
        return self.entry.request_status(request_id)

    def active_kv(self):
        return [port.kv_snapshot()["active_state_count"] for port in self.ports]


def _target_only(case, max_new_tokens=14):
    mesh = Mesh(case)
    sink = mesh.start("target-only", max_new_tokens)
    assert mesh.run("target-only", mesh.entry.decode_one_distributed) == "COMPLETED"
    return list(sink.token_ids)


class ScriptedDraft:
    """A draft that proposes the target's own continuation with scheduled errors."""

    def __init__(self, reference, *, error_offsets, unavailable_rounds=()):
        self.reference = list(reference)
        self.error_offsets = list(error_offsets)
        self.unavailable_rounds = set(unavailable_rounds)
        self.context: tuple[int, ...] = ()
        self.proposal: tuple[int, ...] = ()
        self.round = 0
        self.commits = 0

    def sync(self, context):
        self.context = tuple(context)
        self.proposal = ()

    def propose(self, *, prefix_position, prefix_digest, width, deadline_ms):
        assert prefix_position == len(self.context)
        index = self.round
        self.round += 1
        if index in self.unavailable_rounds:
            return DraftProposal(unavailable=True)
        generated = len(self.context) - len(PROMPT)
        tokens = list(self.reference[generated : generated + width])
        offset = self.error_offsets[index % len(self.error_offsets)]
        if offset < len(tokens):
            tokens[offset] = (tokens[offset] + 1) % 16
        self.proposal = tuple(tokens)
        return DraftProposal(tokens=self.proposal, draft_compute_ms=1.0)

    def commit_verified_prefix(self, *, tokens, correction_token):
        if tuple(tokens) != self.proposal[: len(tokens)]:
            return False
        self.context = self.context + tuple(tokens) + (() if correction_token is None else (correction_token,))
        self.proposal = ()
        self.commits += 1
        return True

    def rollback_suffix(self, count):
        self.proposal = ()


def _speculative_mesh(case, draft, *, width=4, policy=None):
    mesh = Mesh(case)
    controller = RouteSpeculationController(
        policy=policy or _policy(width=width),
        draft_factory=lambda request_id: draft,
        runtime_incarnation=INCARNATION,
    )
    mesh.entry.entry.a11_controller = controller
    return mesh, controller


# ---------------------------------------------------------------------------
# Route rounds
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bonus", [True, False])
def test_all_match_rounds_take_the_target_bonus_and_the_last_token_stays_a_decode(case, bonus):
    reference = _target_only(case)
    draft = ScriptedDraft(reference, error_offsets=[4])  # never wrong at width 4
    mesh, controller = _speculative_mesh(case, draft, policy=_policy(target_bonus=bonus))
    controller.set_preference("bonus", "speculative_requested")
    sink = mesh.start("bonus", 14)
    assert mesh.run("bonus", mesh.entry.speculate_one_distributed) == "COMPLETED"
    assert sink.token_ids == reference
    assert sink.token_indexes == list(range(len(reference)))
    summary = controller.completed[-1]
    assert summary["rejected_count"] == 0 and summary["target_corrected_count"] == 0
    assert summary["committed_count"] == summary["accepted_count"] + summary["target_bonus_count"]
    # 13 tokens after the prefill token, width 4, the last one a DECODE: with the bonus
    # the rounds commit 5, 5 and then 2 (a bonus there would complete the request).
    assert summary["rounds"] == 3
    assert summary["target_bonus_count"] == (2 if bonus else 0)
    assert summary["accepted_count"] == (10 if bonus else 12)
    verify_hops = [d for d in mesh.mesh.hop_deliveries if d.header.phase == "VERIFY"]
    decode_hops = [d for d in mesh.mesh.hop_deliveries if d.header.phase == "DECODE"]
    # The last committed token is always an ordinary terminal DECODE.
    last = max(verify_hops + decode_hops, key=lambda d: d.header.token_index)
    assert last.header.phase == "DECODE" and last.header.token_index == len(reference) - 1
    assert mesh.active_kv() == [0, 0]


def test_the_target_bonus_saves_a_pipeline_pass(case):
    reference = _target_only(case, 12)
    passes = {}
    for bonus in (False, True):
        draft = ScriptedDraft(reference, error_offsets=[4])
        mesh, controller = _speculative_mesh(case, draft, policy=_policy(target_bonus=bonus))
        controller.set_preference("pass", "speculative_requested")
        sink = mesh.start("pass", 12)
        assert mesh.run("pass", mesh.entry.speculate_one_distributed) == "COMPLETED"
        assert sink.token_ids == reference
        hops = [d for d in mesh.mesh.hop_deliveries if d.header.phase in {"VERIFY", "DECODE"}]
        passes[bonus] = len(hops) // 2
    # 11 tokens after the prefill token: 4+4+2 then a DECODE, or 5+5 then a DECODE.
    assert passes == {False: 4, True: 3}


@pytest.mark.parametrize("error_offsets", [[4], [0], [1, 3], [2, 4, 0]])
def test_speculative_output_equals_target_only_output(case, error_offsets):
    reference = _target_only(case)
    draft = ScriptedDraft(reference, error_offsets=error_offsets)
    mesh, controller = _speculative_mesh(case, draft)
    controller.set_preference("spec", "speculative_requested")
    sink = mesh.start("spec", 14)
    assert mesh.run("spec", mesh.entry.speculate_one_distributed) == "COMPLETED"
    assert sink.token_ids == reference
    assert sink.token_indexes == list(range(len(reference)))
    summary = controller.completed[-1]
    assert summary["rounds"] >= 1
    assert summary["committed_count"] == (
        summary["accepted_count"] + summary["target_corrected_count"] + summary["target_bonus_count"]
    )
    assert summary["accepted_count"] + summary["rejected_count"] <= summary["verified_count"] <= summary["proposed_count"]
    # Fewer pipeline passes than tokens whenever any candidate is accepted.
    verify_hops = [d for d in mesh.mesh.hop_deliveries if d.header.phase == "VERIFY"]
    decode_hops = [d for d in mesh.mesh.hop_deliveries if d.header.phase == "DECODE"]
    passes = (len(verify_hops) + len(decode_hops)) // 2
    if summary["accepted_count"]:
        assert passes < len(reference) - 1
    else:
        assert passes == len(reference) - 1
    assert mesh.active_kv() == [0, 0]


def test_rejections_and_corrections_are_observed_and_counted(case):
    reference = _target_only(case)
    draft = ScriptedDraft(reference, error_offsets=[1])
    mesh, controller = _speculative_mesh(case, draft)
    controller.set_preference("spec", "speculative_requested")
    sink = mesh.start("spec", 14)
    mesh.run("spec", mesh.entry.speculate_one_distributed)
    summary = controller.completed[-1]
    assert sink.token_ids == reference
    assert summary["rejected_count"] > 0 and summary["target_corrected_count"] > 0
    assert summary["accepted_count"] > 0


def test_unavailable_proposal_falls_back_to_an_ordinary_decode_round(case):
    reference = _target_only(case)
    draft = ScriptedDraft(reference, error_offsets=[4], unavailable_rounds={0, 2})
    mesh, controller = _speculative_mesh(case, draft)
    controller.set_preference("spec", "speculative_requested")
    sink = mesh.start("spec", 14)
    assert mesh.run("spec", mesh.entry.speculate_one_distributed) == "COMPLETED"
    assert sink.token_ids == reference
    summary = controller.completed[-1]
    assert summary["deadline_fallbacks"] == 2 and summary["decode_rounds"] >= 2


def test_three_consecutive_deadlines_open_the_breaker_for_the_rest_of_the_request(case):
    reference = _target_only(case)
    draft = ScriptedDraft(reference, error_offsets=[4], unavailable_rounds={0, 1, 2})
    mesh, controller = _speculative_mesh(case, draft)
    controller.set_preference("spec", "speculative_requested")
    sink = mesh.start("spec", 14)
    mesh.run("spec", mesh.entry.speculate_one_distributed)
    summary = controller.completed[-1]
    assert sink.token_ids == reference
    assert summary["stop_reason"] == "circuit_breaker_open"
    assert summary["circuit_breaker"]["reason"] == "consecutive_proposal_deadlines"
    assert summary["rounds"] == 0 and draft.round == 3
    # The performance breaker also cools down new admission.
    controller.set_preference("next", "speculative_requested")
    assert _admit(controller, "next")["fallback_reason"] == "cooldown_active"


def test_draft_that_cannot_reproduce_the_commit_stops_speculation_not_the_request(case):
    reference = _target_only(case)

    class Forgetful(ScriptedDraft):
        def commit_verified_prefix(self, *, tokens, correction_token):
            return False

    draft = Forgetful(reference, error_offsets=[2])
    mesh, controller = _speculative_mesh(case, draft)
    controller.set_preference("spec", "speculative_requested")
    sink = mesh.start("spec", 14)
    assert mesh.run("spec", mesh.entry.speculate_one_distributed) == "COMPLETED"
    assert sink.token_ids == reference
    summary = controller.completed[-1]
    assert summary["stop_reason"] == "draft_not_reproducible" and summary["rounds"] == 1


def test_lost_draft_falls_back_from_the_committed_watermark(case):
    reference = _target_only(case)

    class Lost(ScriptedDraft):
        def propose(self, **kwargs):
            if self.round >= 1:
                return DraftProposal(lost=True)
            return super().propose(**kwargs)

    draft = Lost(reference, error_offsets=[1])
    mesh, controller = _speculative_mesh(case, draft)
    controller.set_preference("spec", "speculative_requested")
    sink = mesh.start("spec", 14)
    assert mesh.run("spec", mesh.entry.speculate_one_distributed) == "COMPLETED"
    assert sink.token_ids == reference
    assert controller.completed[-1]["stop_reason"] == "draft_lost"


def test_target_only_preference_never_dispatches_a_verify(case):
    reference = _target_only(case)
    draft = ScriptedDraft(reference, error_offsets=[4])
    mesh, controller = _speculative_mesh(case, draft)
    sink = mesh.start("plain", 14)
    mesh.run("plain", mesh.entry.speculate_one_distributed)
    assert sink.token_ids == reference
    assert not [d for d in mesh.mesh.hop_deliveries if d.header.phase == "VERIFY"]
    assert draft.round == 0


def test_stale_or_duplicate_verify_events_are_refused_without_effect(case):
    reference = _target_only(case)
    draft = ScriptedDraft(reference, error_offsets=[4])
    mesh, controller = _speculative_mesh(case, draft)
    controller.set_preference("spec", "speculative_requested")
    sink = mesh.start("spec", 14)
    mesh.entry.speculate_one_distributed("spec")
    event = mesh.mesh.verify_events[-1][1]
    before = list(sink.token_ids)
    assert mesh.entry.receive_verify_event(event) is False  # duplicate: round already committed
    assert mesh.entry.receive_verify_event(replace(event, token_index=1)) is False
    assert mesh.entry.receive_verify_event(replace(event, path_attempt=event.path_attempt + 1)) is False
    assert sink.token_ids == before
    mesh.run("spec", mesh.entry.speculate_one_distributed)
    assert sink.token_ids == reference


def test_cancellation_mid_request_releases_every_stage_and_the_draft_state(case):
    reference = _target_only(case)
    draft = ScriptedDraft(reference, error_offsets=[2])
    mesh, controller = _speculative_mesh(case, draft)
    controller.set_preference("spec", "speculative_requested")
    sink = mesh.start("spec", 14)
    mesh.entry.speculate_one_distributed("spec")
    mesh.entry.speculate_one_distributed("spec")
    assert mesh.entry.cancel("spec")
    assert mesh.entry.request_status("spec") == "CANCELLED"
    assert sink.token_ids == reference[: len(sink.token_ids)]
    assert mesh.active_kv() == [0, 0]
    assert controller.snapshot("spec") is None and controller.completed[-1]["rounds"] == 2
