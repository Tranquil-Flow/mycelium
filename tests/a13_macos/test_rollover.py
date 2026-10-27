# SPDX-License-Identifier: AGPL-3.0-only
"""Automatic continuation through real owner/chain code; no native or relay processes."""
import threading
from types import SimpleNamespace

import pytest

from test_demo_chain import make, wait, REMOTE
from test_execution_ceiling import Clock, chat_in_flight
from mycelium_a13_macos.chat import ChatError, ChatService


@pytest.fixture
def live(tmp_path):
    app, ops, members, run = make(tmp_path)
    clock = Clock()
    run._clock = run.chain._monotonic = clock
    run._ceiling_poll_s = .01
    app.owner_run = run
    # Any relay lifecycle call is a test failure, not a no-op.
    def forbidden(*a, **k):
        pytest.fail('rollover touched the relay')
    app.public_relay = app.relay = SimpleNamespace(start=forbidden, stop=forbidden, restart=forbidden,
                                hostname='unchanged.trycloudflare.com')
    run.start()
    assert wait(run, lambda s: s['state'] in {'serving', 'failed'})['state'] == 'serving'
    try:
        yield app, ops, members, run, clock
    finally:
        run.stop()
        run.join(5)
        assert not run._thread.is_alive()


def due(run, clock):
    old = run._ctx
    clock.now = old.state['execution_clock_started_at'] + old.state['work_limits']['max_wall_seconds'] - 120
    run._end_at_ceiling(old)
    return old


def test_rolls_at_limit_minus_margin_once_and_never_restarts_relay(live):
    app, ops, members, run, clock = live
    first = run._ctx
    clock.now = first.state['execution_clock_started_at'] + 1800 - 120 - .01
    run._end_at_ceiling(first)
    assert run.status()['state'] == 'serving'
    old = due(run, clock)
    for _ in range(8):
        run._end_at_ceiling(old)
    status = wait(run, lambda s: s['run_id'] != old.run_id and s['state'] in {'serving', 'failed'})
    assert status['state'] == 'serving', status
    assert status['reason'] == 'rollover'
    assert status['rollover']['completed'] == 1
    assert [c[0] for c in ops.calls].count('stop') == 1
    assert app.relay.hostname == 'unchanged.trycloudflare.com'
    assert run._ctx.deployment_id != old.deployment_id
    assert run._ctx.state['work_limits'] == old.state['work_limits']
    assert clock.now < old.state['execution_clock_started_at'] + 1800


def test_stop_during_switch_cancels_continuation(live):
    app, ops, members, run, clock = live
    entered, release = threading.Event(), threading.Event()
    stop = ops.stop
    def held(app):
        entered.set()
        assert release.wait(5)
        stop(app)
    ops.stop = held
    old = due(run, clock)
    try:
        assert entered.wait(5)
        assert run.status()['rollover']['state'] == 'switching'
        run.stop()
    finally:
        release.set()
    assert wait(run, lambda s: s['state'] == 'stopped')['run_id'] == old.run_id


def test_switch_chat_is_explicit_and_retryable(live):
    app, ops, members, run, clock = live
    entered, release = threading.Event(), threading.Event()
    stop = ops.stop
    def held(app):
        entered.set()
        assert release.wait(5)
        stop(app)
    ops.stop = held
    due(run, clock)
    try:
        assert entered.wait(5)
        with pytest.raises(ChatError) as caught:
            ChatService(app).send('visitor', {'prompt': 'hello'})
        assert caught.value.code == 'chat_switching_runs'
        assert caught.value.status == 503
    finally:
        release.set()


def test_missing_members_halts_instead_of_restarting(live):
    app, ops, members, run, clock = live
    app.authority.coordinator.nodes.remove(REMOTE)
    old = due(run, clock)
    status = wait(run, lambda s: s['state'] == 'failed')
    assert status['error_code'] == 'rollover_members_changed'
    assert [c[0] for c in ops.calls].count('compile') == 1


def test_failed_rollovers_back_off_and_give_up(live):
    app, ops, members, run, clock = live
    ops.compile_error = 'chain_compile_failed'
    due(run, clock)
    status = wait(run, lambda s: s.get('rollover', {}).get('state') == 'backoff')
    assert status['rollover']['consecutive_failures'] == 1
    for failure in (1, 2):
        deadline = status['rollover']['retry_at']
        attempts = [c[0] for c in ops.calls].count('compile')
        clock.now = deadline - .01
        run._end_at_ceiling(run._ctx)
        assert [c[0] for c in ops.calls].count('compile') == attempts
        clock.now = deadline
        status = wait(run, lambda s: s['state'] == 'failed' or (
            s.get('rollover', {}).get('state') == 'backoff'
            and s['rollover']['consecutive_failures'] > failure))
    assert status['state'] == 'failed'
    assert status['error_code'] == 'rollover_failed'
    assert status['rollover']['consecutive_failures'] == 3
    assert [c[0] for c in ops.calls].count('compile') == 4


def test_stop_in_backoff_never_retries(live):
    app, ops, members, run, clock = live
    ops.compile_error = 'chain_compile_failed'
    due(run, clock)
    wait(run, lambda s: s.get('rollover', {}).get('state') == 'backoff')
    count = len(ops.calls)
    run.stop()
    clock.now += 1000
    run.join(5)
    assert run.status()['state'] == 'stopped'
    assert len(ops.calls) == count


def test_scheduler_runs_without_status_polling(live):
    app, ops, members, run, clock = live
    served = threading.Event()
    activate = ops.activate
    def activated(app, proposal):
        activate(app, proposal)
        served.set()
    ops.activate = activated
    old = run._ctx
    clock.now = old.state['execution_clock_started_at'] + 1680
    assert served.wait(5)  # no status() / manual scheduler call
    assert run._ctx.run_id != old.run_id


@pytest.mark.parametrize('change', ['generation', 'incarnation', 'verification_key_digest', 'model'])
def test_changed_binding_stops_automatic_continuation(live, change):
    app, ops, members, run, clock = live
    if change == 'model':
        app.acquisition.prepared_source().manifest_digest = 'sha256:' + '9' * 64
    else:
        member = app.authority.coordinator.member
        app.authority.coordinator.member = lambda n: {**member(n), **({change: 'changed'} if n == REMOTE else {})}
    due(run, clock)
    status = wait(run, lambda s: s['state'] == 'failed')
    assert status['error_code'] == ('rollover_model_changed' if change == 'model' else 'rollover_members_changed')
    assert [c[0] for c in ops.calls].count('compile') == 1


def test_earliest_member_limit_and_configurable_margin(live):
    app, ops, members, run, clock = live
    ctx = run._ctx
    run.chain.budgets['rollover_margin'] = 45
    ctx.state['work_nodes'] = {REMOTE: {'work_limits': {'max_wall_seconds': 120}}}
    clock.now = ctx.state['execution_clock_started_at'] + 74.99
    assert not run.chain.rollover_due(ctx)
    clock.now += .01
    assert run.chain.rollover_due(ctx)
    assert ctx.state['work_limits']['max_wall_seconds'] == 1800
    assert ctx.state['work_nodes'][REMOTE]['work_limits']['max_wall_seconds'] == 120


def test_inflight_chat_is_cancelled_explicitly_before_next_run(live):
    app, ops, members, run, clock = live
    chat, request_id = chat_in_flight(app)
    old = due(run, clock)
    wait(run, lambda s: s['state'] == 'serving' and s['run_id'] != old.run_id)
    # The stream fake is event-driven and completes as soon as cancellation arrives.
    import time
    until = time.monotonic() + 5
    while not chat.events({'request_id': request_id, 'after': 0})['done'] and time.monotonic() < until:
        threading.Event().wait(.005)
    events = chat.events({'request_id': request_id, 'after': 0})
    assert (events['done'], events['terminal'], events['error']) == (True, 'cancelled', 'chat_switching_runs')


def test_chat_switch_between_admission_and_enqueue_is_explicit(live, monkeypatch):
    app, ops, members, run, clock = live
    calls = []
    def client(origin):
        calls.append(origin)
        raise RuntimeError('retired_route_contacted')
    chat = ChatService(app, client_factory=client)
    app.chat = chat
    serving = chat._serving()
    def switch_before_enqueue():
        old = due(run, clock)
        wait(run, lambda s: s['state'] == 'serving' and s['run_id'] != old.run_id)
        return serving
    monkeypatch.setattr(chat, '_serving', switch_before_enqueue)
    rid = chat.send('visitor', {'prompt': 'hello'})['request_id']
    import time
    deadline = time.monotonic() + 5
    while not chat.events({'request_id': rid, 'after': 0})['done'] and time.monotonic() < deadline:
        threading.Event().wait(.005)
    result = chat.events({'request_id': rid, 'after': 0})
    assert result['done'] and result['error'] == 'chat_switching_runs'
    assert calls == []


def test_rollover_discards_old_weight_copies_but_keeps_one_cache(live):
    app, ops, members, run, clock = live
    old = run._ctx
    root = app.root / 'chain-runs' / old.run_id
    cache_root = root / 'compiled/transfer-bundle/deployment'
    cache_root.mkdir(parents=True)
    (cache_root / 'model.safetensors').write_bytes(b'fixture')
    stage = root / 'stage-shards/model'
    stage.mkdir(parents=True)
    (stage / 'weights').write_bytes(b'fixture')
    app._chain_compilation_cache = {'entry': {'root': str(cache_root)}}
    due(run, clock)
    wait(run, lambda s: s['state'] == 'serving' and s['run_id'] != old.run_id)
    assert cache_root.exists()
    assert not (root / 'stage-shards').exists()
    next_ctx = run._ctx
    next_root = app.root / 'chain-runs' / next_ctx.run_id
    (next_root / 'compiled').mkdir(parents=True)
    (next_root / 'compiled/weights').write_bytes(b'fixture')
    due(run, clock)
    wait(run, lambda s: s['state'] == 'serving' and s['run_id'] != next_ctx.run_id)
    assert not (next_root / 'compiled').exists()
    assert cache_root.exists()


def test_fresh_worker_budget_does_not_extend_retired_workers(tmp_path, monkeypatch):
    from test_worker import worker
    from mycelium_a13_macos import worker as module
    clock = Clock()
    monkeypatch.setattr(module, 'time', SimpleNamespace(monotonic=clock))
    (tmp_path / 'old').mkdir()
    (tmp_path / 'new').mkdir()
    old, new = worker(tmp_path / 'old'), worker(tmp_path / 'new')
    try:
        old._started_at = clock.now
        old._check()
        clock.now += old.limits.max_wall_seconds
        with pytest.raises(module.WorkerError, match='worker_wall_limit_exceeded'):
            old._check()
        new._started_at = clock.now
        new._check()
        # Starting another run never moves the old worker's authority deadline.
        with pytest.raises(module.WorkerError, match='worker_wall_limit_exceeded'):
            old._check()
        assert new.limits.max_wall_seconds == old.limits.max_wall_seconds
    finally:
        old.close()
        new.close()


def test_unproven_teardown_never_starts_another_model(live):
    app, ops, members, run, clock = live
    def broken(app):
        raise ValueError('rebuild_stop_timeout')
    ops.stop = broken
    due(run, clock)
    status = wait(run, lambda s: s['state'] == 'failed')
    assert status['error_code'] == 'rollover_cleanup_unproven'
    assert [c[0] for c in ops.calls].count('compile') == 1
