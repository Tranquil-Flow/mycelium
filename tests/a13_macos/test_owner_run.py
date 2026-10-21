# SPDX-License-Identifier: AGPL-3.0-only
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


def hold_caller_stop_until_the_fresh_run_started(run):
    """Force the ordering the rehearsal flake needed, without timing.

    rebuild_async started its worker thread and only then called stop() from
    the caller.  stop() ends whatever run is current, so when the worker got
    through stop -> join -> start('rebuild') first, the caller's stop() hit the
    fresh run: it sat in 'stopped' with reason 'rebuild' and never served.
    Hold the caller's stop() until the worker has started the fresh run, but
    only if that worker already exists (a stop issued before it is untouched).
    """
    caller = threading.get_ident()
    earlier = {t for t in threading.enumerate() if t.name == 'a13-owner-rebuild'}
    fresh_started = threading.Event()
    holds = []
    original_start, original_stop = run.start, run.stop

    def start(reason='start'):
        try:
            return original_start(reason)
        finally:
            if reason == 'rebuild': fresh_started.set()

    def stop():
        if threading.get_ident() == caller:
            worker = [t for t in threading.enumerate() if t.name == 'a13-owner-rebuild' and t not in earlier]
            if worker:
                assert fresh_started.wait(5)
                holds.append('fresh_run_started_before_caller_stop')
            else:
                holds.append('caller_stop_before_worker')
        return original_stop()
    run.start, run.stop = start, stop
    return holds


def wait_for_the_rebuilt_run(run, first):
    # Settles on serving, or on a terminal state once the rebuild worker is done.
    return wait_for(run, lambda s: s['run_id'] != first and (
        s['state'] == 'serving' or (s['state'] in {'stopped', 'failed'} and not run._rebuilding)))


def test_rebuild_async_never_stops_the_fresh_run_it_started():
    chain = FakeChain(); run = OwnerRun(chain)
    run.start(); chain.release_all()
    first = wait_for(run, lambda s: s['state'] == 'serving')['run_id']
    holds = hold_caller_stop_until_the_fresh_run_started(run)
    value = run.rebuild_async()
    assert value['rebuild_requested'] is True and holds
    status = wait_for_the_rebuilt_run(run, first)
    assert (status['state'], status['reason'], status['error_code']) == ('serving', 'rebuild', None)
    assert chain.teardowns == [(first, False)] and chain.rebuild_flags == [False, True]


def test_rebuild_async_after_a_failed_run_serves_a_fresh_run():
    # The owner's Rebuild button after a failed run: the old run has already
    # torn down, so the worker has nothing to wait for and starts the fresh run
    # at once - the widest window for the caller's late stop().
    chain = FakeChain(fail_at='compile', fail_code='two_distinct_hosts_required'); run = OwnerRun(chain)
    run.start(); chain.release_all()
    first = wait_for(run, lambda s: s['state'] == 'failed')['run_id']
    chain.fail_at = None
    holds = hold_caller_stop_until_the_fresh_run_started(run)
    assert run.rebuild_async()['rebuild_requested'] is True and holds
    status = wait_for_the_rebuilt_run(run, first)
    assert (status['state'], status['reason'], status['error_code'], status['failed_step']) == (
        'serving', 'rebuild', None, None)
    assert chain.teardowns == [(first, True)] and chain.rebuild_flags == [False, True]


def test_step_budget_expiry_fails_with_timeout_code():
    class Slow(FakeChain):
        def run_step(self, name, ctx):
            if name == 'model':
                ctx.wait_until(lambda: False, budget=0.05, code='owner_model_not_prepared', interval=0.01)
            return super().run_step(name, ctx)
    chain = Slow(); run = OwnerRun(chain); run.start()
    status = wait_for(run, lambda s: s['state'] == 'failed')
    assert status['error_code'] == 'owner_model_not_prepared' and status['failed_step'] == 'model'


def test_a_failed_run_stays_active_until_its_teardown_finishes():
    # F3: after a step fails the run sits in 'failing' while teardown announces
    # 'stopped' and ticks this Mac's own contributor.  active() said False there,
    # so a concurrent status poll re-announced 'idle' mid-teardown (the local
    # contributor then overwrote its run report with a run-less one) and a new
    # Start was accepted before the failed run had cleaned up.
    chain = FakeChain(fail_at='descriptors', fail_code='two_macs_required')
    entered, leave = threading.Event(), threading.Event()
    seen = []

    def teardown(ctx, *, failed):
        seen.append(failed)
        entered.set()
        assert leave.wait(5)
    chain.teardown = teardown
    run = OwnerRun(chain)
    run.start(); chain.release_all()
    assert entered.wait(5)
    try:
        assert run.status()['state'] == 'failing'
        assert run.active() is True
        with pytest.raises(OwnerRunError, match='owner_run_active'):
            run.start()
    finally:
        leave.set()
    status = wait_for(run, lambda s: s['state'] == 'failed')
    assert (status['failed_step'], status['error_code']) == ('descriptors', 'two_macs_required')
    assert seen == [True] and run.active() is False
