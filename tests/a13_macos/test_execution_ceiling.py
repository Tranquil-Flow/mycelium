# SPDX-License-Identifier: AGPL-3.0-only
"""PV34: the node execution ceiling (``max_wall_seconds``, 1800 s) ends the owner run cleanly.

Measured on the two-Mac demo (2026-10-21): every native worker expires at its start + ``max_wall_seconds`` minus the
10 s cleanup reserve, even in the middle of a request.  The owner never learned of it: ``/api/status`` stayed
``serving`` with ``route_ready`` true for 9+ minutes (so the keeper never restored the demo) and an in-flight chat
hung until it ended ``request_cleanup_unproven``.

The owner knows the same number (``work_limits.max_wall_seconds``), so it ends the run itself a little before the
workers expire: the run leaves ``serving`` (``route_ready`` false, state ``failed`` with the closed code
``execution_ceiling_reached``), the route is torn down, and every in-flight chat request ends with that explicit code.
"""
import threading
import time

from test_demo_chain import LOCAL, REMOTE, make, wait

from mycelium_a13_macos.chat import ChatError, ChatService

CEILING = 'execution_ceiling_reached'


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def serving_run(tmp_path):
    app, ops, members, run = make(tmp_path)
    clock = Clock()
    # Retain the hard-ceiling fallback regression separately from automatic continuation.
    run.chain.budgets['rollover_margin'] = 0
    run.chain._monotonic = clock
    app.owner_run = run
    run.start()
    assert wait(run, lambda s: s['state'] in {'serving', 'failed'})['state'] == 'serving'
    return app, ops, members, run, clock


def ceiling_seconds(run):
    return run._ctx.state['work_limits']['max_wall_seconds']


def test_the_run_keeps_serving_before_the_ceiling(tmp_path):
    app, ops, members, run, clock = serving_run(tmp_path)
    clock.now += ceiling_seconds(run) - 120
    for _ in range(5):
        status = run.status()
        assert (status['state'], status['route_ready'], status['error_code']) == ('serving', True, None)
        time.sleep(0.01)


def test_at_the_ceiling_the_run_leaves_serving_and_tears_the_route_down(tmp_path):
    app, ops, members, run, clock = serving_run(tmp_path)
    clock.now += ceiling_seconds(run)          # the workers expire 10 s before this
    status = wait(run, lambda s: s['state'] in {'failed', 'stopped'})
    assert (status['state'], status['route_ready'], status['error_code']) == ('failed', False, CEILING)
    assert status['failed_step'] == 'serving'
    assert ops.calls[-1] == ('stop',)


def test_the_run_ends_before_the_workers_expire(tmp_path):
    # Native workers die at start + max_wall - 10 s (the cleanup reserve); the owner must end the run first.
    app, ops, members, run, clock = serving_run(tmp_path)
    clock.now += ceiling_seconds(run) - 10 - 1
    status = wait(run, lambda s: s['state'] in {'failed', 'stopped'})
    assert status['error_code'] == CEILING


def test_the_ceiling_ends_the_run_without_anyone_polling_status(tmp_path):
    app, ops, members, run, clock = serving_run(tmp_path)
    run._ceiling_poll_s = 0.01
    clock.now += ceiling_seconds(run)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and ops.calls[-1] != ('stop',):
        time.sleep(0.01)
    assert ops.calls[-1] == ('stop',)
    assert run._error == CEILING


def test_a_new_run_can_start_after_the_ceiling(tmp_path):
    app, ops, members, run, clock = serving_run(tmp_path)
    clock.now += ceiling_seconds(run)
    wait(run, lambda s: s['state'] == 'failed')
    run.start()
    assert wait(run, lambda s: s['state'] in {'serving', 'failed'})['state'] == 'serving'
    assert run.status()['error_code'] is None


class BlockingClient:
    """A route client mid-stream: it only returns when the route is told to cancel."""

    def __init__(self, origin, *, cancel_works=True):
        self.cancelled = threading.Event()
        self.route_gone = threading.Event()
        self.cancel_works = cancel_works

    def stream(self, prompt, max_new_tokens, on_token, qos_class='interactive'):
        on_token('hel')
        deadline = time.monotonic() + 10
        while not (self.cancelled.is_set() or self.route_gone.is_set()) and time.monotonic() < deadline:
            time.sleep(0.005)
        if self.route_gone.is_set():
            raise ChatError(502, 'chat_http_get_events_502_request_cleanup_unproven')
        return {'terminal': 'cancelled', 'code': None, 'deployment_id': 'd', 'model_id': 'm'}

    def cancel(self):
        if not self.cancel_works:
            raise OSError('route already gone')
        self.cancelled.set()
        return True


def chat_in_flight(app, **client_kwargs):
    chat = ChatService(app, client_factory=lambda origin: BlockingClient(origin, **client_kwargs))
    app.chat = chat
    request_id = chat.send('client-1', {'prompt': 'hi'})['request_id']
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not chat._requests[request_id]['tokens']:
        time.sleep(0.01)
    assert chat._requests[request_id]['tokens']
    return chat, request_id


def test_an_in_flight_chat_ends_with_the_explicit_ceiling_code(tmp_path):
    app, ops, members, run, clock = serving_run(tmp_path)
    chat, request_id = chat_in_flight(app)
    clock.now += ceiling_seconds(run)
    wait(run, lambda s: s['state'] == 'failed')
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not chat._requests[request_id]['done']:
        time.sleep(0.01)
    events = chat.events({'request_id': request_id, 'after': 0})
    assert events['done'] is True
    assert (events['terminal'], events['error']) == ('cancelled', CEILING)


def test_an_in_flight_chat_still_ends_with_the_ceiling_code_when_the_route_is_already_gone(tmp_path):
    # The cancel cannot reach the route (workers already expired): the stream dies, and the answer is still the
    # explicit ceiling code instead of the route's request_cleanup_unproven.
    app, ops, members, run, clock = serving_run(tmp_path)
    chat, request_id = chat_in_flight(app, cancel_works=False)
    clock.now += ceiling_seconds(run)
    client = chat._requests[request_id]['client']
    wait(run, lambda s: s['state'] == 'failed')
    client.route_gone.set()                      # the teardown kills the stream
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not chat._requests[request_id]['done']:
        time.sleep(0.01)
    events = chat.events({'request_id': request_id, 'after': 0})
    assert events['done'] is True
    assert events['error'] == CEILING
