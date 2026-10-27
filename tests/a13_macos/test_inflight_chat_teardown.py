# SPDX-License-Identifier: AGPL-3.0-only
"""Real chat/HTTP/request/route composition; scripted native progress only.

This is not target staging cleanup or model/device qualification evidence.
"""
import secrets
import threading
import time
from types import SimpleNamespace

import pytest

from mycelium_a13_macos.chat import ChatService, RegistryClient
from mycelium_a13_macos.demo_chain import AppDemoChain
from mycelium_live.supervisor import create_server
from mycelium_request_gateway.asgi import RequestGatewayASGIApplication
from mycelium_request_gateway.auth import StaticBearerAuthenticator
from mycelium_ui_gateway import GatewayConfig, create_product_gateway_application
from test_inflight_route_stop import stack
from inflight_stop_support import delayed_cleanup_target


@pytest.mark.parametrize('rollover', [False, True])
def test_teardown_cancels_streaming_chat_before_listener_stop(tmp_path, monkeypatch, rollover):
    with stack(tmp_path, monkeypatch, held_command='streaming', auto_submit=False) as t:
        credential = secrets.token_urlsafe(32)
        request_app = RequestGatewayASGIApplication(t.service,
            authenticator=StaticBearerAuthenticator(credential))
        async def unused(*args):
            raise AssertionError('unexpected observatory request')
        ui = create_product_gateway_application(config=GatewayConfig(source_mode='fixture'),
            observatory_app=unused, request_gateway_app=request_app,
            swarm_coordinator=SimpleNamespace(), request_gateway_bearer_token=credential)
        server = create_server(app=ui, route=t.route, static_root=tmp_path,
            host='127.0.0.1', port=0, api_only=True)
        listener = threading.Thread(target=server.serve_forever, name='desk-registry-http')
        listener.start()
        received = threading.Event()
        finished = threading.Event()
        clients = []
        class ObservedClient(RegistryClient):
            def stream(self, prompt, maximum, on_token):
                count = 0
                def token(text):
                    nonlocal count
                    on_token(text)
                    count += 1
                    if count == 13:
                        received.set()
                return super().stream(prompt, maximum, token)
        def client_factory(origin):
            client = ObservedClient(origin, timeout=10)
            clients.append(client)
            return client
        status = {'state': 'serving', 'run_id': 'desk-run',
                  'registry_origin': f'http://127.0.0.1:{server.server_address[1]}'}
        target = delayed_cleanup_target(t, monkeypatch)
        app = SimpleNamespace(root=tmp_path, owner_run=SimpleNamespace(status=lambda: status),
            run_exchange=SimpleNamespace(announce=target.announce))
        app.chat = ChatService(app, client_factory=client_factory)
        work = app.chat._work
        def chat_work(*args):
            try:
                work(*args)
            finally:
                finished.set()
        monkeypatch.setattr(app.chat, '_work', chat_work)
        cancel_before_stop = []
        def stop_before(app, **kwargs):
            cancel_before_stop.append(clients[0]._cancel_sent)
            target.proof_may_return.set()
            server.shutdown()
            server.server_close()
            t.router.close()
            t.route.close()
        chain = AppDemoChain(app, ops=SimpleNamespace(stop_before=stop_before))
        failures = []
        ctx = SimpleNamespace(state={'rollover_requested': rollover, 'model': {},
            'slice_mode': True, 'manifest_digest': 'sha256:'+'a'*64}, result={},
            reason='rollover' if rollover else 'manual', run_id='desk-run', deployment_id='desk-deployment',
            record_exception=lambda code, error: failures.append((code, type(error).__name__)))
        request = app.chat.send('desk-client', {'prompt': 'live recovery prompt', 'max_new_tokens': 256})
        try:
            assert received.wait(3), app.chat.events({'request_id': request['request_id'], 'after': 0})
            assert t.worker.entered.is_set() and not t.worker.release.is_set()
            status['state'] = 'stopping'
            if rollover:
                status['rollover'] = {'state': 'switching'}
            started = time.monotonic()
            chain.teardown(ctx, failed=False)
            assert finished.wait(1), 'chat did not consume the authenticated cancellation terminal'
            event = app.chat.events({'request_id': request['request_id'], 'after': 0})
            assert cancel_before_stop == [True], 'Stop closed the listener before dispatching chat cancel'
            assert event['done'] and event['terminal'] == 'cancelled', event
            assert event['error'] == ('chat_switching_runs' if rollover else 'chat_cancelled')
            assert len(event['tokens']) == 13
            assert not failures and not ctx.result.get('cleanup_unproven')
            assert target.announcements == ['stopped']
            assert target.actions == ['/api/control/stop']
            assert t.session.stop_response['ok'] is True
            assert not t.sender._closed.is_set() and not t.worker.release.is_set()
            assert not any(thread.is_alive() for thread in t.router._worker_pool._threads)
            assert time.monotonic() - started < 4
            print('HTTP streaming Stop seconds:', round(time.monotonic() - started, 3), 'rollover:', rollover)
        finally:
            target.proof_may_return.set()
            target.runtime_stopped.clear()
            app.chat.cancel_all()
            t.router.close()
            server.shutdown()
            server.server_close()
            listener.join(3)
            assert not listener.is_alive()
            assert finished.wait(3)
