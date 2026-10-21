# SPDX-License-Identifier: AGPL-3.0-only
"""Deterministic lifetime/admission races; fake clock/runtime, real threads."""
from dataclasses import replace
from threading import Event, Thread

import pytest

from mycelium_router import relay as relay_module
from mycelium_router.contracts import PathBuildState, ProgressivePrefillContext, RuntimeResult
from mycelium_router.scheduler import BackpressureError
from .test_path_admission_expiry import _middle_router, _hop_header
from test_router_policy import state_table


def registered():
    router, graph, request, manifest, nodes = _middle_router(devices=state_table())
    assert router.relay.register_path(request, manifest, graph, entry_node_id=nodes[0])
    return router, graph, request, manifest, nodes


def progressive():
    router, graph, request, manifest, nodes = _middle_router(devices=state_table())
    build = PathBuildState(request=request, graph=graph, path_id=manifest.path_id,
                           path_attempt=manifest.path_attempt, ordered_hops=manifest.ordered_hops[:2])
    h = _hop_header(request, manifest, token_index=-1)
    from mycelium_router.idempotency import hop_idempotency_key
    h = replace(h, phase="PREFILL", idempotency_key=hop_idempotency_key(
        request_id=h.request_id, path_id=h.path_id, path_attempt=h.path_attempt,
        phase="PREFILL", token_index=-1, hop_index=h.hop_index))
    c = ProgressivePrefillContext(graph=graph, request=request, build=build, payload=b"payload")
    return router, h, c, nodes


def test_progressive_rejected_payload_does_not_create_registration():
    router, h, c, nodes = progressive()
    result = router.receive_progressive_prefill(h, replace(c, payload=object()),
        source_node_id=nodes[0], entry_node_id=nodes[0])
    assert result.reason == "unsupported_payload_type"
    assert h.path_id not in router.relay._provisional_paths
    assert h.path_id not in router.relay._path_activity


def test_progressive_payload_conflict_does_not_refresh():
    router, h, c, nodes = progressive()
    first = router.receive_progressive_prefill(h, c, source_node_id=nodes[0], entry_node_id=nodes[0])
    assert first.disposition == "FORWARDED"
    router.relay.clock.advance(1)
    result = router.receive_progressive_prefill(h, replace(c, payload=b"different"),
        source_node_id=nodes[0], entry_node_id=nodes[0])
    assert result.reason == "idempotency_payload_mismatch"
    router.relay.clock.advance(relay_module._LIVE_PATH_IDLE_SECONDS - 1)
    assert router.relay.expire_idle_paths() == (h.path_id,)


@pytest.mark.parametrize("queued", [False, True])
def test_backpressure_rejection_does_not_refresh(monkeypatch, queued):
    router, graph, request, manifest, nodes = registered()
    relay = router.relay
    def reject(_work):
        error = BackpressureError.__new__(BackpressureError)
        error.reason = "test_capacity"
        error.retry_after_seconds = 1.0
        raise error
    monkeypatch.setattr(relay.batch_scheduler if queued else relay.scheduler, "enqueue", reject)
    relay.clock.advance(relay_module._LIVE_PATH_IDLE_SECONDS - 1)
    call = router.enqueue_hop if queued else router.receive_hop
    result = call(_hop_header(request, manifest, token_index=0), b"payload")
    assert result.reason == "backpressure:test_capacity"
    relay.clock.advance(1)
    assert relay.expire_idle_paths() == (manifest.path_id,)


@pytest.mark.parametrize("queued", [False, True])
@pytest.mark.parametrize("sweep_during_runtime", [False, True])
def test_absolute_expiry_fences_inflight_output(monkeypatch, queued, sweep_during_runtime):
    router, graph, request, manifest, nodes = registered()
    relay = router.relay
    entered, finish = Event(), Event()
    results, errors = [], []
    def runtime(_):
        entered.set()
        assert finish.wait(5), "runtime barrier not released"
        return RuntimeResult(success=True, payload=b"late", token_id=7)
    monkeypatch.setattr(relay.runtime, "execute", runtime)
    monkeypatch.setattr(relay.runtime, "execute_batch", lambda batch: tuple(runtime(i) for i in batch.items))
    header = _hop_header(request, manifest, token_index=0)
    def run():
        try:
            if queued:
                results.extend(relay.drain_ready_batches(force=True))
            else:
                results.append(router.receive_hop(header, b"payload"))
        except BaseException as error:
            errors.append(error)
    if queued:
        assert router.enqueue_hop(header, b"payload").disposition == "QUEUED"
    thread = Thread(target=run, daemon=True)
    thread.start()
    try:
        assert entered.wait(5)
        relay.clock.advance(relay_module._LIVE_PATH_MAX_LIFETIME_SECONDS)
        if sweep_during_runtime:
            assert relay.expire_idle_paths() == (manifest.path_id,)
        finish.set()
        thread.join(5)
        assert not thread.is_alive()
        assert not errors, errors
        assert not relay.transport.hops
        assert not relay._hop_results
        assert manifest.path_id not in relay._paths
        assert not relay._pending_hops
        assert relay.pending_batch_hops() == 0
        assert all(r.disposition == "REJECTED" for r in results)
        assert not relay.register_path(request, manifest, graph, entry_node_id=nodes[0])
    finally:
        finish.set()
        thread.join(5)


def test_old_runtime_finalizer_does_not_refresh_successor(monkeypatch):
    router, graph, request, manifest, nodes = registered()
    relay = router.relay
    entered, finish = Event(), Event()
    results = []
    def runtime(_):
        entered.set()
        assert finish.wait(5)
        return RuntimeResult(success=True, payload=b"late")
    monkeypatch.setattr(relay.runtime, "execute", runtime)
    thread = Thread(target=lambda: results.append(router.receive_hop(
        _hop_header(request, manifest, token_index=0), b"payload")), daemon=True)
    thread.start()
    try:
        assert entered.wait(5)
        relay.clock.advance(relay_module._LIVE_PATH_MAX_LIFETIME_SECONDS)
        assert relay.expire_idle_paths() == (manifest.path_id,)
        successor = replace(manifest, path_attempt=manifest.path_attempt + 1)
        assert relay.register_path(request, successor, graph, entry_node_id=nodes[0])
        relay.clock.advance(relay_module._LIVE_PATH_IDLE_SECONDS - 1)
        finish.set()
        thread.join(5)
        assert not thread.is_alive()
        assert results[0].disposition == "REJECTED"
        relay.clock.advance(1)
        assert relay.expire_idle_paths() == (manifest.path_id,)
        assert not relay.transport.hops
    finally:
        finish.set()
        thread.join(5)


def test_registration_cannot_overtake_expiry_resource_cleanup(monkeypatch):
    router, graph, request, manifest, nodes = registered()
    relay = router.relay
    entered, finish = Event(), Event()
    old_cleanup = relay._release_path_resources
    expired = []
    def cleanup(path_id):
        entered.set()
        assert finish.wait(5)
        old_cleanup(path_id)
    monkeypatch.setattr(relay, "_release_path_resources", cleanup)
    relay.clock.advance(relay_module._LIVE_PATH_MAX_LIFETIME_SECONDS)
    thread = Thread(target=lambda: expired.extend(relay.expire_idle_paths()), daemon=True)
    thread.start()
    try:
        assert entered.wait(5)
        successor = replace(manifest, path_attempt=manifest.path_attempt + 1)
        # Fail closed during asynchronous path-id cleanup, not a stale success
        # whose new runtime/scheduler resources the old cleanup can destroy.
        assert not relay.register_path(request, successor, graph, entry_node_id=nodes[0])
        finish.set()
        thread.join(5)
        assert not thread.is_alive()
        assert expired == [manifest.path_id]
        assert relay.register_path(request, successor, graph, entry_node_id=nodes[0])
        assert router.enqueue_hop(_hop_header(request, successor, token_index=0), b"new").disposition == "QUEUED"
        assert relay.pending_batch_hops() == 1
    finally:
        finish.set()
        thread.join(5)


def test_evicted_tombstone_never_revalidates_old_dispatch_permit(monkeypatch):
    router, graph, request, manifest, nodes = registered()
    relay = router.relay
    old_generation = relay.path_generation(manifest.path_id)
    relay.clock.advance(relay_module._LIVE_PATH_MAX_LIFETIME_SECONDS)
    assert relay.expire_idle_paths() == (manifest.path_id,)
    relay.clock.advance(relay_module._TOMBSTONE_RETENTION_SECONDS)
    monkeypatch.setattr(relay_module, "_TOMBSTONE_QUOTA_PER_CREATOR", 1)
    other = replace(manifest, path_id="other")
    assert relay.register_path(request, other, graph, entry_node_id=nodes[0])
    successor = replace(manifest, path_attempt=manifest.path_attempt + 1)
    assert relay.register_path(request, successor, graph, entry_node_id=nodes[0])
    sent = []
    assert not relay.dispatch_if_current(path_id=manifest.path_id,
        path_attempt=manifest.path_attempt, generation=old_generation,
        sender=lambda: sent.append(True))
    assert not sent


def test_old_batch_cannot_consume_reused_hop_identity(monkeypatch):
    router, graph, request, manifest, nodes = registered()
    relay = router.relay
    entered, finish = Event(), Event()
    results = []
    header = _hop_header(request, manifest, token_index=0)
    def runtime(batch):
        entered.set()
        assert finish.wait(5)
        return tuple(RuntimeResult(success=True, payload=b"obsolete") for _ in batch.items)
    monkeypatch.setattr(relay, "_execute_runtime_batch", runtime)
    assert router.enqueue_hop(header, b"old").disposition == "QUEUED"
    thread = Thread(target=lambda: results.extend(relay.drain_ready_batches(
        force=True, maximum_batches=1)), daemon=True)
    thread.start()
    try:
        assert entered.wait(5)
        relay.clock.advance(relay_module._LIVE_PATH_MAX_LIFETIME_SECONDS)
        assert relay.expire_idle_paths() == (manifest.path_id,)
        relay.clock.advance(relay_module._TOMBSTONE_RETENTION_SECONDS)
        monkeypatch.setattr(relay_module, "_TOMBSTONE_QUOTA_PER_CREATOR", 1)
        assert relay.register_path(request, replace(manifest, path_id="other"), graph, entry_node_id=nodes[0])
        # After bounded replay metadata eviction this is a fresh registration;
        # the still-running old batch must not claim its identical hop key.
        assert relay.register_path(request, manifest, graph, entry_node_id=nodes[0])
        assert router.enqueue_hop(header, b"new").disposition == "QUEUED"
        finish.set()
        thread.join(5)
        assert not thread.is_alive()
        assert not relay.transport.hops
        assert len(relay._pending_hops) == 1
        assert relay.pending_batch_hops() == 1
        assert not results
    finally:
        finish.set()
        thread.join(5)
