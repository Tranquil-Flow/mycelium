# SPDX-License-Identifier: AGPL-3.0-only
"""The gateway emitter callable must honor the frozen ``TokenEmitter`` protocol.

``mycelium_request_gateway/contracts.py`` freezes the backend-facing emitter shape:

    class TokenEmitter(Protocol):
        def __call__(self, token_index: int, token_text: str, token_id: int | None = None) -> None: ...

The A6 integration replaced the service's inline emitter lambda with the private
``_GatewayEmission`` callable so the Router replay path can carry canonical token IDs
atomically with the gateway delivery event.  Widening the *emitter* to a private object
must not narrow the *protocol*: the frozen optional third ``token_id`` stays part of the
callable's contract, positionally and by keyword, because a conforming backend calls it
that way.

RED-first: this file was written before ``_GatewayEmission.__call__`` was widened.  On the
unrepaired A6 candidate every direct-call case below dies inside the backend with
``TypeError: __call__() takes 3 positional arguments but 4 were given`` and the request then
reports a ``failed`` terminal with **zero** token events -- the token ids never reach the
stream.  The private ``emit_router_token`` replay case is included so the repair cannot be
"fixed" by deleting that path.

This is wiring evidence only: no physical run, no browser run, no A6 gate promotion.
"""

from __future__ import annotations

from dataclasses import replace

from mycelium_request_gateway.backend import _GatewayTokenSink
from mycelium_request_gateway.contracts import (
    REQUEST_GATEWAY_PROTOCOL_V2,
    InferenceSubmission,
    qualification_binding,
)
from mycelium_request_gateway.service import RequestGatewayService
from test_core import MutableQualificationSource, _synthetic_qualification
from test_stream import _drain, _submission


class _Codec:
    def encode(self, prompt):
        return (7, 8, 9)

    def decode_token(self, token_id):
        return f"<{token_id}>"


class _DirectEmitterBackend:
    """A conforming backend that calls the frozen ``TokenEmitter`` directly."""

    def __init__(self, shape: str) -> None:
        self.shape = shape
        self.calls: list[tuple] = []

    def run(self, request_id, submission, emit_token, is_cancelled):
        if self.shape == "positional":
            emit_token(0, "<101>", 101)
            emit_token(1, "<102>", 102)
        elif self.shape == "keyword":
            emit_token(0, "<101>", token_id=101)
            emit_token(1, "<102>", token_id=102)
        elif self.shape == "legacy_two_argument":
            emit_token(0, "<101>")
            emit_token(1, "<102>")
        else:  # pragma: no cover - defensive
            raise AssertionError(self.shape)
        return "completed"

    def cancel(self, request_id) -> None:
        pass


class _RouterReplayBackend:
    """The production-shaped path: the Router sink prefers ``emit_router_token``."""

    def __init__(self, replay_id: int = 11) -> None:
        self.replay_id = replay_id

    def run(self, request_id, submission, emit_token, is_cancelled):
        sink = _GatewayTokenSink(_Codec(), emit_token)
        sink.emit(0, 11)
        sink.emit(0, self.replay_id)
        sink.emit(1, 12)
        return "completed"

    def cancel(self, request_id) -> None:
        pass


def _run(backend, *, max_new_tokens: int = 2):
    qualification = _synthetic_qualification()
    service = RequestGatewayService(
        qualification_source=MutableQualificationSource(qualification),
        backend=backend,
        request_id_source=lambda: "a6-emitter-protocol",
        max_buffered_events=16,
    )
    submission: InferenceSubmission = _submission(qualification, max_new_tokens=max_new_tokens)
    submission = replace(
        submission,
        protocol=REQUEST_GATEWAY_PROTOCOL_V2,
        workload_profile_id="interactive_chat_v1",
        qos_class="interactive",
    )
    try:
        request_id = service.submit(submission)
        events = _drain(service.subscribe(request_id, last_event_id=None))
        return request_id, events, service
    finally:
        service.close()


def _terminals(events):
    return [event for event in events if event.kind in {"completed", "failed", "cancelled"}]


def test_emitter_callable_accepts_third_token_id_positionally() -> None:
    """The frozen protocol's third argument is positional-compatible."""
    _, events, _ = _run(_DirectEmitterBackend("positional"))

    assert [event.kind for event in _terminals(events)] == ["completed"], [
        (event.kind, event.code) for event in _terminals(events)
    ]
    tokens = [event for event in events if event.kind == "token"]
    assert [(event.token_index, event.text) for event in tokens] == [
        (0, "<101>"),
        (1, "<102>"),
    ]
    assert [event.token_id for event in tokens] == [101, 102]


def test_emitter_callable_accepts_third_token_id_by_keyword() -> None:
    """The frozen protocol's third argument is keyword-compatible."""
    _, events, _ = _run(_DirectEmitterBackend("keyword"))

    assert [event.kind for event in _terminals(events)] == ["completed"], [
        (event.kind, event.code) for event in _terminals(events)
    ]
    tokens = [event for event in events if event.kind == "token"]
    assert [(event.token_index, event.text) for event in tokens] == [
        (0, "<101>"),
        (1, "<102>"),
    ]
    assert [event.token_id for event in tokens] == [101, 102]


def test_emitter_callable_still_accepts_the_legacy_two_argument_call() -> None:
    """Backends that never learned about token ids keep working, with no id invented."""
    _, events, _ = _run(_DirectEmitterBackend("legacy_two_argument"))

    assert [event.kind for event in _terminals(events)] == ["completed"], [
        (event.kind, event.code) for event in _terminals(events)
    ]
    tokens = [event for event in events if event.kind == "token"]
    assert [event.text for event in tokens] == ["<101>", "<102>"]
    assert [event.token_id for event in tokens] == [None, None]


def test_private_emit_router_token_replay_path_is_not_narrowed() -> None:
    """The Router sink's private channel still carries canonical ids atomically."""
    _, events, _ = _run(_RouterReplayBackend(replay_id=11))

    assert [event.kind for event in _terminals(events)] == ["completed"], [
        (event.kind, event.code) for event in _terminals(events)
    ]
    tokens = [event for event in events if event.kind == "token"]
    assert [event.token_id for event in tokens] == [11, 12]
    assert [event.text for event in tokens] == ["<11>", "<12>"]


def test_private_replay_path_still_rejects_an_inconsistent_re_emit() -> None:
    """A re-emit of the same index with a different id still fails closed."""
    _, events, service = _run(_RouterReplayBackend(replay_id=99), max_new_tokens=2)

    terminals = _terminals(events)
    # The mismatch is a token_order_violation; the request fails closed rather than
    # silently rewriting the committed id.
    assert [event.kind for event in terminals] == ["failed"], [
        (event.kind, event.code) for event in terminals
    ]
