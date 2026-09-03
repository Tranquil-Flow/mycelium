import threading
import time

import pytest

from mycelium_live.route import FakeLiveRoute, InferenceCancelled, InferenceResult
from mycelium_live.router_port import LiveRouterPort


def test_admit_then_decode_one_streams_every_token(
    live_graph, recording_sink, request_factory
):
    route = FakeLiveRoute(scripted_tokens=(4599, 3329, 2506))
    route.open()
    port = LiveRouterPort(route=route, execution_graph=live_graph)
    request_id = port.admit(request_factory("request-a"), recording_sink)
    assert port.request_status(request_id) == "DECODING"
    while port.decode_one(request_id):
        pass
    assert recording_sink.tokens == [(0, 4599), (1, 3329), (2, 2506)]
    assert port.request_status(request_id) == "COMPLETED"


def test_admit_on_dead_route_is_rejected(
    live_graph, recording_sink, request_factory
):
    route = FakeLiveRoute(scripted_tokens=(1,))
    route.open()
    route.close()
    port = LiveRouterPort(route=route, execution_graph=live_graph)
    with pytest.raises(RuntimeError, match="route_not_open"):
        port.admit(request_factory("request-b"), recording_sink)


def test_current_deployment_returns_the_bound_graph(live_graph):
    route = FakeLiveRoute(scripted_tokens=(1,))
    route.open()
    port = LiveRouterPort(route=route, execution_graph=live_graph)
    assert port.current_deployment() == live_graph


def test_tokens_are_visible_before_the_physical_call_completes(
    live_graph, recording_sink, request_factory
) -> None:
    first_emitted = threading.Event()
    release = threading.Event()

    class IncrementalRoute(FakeLiveRoute):
        def infer(
            self,
            token_ids,
            *,
            max_new_tokens,
            request_id,
            sink,
            cancel_requested=None,
        ):
            sink.emit(0, 4599)
            first_emitted.set()
            assert release.wait(timeout=5.0)
            sink.emit(1, 3329)
            return InferenceResult(request_id=request_id, token_ids=(4599, 3329))

    route = IncrementalRoute(scripted_tokens=())
    route.open()
    port = LiveRouterPort(route=route, execution_graph=live_graph)
    request_id = port.admit(request_factory("request-incremental"), recording_sink)
    assert first_emitted.wait(timeout=5.0)
    assert port.decode_one(request_id) is True
    assert recording_sink.tokens == [(0, 4599)]
    assert port.request_status(request_id) == "DECODING"

    release.set()
    assert port.decode_one(request_id) is True
    assert recording_sink.tokens == [(0, 4599), (1, 3329)]
    assert port.request_status(request_id) == "COMPLETED"


def test_cancel_reaches_route_and_deferred_release_drops_adapter_state(
    live_graph, recording_sink, request_factory
) -> None:
    started = threading.Event()
    physically_cancelled = threading.Event()

    class CancellableRoute(FakeLiveRoute):
        def infer(
            self,
            token_ids,
            *,
            max_new_tokens,
            request_id,
            sink,
            cancel_requested=None,
        ):
            started.set()
            deadline = time.monotonic() + 5.0
            while cancel_requested is None or not cancel_requested():
                if time.monotonic() >= deadline:
                    raise AssertionError("cancel_not_propagated")
                time.sleep(0.001)
            physically_cancelled.set()
            raise InferenceCancelled("inference_cancelled")

    route = CancellableRoute(scripted_tokens=())
    route.open()
    port = LiveRouterPort(route=route, execution_graph=live_graph)
    request_id = port.admit(request_factory("request-cancel"), recording_sink)
    assert started.wait(timeout=5.0)

    assert port.cancel(request_id) is True
    port.release_request(request_id)
    assert physically_cancelled.wait(timeout=5.0)
    deadline = time.monotonic() + 5.0
    while port.request_status(request_id) != "UNKNOWN":
        if time.monotonic() >= deadline:
            raise AssertionError("released_request_retained")
        time.sleep(0.001)

    assert port.cancel(request_id) is False


@pytest.mark.parametrize(
    ("message", "expected_code"),
    [
        ("a5_track_identity_mismatch:private detail", "a5_track_identity_mismatch"),
        ("private path /Users/operator/secret", "runtime_error"),
    ],
)
def test_worker_failure_exposes_only_bounded_error_code(
    live_graph,
    recording_sink,
    request_factory,
    message,
    expected_code,
) -> None:
    class FailingRoute(FakeLiveRoute):
        def infer(
            self,
            token_ids,
            *,
            max_new_tokens,
            request_id,
            sink,
            cancel_requested=None,
            **_options,
        ):
            raise RuntimeError(message)

    route = FailingRoute(scripted_tokens=())
    route.open()
    port = LiveRouterPort(route=route, execution_graph=live_graph)
    request_id = port.admit(request_factory("request-failed"), recording_sink)

    assert port.decode_one(request_id) is False
    assert port.request_status(request_id) == "FAILED"
    assert port.request_error_code(request_id) == expected_code


def test_poll_one_returns_immediately_while_route_worker_is_active(
    live_graph,
    recording_sink,
    request_factory,
) -> None:
    started = threading.Event()
    release = threading.Event()

    class WaitingRoute(FakeLiveRoute):
        def infer(
            self,
            token_ids,
            *,
            max_new_tokens,
            request_id,
            sink,
            cancel_requested=None,
            **_options,
        ):
            started.set()
            assert release.wait(timeout=5.0)
            return InferenceResult(request_id=request_id, token_ids=())

    route = WaitingRoute(scripted_tokens=())
    route.open()
    port = LiveRouterPort(route=route, execution_graph=live_graph)
    request_id = port.admit(request_factory("request-poll"), recording_sink)
    assert started.wait(timeout=5.0)

    started_at = time.monotonic()
    assert port.poll_one(request_id) is False
    assert time.monotonic() - started_at < 0.1
    assert port.request_status(request_id) == "DECODING"

    release.set()
    deadline = time.monotonic() + 5.0
    while port.request_status(request_id) != "COMPLETED":
        if time.monotonic() >= deadline:
            raise AssertionError("route_worker_did_not_complete")
        time.sleep(0.001)
