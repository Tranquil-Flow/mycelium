"""Deterministic named traces spanning every required adapter interleaving."""

from __future__ import annotations

from dataclasses import dataclass

from .model import AdapterAction


@dataclass(frozen=True)
class ConformanceScenario:
    name: str
    requirement: str
    actions: tuple[AdapterAction, ...]


def _actions(*names: str) -> tuple[AdapterAction, ...]:
    return tuple(AdapterAction(name) for name in names)


_START = _actions("bind_router", "start")
_RECEIVE_TO_ACK = _actions(
    "receive_frame",
    "dispatch_begin",
    "dispatch_complete",
    "ack_begin",
)
_RECEIVE_COMPLETE = _RECEIVE_TO_ACK + _actions("ack_success")


REQUIRED_SCENARIOS: tuple[ConformanceScenario, ...] = (
    ConformanceScenario(
        "start-close-restart",
        "start/close/restart",
        _actions("bind_router", "start", "start", "close", "close", "restart"),
    ),
    ConformanceScenario(
        "fatal-receive-restart",
        "fatal receive state",
        _START + (AdapterAction("fatal_receive", error="sequence_gap"), AdapterAction("start")),
    ),
    ConformanceScenario(
        "send-reconnect-success",
        "send reconnect",
        _START
        + _actions(
            "queue_send",
            "send_disconnect",
            "send_reconnect_complete",
            "send_confirmed",
            "close",
        ),
    ),
    ConformanceScenario(
        "send-reconnect-failure",
        "send reconnect",
        _START
        + _actions("queue_send", "send_disconnect", "send_reconnect_fail", "close"),
    ),
    ConformanceScenario(
        "receive-reconnect-success",
        "receive reconnect",
        _START + _actions("receive_disconnect", "receive_reconnect_complete", "close"),
    ),
    ConformanceScenario(
        "receive-reconnect-failure",
        "receive reconnect",
        _START + _actions("receive_disconnect", "receive_reconnect_fail", "close"),
    ),
    ConformanceScenario(
        "close-during-send-reconnect",
        "close during blocked reconnect",
        _START
        + _actions(
            "queue_send",
            "send_disconnect",
            "close",
            "send_reconnect_complete",
        ),
    ),
    ConformanceScenario(
        "close-during-receive-reconnect",
        "close during blocked reconnect",
        _START
        + _actions(
            "receive_disconnect",
            "close",
            "receive_reconnect_complete",
        ),
    ),
    ConformanceScenario(
        "rotation-during-queue",
        "endpoint rotation during queue",
        _START + _actions("queue_send", "rotate_peer", "finish_cancelled", "close"),
    ),
    ConformanceScenario(
        "rotation-during-send",
        "endpoint rotation during send",
        _START
        + _actions("queue_send", "send_begin", "rotate_peer", "finish_cancelled", "close"),
    ),
    ConformanceScenario(
        "rotation-during-receive",
        "endpoint rotation during receive",
        _START + _actions("rotate_peer", "receive_stale_generation", "close"),
    ),
    ConformanceScenario(
        "rotation-during-dispatch",
        "endpoint rotation during dispatch",
        _START
        + _actions("receive_frame", "dispatch_begin", "rotate_peer", "close"),
    ),
    ConformanceScenario(
        "rotation-during-ack",
        "endpoint rotation during ACK",
        _START + _RECEIVE_TO_ACK + _actions("rotate_peer", "close"),
    ),
    ConformanceScenario(
        "delayed-ack",
        "delayed ACK",
        _START + _RECEIVE_TO_ACK + _actions("delayed_ack", "ack_success", "close"),
    ),
    ConformanceScenario(
        "lost-ack",
        "lost ACK",
        _START + _RECEIVE_TO_ACK + _actions("lost_ack", "close"),
    ),
    ConformanceScenario(
        "deadline-before-confirmation",
        "cancellation/deadline races",
        _START
        + _actions("queue_send", "delay_confirmation", "deadline", "finish_cancelled", "close"),
    ),
    ConformanceScenario(
        "confirmation-before-deadline",
        "cancellation/deadline races",
        _START + _actions("queue_send", "send_confirmed", "deadline", "close"),
    ),
    ConformanceScenario(
        "queue-exhaustion-permit-release",
        "queue exhaustion and permit release",
        _START
        + _actions(
            "queue_send",
            "queue_send",
            "queue_send",
            "send_confirmed",
            "send_confirmed",
            "close",
        ),
    ),
    ConformanceScenario(
        "exact-replay",
        "exact replay",
        _START + _RECEIVE_COMPLETE + _actions("receive_exact_replay", "ack_success", "close"),
    ),
    ConformanceScenario(
        "replay-collision",
        "same-ID/different-payload collision",
        _START + _RECEIVE_COMPLETE + _actions("receive_collision", "close"),
    ),
    ConformanceScenario(
        "stale-sequence",
        "stale sequence",
        _START + _RECEIVE_COMPLETE + _actions("receive_stale_sequence", "close"),
    ),
    ConformanceScenario(
        "future-sequence",
        "future sequence",
        _START + _RECEIVE_COMPLETE + _actions("receive_future_sequence", "close"),
    ),
    ConformanceScenario(
        "stale-generation",
        "stale generation",
        _START + _actions("receive_stale_generation", "close"),
    ),
    ConformanceScenario(
        "future-generation",
        "future generation",
        _START + _actions("receive_future_generation", "close"),
    ),
    ConformanceScenario(
        "malformed-canonical-frame",
        "malformed canonical frames",
        _START + _actions("receive_malformed_frame", "close"),
    ),
    ConformanceScenario(
        "truncated-canonical-frame",
        "truncated canonical frames",
        _START + _actions("receive_truncated_frame", "close"),
    ),
)


REQUIRED_REQUIREMENTS = frozenset(
    {
        "start/close/restart",
        "fatal receive state",
        "send reconnect",
        "receive reconnect",
        "close during blocked reconnect",
        "endpoint rotation during queue",
        "endpoint rotation during send",
        "endpoint rotation during receive",
        "endpoint rotation during dispatch",
        "endpoint rotation during ACK",
        "delayed ACK",
        "lost ACK",
        "cancellation/deadline races",
        "queue exhaustion and permit release",
        "exact replay",
        "same-ID/different-payload collision",
        "stale sequence",
        "future sequence",
        "stale generation",
        "future generation",
        "malformed canonical frames",
        "truncated canonical frames",
    }
)


__all__ = ["ConformanceScenario", "REQUIRED_REQUIREMENTS", "REQUIRED_SCENARIOS"]
