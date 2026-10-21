# SPDX-License-Identifier: AGPL-3.0-only
"""Deterministic tests for the A6 gateway-side replay checkpoint freeze seam.

Proves the request gateway can freeze its committed delivery state (canonical
prompt, committed token-digest chain, delivery cursor, publisher generation)
into an owner-private A6 replay checkpoint, without changing any existing
gateway behaviour.  No physical or browser result is claimed here.
"""

from __future__ import annotations

import asyncio
import threading
from dataclasses import replace

import pytest

from mycelium_a6_gateway import (
    CommittedDeliverySnapshot,
    canonical_generation_settings,
    checkpoint_binds_snapshot,
    freeze_replay_checkpoint,
)
from mycelium_a6_recovery import CheckpointStore, ReplayCheckpoint, validate_checkpoint_projection
from mycelium_request_gateway.contracts import (
    AdmissionError,
    InferenceSubmission,
    qualification_binding,
)
from mycelium_request_gateway.asgi import RequestGatewayASGIApplication
from mycelium_request_gateway.auth import StaticBearerAuthenticator
from mycelium_request_gateway.init import create_request_gateway_application
from mycelium_request_gateway.service import RequestGatewayService
from test_a6_recovery import _sha
from test_core import ASGIHarness, MutableQualificationSource, _synthetic_qualification

GENERATION_SETTINGS = {
    "protocol": "mycelium.request_gateway.v1",
    "max_new_tokens": 8,
    "workload_profile_id": None,
    "qos_class": None,
}


def _snapshot(*, prefix: tuple[int, ...] = (101, 102)) -> CommittedDeliverySnapshot:
    return CommittedDeliverySnapshot(
        request_id="req-1",
        encoded_prompt_token_ids=(17, 23, 42),
        generation_settings=dict(GENERATION_SETTINGS),
        committed_token_ids=prefix,
        delivery_cursor=2,
        publisher_generation=1,
        session_id="sess-1",
        source_attempt=1,
        path_id="path-source-1",
        path_digest=_sha("a"),
        path_attempt=0,
        deployment_id="deployment-a4",
        deployment_epoch=1,
        topology_generation=1,
        source_qualification_digest=_sha("0"),
        model_id="model-7b",
        representation_digest=_sha("1"),
        tokenizer_digest=_sha("4"),
        recovery_attempt=0,
    )


class FreezeProbeBackend:
    """Emits a fixed token prefix, then blocks until released."""

    def __init__(self, tokens: tuple[str, ...]) -> None:
        self.tokens = tokens
        self.release = threading.Event()
        self.cancelled: list[str] = []

    def run(self, request_id, submission, emit_token, is_cancelled):
        emit_token.bind_encoded_prompt((17, 23, 42))
        for index, text in enumerate(self.tokens):
            if is_cancelled():
                return "cancelled"
            emit_token.emit_router_token(index, 101 + index, text)
        self.release.wait(timeout=10)
        if is_cancelled():
            return "cancelled"
        return "completed"

    def cancel(self, request_id: str) -> None:
        self.cancelled.append(request_id)
        self.release.set()


class TextOnlyFreezeProbeBackend(FreezeProbeBackend):
    """Legacy text-only backend: deliberately lacks exact replay material."""

    def run(self, request_id, submission, emit_token, is_cancelled):
        for index, text in enumerate(self.tokens):
            if is_cancelled():
                return "cancelled"
            emit_token(index, text)
        self.release.wait(timeout=10)
        return "cancelled" if is_cancelled() else "completed"


def test_request_gateway_composition_wires_private_replay_root(tmp_path) -> None:
    checkpoint_root = tmp_path / "private-checkpoints"

    app = create_request_gateway_application(
        qualification_source=MutableQualificationSource(_synthetic_qualification()),
        router=object(),
        codec=object(),
        bearer_token="test-token",
        replay_checkpoint_root=checkpoint_root,
    )

    assert app._service._replay_checkpoint_root == checkpoint_root
    assert checkpoint_root.is_dir()
    assert checkpoint_root.stat().st_mode & 0o077 == 0


def _submission(qualification, max_new_tokens: int = 4) -> InferenceSubmission:
    return InferenceSubmission(
        prompt="sensitive prompt never log",
        max_new_tokens=max_new_tokens,
        qualification=qualification_binding(qualification),
    )


def _drain(subscription, *, timeout: float = 2.0):
    events = []
    while True:
        try:
            event = subscription.next_event(timeout=timeout)
        except TimeoutError:
            break
        if event is None:
            break
        events.append(event)
        subscription.ack(event.sequence)
    return events


# --------------------------------------------------------------------------- #
# Pure bridge: freeze, bind, tamper
# --------------------------------------------------------------------------- #


def test_snapshot_freezes_into_bound_checkpoint() -> None:
    snapshot = _snapshot()
    checkpoint = freeze_replay_checkpoint(
        snapshot,
        store_identity="store-1",
        created_at_unix_ms=1_000,
        expiry_unix_ms=2_000_000,
    )
    assert isinstance(checkpoint, ReplayCheckpoint)
    assert checkpoint_binds_snapshot(checkpoint, snapshot)
    assert checkpoint.committed_token_count == 2
    assert checkpoint.watermark == 2
    assert checkpoint.prompt_digest == snapshot.prompt_digest()
    assert checkpoint.committed_prefix_tokens == snapshot.committed_token_ids
    assert checkpoint.source_qualification_digest == _sha("0")
    assert checkpoint.topology_generation == 1


def test_snapshot_tamper_breaks_binding() -> None:
    snapshot = _snapshot()
    checkpoint = freeze_replay_checkpoint(
        snapshot,
        store_identity="store-1",
        created_at_unix_ms=1_000,
        expiry_unix_ms=2_000_000,
    )
    # Change one committed token digest: the binding must fail closed.
    tampered = _snapshot(prefix=(101, 999))
    assert not checkpoint_binds_snapshot(checkpoint, tampered)
    # Change the prompt: binding fails.
    different_prompt = replace(snapshot, encoded_prompt_token_ids=(17, 23, 99))
    assert not checkpoint_binds_snapshot(checkpoint, different_prompt)
    # A different publisher generation breaks the binding.
    different_generation = replace(snapshot, publisher_generation=7)
    assert not checkpoint_binds_snapshot(checkpoint, different_generation)


def test_zero_token_prefix_checkpoint() -> None:
    snapshot = _snapshot(prefix=())
    checkpoint = freeze_replay_checkpoint(
        snapshot,
        store_identity="store-1",
        created_at_unix_ms=1_000,
        expiry_unix_ms=2_000_000,
    )
    assert checkpoint.committed_token_count == 0
    assert checkpoint.committed_prefix_digest is None
    assert checkpoint_binds_snapshot(checkpoint, snapshot)


def test_snapshot_accepts_nothing_committed_delivery_cursor() -> None:
    snapshot = replace(_snapshot(), delivery_cursor=-1)
    checkpoint = freeze_replay_checkpoint(
        snapshot,
        store_identity="store-1",
        created_at_unix_ms=1_000,
        expiry_unix_ms=2_000_000,
    )
    assert checkpoint.delivery_cursor == -1
    assert checkpoint_binds_snapshot(checkpoint, snapshot)


def test_checkpoint_public_projection_hides_private_material() -> None:
    checkpoint = freeze_replay_checkpoint(
        _snapshot(),
        store_identity="store-1",
        created_at_unix_ms=1_000,
        expiry_unix_ms=2_000_000,
    )
    projection = validate_checkpoint_projection(checkpoint.to_dict())
    assert "prompt" not in projection
    assert "encoded_prompt" not in projection
    assert "committed_prefix_tokens" not in projection


def test_generation_settings_are_closed_and_canonical() -> None:
    assert canonical_generation_settings(GENERATION_SETTINGS) == GENERATION_SETTINGS
    with pytest.raises(ValueError):
        canonical_generation_settings({**GENERATION_SETTINGS, "extra": 1})
    with pytest.raises(ValueError):
        canonical_generation_settings({**GENERATION_SETTINGS, "max_new_tokens": 0})


def test_committed_token_ids_reject_non_integer() -> None:
    with pytest.raises(ValueError):
        _snapshot(prefix=("not-a-token-id",))  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Live gateway seam: freeze mid-stream through the ordinary service path
# --------------------------------------------------------------------------- #


def test_live_service_freezes_committed_delivery_state(tmp_path) -> None:
    qualification = _synthetic_qualification()
    source = MutableQualificationSource(qualification)
    backend = FreezeProbeBackend(("moon", "lit"))
    service = RequestGatewayService(
        qualification_source=source,
        backend=backend,
        request_id_source=lambda: "a6-frz-001",
        max_buffered_events=16,
        replay_checkpoint_root=tmp_path,
    )
    try:
        request_id = service.submit(_submission(qualification))
        subscription = service.subscribe(request_id, last_event_id=None)
        events = _drain(subscription)
        token_events = [event for event in events if event.kind == "token"]
        assert len(token_events) == 2  # backend emitted two, then blocked

        binding = qualification_binding(qualification)
        checkpoint = service.freeze_replay_checkpoint(
            request_id,
            store_identity="store-live-1",
            session_id="sess-live-1",
            source_attempt=1,
            path_id="path-source-1",
            path_attempt=0,
            tokenizer_digest=_sha("4"),
            created_at_unix_ms=1_000,
            expiry_unix_ms=2_000_000,
        )

        # The frozen checkpoint binds the exact committed prefix the gateway
        # accepted, plus the delivery cursor and publisher generation.
        assert checkpoint.committed_token_count == 2
        assert checkpoint.encoded_prompt == (17, 23, 42)
        assert checkpoint.committed_prefix_tokens == (101, 102)
        assert checkpoint.delivery_cursor == events[-1].sequence
        assert checkpoint.publisher_generation == subscription.publisher_generation
        assert checkpoint.path_digest == binding.path_manifest_digest
        assert checkpoint.deployment_id == binding.deployment_id
        assert checkpoint.model_id == binding.model_id
        assert checkpoint.representation_digest == binding.manifest_digest
        store = CheckpointStore(
            tmp_path,
            store_identity=checkpoint.store_identity,
        )
        assert checkpoint.store_identity != "store-live-1"
        assert store.load() == checkpoint

        # Release the backend so the request reaches its terminal, then the
        # freeze must fail closed.
        backend.release.set()
        _drain(subscription)
        assert store.load() is None
        with pytest.raises(AdmissionError, match="request_already_terminal"):
            service.freeze_replay_checkpoint(
                request_id,
                store_identity="store-live-1",
                session_id="sess-live-1",
                source_attempt=1,
                path_id="path-source-1",
                path_attempt=0,
                tokenizer_digest=_sha("4"),
                created_at_unix_ms=1_000,
                expiry_unix_ms=2_000_000,
            )
    finally:
        service.close()


def test_live_service_commits_ledger_tokens_before_delivery_ack(tmp_path) -> None:
    qualification = _synthetic_qualification()
    backend = FreezeProbeBackend(("moon", "lit"))
    service = RequestGatewayService(
        qualification_source=MutableQualificationSource(qualification),
        backend=backend,
        request_id_source=lambda: "a6-frz-unacked",
        max_buffered_events=16,
        replay_checkpoint_root=tmp_path,
    )
    try:
        request_id = service.submit(_submission(qualification))
        subscription = service.subscribe(request_id, last_event_id=None)
        events = []
        for _ in range(16):
            event = subscription.next_event(timeout=1.0)
            assert event is not None
            events.append(event)
            if event.kind == "token" and event.text == "lit":
                break
            subscription.ack(event.sequence)
        assert [event.text for event in events if event.kind == "token"] == [
            "moon",
            "lit",
        ]
        acknowledged_cursor = events[-2].sequence

        checkpoint = service.freeze_replay_checkpoint(
            request_id,
            store_identity="store-live-unacked",
            session_id="sess-live-unacked",
            source_attempt=1,
            path_id="path-source-1",
            path_attempt=0,
            tokenizer_digest=_sha("4"),
            created_at_unix_ms=1_000,
            expiry_unix_ms=2_000_000,
        )
        assert checkpoint.committed_prefix_tokens == (101, 102)
        assert checkpoint.committed_token_count == 2
        assert checkpoint.delivery_cursor == acknowledged_cursor
    finally:
        backend.release.set()
        service.close()


def test_live_service_freezes_zero_token_prefix() -> None:
    qualification = _synthetic_qualification()
    source = MutableQualificationSource(qualification)
    backend = FreezeProbeBackend(())  # emits nothing, blocks forever
    service = RequestGatewayService(
        qualification_source=source,
        backend=backend,
        request_id_source=lambda: "a6-frz-002",
        max_buffered_events=16,
    )
    try:
        request_id = service.submit(_submission(qualification))
        subscription = service.subscribe(request_id, last_event_id=None)
        events = _drain(subscription)
        assert all(event.kind != "token" for event in events)

        checkpoint = service.freeze_replay_checkpoint(
            request_id,
            store_identity="store-live-2",
            session_id="sess-live-2",
            source_attempt=1,
            path_id="path-source-1",
            path_attempt=0,
            tokenizer_digest=_sha("4"),
            created_at_unix_ms=1_000,
            expiry_unix_ms=2_000_000,
        )
        assert checkpoint.committed_token_count == 0
        assert checkpoint.committed_prefix_digest is None
    finally:
        service.close()


def test_live_service_freeze_rejects_unknown_and_terminal_request() -> None:
    qualification = _synthetic_qualification()
    source = MutableQualificationSource(qualification)
    backend = FreezeProbeBackend(("only",))
    service = RequestGatewayService(
        qualification_source=source,
        backend=backend,
        request_id_source=lambda: "a6-frz-003",
        max_buffered_events=16,
    )
    try:
        with pytest.raises(AdmissionError, match="unknown_request"):
            service.freeze_replay_checkpoint(
                "never-submitted",
                store_identity="store-live-3",
                session_id="sess-live-3",
                source_attempt=1,
                path_id="path-source-1",
                path_attempt=0,
                tokenizer_digest=_sha("4"),
                created_at_unix_ms=1_000,
                expiry_unix_ms=2_000_000,
            )
        request_id = service.submit(_submission(qualification))
        subscription = service.subscribe(request_id, last_event_id=None)
        _drain(subscription)
        backend.release.set()
        _drain(subscription)
        # Once the request reached its terminal, the freeze window is closed.
        with pytest.raises(AdmissionError, match="request_already_terminal"):
            service.freeze_replay_checkpoint(
                request_id,
                store_identity="store-live-3",
                session_id="sess-live-3",
                source_attempt=1,
                path_id="path-source-1",
                path_attempt=0,
                tokenizer_digest=_sha("4"),
                created_at_unix_ms=1_000,
                expiry_unix_ms=2_000_000,
            )
    finally:
        service.close()


class ExactMaterialProbeBackend(FreezeProbeBackend):
    """Uses the private gateway emission channel required by A6."""

    def run(self, request_id, submission, emit_token, is_cancelled):
        emit_token.bind_encoded_prompt((17, 23, 42))
        for index, (token_id, text) in enumerate(((101, "moon"), (102, "lit"))):
            if is_cancelled():
                return "cancelled"
            emit_token.emit_router_token(index, token_id, text)
        self.release.wait(timeout=10)
        return "cancelled" if is_cancelled() else "completed"


def test_checkpoint_retains_exact_encoded_prompt_and_committed_token_ids() -> None:
    snapshot = CommittedDeliverySnapshot(
        request_id="req-exact",
        encoded_prompt_token_ids=(17, 23, 42),
        generation_settings=dict(GENERATION_SETTINGS),
        committed_token_ids=(101, 102),
        delivery_cursor=2,
        publisher_generation=1,
        session_id="sess-exact",
        source_attempt=1,
        path_id="path-source-1",
        path_digest=_sha("a"),
        path_attempt=0,
        deployment_id="deployment-a4",
        deployment_epoch=1,
        topology_generation=1,
        source_qualification_digest=_sha("0"),
        model_id="model-7b",
        representation_digest=_sha("1"),
        tokenizer_digest=_sha("4"),
    )

    checkpoint = freeze_replay_checkpoint(
        snapshot,
        store_identity="store-exact",
        created_at_unix_ms=1_000,
        expiry_unix_ms=2_000_000,
    )

    assert checkpoint.encoded_prompt == (17, 23, 42)
    assert checkpoint.committed_prefix_tokens == (101, 102)
    assert checkpoint.committed_token_count == 2
    assert checkpoint_binds_snapshot(checkpoint, snapshot)


def test_live_service_freezes_exact_private_router_material() -> None:
    qualification = _synthetic_qualification()
    backend = ExactMaterialProbeBackend(())
    service = RequestGatewayService(
        qualification_source=MutableQualificationSource(qualification),
        backend=backend,
        request_id_source=lambda: "a6-exact-001",
        max_buffered_events=16,
    )
    try:
        request_id = service.submit(_submission(qualification))
        subscription = service.subscribe(request_id, last_event_id=None)
        events = _drain(subscription)
        assert [event.text for event in events if event.kind == "token"] == ["moon", "lit"]

        checkpoint = service.freeze_replay_checkpoint(
            request_id,
            store_identity="store-exact-live",
            session_id="sess-exact-live",
            source_attempt=1,
            path_id="path-source-1",
            path_attempt=0,
            tokenizer_digest=_sha("4"),
            created_at_unix_ms=1_000,
            expiry_unix_ms=2_000_000,
        )
        assert checkpoint.encoded_prompt == (17, 23, 42)
        assert checkpoint.committed_prefix_tokens == (101, 102)
        binding = qualification_binding(qualification)
        assert checkpoint.source_qualification_digest == binding.qualification_digest
        assert checkpoint.topology_generation == binding.topology_version
    finally:
        service.close()


def test_live_service_rejects_digest_only_replay_material() -> None:
    qualification = _synthetic_qualification()
    backend = TextOnlyFreezeProbeBackend(("moon", "lit"))
    service = RequestGatewayService(
        qualification_source=MutableQualificationSource(qualification),
        backend=backend,
        request_id_source=lambda: "a6-digest-only-001",
        max_buffered_events=16,
    )
    try:
        request_id = service.submit(_submission(qualification))
        subscription = service.subscribe(request_id, last_event_id=None)
        _drain(subscription)
        with pytest.raises(AdmissionError, match="replay_material_unavailable"):
            service.freeze_replay_checkpoint(
                request_id,
                store_identity="store-digest-only",
                session_id="sess-digest-only",
                source_attempt=1,
                path_id="path-source-1",
                path_attempt=0,
                tokenizer_digest=_sha("4"),
                created_at_unix_ms=1_000,
                expiry_unix_ms=2_000_000,
            )
    finally:
        service.close()


def test_recovery_projection_endpoint_requires_service_auth(monkeypatch) -> None:
    qualification = _synthetic_qualification()
    service = RequestGatewayService(
        qualification_source=MutableQualificationSource(qualification),
        backend=FreezeProbeBackend(()),
        request_id_source=lambda: "unused",
    )
    projection = {
        "protocol": "mycelium.full_context_recovery_runtime.v1",
        "bounded": True,
    }
    monkeypatch.setattr(service, "recovery_public_projection", lambda: projection)
    app = RequestGatewayASGIApplication(
        service, authenticator=StaticBearerAuthenticator("request-service-token")
    )
    try:
        missing = asyncio.run(ASGIHarness.request(app, "/v1/recovery/status"))
        accepted = asyncio.run(
            ASGIHarness.request(
                app, "/v1/recovery/status", token="request-service-token"
            )
        )
        assert missing[0] == 401
        assert accepted[0] == 200
        assert accepted[2] == projection
        assert accepted[1]["cache-control"] == "no-store"
    finally:
        service.close()


def test_gateway_restart_aborts_orphaned_private_checkpoint_without_content(tmp_path) -> None:
    qualification = _synthetic_qualification()
    checkpoint = freeze_replay_checkpoint(
        _snapshot(),
        store_identity="restart-store-a6",
        created_at_unix_ms=1_000,
        expiry_unix_ms=2_000_000,
    )
    store = CheckpointStore(tmp_path, store_identity="restart-store-a6")
    store.write(checkpoint)

    service = RequestGatewayService(
        qualification_source=MutableQualificationSource(qualification),
        backend=FreezeProbeBackend(()),
        request_id_source=lambda: "restart-unused",
        replay_checkpoint_root=tmp_path,
    )
    try:
        assert store.load() is None
        reconciliations = service.startup_recovery_reconciliations()
        assert reconciliations == (
            {
                "checkpoint_digest": checkpoint.checkpoint_digest(),
                "outcome": "aborted",
                "reason_code": "runtime_owner_not_restored",
            },
        )
        assert "restart-session-a6" not in repr(reconciliations)
        assert checkpoint.request_id not in repr(reconciliations)
        assert "encoded_prompt" not in repr(reconciliations)
    finally:
        service.close()


def test_gateway_restart_reconciliation_validates_all_before_deleting(tmp_path) -> None:
    checkpoint = freeze_replay_checkpoint(
        _snapshot(),
        store_identity="aaa-valid-a6",
        created_at_unix_ms=1_000,
        expiry_unix_ms=2_000_000,
    )
    valid_store = CheckpointStore(tmp_path, store_identity="aaa-valid-a6")
    valid_store.write(checkpoint)
    malformed = tmp_path / "zzz-malformed.checkpoint.json"
    malformed.write_text("{not-json", encoding="utf-8")
    malformed.chmod(0o600)

    with pytest.raises(ValueError, match="checkpoint_store_corrupt"):
        RequestGatewayService(
            qualification_source=MutableQualificationSource(_synthetic_qualification()),
            backend=FreezeProbeBackend(()),
            request_id_source=lambda: "restart-unused",
            replay_checkpoint_root=tmp_path,
        )

    assert valid_store.load() == checkpoint
    assert malformed.exists()



def test_gateway_derives_request_isolated_checkpoint_authority(tmp_path) -> None:
    qualification = _synthetic_qualification()
    request_ids = iter(("request-a", "request-b"))
    backend = FreezeProbeBackend(("moon",))
    service = RequestGatewayService(
        qualification_source=MutableQualificationSource(qualification),
        backend=backend,
        request_id_source=lambda: next(request_ids),
        max_concurrent_requests=2,
        replay_checkpoint_root=tmp_path,
    )
    try:
        first_id = service.submit(_submission(qualification))
        second_id = service.submit(_submission(qualification))
        _drain(service.subscribe(first_id, last_event_id=None))
        _drain(service.subscribe(second_id, last_event_id=None))

        common = dict(
            store_identity="caller-shared-store",
            session_id="session",
            source_attempt=1,
            path_id="path-source-1",
            path_attempt=0,
            tokenizer_digest=_sha("4"),
            created_at_unix_ms=1_000,
            expiry_unix_ms=2_000_000,
        )
        first = service.freeze_replay_checkpoint(first_id, **common)
        second = service.freeze_replay_checkpoint(second_id, **common)

        assert first.store_identity != second.store_identity
        assert first.store_identity != "caller-shared-store"
        assert second.store_identity != "caller-shared-store"
        assert len(tuple(tmp_path.glob("*.checkpoint.json"))) == 2
        assert CheckpointStore(tmp_path, store_identity=first.store_identity).load() == first
        assert CheckpointStore(tmp_path, store_identity=second.store_identity).load() == second
    finally:
        service.close()



def test_gateway_restart_reconciliation_enters_privacy_reduced_product_projection(
    tmp_path,
) -> None:
    from mycelium_a6_recovery import _digest

    checkpoint = freeze_replay_checkpoint(
        _snapshot(),
        store_identity="restart-product-a6",
        created_at_unix_ms=1_000,
        expiry_unix_ms=2_000_000,
    )
    CheckpointStore(tmp_path, store_identity=checkpoint.store_identity).write(checkpoint)
    backend = FreezeProbeBackend(())

    def recovery_projection(_committed_counts):
        body = {
            "protocol": "mycelium.full_context_recovery_runtime.v1",
            "binding": {
                "deployment_id": checkpoint.deployment_id,
                "deployment_epoch": checkpoint.deployment_epoch,
                "qualification_digest": checkpoint.source_qualification_digest,
                "a4_atomic_commit": None,
            },
            "requests": [],
            "breaker": {
                "state": "closed",
                "failure_observations_unix_ms": [],
                "open_until_unix_ms": 0,
            },
        }
        return {**body, "runtime_digest": _digest(body)}

    backend.recovery_public_projection = recovery_projection
    service = RequestGatewayService(
        qualification_source=MutableQualificationSource(_synthetic_qualification()),
        backend=backend,
        request_id_source=lambda: "restart-unused",
        replay_checkpoint_root=tmp_path,
    )
    try:
        projection = service.recovery_public_projection()
        assert len(projection["requests"]) == 1
        record = projection["requests"][0]
        assert record["phase"] == "aborted"
        assert record["reconciled"] == "aborted"
        assert record["cleanup_complete"] is True
        assert record["terminal"] is None
        rendered = repr(projection)
        assert checkpoint.checkpoint_digest() not in rendered
        assert checkpoint.request_id not in rendered
        assert checkpoint.session_id not in rendered
    finally:
        service.close()
