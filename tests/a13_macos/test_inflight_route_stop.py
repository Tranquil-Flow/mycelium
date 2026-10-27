# SPDX-License-Identifier: AGPL-3.0-only
"""Desk lifecycle composition, not model/device qualification.

Real router, physical route, managed owner/session, TLS mailbox, concurrent
pump and signature verification. Only the native worker and startup
qualification are controlled doubles. A bind executes, but its response is
withheld until the test releases it; cancellation still returns signed proof.
"""
from contextlib import contextmanager
from dataclasses import replace
import sys
import threading
import time
import traceback
from types import SimpleNamespace

import pytest

from mycelium_a13_macos.concurrent_pump import ConcurrentTargetControlPump
from mycelium_a13_macos.control_session import ManagedControlOwner
from mycelium_a13_macos.managed_control import CONTROL_COMMAND_PROTOCOL
from mycelium_live import route as route_module
from mycelium_qualification.signing import generate_ed25519_signer
from mycelium_request_gateway.contracts import InferenceSubmission, qualification_binding
from physical_inference_qualification import QualificationController
from test_managed_control import _world, RUN_ID
from tests.live.test_a6_live_router_recovery import _runtime
from tests.live.test_a6_m16_recovery import _graph
from tests.live.token_path_test_support import ScriptedTokenOwner
from tests.request_gateway.test_core import _synthetic_qualification


class HeldWorker:
    """Signed fixture observations of controlled state, never native evidence."""
    def __init__(self, node_id, deployment_id, *, held_command='bind_request_control'):
        self.node_id, self.run_id, self.deployment_id = node_id, RUN_ID, deployment_id
        self.held_command = held_command
        self.signer = generate_ed25519_signer(endpoint_id='desk-worker')
        self.entered = threading.Event()
        self.release = threading.Event()
        self.cancelled = threading.Event()
        self.stopped = threading.Event()
        self.commands = []
        self.clean = True
        self.tamper = False

    def send(self, *, command_id, command, payload):
        self.commands.append(command)
        streaming = command == 'infer_start' and self.held_command == 'streaming'
        if streaming:
            self.token_owner.publish(command_id, tokens=list(range(101, 114)), status='DECODING')
        if command == self.held_command or streaming:
            self.entered.set()
            assert self.release.wait(15), 'test did not release withheld reply'
        if command == 'bind_request_control':
            event, details = 'request_control_bound', {}
        elif command == 'infer_start':
            event = 'inference_started'
            details = self.token_owner.publish(command_id, tokens=[101, 102, 103, 104], status='COMPLETED')
        elif command == 'snapshot' and payload == {}:
            event, details = 'snapshot', {}
        elif command == 'infer_cancel_wait':
            self.cancelled.set()
            event = 'inference_cancelled'
            details = {'request_cleanup': {
                **{k: v for k, v in payload.items() if k != 'deadline_budget_ms'},
                'runtime_clean': self.clean, 'transport_clean': self.clean,
                'cancellation_worker_complete': self.clean, 'complete': self.clean}}
        elif command == 'snapshot' and 'cleanup_subject' in payload:
            event = 'snapshot'
            details = {'request_cleanup': {**payload['cleanup_subject'],
                                           'complete': self.clean}}
        elif command == 'stop':
            self.stopped.set()
            event, details = 'stopping', {}
        else:
            raise AssertionError('unexpected native command: ' + command)
        observation = dict(protocol='mycelium.physical_node_observation.v1', event=event,
            monotonic_ns=time.monotonic_ns(), run_id=self.run_id,
            deployment_id=self.deployment_id, node_id=self.node_id,
            host_id='desk-host', process_id=123, endpoint_id=self.signer.endpoint_id,
            peer_generation=1, state='STARTED', route_ready=False, details=details)
        signature = self.signer.sign(observation)
        if self.tamper:
            observation['monotonic_ns'] += 1
        return dict(protocol=CONTROL_COMMAND_PROTOCOL, command_id=command_id,
            node_id=self.node_id, ok=True, route_ready=False,
            result=dict(observation=observation, signature=signature,
                        verification_key=self.signer.public_key_record()))

    def close(self):
        self.release.set()


@contextmanager
def stack(tmp_path, monkeypatch, *, held_command='bind_request_control', auto_submit=True,
          progress_factory=None, restarting_owner=False):
    with _world(tmp_path / 'swarm') as world:
        node = world['member']['node_id']
        qualification = _synthetic_qualification()
        worker = HeldWorker(node, qualification.deployment_id, held_command=held_command)
        graph = _graph()
        graph = replace(graph,
            stages=tuple(replace(s, placements=(replace(s.placements[0], node_id=node),))
                         for s in graph.stages),
            edges=graph.edges[:1], loopback_edges=graph.loopback_edges[:1])
        monkeypatch.setattr(route_module, 'execution_graph_from_document', lambda _: graph)
        peer = SimpleNamespace(node_id=node, process_transport='managed_https', host_id='desk-host')
        controller = SimpleNamespace(peers=(peer,),
            _verified_observation=QualificationController._verified_observation)
        # Bind the production verifier, with its real key/identity/signature checks.
        controller._verified_observation = lambda *a, **kw: QualificationController._verified_observation(controller, *a, **kw)
        route = route_module.PhysicalLiveRoute(token_path_owner=ScriptedTokenOwner(),
            controller=controller, endpoints={node: {'endpoint_id': worker.signer.endpoint_id}},
            run_plan={'run_id': RUN_ID, 'deployment_id': qualification.deployment_id,
                      'request': {}, 'nodes': [{'node_id': node, 'configure': {'graph': {}}}]})
        worker.token_owner = route._token_path_owner
        route._open = True
        route._deployment_qualification = qualification
        route._identities[node] = {'process_id': 123}
        route._verification_keys[node] = worker.signer.public_key_record()
        sender = ManagedControlOwner(tmp_path / 'sender', client=world['owner_client'],
            current_member=lambda n: world['authority'].coordinator.member(n))
        session = sender.session(node_id=node, run_id=RUN_ID,
                                 deployment_id=qualification.deployment_id, timeout_seconds=10)
        route._sessions[node] = session
        owner = route
        if restarting_owner:
            from mycelium_live.supervisor import ColdRouteRestartOwner
            def unexpected_restart(*args):
                raise AssertionError('cancel_must_not_restart_the_route')
            owner = ColdRouteRestartOwner(route_factory=unexpected_restart,
                open_and_qualify=unexpected_restart)
            owner._current, owner._generation = route, 1
            owner._qualification, owner._state = qualification, 'ready'
        coordinator, router, service, qualification = _runtime(
            tmp_path, owner, base_graph=graph, qualification=qualification)
        route._graph = router.current_deployment()
        cursor = None if progress_factory is None else progress_factory(world, route, worker)
        pump = ConcurrentTargetControlPump(tmp_path / 'pump', client=world['target_client'],
            worker=worker, current_member=lambda: world['authority'].coordinator.member(node),
            token_progress_cursor=cursor)
        if progress_factory is None:
            # Legacy tests bypass progress transport explicitly. A progress
            # factory opts into the real target bridge and owner TLS mailbox.
            monkeypatch.setattr(pump._token_progress, 'open', lambda command: None)
        stop = threading.Event()
        errors = []
        def pumping():
            try:
                while not stop.is_set():
                    pump.step()
                    stop.wait(.005)
            except BaseException as error:
                errors.append(error)
        thread = threading.Thread(target=pumping, name='desk-target-pump')
        thread.start()
        try:
            request_id = None
            if auto_submit:
                request_id = service.submit(InferenceSubmission(prompt='live recovery prompt',
                    max_new_tokens=256 if held_command == 'streaming' else 4, qualification=qualification_binding(qualification)))
                assert worker.entered.wait(3), (errors, route._incidents if hasattr(route, '_incidents') else '')
            yield SimpleNamespace(worker=worker, route=route, router=router, service=service,
                sender=sender, session=session, request_id=request_id, errors=errors,
                qualification=qualification, pump=pump, world=world)
        finally:
            worker.clean, worker.tamper = True, False
            worker.release.set()
            service.close()
            route.close()
            sender.close()
            stop.set()
            thread.join(5)
            pump.close()
            assert not thread.is_alive()
            assert not errors


@pytest.mark.parametrize('held_command', ['bind_request_control', 'infer_start', 'snapshot', 'streaming'])
def test_stop_retires_withheld_waiter_after_authenticated_cleanup(tmp_path, monkeypatch, held_command):
    with stack(tmp_path, monkeypatch, held_command=held_command) as t:
        started = time.monotonic()
        try:
            t.router.close()
        except RuntimeError:
            active = t.route._active_route_requests.get(t.request_id, {})
            print('worker operations:', t.worker.commands)
            print('cancellation error:', active.get('cancellation_fanout_error'))
            print('reconciliation error:', active.get('cleanup_reconciliation_error'))
            print('signed events:', [r['observation']['event'] for r in t.route._signed_observations])
            frames = sys._current_frames()
            for thread in t.router._worker_pool._threads:
                if thread.ident in frames:
                    traceback.print_stack(frames[thread.ident])
            raise
        assert time.monotonic() - started < 4
        assert t.worker.cancelled.is_set()
        assert not t.worker.release.is_set(), 'success must not depend on the withheld ordinary reply'
        assert not t.sender._closed.is_set(), 'owner channel must survive for target stop/proof'
        assert not any(thread.is_alive() for thread in t.router._worker_pool._threads)
        assert any(row['observation']['event'] == 'inference_cancelled'
                   for row in t.route._signed_observations)
        if held_command == 'bind_request_control':
            assert 'infer_start' not in t.worker.commands
        t.route.close()
        assert t.session.stop_response['ok'] is True
        assert t.worker.stopped.is_set()


def test_cancelled_request_cannot_enqueue_another_bind(tmp_path, monkeypatch):
    with stack(tmp_path, monkeypatch) as t:
        assert t.router.cancel(t.request_id)
        active = t.route._active_route_requests[t.request_id]
        assert active['cancellation_requested'] is True
        # Model the gap between the first bind returning and the next being
        # registered. It must not escape through the unrelated in-flight guard.
        t.route._active_route_requests['already-cancelled'] = {
            **active, 'inflight_commands': {}}
        def forbidden_send(**kwargs):
            pytest.fail('cancelled request reached the native send boundary')
        try:
            with monkeypatch.context() as scoped:
                scoped.setattr(t.session, 'send', forbidden_send)
                with pytest.raises(route_module.InferenceCancelled):
                    t.route._send_request_command(request_id='already-cancelled',
                        node_id=t.session.node_id, command_id='late-bind',
                        command='bind_request_control', payload={'request_id': 'already-cancelled', 'control': {}})
            assert 'late-bind' not in t.sender._state['commands']
        finally:
            t.route._active_route_requests.pop('already-cancelled', None)


@pytest.mark.parametrize('proof', ['incomplete', 'bad-signature'])
def test_unproven_cleanup_does_not_retire_bind_or_publish_success(tmp_path, monkeypatch, proof):
    with stack(tmp_path, monkeypatch) as t:
        t.worker.clean = proof != 'incomplete'
        t.worker.tamper = proof == 'bad-signature'
        assert t.router.cancel(t.request_id)
        active = t.route._active_route_requests[t.request_id]
        assert active['cancellation_fanout_complete'].wait(3)
        assert t.worker.cancelled.is_set()
        assert t.route.request_cleanup_receipt(t.request_id) is None
        assert not active['cleanup_receipt_published']
        with t.session._lock:
            waiters = [t.session._waiters[c] for c in active['inflight_commands'].values()]
        assert waiters and all(not waiter.is_set() for waiter in waiters)
        if proof == 'bad-signature':
            assert active['cancellation_fanout_error'] == 'node_observation_signature_invalid'
        else:
            assert any(r['observation']['event'] == 'inference_cancelled'
                       for r in t.route._signed_observations)


@pytest.mark.parametrize('rollover', [False, True])
def test_chat_stop_terminal_precedes_channel_release(tmp_path, monkeypatch, rollover):
    from mycelium_a13_macos.chat import ChatService
    with stack(tmp_path, monkeypatch) as t:
        consuming = threading.Event()
        finished = threading.Event()
        class GatewayClient:
            """In-process bridge to the real gateway; no invented terminal."""
            def __init__(self, origin):
                pass
            def stream(self, prompt, max_new_tokens, on_token):
                subscription = t.service.subscribe(t.request_id, last_event_id=None)
                consuming.set()
                try:
                    while True:
                        event = subscription.next_event(timeout=5)
                        assert event is not None, 'gateway closed without a terminal'
                        subscription.ack(event.sequence)
                        if event.kind in {'completed', 'failed', 'cancelled'}:
                            return {'terminal': event.kind, 'code': event.code}
                finally:
                    subscription.close()
            def cancel(self):
                return t.service.cancel(t.request_id)
        app = SimpleNamespace(owner_run=SimpleNamespace(status=lambda: {
            'state': 'serving', 'run_id': RUN_ID, 'registry_origin': 'http://127.0.0.1:55555'}))
        chat = ChatService(app, client_factory=GatewayClient)
        work = chat._work
        def run_chat(*args):
            try:
                work(*args)
            finally:
                finished.set()
        monkeypatch.setattr(chat, '_work', run_chat)
        request = chat.send('desk-client', {'prompt': 'live recovery prompt', 'max_new_tokens': 4})
        assert consuming.wait(3)
        started = time.monotonic()
        if rollover:
            assert chat.cancel_all('chat_switching_runs') == 1
        t.router.close()
        assert finished.wait(1)
        event = chat.events({'request_id': request['request_id'], 'after': 0})
        assert event['done'] and event['terminal'] == 'cancelled', event
        assert event['error'] == ('chat_switching_runs' if rollover else 'chat_cancelled')
        assert not t.sender._closed.is_set()
        assert not t.worker.release.is_set()
        print('desk stop seconds:', round(time.monotonic() - started, 3), 'rollover:', rollover)
