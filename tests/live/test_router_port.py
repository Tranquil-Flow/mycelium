# SPDX-License-Identifier: AGPL-3.0-only
import json
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

def test_blocked_terminal_is_persisted_so_the_next_round_reads_one_file(
    live_graph, request_factory, tmp_path, monkeypatch
) -> None:
    """A withheld terminal must leave a bounded, joinable record on disk.

    ERRATA-23 (round p78r1): the route withheld ~104 terminals and the reason for each lived only in
    ``self._incidents`` -- in memory -- so it died with the app.  The round then had to be reconstructed
    from four artifacts over three attempts and still could not split "the proof command was issued and
    went unclaimed" from "no proof command was ever minted".  This record makes that split a file read.
    """
    from mycelium_m16_runtime import build_live_m16_runtime

    class Qualification:
        qualification_digest = "sha256:" + "e" * 64

    class Sink:
        def emit(self, _index, _token):
            return None

    class NoReceiptRoute:
        # A non-simulated route is required: the shipped raise is skipped for simulated routes, so a
        # FakeLiveRoute-based test would assert about a path the product never takes.
        is_simulated = False

        def is_alive(self) -> bool:
            return True

        def infer(self, _tokens, *, request_id, sink, command_identity, **_kwargs):
            sink.emit(0, 4599)
            raise RuntimeError("simulated_cleanup_unproven")

        def request_cleanup_receipt(self, _request_id):
            return None

    log = tmp_path / "route-blocked-terminals.jsonl"
    monkeypatch.setenv("MYCELIUM_LIVE_BLOCKED_TERMINAL_LOG", str(log))

    route = NoReceiptRoute()
    coordinator = build_live_m16_runtime(live_graph)
    port = LiveRouterPort(
        route=route, execution_graph=live_graph, runtime_coordinator=coordinator
    )
    request_id = port.admit(
        request_factory("blocked-terminal"), Sink(), qualification_binding=Qualification()
    )
    # The withheld terminal is recorded on the pending state (the same accessor the existing
    # missing-cleanup-receipt test uses); the port then parks the request, so wait on the reason.
    deadline = time.monotonic() + 2.0
    pending = port._pending[request_id]
    while pending.terminal_blocked_reason is None:
        assert time.monotonic() < deadline, "the receipt-less terminal was not withheld"
        time.sleep(0.005)
    assert pending.terminal_blocked_reason == "command_cleanup_receipt_missing"

    assert log.exists(), "the withheld terminal left no record at all"
    lines = [json.loads(line) for line in log.read_text().splitlines() if line.strip()]
    assert lines, "the blocked-terminal record is empty"
    record = lines[-1]
    assert record["protocol"] == "mycelium.live_route_blocked_terminal.v1"
    assert record["reason"] == "command_cleanup_receipt_missing"
    assert record["request_id_prefix"] == request_id[:16]
    assert record["command_id_prefix"], "the record must name the command, or it cannot be joined"
    assert record["execution_started"] is True
    # The route's own minted deadline travels with the record: the shape under suspicion in ERRATA-23 was a
    # deadline the route could not honour, so the record must carry it rather than leave it to inference.
    assert isinstance(record["absolute_deadline_ms"], int) and record["absolute_deadline_ms"] > 0
    # The CAUSE must travel too, not only the withholding reason.  The route's own bounded failure code is
    # computed a few lines ABOVE the block site (``_bounded_error_code``) and was then discarded: all 103
    # p78r1 blocks reduced to the same sentence, ``command_cleanup_receipt_missing``, and the route failure
    # underneath never reached any artifact -- the fourth occurrence of this program's "records where, never
    # why" class.  Assert the two are distinguishable instead of assuming it.
    assert record["route_error_code"] == "simulated_cleanup_unproven"
    assert record["route_error_code"] != record["reason"]
    # Whether a scoped/route cleanup incident existed decides the branch that follows: ``complete()`` retires
    # the runtime reservation, ``retire_dispatch_slot()`` retains it.  A reader must not have to re-derive
    # which branch ran (and therefore whether capacity was retained) from an in-memory list.
    assert record["scoped_incident"] is False
