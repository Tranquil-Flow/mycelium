# SPDX-License-Identifier: AGPL-3.0-only
"""Renewal of the actual production-shaped, signed qualification record."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import threading
import pytest

from mycelium_live.supervisor import ColdRouteRestartOwner
from mycelium_live.router_port import LiveRouterPort
from mycelium_live.route import RouteIdentity
from mycelium_qualification import issue_live_route_qualification
from mycelium_qualification.contracts import RouteQualificationV1
from mycelium_request_gateway.contracts import qualification_binding, qualification_digest
from mycelium_m16_runtime import M16AdmissionError
from tests.live.test_a6_m16_recovery import _coordinator, _request
from mycelium_router.serialization import execution_graph_to_dict
from mycelium_qualification.evidence import sha256_document


def record_at(timestamp: int) -> RouteQualificationV1:
    path = Path(__file__).resolve().parents[1] / "qualification" / "test_live_route_authority.py"
    spec = importlib.util.spec_from_file_location("renewal_live_authority_fixture", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    attestation = module._attestation()
    attestation["captured_at_unix_ms"] = timestamp
    return issue_live_route_qualification(
        attestation,
        expected_prompt_token_ids=module.PROMPT,
        expected_output_token_ids=module.OUTPUT,
    )


class Route:
    alive = True

    def open(self):
        return RouteIdentity(
            deployment_id="deployment", model_id="model", resolved_commit="commit",
            endpoint_ids=("endpoint",),
        )

    def is_alive(self):
        return self.alive


def owner_with(record):
    route = Route()
    owner = ColdRouteRestartOwner(
        route_factory=lambda: route,
        open_and_qualify=lambda candidate, _generation: (candidate.open(), record),
    )
    owner.open()
    return owner, route


class _Sink:
    def __init__(self):
        self.tokens = []

    def emit(self, index, token):
        self.tokens.append((index, token))


class _CompleteRouterRoute:
    """Deterministic command-only route; no model or hardware execution."""
    is_simulated = True

    def is_alive(self):
        return True

    def infer(self, _tokens, *, sink, **_kwargs):
        sink.emit(0, 101)

    def release_request(self, _request_id):
        return None

    def close(self):
        return None


def _real_renewal_port():
    old = record_at(1_000_000)
    renewed = record_at(1_000_000 + 55 * 60_000)
    graph = sys.modules['renewal_live_authority_fixture']._graph()
    assert sha256_document(execution_graph_to_dict(graph)) == old.execution_graph_digest
    owner, _concrete = owner_with(old)
    port = LiveRouterPort(
        route=_CompleteRouterRoute(), execution_graph=graph,
        runtime_coordinator=_coordinator(graph),
        recovery_a4_atomic_commit='a' * 40,
        qualification_source=owner,
    )
    clock = [old.issued_at_unix_ms]
    owner.configure_qualification_refresh(
        refresh=lambda _route: renewed,
        refresh_allowed=port.is_idle,
        run_when_idle=port.run_qualification_refresh_when_idle,
        clock_unix_ms=lambda: clock[0],
    )
    return old, renewed, owner, port, clock


def test_real_port_rotates_recovery_binding_after_signed_idle_renewal():
    old, renewed, owner, port, clock = _real_renewal_port()
    first_id = 'qualification-first'
    try:
        assert owner.current() is old
        port.admit(_request(first_id), _Sink(), qualification_binding=qualification_binding(old))
        assert port.decode_one(first_id) is True
        assert port.decode_one(first_id) is False
        assert port.request_status(first_id) == 'COMPLETED'
        assert port._pending[first_id].command_identity.qualification_digest == qualification_digest(old)
        port.release_request(first_id)
        assert port.is_idle()
        before = port.recovery_public_projection({first_id: 1})
        assert before['binding']['qualification_digest'] == qualification_digest(old)
        assert [item['request_id'] for item in before['requests']] == [first_id]

        clock[0] = renewed.issued_at_unix_ms
        assert owner.current() is renewed  # genuine signed record, not a fake timestamp
        after = port.recovery_public_projection({first_id: 1})
        assert after['binding']['qualification_digest'] == qualification_digest(renewed)
        # REVERSAL: this asserted ``after['requests'] == []`` ("old terminal
        # history is not re-labelled new"). Dropping the record made a request
        # that finished just before a renewal or a cold-restart rotation vanish
        # from the public projection (the A6 browser matrix on f9a8d961 never
        # saw a completed recovery's cutover after reload), against A6 spec
        # section 7: a refresh reconstructs public recovery state. The finished
        # record is carried, bounded, with its own unchanged terminal facts; it
        # is never relabelled as live state or as an admission under the new
        # binding.
        assert after['requests'] == before['requests']
        assert [item['terminal'] for item in after['requests']] == ['completed']

        second_id = 'qualification-second'
        port.admit(_request(second_id), _Sink(), qualification_binding=qualification_binding(renewed))
        assert port._pending[second_id].command_identity.qualification_digest == qualification_digest(renewed)
        assert port.decode_one(second_id) is True
        assert port.decode_one(second_id) is False
        port.release_request(second_id)
        current = port.recovery_public_projection({first_id: 1, second_id: 1})
        assert [item['request_id'] for item in current['requests']] == [first_id, second_id]
        assert current['requests'][0] == before['requests'][0]
        assert port._coordinator.route_identity(first_id)['request_id'] == first_id
    finally:
        port.close()


def test_outstanding_request_cannot_rebind_and_failed_renewal_preserves_old_digest():
    old, renewed, owner, port, clock = _real_renewal_port()
    try:
        port.admit(_request('qualification-outstanding'), _Sink(), qualification_binding=qualification_binding(old))
        clock[0] = renewed.issued_at_unix_ms
        assert owner.current() is old  # still pending even after terminal
        called = []
        port.run_qualification_refresh_when_idle(lambda: called.append(renewed) or renewed)
        assert called == []
        assert port._recovery_qualification_digest == qualification_digest(old)
        assert port.decode_one('qualification-outstanding') is True
        assert port.decode_one('qualification-outstanding') is False
        assert port.request_status('qualification-outstanding') == 'COMPLETED'
        port.release_request('qualification-outstanding')
        assert port.is_idle()
        # A failed challenge cannot silently rotate authority.
        owner.configure_qualification_refresh(
            refresh=lambda _route: (_ for _ in ()).throw(RuntimeError('challenge failed')),
            refresh_allowed=port.is_idle,
            run_when_idle=port.run_qualification_refresh_when_idle,
            clock_unix_ms=lambda: clock[0],
        )
        assert owner.current() is old
        assert port._recovery_qualification_digest == qualification_digest(old)
        clock[0] = old.issued_at_unix_ms + 60 * 60_000 + 1
        assert owner.peek_current() is None
        with pytest.raises(M16AdmissionError) as expired:
            port.recovery_public_projection({'qualification-outstanding': 1})
        assert expired.value.code == 'recovery_projection_unavailable'
        with pytest.raises(M16AdmissionError) as failure:
            port.admit(_request('qualification-foreign'), _Sink(), qualification_binding=qualification_binding(renewed))
        assert failure.value.code == 'qualification_binding_changed'
    finally:
        port.close()


def test_status_and_recovery_projection_never_launch_a_challenge_on_http_read():
    old, renewed, owner, port, clock = _real_renewal_port()
    calls = []
    owner.configure_qualification_refresh(
        refresh=lambda _route: calls.append('challenge') or renewed,
        refresh_allowed=port.is_idle,
        run_when_idle=port.run_qualification_refresh_when_idle,
        clock_unix_ms=lambda: clock[0],
    )
    try:
        clock[0] = renewed.issued_at_unix_ms
        assert owner.public_status()['route_ready'] is True
        assert port.recovery_public_projection({})['binding']['qualification_digest'] == qualification_digest(old)
        assert calls == []
        assert owner.current() is renewed
        assert calls == ['challenge']
    finally:
        port.close()


def test_dead_route_or_foreign_challenge_cannot_rotate_recovery_binding():
    old, renewed, owner, port, clock = _real_renewal_port()
    try:
        port.admit(_request('qualification-original'), _Sink(), qualification_binding=qualification_binding(old))
        assert port.decode_one('qualification-original') is True
        assert port.decode_one('qualification-original') is False
        port.release_request('qualification-original')
        foreign = record_at(renewed.issued_at_unix_ms)
        object.__setattr__(foreign, 'deployment_id', 'foreign-deployment')  # adversarial tamper
        with pytest.raises(M16AdmissionError) as failure:
            port.run_qualification_refresh_when_idle(lambda: foreign)
        assert failure.value.code == 'qualification_binding_changed'
        assert port._recovery_qualification_digest == qualification_digest(old)
        assert owner._current is not None
        setattr(owner._current, 'alive', False)
        clock[0] = renewed.issued_at_unix_ms
        assert owner.current() is None
        with pytest.raises(M16AdmissionError) as unavailable:
            port.recovery_public_projection({'qualification-original': 1})
        assert unavailable.value.code == 'recovery_projection_unavailable'
        assert port._recovery_qualification_digest == qualification_digest(old)
    finally:
        port.close()


def test_physical_owner_renews_after_55_minutes_only_when_idle():
    start = 1_000_000
    old = record_at(start)
    renewed = record_at(start + 55 * 60_000)
    assert isinstance(old, RouteQualificationV1) and old.route_ready
    assert renewed.qualification_id != old.qualification_id
    owner, route = owner_with(old)
    clock = [start + 55 * 60_000]
    idle = [False]
    calls = []
    installed = []
    owner.configure_qualification_refresh(
        refresh=lambda candidate: calls.append(candidate) or renewed,
        refresh_allowed=lambda: idle[0],
        clock_unix_ms=lambda: clock[0],
        on_refresh=lambda candidate, record: installed.append((candidate, record)),
    )
    assert owner.current() is old
    idle[0] = True
    assert owner.current() is renewed
    assert calls == [route]
    assert installed == [(route, renewed)]
    assert owner.current() is renewed
    route.alive = False
    assert owner.current() is None


def test_failed_or_invalid_renewal_retains_original_timestamp_and_expires_strictly():
    start = 1_000_000
    old = record_at(start)
    owner, _route = owner_with(old)
    clock = [start + 55 * 60_000]
    owner.configure_qualification_refresh(
        refresh=lambda _candidate: old,  # no advanced attested clock
        refresh_allowed=lambda: True,
        clock_unix_ms=lambda: clock[0],
    )
    assert owner.current() is old
    clock[0] = start + 60 * 60_000 + 1
    assert owner.current() is None
    assert owner.public_status()["route_ready"] is False
    assert old.issued_at_unix_ms == start
    owner.configure_qualification_refresh(
        refresh=lambda _candidate: (_ for _ in ()).throw(RuntimeError("challenge failed")),
        refresh_allowed=lambda: True,
        clock_unix_ms=lambda: clock[0],
    )
    assert owner.current() is None


def test_router_serializes_renewal_against_request_admission():
    start = 1_000_000
    old, renewed = record_at(start), record_at(start + 55 * 60_000)
    owner, _ = owner_with(old)
    port = object.__new__(LiveRouterPort)
    port._lock = threading.RLock()
    port._admission_dispatch_lock = threading.Lock()
    port._pending = {}
    port._closed = False
    port._graph = sys.modules['renewal_live_authority_fixture']._graph()
    port._recovery_qualification_digest = None
    port._recovery_current_request_ids = set()
    entered = threading.Event()
    release = threading.Event()

    def challenge(_route):
        entered.set()
        assert release.wait(timeout=3)
        return renewed

    owner.configure_qualification_refresh(
        refresh=challenge,
        refresh_allowed=port.is_idle,
        run_when_idle=port.run_qualification_refresh_when_idle,
        clock_unix_ms=lambda: start + 55 * 60_000,
    )
    # A pending admitted request prevents any challenge, even if a stale
    # optimistic idle check raced before the guarded check.
    port._pending['inflight'] = object()
    assert owner.current() is old
    assert not entered.is_set()
    port._pending.clear()
    outcome = []
    worker = threading.Thread(target=lambda: outcome.append(owner.current()))
    worker.start()
    try:
        assert entered.wait(timeout=3)
        assert port._admission_dispatch_lock.acquire(blocking=False) is False
    finally:
        release.set()
        worker.join(timeout=3)
    assert not worker.is_alive()
    assert outcome == [renewed]
    assert port._recovery_qualification_digest == qualification_digest(renewed)
    assert owner.current() is renewed


def test_foreign_identity_does_not_replace_current_authority():
    start = 1_000_000
    old = record_at(start)
    foreign = record_at(start + 55 * 60_000)
    object.__setattr__(foreign, "deployment_id", "foreign-deployment")
    owner, _ = owner_with(old)
    owner.configure_qualification_refresh(
        refresh=lambda _: foreign,
        refresh_allowed=lambda: True,
        clock_unix_ms=lambda: start + 55 * 60_000,
    )
    assert owner.current() is old
    assert owner.current().deployment_id == old.deployment_id
