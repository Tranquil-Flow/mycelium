# SPDX-License-Identifier: AGPL-3.0-only
"""Deterministic ordinary gateway-path tests for the A6 recovery handshake.

No physical or browser success is claimed: the RouterPort is a local fake, while the
RequestGatewayService and RouterSessionBackend are production classes.
"""
from __future__ import annotations

from dataclasses import dataclass

from mycelium_a6_recovery import CheckpointStore, ReplayCheckpoint
from mycelium_request_gateway.backend import RouterSessionBackend
from mycelium_request_gateway.contracts import (
    InferenceSubmission,
    qualification_binding,
)
from mycelium_request_gateway.service import RequestGatewayService
from test_a6_recovery import _sha
from test_core import MutableQualificationSource, _synthetic_qualification


class TokenCodec:
    tokenizer_digest = _sha("9")

    def encode(self, prompt: str) -> tuple[int, ...]:
        assert prompt == "ordinary gateway prompt"
        return (7, 8, 9)

    def decode_token(self, token_id: int) -> str:
        return f"<{token_id}>"


@dataclass
class RecoveringRouterPort:
    recover_ok: bool = True
    source_prefix: tuple[int, ...] = (101, 102)

    supports_publisher_generation = False
    requires_qualification_binding = False
    supports_workload_profiles = False

    def __post_init__(self) -> None:
        self.sink = None
        self.request = None
        self.mode = "initial"
        self.cursor = 0
        self.cancelled = False
        self.recovery_checkpoint: ReplayCheckpoint | None = None

    def admit(self, request, client_sink, **_kwargs):
        self.request = request
        self.sink = client_sink
        return request.request_id

    def request_status(self, _request_id: str) -> str:
        if self.cancelled:
            return "CANCELLED"
        if self.mode == "recovery_required":
            return "RECOVERY_REQUIRED"
        if self.mode == "completed":
            return "COMPLETED"
        return "DECODING"

    def decode_one(self, _request_id: str) -> bool:
        assert self.sink is not None
        if self.mode == "initial":
            if not self.source_prefix:
                self.mode = "recovery_required"
                return False
            token_id = self.source_prefix[self.cursor]
            self.sink.emit(self.cursor, token_id)
            self.cursor += 1
            if self.cursor == len(self.source_prefix):
                self.mode = "recovery_required"
            return True
        if self.mode == "successor":
            successor_tokens = (101, 102, 103, 104)[len(self.source_prefix) :]
            token_id = successor_tokens[self.cursor]
            self.sink.emit(self.cursor + len(self.source_prefix), token_id)
            self.cursor += 1
            if self.cursor == len(successor_tokens):
                self.mode = "completed"
            return True
        return False

    def recovery_checkpoint_binding(self, request_id: str) -> dict[str, object]:
        assert self.request is not None and request_id == self.request.request_id
        return {
            "store_identity": f"a6-store-{request_id}",
            "session_id": f"session-{request_id}",
            "source_attempt": 1,
            "path_id": "path-source",
            "path_attempt": 0,
            "tokenizer_digest": TokenCodec.tokenizer_digest,
            "created_at_unix_ms": 1_000,
            "expiry_unix_ms": 2_000_000,
        }

    def recover_full_context(
        self,
        request_id: str,
        checkpoint: ReplayCheckpoint,
    ) -> bool:
        assert self.request is not None and request_id == self.request.request_id
        self.recovery_checkpoint = checkpoint
        assert checkpoint.encoded_prompt == (7, 8, 9)
        assert checkpoint.committed_prefix_tokens == self.source_prefix
        assert checkpoint.committed_token_count == len(self.source_prefix)
        if not self.recover_ok:
            return False
        self.mode = "successor"
        self.cursor = 0
        return True

    def cancel(self, _request_id: str) -> bool:
        self.cancelled = True
        return True

    def cancel_with_deadline(self, request_id: str, **_kwargs) -> bool:
        return self.cancel(request_id)


def _run_request(
    tmp_path,
    *,
    recover_ok: bool,
    source_prefix: tuple[int, ...] = (101, 102),
):
    qualification = _synthetic_qualification()
    router = RecoveringRouterPort(
        recover_ok=recover_ok,
        source_prefix=source_prefix,
    )
    service = RequestGatewayService(
        qualification_source=MutableQualificationSource(qualification),
        backend=RouterSessionBackend(
            router=router,
            codec=TokenCodec(),
            clock=lambda: 100.0,
        ),
        request_id_source=lambda: "ordinary-a6-001",
        max_buffered_events=16,
        replay_checkpoint_root=tmp_path,
    )
    subscription = None
    try:
        request_id = service.submit(
            InferenceSubmission(
                prompt="ordinary gateway prompt",
                max_new_tokens=4,
                qualification=qualification_binding(qualification),
            )
        )
        subscription = service.subscribe(request_id, last_event_id=None)
        events = []
        while True:
            event = subscription.next_event(timeout=2)
            if event is None:
                break
            events.append(event)
            subscription.ack(event.sequence)
        return service, router, events
    except BaseException:
        service.close()
        raise
    finally:
        if subscription is not None:
            subscription.close()


def test_ordinary_gateway_request_continues_after_durable_replay(tmp_path) -> None:
    service, router, events = _run_request(tmp_path, recover_ok=True)
    try:
        tokens = [event for event in events if event.kind == "token"]
        terminals = [
            event
            for event in events
            if event.kind in {"completed", "failed", "cancelled"}
        ]
        assert [(event.token_index, event.text) for event in tokens] == [
            (0, "<101>"),
            (1, "<102>"),
            (2, "<103>"),
            (3, "<104>"),
        ]
        assert [event.kind for event in terminals] == ["completed"]
        assert service.terminal_event_count("ordinary-a6-001") == 1
        assert router.recovery_checkpoint is not None

        stored = CheckpointStore(
            tmp_path,
            store_identity="a6-store-ordinary-a6-001",
        ).load()
        assert router.recovery_checkpoint.committed_prefix_tokens == (101, 102)
        assert router.recovery_checkpoint.encoded_prompt == (7, 8, 9)
        assert stored is None
    finally:
        service.close()


def test_failed_recovery_yields_one_explicit_terminal_without_restart(tmp_path) -> None:
    service, router, events = _run_request(tmp_path, recover_ok=False)
    try:
        tokens = [event for event in events if event.kind == "token"]
        terminals = [
            event
            for event in events
            if event.kind in {"completed", "failed", "cancelled"}
        ]
        assert [(event.token_index, event.text) for event in tokens] == [
            (0, "<101>"),
            (1, "<102>"),
        ]
        assert [event.kind for event in terminals] == ["failed"]
        assert service.terminal_event_count("ordinary-a6-001") == 1
        assert router.recovery_checkpoint is not None
    finally:
        service.close()


def test_ordinary_gateway_recovers_before_first_generated_token(tmp_path) -> None:
    service, router, events = _run_request(
        tmp_path,
        recover_ok=True,
        source_prefix=(),
    )
    try:
        tokens = [event for event in events if event.kind == "token"]
        terminals = [
            event
            for event in events
            if event.kind in {"completed", "failed", "cancelled"}
        ]
        assert [(event.token_index, event.text) for event in tokens] == [
            (0, "<101>"),
            (1, "<102>"),
            (2, "<103>"),
            (3, "<104>"),
        ]
        assert [event.kind for event in terminals] == ["completed"]
        assert router.recovery_checkpoint is not None
        assert router.recovery_checkpoint.committed_prefix_tokens == ()
        assert router.recovery_checkpoint.committed_token_count == 0
    finally:
        service.close()
