"""Owner one-click Start/Stop/Rebuild: deterministic state machine over a fake chain."""
import threading
import time

import pytest

from mycelium_a13_macos.owner_run import OwnerRun, OwnerRunError, STEP_NAMES


class FakeChain:
    """Every step blocks on its own gate so the test drives progress exactly."""
    def __init__(self, *, fail_at=None, fail_code='chain_compile_failed'):
        self.calls = []
        self.gates = {name: threading.Event() for name in STEP_NAMES}
        self.fail_at = fail_at
        self.fail_code = fail_code
        self.teardowns = []
        self.rebuild_flags = []

    def release(self, *names):
        for name in names: self.gates[name].set()

    def release_all(self):
        self.release(*STEP_NAMES)

    def run_step(self, name, ctx):
        self.calls.append((name, ctx.run_id, ctx.reason))
        if name == 'compile': self.rebuild_flags.append(ctx.reason == 'rebuild')
        while not self.gates[name].wait(0.01):
            ctx.check_stopped()
        if name == self.fail_at:
            raise OwnerRunError(self.fail_code)
        if name == 'serving':
            ctx.result['registry_origin'] = 'http://127.0.0.1:49999'
        ctx.progress(name + '_done')

    def teardown(self, ctx, *, failed):
        self.teardowns.append((ctx.run_id, failed))


def wait_for(run, predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = run.status()
        if predicate(status): return status
        time.sleep(0.01)
    raise AssertionError(run.status())


def test_idle_then_start_walks_every_step_in_order_to_serving():
    chain = FakeChain(); run = OwnerRun(chain)
    assert run.status()['state'] == 'idle' and run.active() is False
    first = run.start()
    assert first['state'] == 'running' and first['reason'] == 'start'
    status = wait_for(run, lambda s: s['step'] == STEP_NAMES[0])
    assert [s['state'] for s in status['steps']][:2] == ['active', 'pending']
    chain.release(*STEP_NAMES[:3])
    status = wait_for(run, lambda s: s['step'] == STEP_NAMES[3])
    assert [s['state'] for s in status['steps'][:4]] == ['done', 'done', 'done', 'active']
    assert status['steps'][0]['detail'] == STEP_NAMES[0] + '_done'
    chain.release_all()
    status = wait_for(run, lambda s: s['state'] == 'serving')
    assert [c[0] for c in chain.calls] == list(STEP_NAMES)
    assert all(s['state'] == 'done' for s in status['steps'])
    assert status['registry_origin'] == 'http://127.0.0.1:49999'
    assert status['error_code'] is None and run.active() is True
    assert len({c[1] for c in chain.calls}) == 1 and status['run_id'] == chain.calls[0][1]


def test_second_start_while_active_is_refused():
    chain = FakeChain(); run = OwnerRun(chain)
    run.start()
    with pytest.raises(OwnerRunError, match='owner_run_active'):
        run.start()
    run.stop(); wait_for(run, lambda s: s['state'] == 'stopped')


def test_failure_records_step_code_tears_down_and_allows_restart():
    chain = FakeChain(fail_at='compile', fail_code='two_distinct_hosts_required'); run = OwnerRun(chain)
    run.start(); chain.release_all()
    status = wait_for(run, lambda s: s['state'] == 'failed')
    assert status['failed_step'] == 'compile' and status['error_code'] == 'two_distinct_hosts_required'
    assert {s['name']: s['state'] for s in status['steps']}['compile'] == 'failed'
    assert {s['name']: s['state'] for s in status['steps']}['publish'] == 'pending'
    assert chain.teardowns == [(status['run_id'], True)]
    assert run.active() is False
    chain.fail_at = None
    run.start(); status = wait_for(run, lambda s: s['state'] == 'serving')
    assert status['error_code'] is None and status['failed_step'] is None


def test_unexpected_exception_is_reduced_to_a_closed_code():
    class Boom(FakeChain):
        def run_step(self, name, ctx):
            if name == 'descriptors': raise RuntimeError('/private/secret path leaked')
            return super().run_step(name, ctx)
    chain = Boom(); run = OwnerRun(chain); run.start(); chain.release_all()
    status = wait_for(run, lambda s: s['state'] == 'failed')
    assert status['error_code'] == 'owner_run_step_failed' and status['failed_step'] == 'descriptors'
    assert 'secret' not in repr(status)


def test_stop_mid_run_interrupts_the_waiting_step_and_tears_down():
    chain = FakeChain(); run = OwnerRun(chain)
    run.start(); chain.release(*STEP_NAMES[:4])
    wait_for(run, lambda s: s['step'] == STEP_NAMES[4])
    stopping = run.stop()
    assert stopping['state'] in {'stopping', 'stopped'}
    status = wait_for(run, lambda s: s['state'] == 'stopped')
    assert status['error_code'] is None
    assert chain.teardowns == [(status['run_id'], False)]
    assert STEP_NAMES[5] not in [c[0] for c in chain.calls]


def test_stop_while_serving_and_stop_when_idle():
    chain = FakeChain(); run = OwnerRun(chain)
    assert run.stop()['state'] == 'idle'
    run.start(); chain.release_all(); wait_for(run, lambda s: s['state'] == 'serving')
    run.stop()
    status = wait_for(run, lambda s: s['state'] == 'stopped')
    assert chain.teardowns == [(status['run_id'], False)] and run.active() is False


def test_rebuild_stops_then_starts_a_fresh_run_with_the_rebuild_flag():
    chain = FakeChain(); run = OwnerRun(chain)
    run.start(); chain.release_all()
    first = wait_for(run, lambda s: s['state'] == 'serving')['run_id']
    rebuilt = run.rebuild()
    assert rebuilt['reason'] == 'rebuild'
    status = wait_for(run, lambda s: s['state'] == 'serving' and s['run_id'] != first)
    assert chain.teardowns[0] == (first, False)
    assert chain.rebuild_flags == [False, True]
    assert status['reason'] == 'rebuild'


def test_rebuild_requires_a_previous_run():
    run = OwnerRun(FakeChain())
    with pytest.raises(OwnerRunError, match='owner_run_rebuild_requires_run'):
        run.rebuild()
    with pytest.raises(OwnerRunError, match='owner_run_rebuild_requires_run'):
        run.rebuild_async()


def test_rebuild_async_returns_at_once_and_ends_serving_a_fresh_run():
    chain = FakeChain(); run = OwnerRun(chain)
    run.start(); chain.release_all()
    first = wait_for(run, lambda s: s['state'] == 'serving')['run_id']
    started = time.monotonic()
    value = run.rebuild_async()
    assert time.monotonic() - started < 1 and value['rebuild_requested'] is True
    status = wait_for(run, lambda s: s['state'] == 'serving' and s['run_id'] != first)
    assert status['reason'] == 'rebuild' and chain.rebuild_flags == [False, True]


def test_step_budget_expiry_fails_with_timeout_code():
    class Slow(FakeChain):
        def run_step(self, name, ctx):
            if name == 'model':
                ctx.wait_until(lambda: False, budget=0.05, code='owner_model_not_prepared', interval=0.01)
            return super().run_step(name, ctx)
    chain = Slow(); run = OwnerRun(chain); run.start()
    status = wait_for(run, lambda s: s['state'] == 'failed')
    assert status['error_code'] == 'owner_model_not_prepared' and status['failed_step'] == 'model'
