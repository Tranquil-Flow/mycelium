# SPDX-License-Identifier: AGPL-3.0-only
"""Registry teardown over real progress/cancel transport, controlled compute."""
import threading
from types import SimpleNamespace

import pytest

from mycelium_live import supervisor
from mycelium_live.registry import LiveDeploymentRegistry
from mycelium_live.router_port import ROUTER_CLOSE_TIMEOUT_SECONDS
from mycelium_request_gateway.asgi import RequestGatewayASGIApplication
from mycelium_ui_gateway import GatewayConfig, create_product_gateway_application
from test_inflight_progress_stop import native_progress_transport
from test_inflight_route_stop import stack


def test_registry_stop_drains_gateway_before_router_worker_deadline(tmp_path, monkeypatch):
    # Hold real native request cleanup beyond the independent router thread-join
    # allowance, but inside the original 8s command cancellation authority.
    # Neither production bound changes; no model, subprocess or device is used.
    cleanup_release = threading.Event()
    timers = []
    def transport(world, route, worker):
        cursor = native_progress_transport(world, route, worker)
        def cancel(_):
            worker.release.set()
            timer = threading.Timer(ROUTER_CLOSE_TIMEOUT_SECONDS + .25, cleanup_release.set)
            timers.append(timer)
            timer.start()
            assert cleanup_release.wait(6), 'fixture cleanup was not released'
        worker.native.runtime.cancel = cancel
        return cursor

    with stack(tmp_path, monkeypatch, held_command='streaming',
               progress_factory=transport, restarting_owner=True) as t:
        subscription = t.service.subscribe(t.request_id, last_event_id=None)
        try:
            tokens = []
            while len(tokens) < 13:
                event = subscription.next_event(timeout=3)
                assert event is not None
                subscription.ack(event.sequence)
                if event.kind == 'token':
                    tokens.append(event)
                assert event.kind not in {'completed', 'cancelled', 'failed'}
            assert t.worker.progress_sealed

            request_app = RequestGatewayASGIApplication(t.service,
                authenticator=SimpleNamespace(is_authorized=lambda _: True))
            async def unused_app(scope, receive, send):
                raise AssertionError('unrelated observatory traffic')
            app = create_product_gateway_application(config=GatewayConfig(bind_host='127.0.0.1'),
                observatory_app=unused_app, request_gateway_app=request_app,
                swarm_coordinator=SimpleNamespace(), request_gateway_bearer_token='desk-private-test-token')
            # Startup qualification/model loading are deliberately out of scope.
            # Keep the actual registry.close method and router/route ownership.
            registry = object.__new__(LiveDeploymentRegistry)
            registry._lock = threading.RLock()
            registry._runtimes = {'desk': SimpleNamespace(route=t.router._route)}
            registry._routers = {'desk': t.router}
            registry.public_status = lambda: {}
            monkeypatch.setattr(supervisor, 'native_deployment_projection', lambda _: SimpleNamespace())
            monkeypatch.setattr(supervisor, '_qualified_runtime', lambda *a, **k:
                SimpleNamespace(historical_evidence=()))
            monkeypatch.setattr(supervisor, 'LiveDeploymentRegistry', lambda *a, **k: registry)
            monkeypatch.setattr(supervisor, 'build_registry_stack', lambda **k: SimpleNamespace(app=app))
            stopped = threading.Event()
            result = supervisor.run_registry_server(operator_plans=(tmp_path/'fixture-plan',),
                host='127.0.0.1', port=0, seed_state_root=tmp_path/'seed',
                governance_root=None, api_only=True, stop_event=stopped,
                on_started=lambda _: stopped.set())
            assert result == 0
            assert t.service._closed
            assert t.service._sessions[t.request_id].future.done()
            assert t.service._sessions[t.request_id].terminal_event.kind == 'cancelled'
            assert not any(thread.is_alive() for thread in t.router._worker_pool._threads)
            assert not t.errors
            assert t.worker.cancelled.is_set()
        finally:
            cleanup_release.set()
            for timer in timers:
                timer.cancel()
                timer.join(1)
            subscription.close()


@pytest.mark.parametrize('drain_error', [False, True])
def test_registry_always_retires_routes_after_gateway_drain(tmp_path, monkeypatch, drain_error):
    effects = []
    def close_app():
        effects.append('gateway_drain')
        if drain_error:
            raise RuntimeError('fixture_drain_failure')
    app = SimpleNamespace(close=close_app)
    registry = SimpleNamespace(public_status=lambda: {}, close=lambda: effects.append('route_close'))
    server = SimpleNamespace(server_address=('127.0.0.1', 12345),
        server_close=lambda: effects.append('listener_close'))
    monkeypatch.setattr(supervisor, 'native_deployment_projection', lambda _: SimpleNamespace())
    monkeypatch.setattr(supervisor, '_qualified_runtime', lambda *a, **k:
        SimpleNamespace(historical_evidence=()))
    monkeypatch.setattr(supervisor, 'LiveDeploymentRegistry', lambda *a, **k: registry)
    monkeypatch.setattr(supervisor, 'build_registry_stack', lambda **k: SimpleNamespace(app=app))
    monkeypatch.setattr(supervisor, 'create_server', lambda **k: server)
    stopped = threading.Event()
    def run():
        return supervisor.run_registry_server(operator_plans=(tmp_path/'fixture-plan',),
            host='127.0.0.1', port=0, seed_state_root=tmp_path/'seed', governance_root=None,
            api_only=True, stop_event=stopped, on_started=lambda _: stopped.set())
    if drain_error:
        with pytest.raises(RuntimeError, match='^fixture_drain_failure$'):
            run()
    else:
        assert run() == 0
    assert effects == ['listener_close', 'gateway_drain', 'route_close']
