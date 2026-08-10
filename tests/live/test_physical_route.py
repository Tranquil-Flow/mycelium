import os
from pathlib import Path
import uuid

import pytest

from mycelium_live.route import PhysicalLiveRoute
from mycelium_qualification import issue_live_route_qualification


PLAN = Path(
    "/Users/evinova-self/.hermes/missions/mycelium-distributed-inference-mvp"
    "/evidence/g4-live/w8-mvp-live-533d107-20260809t091035z/operator-plan.json"
)

pytestmark = pytest.mark.skipif(
    os.environ.get("MYCELIUM_PHYSICAL") != "1",
    reason="set MYCELIUM_PHYSICAL=1 to run against real devices",
)


class RecordingSink:
    def __init__(self):
        self.tokens = []

    def emit(self, token_index: int, token_id: int) -> None:
        self.tokens.append((token_index, token_id))


def test_startup_challenge_reproduces_proven_tokens():
    route = PhysicalLiveRoute.from_operator_plan(PLAN)
    try:
        identity = route.open()
        assert len(set(identity.endpoint_ids)) == 2
        sink = RecordingSink()
        request_id = f"challenge-{uuid.uuid4()}"
        result = route.infer(
            (15496, 11, 703, 389, 345, 30),
            max_new_tokens=4,
            request_id=request_id,
            sink=sink,
        )
        assert result.token_ids == (4599, 3329, 2506, 5145)
        assert sink.tokens == [(0, 4599), (1, 3329), (2, 2506), (3, 5145)]
        assert route.counters().fatal is None
        qualification = issue_live_route_qualification(
            route.live_attestation(request_id=request_id),
            expected_prompt_token_ids=(15496, 11, 703, 389, 345, 30),
            expected_output_token_ids=(4599, 3329, 2506, 5145),
        )
        assert qualification.route_ready is True
        assert qualification.deployment_id == identity.deployment_id
    finally:
        route.close()


def test_route_survives_two_arbitrary_prompts():
    route = PhysicalLiveRoute.from_operator_plan(PLAN)
    try:
        route.open()
        first_sink, second_sink = RecordingSink(), RecordingSink()
        suffix = uuid.uuid4()
        route.infer(
            (40, 716, 257),
            max_new_tokens=4,
            request_id=f"arbitrary-a-{suffix}",
            sink=first_sink,
        )
        after_first = route.counters().frames_sent
        route.infer(
            (2504, 318, 262),
            max_new_tokens=4,
            request_id=f"arbitrary-b-{suffix}",
            sink=second_sink,
        )
        assert route.counters().frames_sent > after_first
        assert first_sink.tokens != second_sink.tokens
        assert route.is_alive() is True
    finally:
        route.close()
