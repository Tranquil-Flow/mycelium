# SPDX-License-Identifier: AGPL-3.0-only
"""A11 Wave D: the VerifyEvent wire message and a real-socket VERIFY round.

The loopback socket mesh frames every message with the router wire codec and
delivers it on its own threads, so the VERIFY hop, the activation hop and the
returned ``VerifyEvent`` all cross a real (localhost) socket asynchronously.
"""

from __future__ import annotations

import time

import pytest

from mycelium_a11_route_speculation import RouteSpeculationController, RouteSpeculationPolicy
from mycelium_qualification.physical_deployment import build_physical_device_states
from mycelium_router.contracts import HopHeader, RequestContext, RouterConfig, VerifyEvent
from mycelium_router.fakes import (
    FakeCapacityPort,
    FakeDeviceStateProvider,
    FakeTopologyProvider,
    InMemoryClientSink,
    ManualClock,
    SequenceIdSource,
)
from mycelium_router.idempotency import hop_idempotency_key
from mycelium_router.router import Router
from mycelium_router.transports.loopback_socket import LoopbackSocketMesh
from mycelium_router.wire import WireError, decode_frame, encode_frame
from a11_route_fixtures import fresh_ports, varied_two_stage_case
from test_a11_route_speculation import PROMPT, ScriptedDraft

INCARNATION = "sha256:" + "2" * 64


def _event(**overrides):
    values = {"request_id": "r", "path_id": "p", "path_attempt": 0, "token_index": 3, "decisions": (1, 2, 3)}
    values.update(overrides)
    return VerifyEvent(**values)


def test_verify_event_round_trips_through_the_wire_codec():
    event = _event()
    decoded = decode_frame(encode_frame(event))
    assert decoded.message == event and isinstance(decoded.message.decisions, tuple)


@pytest.mark.parametrize(
    "overrides",
    [
        {"decisions": (1,)},
        {"decisions": tuple(range(18))},
        {"decisions": (1, -2)},
        {"token_index": 0},
        {"request_id": ""},
        {"path_attempt": -1},
    ],
)
def test_malformed_verify_events_are_refused_by_the_codec(overrides):
    with pytest.raises(WireError):
        encode_frame(_event(**overrides))


@pytest.fixture(scope="module")
def case():
    temporary, value = varied_two_stage_case()
    yield value
    temporary.cleanup()


def _request(request_id, max_new_tokens):
    return RequestContext(
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


class SocketRoute:
    def __init__(self, case):
        clock = ManualClock(1.0)
        self.ports = fresh_ports(case, clock=clock.now)
        self.mesh = LoopbackSocketMesh(connect_timeout_seconds=10.0)
        states = build_physical_device_states(case.graph)
        capacity = FakeCapacityPort()
        self.routers = {}
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
        self.mesh.start()

    def generate(self, request_id, step, max_new_tokens=12, timeout=20.0):
        sink = InMemoryClientSink()
        self.entry.start_distributed_prefill(_request(request_id, max_new_tokens), sink)
        deadline = time.monotonic() + timeout
        while self.entry.request_status(request_id) not in {"DECODING", "COMPLETED", "FAILED"}:
            assert time.monotonic() < deadline
            time.sleep(0.005)
        while self.entry.request_status(request_id) == "DECODING":
            count = len(sink.token_ids)
            step(request_id)
            while len(sink.token_ids) == count and self.entry.request_status(request_id) == "DECODING":
                assert time.monotonic() < deadline, "round did not complete"
                time.sleep(0.002)
        return sink, self.entry.request_status(request_id)

    def close(self):
        self.mesh.close()


def test_verify_round_over_real_sockets_matches_target_only_output(case):
    route = SocketRoute(case)
    try:
        reference, status = route.generate("plain", route.entry.decode_one_distributed)
        assert status == "COMPLETED"
        draft = ScriptedDraft(reference.token_ids, error_offsets=[1, 4, 2])
        controller = RouteSpeculationController(
            policy=RouteSpeculationPolicy(width=4, warm_target_tpot_p95_ms=40.0, warm_target_tpot_p50_ms=30.0),
            draft_factory=lambda request_id: draft,
            runtime_incarnation=INCARNATION,
        )
        route.entry.entry.a11_controller = controller
        controller.set_preference("spec", "speculative_requested")
        speculative, status = route.generate("spec", route.entry.speculate_one_distributed)
        assert status == "COMPLETED"
        assert speculative.token_ids == reference.token_ids
        summary = controller.completed[-1]
        assert summary["rounds"] >= 2 and summary["accepted_count"] > 0 and summary["rejected_count"] > 0
        deadline = time.monotonic() + 5.0
        while [p.kv_snapshot()["active_state_count"] for p in route.ports] != [0, 0]:
            assert time.monotonic() < deadline
            time.sleep(0.01)
    finally:
        route.close()


def test_a_verify_hop_with_an_out_of_bounds_span_is_rejected_by_the_relay(case):
    route = SocketRoute(case)
    try:
        sink = InMemoryClientSink()
        route.entry.start_distributed_prefill(_request("span", 6), sink)
        deadline = time.monotonic() + 10.0
        while route.entry.request_status("span") != "DECODING":
            assert time.monotonic() < deadline
            time.sleep(0.005)
        record = route.entry.get_request("span")
        manifest = record.manifest
        first = manifest.ordered_hops[0]
        for span in (1, 18):
            header = HopHeader(
                request_id="span",
                path_id=manifest.path_id,
                path_attempt=manifest.path_attempt,
                phase="VERIFY",
                token_index=1,
                hop_index=0,
                source_placement_id="",
                destination_placement_id=first.placement_id,
                topology_version=manifest.topology_version,
                idempotency_key=hop_idempotency_key(
                    request_id="span", path_id=manifest.path_id, path_attempt=manifest.path_attempt,
                    phase="VERIFY", token_index=1, hop_index=0,
                ),
                prefill_chunk_token_count=span,
            )
            result = route.entry.receive_hop(header, b"")
            assert result.disposition == "REJECTED" and result.reason == "invalid_verify_span"
        assert route.ports[0].kv_snapshot()["active_state_count"] == 1
    finally:
        route.close()
