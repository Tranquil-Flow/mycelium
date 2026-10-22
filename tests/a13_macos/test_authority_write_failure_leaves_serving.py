# SPDX-License-Identifier: AGPL-3.0-only
"""An authority state write failure must end ``serving`` everywhere, not only in a side file.

Measured on the live demo (2026-10-21): the owner node's control pump could not persist a
256 KiB-plus ``snapshot`` response (``authority_state_write_failed``), its control loop died
and ``runtime-control-failure.private.json`` said so, yet ``/api/status`` kept ``serving`` with
``route_ready`` true because nothing on the owner read the local node's ``blocked`` state:
only ``peer_liveness.last_seen_seconds_ago`` grew and chats ran into ``request_cleanup_unproven``.

Three layers, three tests:

* the node's control loop (``ManagedTargetRuntime``) goes terminal with the failure code;
* the member agent that reports the node (``DemoTargetAgent``) reports ``error`` with that code
  once the run is serving (before serving the existing wait behaviour is unchanged);
* the owner chain monitor turns that report into a failed run (``route_ready`` false, the same
  code) and ends in-flight chats with it.

FAKE (labelled): the pump, worker and exchange doubles; ``ChatService``, ``OwnerRun``,
``AppDemoChain``, ``DemoTargetAgent`` and ``ManagedTargetRuntime._serve`` are the shipped code.
"""
from __future__ import annotations

import threading
import time

import pytest

from mycelium_a13_macos import authority
from mycelium_a13_macos.authority import DemoAuthorityError, _atomic_private_json
from mycelium_a13_macos.chat import ChatError, ChatService
from mycelium_a13_macos.demo_target import DemoTargetAgent
from mycelium_a13_macos.managed_runtime import ManagedTargetRuntime
from mycelium_a13_macos.run_exchange import MEMBER_REPORT

from test_demo_chain import LOCAL, REMOTE, Members, make, wait
from test_demo_target import App, Gate, announcement, offer

CODE = 'authority_state_write_failed'


# -- the cap itself ----------------------------------------------------------------------------

def test_a_document_over_the_claim_row_cap_is_refused_by_name(tmp_path):
    root = tmp_path / 'state'
    root.mkdir(mode=0o700)
    pending = {'native_response': 'x' * authority._MAX_PRIVATE_JSON_BYTES}
    with pytest.raises(DemoAuthorityError) as refused:
        _atomic_private_json(root / 'state.json', {'pending': pending})
    assert refused.value.code == CODE
    # A bounded snapshot (<= 64 KiB response, see tests/a10_deterministic/test_snapshot_bound.py)
    # persists with room to spare.
    _atomic_private_json(root / 'state.json', {'pending': {'native_response': 'x' * 70 * 1024}})


# -- the node ----------------------------------------------------------------------------------

class _Pump:
    def __init__(self):
        self.closed = threading.Event()
        self.steps = 0

    def step(self):
        self.steps += 1
        raise DemoAuthorityError(CODE)

    def close(self):
        self.closed.set()


def test_the_node_goes_terminal_with_the_authority_failure_code(tmp_path):
    runtime = object.__new__(ManagedTargetRuntime)
    runtime.root = tmp_path
    tmp_path.chmod(0o700)
    runtime.run_id = 'run-1'
    runtime._lock = threading.RLock()
    runtime._stop = threading.Event()
    runtime._state = 'running'
    runtime._error = None
    runtime._adaptive = None
    runtime._outbound_retry_backoff = .1
    runtime._replacement_worker = None
    runtime._withdrawn = False
    runtime._authorize_load = object()
    runtime._member = lambda: None
    runtime._replace_worker_if_dead = lambda: None
    runtime.pump = _Pump()
    runtime.worker = type('W', (), {'status': lambda self: {'running': True, 'model_load_authorized': True}})()

    runtime._serve()

    status = runtime.status()
    assert (status['state'], status['error_code'], status['route_ready']) == ('blocked', CODE, False)
    assert runtime.pump.closed.is_set() and runtime.pump.steps == 1
    assert runtime._stop.is_set()  # the loop is over; nothing keeps serving


# -- the member agent --------------------------------------------------------------------------

def _agent(tmp_path, monkeypatch):
    (tmp_path / 'target').mkdir()
    app = App(tmp_path)
    state = {'a': None, 'reports': []}
    monkeypatch.setattr('mycelium_a13_macos.installation_descriptor.installation_descriptor',
                        lambda app, **k: {'statement': {'target_node_id': 'node-b'}, 'signed': True})
    agent = DemoTargetAgent(app, consent=Gate(), source=lambda: state['a'],
                            sink=state['reports'].append, report_interval=0)
    return app, agent, state


def test_a_blocked_node_while_serving_is_reported_as_an_error_with_its_code(tmp_path, monkeypatch):
    app, agent, state = _agent(tmp_path, monkeypatch)
    state['a'] = announcement('collect'); agent.tick()
    state['a'] = announcement('serving')
    app.authority.native = {'state': 'running', 'model_load_authorized': True, 'work_admitted': True}
    assert agent.tick()['state'] == 'serving'

    app.authority.native = {'state': 'blocked', 'error_code': CODE,
                            'model_load_authorized': True, 'work_admitted': True}
    status = agent.tick()

    assert (status['state'], status['detail']) == ('error', CODE)
    assert (state['reports'][-1]['state'], state['reports'][-1]['detail']) == ('error', CODE)


def test_a_blocked_node_before_serving_keeps_waiting(tmp_path, monkeypatch):
    """Unchanged: a stale member can still be replaced while the route is built."""
    app, agent, state = _agent(tmp_path, monkeypatch)
    state['a'] = announcement('collect'); agent.tick()
    state['a'] = announcement('work', work_limits={'max_rss_bytes': 1, 'max_disk_bytes': 2,
                                                   'max_wall_seconds': 600, 'max_commands': 5000})
    app.authority.native = {'state': 'blocked', 'error_code': 'runtime_member_stale',
                            'model_load_authorized': False, 'work_admitted': True}
    status = agent.tick()
    assert (status['state'], status['detail']) == ('reported', 'runtime_member_stale')


# -- the owner ---------------------------------------------------------------------------------

class _BreakableMembers(Members):
    """Members that can flip one node to an ``error`` report after the route serves."""
    broken = None  # (node, code)

    def tick(self):
        super().tick()
        if self.broken is None:
            return
        node, code = self.broken
        a = self.exchange.announcement(node)
        if a['phase'] != 'serving':
            return
        self.exchange.submit_local(node, {'protocol': MEMBER_REPORT, 'run_id': a['run_id'],
            'deployment_id': a['deployment_id'], 'state': 'error', 'detail': code, 'model': None,
            'progress': None, 'descriptor': {'statement': {'target_node_id': node}},
            'issued_at': time.time()})


class _Client:
    """A registry client whose generation never ends by itself; only a cancel ends it."""
    def __init__(self, origin):
        self.cancelled = threading.Event()
        self.started = threading.Event()

    def stream(self, prompt, max_new_tokens, on_token):
        on_token('Ber'); self.started.set()
        if self.cancelled.wait(10):
            return {'terminal': 'cancelled'}
        return {'terminal': 'completed'}

    def cancel(self):
        self.cancelled.set(); return True

    def request_cancel(self):
        self.cancelled.set()


def _serving_world(tmp_path):
    app, ops, members, run = make(tmp_path)
    broken = {}
    chain = run.chain
    original = chain._agent_factory
    def factory(app_, local, exchange):
        members_ = _BreakableMembers(app_, exchange, [LOCAL, REMOTE])
        members_.broken = None
        broken['m'] = members_
        return members_
    chain._agent_factory = factory
    app.owner_run = run
    clients = []
    def client_factory(origin):
        clients.append(_Client(origin)); return clients[-1]
    app.chat = ChatService(app, client_factory=client_factory)
    run.start()
    status = wait(run, lambda s: s['state'] in {'serving', 'failed'})
    assert status['state'] == 'serving', status
    return app, ops, run, broken['m'], clients


@pytest.mark.parametrize('node', [LOCAL, REMOTE])
def test_owner_leaves_serving_and_ends_in_flight_chats_when_a_node_dies(tmp_path, node):
    app, ops, run, members, clients = _serving_world(tmp_path)
    sent = app.chat.send('client-1', {'prompt': 'hello', 'max_new_tokens': 4})
    assert clients[0].started.wait(5)
    assert run.status()['route_ready'] is True

    members.broken = (node, CODE)
    status = wait(run, lambda s: s['state'] in {'failing', 'failed'})

    assert status['error_code'] == CODE and status['failed_step'] == 'serving'
    assert status['route_ready'] is False
    status = wait(run, lambda s: s['state'] == 'failed')
    assert status['route_ready'] is False and ops.calls[-1] == ('stop',)
    # The in-flight request ended cleanly: a terminal, with the failure code, not a hang.
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        events = app.chat.events({'request_id': sent['request_id'], 'after': 0})
        if events['done']:
            break
        time.sleep(0.01)
    assert events['done'] is True
    assert (events['terminal'], events['error']) == ('cancelled', CODE)
    # And nothing new is accepted.
    with pytest.raises(ChatError) as refused:
        app.chat.send('client-1', {'prompt': 'again'})
    assert refused.value.code == 'chat_not_ready'


def test_a_healthy_serving_run_is_untouched_by_the_monitor(tmp_path):
    app, ops, run, members, clients = _serving_world(tmp_path)
    for _ in range(20):
        status = run.status()
        time.sleep(0.01)
    assert (status['state'], status['route_ready'], status['error_code']) == ('serving', True, None)
    run.stop(); wait(run, lambda s: s['state'] == 'stopped')
