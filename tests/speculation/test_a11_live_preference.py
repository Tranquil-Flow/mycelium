# SPDX-License-Identifier: AGPL-3.0-only
"""A11: the explicit speculation preference from the gateway to the entry node's infer_start.

The preference is a request, never a grant.  The route validates it and sends it
only when stated; recoveries (A6 replay and the successor's physical challenge)
re-enter the route without it and therefore run target-only.
"""

from __future__ import annotations

import threading
from collections import deque
from types import SimpleNamespace

import pytest

from mycelium_live.a6_recovery import LiveA6RecoveryQualifier
from mycelium_live.route import PhysicalLiveRoute
from mycelium_live.supervisor import ColdRouteRestartOwner
from mycelium_request_gateway.contracts import (
    REQUEST_GATEWAY_PROTOCOL,
    REQUEST_GATEWAY_PROTOCOL_V2,
    AdmissionError,
    InferenceSubmission,
    qualification_binding,
)
from mycelium_ui_gateway.validation import GatewayValidationError, validate_submission
from tests.live.test_a6_cold_restart_owner_recovery import _KilledSourceRoute, _owner
from tests.live.test_a6_live_router_recovery import FailingThenRecoveringRoute, _runtime

INVALID_PREFERENCES = ("always", "", "SPECULATIVE_REQUESTED", 1, True, ("speculative_requested",))


class _Sink:
    def __init__(self) -> None:
        self.tokens: list[tuple[int, int]] = []

    def emit(self, token_index: int, token_id: int) -> None:
        self.tokens.append((token_index, token_id))


def _physical_route() -> tuple[PhysicalLiveRoute, list[dict]]:
    """The smallest physical route that runs one request to completion."""

    starts: list[dict] = []

    class Session:
        def send(self, *, command_id, command, payload):
            del command_id
            if command == "infer_start":
                starts.append(payload)
                return {"details": {"status": "DECODING", "output": {"token_indexes": [0], "token_ids": [101]}}}
            assert command == "infer_decode"
            return {"details": {"status": "COMPLETED", "output": {"token_indexes": [0, 1], "token_ids": [101, 102]}}}

    route = object.__new__(PhysicalLiveRoute)
    route._lock = threading.Lock()
    route._fatal = None
    route._plan = {
        "entry_node_id": "node-0",
        "request": {
            "qos_class": "interactive",
            "admitted_at": 0.0,
            "target_ttft_ms": 1_000.0,
            "target_tpot_ms": 1_000.0,
            "target_tokens_per_second": 1.0,
            "sampling_seed": 17,
            "generation_config_digest": "sha256:" + "a" * 64,
        },
    }
    route._sessions = {"node-0": Session()}
    route._stop_token_ids = frozenset()
    route._request_outputs = {}
    route._request_inputs = {}
    route._request_limits = {}
    route._request_entry_nodes = {}
    route._recent_inferences = deque(maxlen=16)
    route.is_alive = lambda: True
    route._command_id = lambda node_id, action: f"{node_id}-{action}"
    route._verify_observation = lambda node_id, response, **kwargs: response
    route._peer_counters = lambda: {"node-0": {"frames_sent": 0, "frames_received": 0, "applied_operation_count": 0}}
    route._snapshot_all = lambda: None
    return route, starts


# ---------------------------------------------------------------------------
# route.py (the owner-approved diff)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("preference", INVALID_PREFERENCES)
def test_route_refuses_an_invalid_preference_before_any_command(preference) -> None:
    route, starts = _physical_route()
    with pytest.raises(ValueError, match="^invalid_speculation_preference$"):
        route.infer((7, 8), max_new_tokens=2, request_id="r-bad", sink=_Sink(), speculation_preference=preference)
    assert starts == []


def test_route_never_sends_an_unstated_preference() -> None:
    route, starts = _physical_route()
    result = route.infer((7, 8), max_new_tokens=2, request_id="r-none", sink=_Sink())
    assert result.token_ids == (101, 102)
    assert len(starts) == 1 and "speculation" not in starts[0]
    route.infer((7, 8), max_new_tokens=2, request_id="r-none-2", sink=_Sink(), speculation_preference=None)
    assert "speculation" not in starts[1]


@pytest.mark.parametrize("preference", ["target_only", "speculative_requested"])
def test_route_carries_a_stated_preference_in_infer_start_only(preference) -> None:
    route, starts = _physical_route()
    sink = _Sink()
    result = route.infer((7, 8), max_new_tokens=2, request_id="r-pref", sink=sink, speculation_preference=preference)
    assert result.token_ids == (101, 102) and sink.tokens == [(0, 101), (1, 102)]
    assert starts == [{"request": starts[0]["request"], "speculation": {"preference": preference}}]


# ---------------------------------------------------------------------------
# Gateway request field
# ---------------------------------------------------------------------------


def _binding():
    from tests.request_gateway.test_core import _synthetic_qualification

    return qualification_binding(_synthetic_qualification())


def _v2(**extra) -> dict:
    return {
        "protocol": REQUEST_GATEWAY_PROTOCOL_V2,
        "prompt": "p",
        "max_new_tokens": 2,
        "qualification": _binding().to_dict(),
        "workload_profile_id": "interactive_chat_v1",
        "qos_class": "interactive",
        **extra,
    }


@pytest.mark.parametrize("preference", ["target_only", "speculative_requested"])
def test_gateway_submission_round_trips_a_stated_preference(preference) -> None:
    document = _v2(speculation_preference=preference)
    submission = InferenceSubmission.from_dict(document)
    assert submission.speculation_preference == preference
    assert submission.to_dict() == document
    validate_submission(document)


def test_gateway_submission_without_a_preference_is_unchanged() -> None:
    document = _v2()
    submission = InferenceSubmission.from_dict(document)
    assert submission.speculation_preference is None
    assert "speculation_preference" not in submission.to_dict()
    validate_submission(document)


@pytest.mark.parametrize("preference", [*INVALID_PREFERENCES, None])
def test_gateway_refuses_an_invalid_or_null_preference(preference) -> None:
    document = _v2(speculation_preference=preference)
    with pytest.raises(AdmissionError):
        InferenceSubmission.from_dict(document)
    with pytest.raises(GatewayValidationError):
        validate_submission(document)


def test_gateway_refuses_a_preference_on_the_v1_protocol() -> None:
    document = {
        "protocol": REQUEST_GATEWAY_PROTOCOL,
        "prompt": "p",
        "max_new_tokens": 2,
        "qualification": _binding().to_dict(),
        "speculation_preference": "speculative_requested",
    }
    with pytest.raises(AdmissionError):
        InferenceSubmission.from_dict(document)
    with pytest.raises(AdmissionError):
        InferenceSubmission(prompt="p", max_new_tokens=2, qualification=_binding(), speculation_preference="target_only")
    with pytest.raises(GatewayValidationError):
        validate_submission(document)


# ---------------------------------------------------------------------------
# Router port + supervisor passthrough + A6 replay re-entry
# ---------------------------------------------------------------------------


def _recording(base):
    class Recording(base):
        def __init__(self, **kwargs) -> None:
            super().__init__(**kwargs)
            self.preferences: list[tuple[int, object]] = []

        def infer(self, *args, route_identity, **kwargs):
            self.preferences.append(
                (route_identity["request_attempt"], kwargs.get("speculation_preference", "<absent>"))
            )
            kwargs.pop("speculation_preference", None)
            return super().infer(*args, route_identity=route_identity, **kwargs)

    return Recording


@pytest.fixture
def v2_replay_settings(monkeypatch):
    """The synthetic successor qualifier binds the v2 generation settings these requests use."""

    import tests.live.test_a6_m16_recovery as m16_recovery

    original = m16_recovery.CommittedDeliverySnapshot

    def snapshot(**fields):
        fields["generation_settings"] = {
            "protocol": REQUEST_GATEWAY_PROTOCOL_V2,
            "max_new_tokens": fields["generation_settings"]["max_new_tokens"],
            "workload_profile_id": "interactive_chat_v1",
            "qos_class": "interactive",
        }
        return original(**fields)

    monkeypatch.setattr(m16_recovery, "CommittedDeliverySnapshot", snapshot)


def _submit(service, qualification, **extra):
    request_id = service.submit(
        InferenceSubmission(
            prompt="live recovery prompt",
            max_new_tokens=4,
            qualification=qualification_binding(qualification),
            protocol=REQUEST_GATEWAY_PROTOCOL_V2,
            workload_profile_id="interactive_chat_v1",
            qos_class="interactive",
            **extra,
        )
    )
    subscription = service.subscribe(request_id, last_event_id=None)
    events = []
    try:
        while (event := subscription.next_event(timeout=2)) is not None:
            events.append(event)
            subscription.ack(event.sequence)
    finally:
        subscription.close()
    return [event.kind for event in events if event.kind in {"completed", "failed", "cancelled"}]


@pytest.mark.parametrize("preference", ["target_only", "speculative_requested"])
def test_first_attempt_carries_the_preference_and_the_a6_replay_runs_target_only(
    tmp_path, preference, v2_replay_settings
) -> None:
    route = _recording(FailingThenRecoveringRoute)()
    _coordinator, _router, service, qualification = _runtime(tmp_path, route)
    try:
        assert _submit(service, qualification, speculation_preference=preference) == ["completed"]
    finally:
        service.close()
    # Attempt 1 (the source) states the preference; the A6 replay successor
    # (attempt 2) re-enters without it, so the entry runs it target-only.
    assert route.preferences == [(1, preference), (2, "<absent>")]


def test_an_unstated_preference_never_reaches_the_route(tmp_path, v2_replay_settings) -> None:
    route = _recording(FailingThenRecoveringRoute)()
    _coordinator, _router, service, qualification = _runtime(tmp_path, route)
    try:
        assert _submit(service, qualification) == ["completed"]
    finally:
        service.close()
    assert route.preferences == [(1, "<absent>"), (2, "<absent>")]


def test_supervisor_owner_passes_the_preference_through_and_replay_stays_target_only(
    tmp_path, v2_replay_settings
) -> None:
    route = _recording(_KilledSourceRoute)()
    spare = _recording(_KilledSourceRoute)()
    owner = _owner([route, spare], "qualification")
    owner.open()
    _coordinator, _router, service, qualification = _runtime(tmp_path, owner)
    try:
        assert _submit(service, qualification, speculation_preference="speculative_requested") == ["completed"]
    finally:
        service.close()
    assert route.preferences == [(1, "speculative_requested"), (2, "<absent>")]
    assert spare.preferences == []


def test_supervisor_owner_keeps_a_plain_route_signature_without_a_preference() -> None:
    class Plain:
        """A route that predates A11: it takes no speculation keyword."""

        def __init__(self) -> None:
            self.calls = 0

        def open(self):
            return _KilledSourceRoute().open()

        def is_alive(self) -> bool:
            return True

        def infer(self, token_ids, *, max_new_tokens, request_id, sink, cancel_requested=None,
                  selected_placement_ids=None, route_identity=None, locked_path_manifest=None,
                  command_identity=None, authorize_cleanup=None, publish_cleanup_receipt=None):
            self.calls += 1
            return SimpleNamespace(token_ids=())

    plain = Plain()
    owner = ColdRouteRestartOwner(route_factory=lambda: plain, open_and_qualify=lambda r, _g: (r.open(), "q"))
    owner.open()
    owner.infer((1,), max_new_tokens=1, request_id="r-plain", sink=_Sink())
    assert plain.calls == 1
    with pytest.raises(TypeError):
        owner.infer((1,), max_new_tokens=1, request_id="r-plain-2", sink=_Sink(), speculation_preference="target_only")


def test_router_port_refuses_an_invalid_preference_at_admission(tmp_path) -> None:
    from mycelium_m16_runtime import M16AdmissionError

    route = FailingThenRecoveringRoute()
    _coordinator, router, service, _qualification = _runtime(tmp_path, route)
    try:
        assert router.supports_speculation_preference is True
        with pytest.raises(M16AdmissionError, match="invalid_speculation_preference"):
            router.admit(SimpleNamespace(request_id="r-x"), object(), speculation_preference="always")
        assert route.calls == []
    finally:
        service.close()


# ---------------------------------------------------------------------------
# Successor physical challenge (A6 replay successor and A7-covered fallback)
# ---------------------------------------------------------------------------


def test_successor_physical_challenge_never_states_a_preference(tmp_path) -> None:
    seen: list[dict] = []

    class ChallengeRoute:
        startup_challenge = ((5, 6), (7, 8))

        def infer(self, prompt, **kwargs):
            seen.append(kwargs)
            for index, token in enumerate((7, 8)):
                kwargs["sink"].emit(index, token)
            return SimpleNamespace(token_ids=(7, 8))

        def release_request(self, _request_id) -> None:
            return None

    qualifier = LiveA6RecoveryQualifier(
        graph=None,
        deployment_dir=tmp_path,
        tokenizer_digest="sha256:" + "0" * 64,
        route=ChallengeRoute(),
        clock_ms=lambda: 0,
    )
    outcome = qualifier._run_physical_challenge(
        request_id="r-successor", placement_ids=("placement-b",), absolute_deadline_unix_ms=10**12
    )
    assert outcome["passed"] is True
    assert len(seen) == 1 and "speculation_preference" not in seen[0]
