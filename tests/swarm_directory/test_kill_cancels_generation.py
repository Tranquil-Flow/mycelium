# SPDX-License-Identifier: AGPL-3.0-only
"""The owner kill switch also stops the backend generation, not only the browser's view of it.

FAKE (labelled): ``CancellableChat`` is a duck-typed ChatService stand-in with the new ``cancel``/``cancel_all``
surface; the guard, ledger, gateway and publisher are the shipped code.
"""
import json

from mycelium_a13_macos.swarm_publish import action
from mycelium_swarm_directory import docs

from _swarm_fixtures import FakeApp
from test_entry_guard import SWARM, FakeChat, send, world  # noqa: F401  (fixture re-export)
from test_open_guard_rails import PREFIX, StubDirectory, kill_doc, make_open, post, visitor  # noqa: F401


class CancellableChat(FakeChat):
    """FAKE: one generation that streams a token and keeps running until cancelled."""

    def __init__(self):
        super().__init__()
        self.cancelled = []
        self.running = True

    def events(self, body):
        if not self.running:
            return {'tokens': [], 'next': 1, 'done': True, 'error': 'entry_killed_by_owner', 'terminal': 'cancelled',
                    'served_by': [], 'seconds': 1.0}
        return {'tokens': ['Ber'][body['after']:], 'next': 1, 'done': False, 'error': None, 'served_by': [],
                'seconds': None}

    def cancel(self, request_id, code='chat_cancelled'):
        self.cancelled.append((request_id, code))
        self.running = False
        return True

    def cancel_all(self, code='chat_cancelled'):
        self.cancelled.append((None, code))
        self.running = False
        return 1


def test_engaging_the_kill_switch_cancels_the_running_generation(world):
    make_open(world)
    chat = CancellableChat()
    world['gateway'].chat = lambda: chat
    key, grant, ip = visitor(world, 1)
    status, accepted = send(world, key, grant, client=ip)
    assert status == 200
    assert post(world, 'kill', kill_doc(world), ip)[0] == 200
    assert chat.cancelled == [(None, 'entry_killed_by_owner')]
    assert chat.running is False


def test_releasing_the_kill_switch_cancels_nothing(world):
    make_open(world)
    chat = CancellableChat()
    world['gateway'].chat = lambda: chat
    assert post(world, 'kill', kill_doc(world, engaged=False, seq=1))[0] == 200
    assert chat.cancelled == []


def test_the_first_poll_after_a_kill_cancels_that_request_even_if_the_bulk_cancel_missed_it(world):
    make_open(world)
    chat = CancellableChat()
    world['gateway'].chat = lambda: chat
    key, grant, ip = visitor(world, 1)
    status, accepted = send(world, key, grant, client=ip)
    world['gateway'].guard.on_kill = None  # model a cancel_all that raced and missed this request
    post(world, 'kill', kill_doc(world), ip)
    page = post(world, 'events', {'request_id': accepted['request_id'], 'after': 1}, ip)[1]
    assert page['done'] is True and page['killed'] is True and page['error'] == 'entry_killed_by_owner'
    assert chat.cancelled == [(accepted['request_id'], 'entry_killed_by_owner')]


def test_a_chat_backend_without_cancel_still_gets_the_kill_applied(world):
    make_open(world)
    key, grant, ip = visitor(world, 1)
    assert send(world, key, grant, client=ip)[0] == 200  # plain FakeChat: no cancel surface
    assert post(world, 'kill', kill_doc(world), ip)[0] == 200
    assert post(world, 'state', {})[1]['killed'] is True


def test_a_cancel_failure_never_blocks_the_kill(world):
    make_open(world)

    class Broken(CancellableChat):
        def cancel_all(self, code='chat_cancelled'):
            raise OSError('gateway unreachable')

    world['gateway'].chat = lambda: Broken()
    assert post(world, 'kill', kill_doc(world))[0] == 200
    assert post(world, 'state', {})[1]['killed'] is True


def test_the_owner_app_kill_action_reaches_the_backend_cancel(tmp_path):
    from mycelium_a13_macos.swarm_publish import SwarmPublisher
    app = FakeApp(tmp_path, 'http://127.0.0.1:9')
    clock = type('C', (), {'t': 1_800_000_000, '__call__': lambda self: self.t})()
    chat = CancellableChat()
    publisher = SwarmPublisher(app, origin=lambda: 'https://quiet-river.trycloudflare.com', client_factory=StubDirectory,
                               clock=clock, chat=lambda: chat)
    app.swarm_publisher = publisher
    try:
        publisher.publish({'directory': 'https://mycelium.now', 'name': 'Open demo', 'access_mode': 'open'})
        assert action(app, '/api/swarm/kill', {'engaged': True})['killed'] is True
        assert chat.cancelled == [(None, 'entry_killed_by_owner')]
        action(app, '/api/swarm/kill', {'engaged': False})
        assert chat.cancelled == [(None, 'entry_killed_by_owner')]
    finally:
        publisher.close()
