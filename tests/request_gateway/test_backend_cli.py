"""Production Router adapter and CLI tests using local fakes only.

Passing these tests is not distributed or physical-route evidence.
"""
from __future__ import annotations

import importlib.util
from io import StringIO
import json
import sys

import pytest

from mycelium_request_gateway.backend import RouterSessionBackend
from mycelium_request_gateway.client import (
    GatewayClientError,
    HTTPGatewayClient,
    ServiceGatewayClient,
)
from mycelium_request_gateway.cli import stream_prompt
from mycelium_request_gateway.contracts import StreamEvent, qualification_binding
from mycelium_request_gateway.service import RequestGatewayService
from mycelium_router.contracts import RouterConfig
from mycelium_router.fakes import (
    FakeCapacityPort,
    FakeDeviceStateProvider,
    FakeRuntimePort,
    FakeTopologyProvider,
    FakeTransportPort,
    ManualClock,
    SequenceIdSource,
)
from mycelium_router.router import Router
from mycelium_router.serialization import execution_graph_from_dict
from test_core import ROOT, MutableQualificationSource, _synthetic_qualification
from test_router_contracts import graph_fixture
from test_router_policy import state_table


class RecordingCodec:
    def __init__(self) -> None:
        self.encoded: list[str] = []
        self.decoded: list[int] = []

    def encode(self, prompt: str) -> tuple[int, ...]:
        self.encoded.append(prompt)
        return (7, 8, 9)

    def decode_token(self, token_id: int) -> str:
        self.decoded.append(token_id)
        return f"<{token_id}>"


def _synthetic_execution_graph():
    spec = importlib.util.spec_from_file_location(
        "request_gateway_execution_graph_fixture",
        ROOT / "tests" / "qualification" / "conftest.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    files, _manifest = module.make_case().render()
    return execution_graph_from_dict(
        json.loads(files["router/execution-graph.json"])
    )


def _runtime_stack(*, graph=None, router_type=Router):
    graph = graph or graph_fixture()
    clock = ManualClock()
    capacity = FakeCapacityPort(clock=clock)
    runtime = FakeRuntimePort(token_base=100)
    router = router_type(
        node_id="entry-node",
        topology=FakeTopologyProvider(graph),
        device_states=FakeDeviceStateProvider(state_table()),
        capacity=capacity,
        runtime=runtime,
        transport=FakeTransportPort(),
        clock=clock,
        id_source=SequenceIdSource(),
        config=RouterConfig(),
    )
    return router, clock, capacity, runtime


def test_cli_streams_through_production_service_and_router_session_interface():
    qualification = _synthetic_qualification()
    router, clock, capacity, runtime = _runtime_stack(
        graph=_synthetic_execution_graph()
    )
    codec = RecordingCodec()
    backend = RouterSessionBackend(
        router=router,
        codec=codec,
        clock=clock.now,
        qualification_source=MutableQualificationSource(qualification),
    )
    service = RequestGatewayService(
        qualification_source=MutableQualificationSource(qualification),
        backend=backend,
        request_id_source=lambda: "cli-router-001",
        max_buffered_events=8,
    )
    output = StringIO()
    try:
        result = stream_prompt(
            ServiceGatewayClient(service),
            prompt="private cli prompt",
            max_new_tokens=3,
            output=output,
        )

        assert result == 0
        assert output.getvalue() == "<101><102><103>"
        assert codec.encoded == ["private cli prompt"]
        assert codec.decoded == [101, 102, 103]
        assert len(capacity.release_calls) == 1
        assert len(runtime.cancel_calls) == 1
        assert router.request_status("cli-router-001") == "COMPLETED"
    finally:
        service.close()


def test_router_adapter_cancellation_releases_capacity_and_kv_once():
    qualification = _synthetic_qualification()
    router, clock, capacity, runtime = _runtime_stack(
        graph=_synthetic_execution_graph()
    )
    codec = RecordingCodec()

    class CancelAfterFirstCodec(RecordingCodec):
        pass

    backend = RouterSessionBackend(
        router=router,
        codec=codec,
        clock=clock.now,
        qualification_source=MutableQualificationSource(qualification),
    )
    service = RequestGatewayService(
        qualification_source=MutableQualificationSource(qualification),
        backend=backend,
        request_id_source=lambda: "cli-router-cancel",
        max_buffered_events=2,
    )
    try:
        from mycelium_request_gateway.contracts import InferenceSubmission, qualification_binding

        request_id = service.submit(
            InferenceSubmission(
                prompt="cancel me",
                max_new_tokens=4,
                qualification=qualification_binding(qualification),
            )
        )
        subscription = service.subscribe(request_id, last_event_id=None)
        accepted = subscription.next_event(timeout=1)
        assert accepted is not None
        subscription.ack(accepted.sequence)
        first = subscription.next_event(timeout=1)
        assert first is not None and first.kind == "token"

        assert service.cancel(request_id) is True
        subscription.ack(first.sequence)
        remaining = []
        while True:
            event = subscription.next_event(timeout=1)
            if event is None:
                break
            remaining.append(event)
            subscription.ack(event.sequence)

        assert remaining[-1].kind == "cancelled"
        assert len(capacity.release_calls) == 1
        assert len(runtime.cancel_calls) == 1
        assert service.terminal_event_count(request_id) == 1
        session = service._get_session(request_id)
        with session.condition:
            assert session.condition.wait_for(lambda: session.worker_done, timeout=1)
        assert backend._active == set()
        assert backend._cancelled == set()
        assert backend._pending_cancelled == set()
        assert backend._internally_cancelled == set()
        assert backend._external_cancellation_observed == set()
        assert backend._awaiting_cancel_ack == set()
    finally:
        service.close()


def test_cli_reconnects_from_last_applied_event_without_duplicate_output():
    qualification = _synthetic_qualification()

    class InterruptingClient:
        def __init__(self):
            self.cursors = []

        def current_qualification(self):
            return qualification_binding(qualification)

        def submit(self, submission):
            return "request-resume"

        def events(self, request_id, *, last_event_id=None):
            assert request_id == "request-resume"
            self.cursors.append(last_event_id)
            if len(self.cursors) == 1:
                yield StreamEvent(request_id=request_id, sequence=0, kind="accepted")
                yield StreamEvent(
                    request_id=request_id,
                    sequence=1,
                    kind="token",
                    token_index=0,
                    text="A",
                )
                raise GatewayClientError("stream_disconnected")
            assert last_event_id == 1
            yield StreamEvent(
                request_id=request_id,
                sequence=2,
                kind="token",
                token_index=1,
                text="B",
            )
            yield StreamEvent(request_id=request_id, sequence=3, kind="completed")

        def cancel(self, request_id):
            return False

    client = InterruptingClient()
    output = StringIO()

    assert stream_prompt(client, prompt="local prompt", max_new_tokens=2, output=output) == 0
    assert output.getvalue() == "AB"
    assert client.cursors == [None, 1]


def test_http_client_rejects_base_paths_and_unsafe_server_request_ids(monkeypatch):
    with pytest.raises(ValueError, match="invalid_gateway_url"):
        HTTPGatewayClient(base_url="https://gateway.invalid/prefix", bearer_token="test-token")
    with pytest.raises(ValueError, match="insecure_gateway_url"):
        HTTPGatewayClient(base_url="http://gateway.invalid", bearer_token="test-token")
    with pytest.raises(ValueError, match="invalid_request_gateway_bearer_token"):
        HTTPGatewayClient(
            base_url="https://gateway.invalid",
            bearer_token="test-token\r\ninjected: value",
        )

    HTTPGatewayClient(base_url="http://127.0.0.1:8080", bearer_token="test-token")
    client = HTTPGatewayClient(base_url="https://gateway.invalid", bearer_token="test-token")
    monkeypatch.setattr(
        client,
        "_json_request",
        lambda *args, **kwargs: {"request_id": "unsafe/request"},
    )
    qualification = _synthetic_qualification()
    from mycelium_request_gateway.contracts import InferenceSubmission

    with pytest.raises(GatewayClientError) as invalid:
        client.submit(
            InferenceSubmission(
                prompt="local",
                max_new_tokens=1,
                qualification=qualification_binding(qualification),
            )
        )
    assert invalid.value.code == "invalid_submission_response"


def test_http_client_rejects_sse_id_or_event_metadata_mismatch(monkeypatch):
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def __iter__(self):
            document = StreamEvent(
                request_id="safe-request",
                sequence=0,
                kind="accepted",
            ).to_dict()
            import json

            return iter(
                (
                    b"id: 9\n",
                    b"event: token\n",
                    f"data: {json.dumps(document)}\n".encode(),
                    b"\n",
                )
            )

    client = HTTPGatewayClient(base_url="https://gateway.invalid", bearer_token="test-token")
    monkeypatch.setattr(client, "_open", lambda *args, **kwargs: Response())

    with pytest.raises(GatewayClientError) as invalid:
        list(client.events("safe-request"))
    assert invalid.value.code == "invalid_event_stream"
