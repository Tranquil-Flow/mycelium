# SPDX-License-Identifier: AGPL-3.0-only
"""The owner chain's steps after descriptors wait on progress, not on a fixed total.

Two-Mac demo, owner + a laptop on a ~1 MB/s phone hotspot whose link can drop
for minutes: every step that waits for the joined Mac renews its budget while
that step visibly advances, tolerates a link drop while the Mac's seed lease is
live, and still ends a stuck step with its own code naming the Mac.
"""
import time

import pytest

from mycelium_a13_macos.managed_observations import ManagedObservationsError
from mycelium_a13_macos.run_exchange import MEMBER_REPORT
from tests.a13_macos.test_demo_chain import LOCAL, REMOTE, Members, make, wait

#: step -> (owner_run step, budget key, stalled code, unreachable code)
STEPS = {
    'bootstrap': ('observe', 'bootstrap', 'member_bootstrap_timeout', 'bootstrap_member_unreachable'),
    'collect': ('observe', 'collect', 'member_observation_timeout', 'collect_member_unreachable'),
    'targets': ('targets', 'targets', 'member_work_timeout', 'targets_member_unreachable'),
    'serving': ('serving', 'serving', 'route_qualification_timeout', 'serving_member_unreachable'),
}


class StepJoiner(Members):
    """The joined Mac answers ``step`` following ``script`` [(seconds, mode)]:
    'progress' advances what the step observes, 'flat' repeats the same report
    (still heard, not advancing), 'silent' sends nothing (a hotspot link drop).
    After the script it answers normally."""

    def __init__(self, *args, step, script, **kwargs):
        super().__init__(*args, **kwargs)
        self.step, self.script = step, list(script)
        self.start = None
        self.count = 0

    def mode(self):
        if self.start is None:
            return None
        elapsed = time.monotonic() - self.start
        for seconds, mode in self.script:
            if elapsed < seconds:
                return mode
            elapsed -= seconds
        return None

    def _active(self, a):
        workflow = self.app._chain_workflow
        context = self.app._observation_context
        if self.step == 'bootstrap':
            return a['phase'] == 'observe' and context is not None and context.observations is None \
                and not getattr(context, 'collect_started', False)
        if self.step == 'collect':
            return getattr(context, 'collect_started', False) and context.observations is None
        if self.step == 'targets':
            return a['phase'] == 'work' and workflow is not None and workflow.state == 'awaiting_activation'
        return a['phase'] == 'work' and workflow is not None and workflow.state in {'starting', 'api_running'} \
            and self.app._chain_serving_pending

    def tick(self):
        a = self.exchange.announcement(REMOTE)
        if a['phase'] in {'idle', 'stopped'} or not self._active(a):
            return super().tick()
        if self.start is None:
            self.start = time.monotonic()
        mode = self.mode()
        if mode is None:
            if self.step == 'serving':
                self.app._chain_serving_pending = False
                self.app._chain_workflow.state = 'api_running'
            return super().tick()
        state = {'bootstrap': 'preparing', 'collect': 'bootstrap_running', 'targets': 'reported',
                 'serving': 'work_running'}[self.step]
        detail = 'work_not_accepted' if self.step == 'targets' else None
        if mode == 'progress':
            self.count += 1
        if mode in {'progress', 'flat'}:
            progress = None if self.step == 'collect' else {'done_bytes': self.count, 'total_bytes': 10**9}
            self.exchange.submit_local(REMOTE, {'protocol': MEMBER_REPORT, 'run_id': a['run_id'],
                'deployment_id': a['deployment_id'], 'state': state, 'detail': detail, 'model': None,
                'progress': progress, 'descriptor': {'statement': {'target_node_id': REMOTE}},
                'issued_at': time.time()})
        saved, self.nodes = self.nodes, [LOCAL]
        try:
            super().tick()
        finally:
            self.nodes = saved


def make_step(tmp_path, step, script, **budgets):
    app, ops, members, run = make(tmp_path)
    app._chain_serving_pending = False

    def factory(app, local, exchange):
        members['m'] = StepJoiner(app, exchange, [LOCAL, REMOTE], step=step, script=script)
        return members['m']
    run.chain._agent_factory = factory
    run.chain.budgets.update({STEPS[step][1]: 0.6, 'report_silence': 0.3, 'member_silence': 3.0, **budgets})
    if step == 'collect':
        prepare = ops.prepare_observation

        def prepared(app, **kwargs):
            prepare(app, **kwargs)
            context = app._observation_context
            context.collect_started = False
            real = context.collect

            def collect():
                # One hello attempt over the whole swarm: it fails while the
                # joined Mac's hello is on its way; a new failure code is a new
                # stage reached (progress), the same one is no progress.
                context.collect_started = True
                joiner = members['m']
                joiner.tick()
                mode = joiner.mode()
                if mode == 'progress':
                    raise ManagedObservationsError('managed_observation_hello_pending_%d' % (joiner.count // 4))
                if mode in {'flat', 'silent'}:
                    raise ManagedObservationsError('managed_observation_hello_pending')
                return real()
            context.collect = collect
        ops.prepare_observation = prepared
    if step == 'serving':
        def activate(app, proposal):
            ops.calls.append(('activate',))
            app._chain_workflow.state = 'starting'  # the startup route challenge runs
            app._chain_serving_pending = True
        ops.activate = activate
    return app, ops, members, run


@pytest.mark.parametrize('step', sorted(STEPS))
def test_a_slow_but_progressing_joined_mac_renews_the_step_budget(tmp_path, step):
    # 2 s of visible progress against a 0.6 s step budget: the budget renews.
    app, ops, members, run = make_step(tmp_path, step, [(2.0, 'progress')])
    run.start()
    status = wait(run, lambda s: s['state'] in {'serving', 'failed'}, timeout=20)
    assert status['state'] == 'serving', status


@pytest.mark.parametrize('step', sorted(STEPS))
def test_a_link_drop_longer_than_the_step_budget_is_waited_for_while_the_lease_lives(tmp_path, step):
    app, ops, members, run = make_step(tmp_path, step, [(0.3, 'progress'), (1.5, 'silent'), (0.3, 'progress')])
    run.start()
    status = wait(run, lambda s: s['state'] in {'serving', 'failed'}, timeout=20)
    assert status['state'] == 'serving', status


@pytest.mark.parametrize('step', sorted(STEPS))
def test_a_stuck_but_reporting_joined_mac_ends_the_step_with_its_code_naming_the_mac(tmp_path, step):
    owner_step, _, stalled, _ = STEPS[step]
    app, ops, members, run = make_step(tmp_path, step, [(60, 'flat')])
    run.start()
    started = time.monotonic()
    status = wait(run, lambda s: s['state'] == 'failed', timeout=20)
    assert (status['failed_step'], status['error_code']) == (owner_step, stalled), status
    assert time.monotonic() - started < 10
    assert REMOTE[-8:] in (status['error_message'] or ''), status['error_message']


@pytest.mark.parametrize('step', sorted(STEPS))
def test_a_joined_mac_silent_past_the_silence_window_is_named_unreachable_in_its_step(tmp_path, step):
    owner_step, _, _, unreachable = STEPS[step]
    app, ops, members, run = make_step(tmp_path, step, [(0.3, 'progress'), (60, 'silent')], member_silence=1.5)
    run.start()
    started = time.monotonic()
    status = wait(run, lambda s: s['state'] == 'failed', timeout=20)
    assert (status['failed_step'], status['error_code']) == (owner_step, unreachable), status
    assert time.monotonic() - started < 10
    assert REMOTE[-8:] in (status['error_message'] or ''), status['error_message']


def test_a_silent_joined_mac_whose_seed_lease_lapsed_is_not_waited_for(tmp_path):
    app, ops, members, run = make_step(tmp_path, 'targets', [(0.3, 'progress'), (60, 'silent')],
                                       member_silence=30.0)
    coordinator = app.authority.coordinator
    real = coordinator.members

    def lapsed():
        rows = real()
        joiner = members.get('m')
        if joiner is not None and joiner.start is not None and time.monotonic() - joiner.start > 0.6:
            rows = [dict(r, lease_expires_at=time.time() - 1) if r['node_id'] == REMOTE else r for r in rows]
        return rows
    coordinator.members = lapsed
    run.start()
    started = time.monotonic()
    status = wait(run, lambda s: s['state'] == 'failed', timeout=20)
    assert (status['failed_step'], status['error_code']) == ('targets', 'targets_member_unreachable'), status
    assert time.monotonic() - started < 10


class VerifyingJoiner(Members):
    """The joined Mac has every byte and verifies/compiles its snapshot for
    ``seconds`` (done == total, detail 'model_verifying' then 'model_compiling')
    before it can describe itself; ``forever`` never finishes."""

    def __init__(self, *args, seconds, **kwargs):
        super().__init__(*args, **kwargs)
        self.seconds, self.start = seconds, None

    def tick(self):
        a = self.exchange.announcement(REMOTE)
        if a['phase'] != 'collect':
            return super().tick()
        if self.start is None:
            self.start = time.monotonic()
        elapsed = time.monotonic() - self.start
        if elapsed >= self.seconds:
            return super().tick()
        detail = 'model_verifying' if elapsed < self.seconds * 0.7 else 'model_compiling'
        self.exchange.submit_local(REMOTE, {'protocol': MEMBER_REPORT, 'run_id': a['run_id'],
            'deployment_id': a['deployment_id'], 'state': 'downloading_model', 'detail': detail,
            'model': None, 'progress': {'done_bytes': 10**9, 'total_bytes': 10**9},
            'descriptor': None, 'issued_at': time.time()})
        saved, self.nodes = self.nodes, [LOCAL]
        try:
            super().tick()
        finally:
            self.nodes = saved


def make_verifying(tmp_path, seconds, **budgets):
    app, ops, members, run = make(tmp_path)

    def factory(app, local, exchange):
        members['m'] = VerifyingJoiner(app, exchange, [LOCAL, REMOTE], seconds=seconds)
        return members['m']
    run.chain._agent_factory = factory
    run.chain.budgets.update({'descriptors': 0.5, 'download_stall': 0.5, 'report_silence': 0.3,
                              'member_silence': 3.0, 'verify_stall': 3.0, 'verify_rate': 1e18, **budgets})
    return app, ops, members, run


def test_a_verification_longer_than_the_byte_stall_window_is_not_a_stalled_download(tmp_path):
    app, ops, members, run = make_verifying(tmp_path, 2.0)
    run.start()
    status = wait(run, lambda s: s['state'] in {'serving', 'failed'}, timeout=20)
    assert status['state'] == 'serving', status


def test_a_verification_that_never_ends_is_named_after_its_own_window(tmp_path):
    app, ops, members, run = make_verifying(tmp_path, 600, verify_stall=1.0)
    run.start()
    started = time.monotonic()
    status = wait(run, lambda s: s['state'] == 'failed', timeout=20)
    assert (status['failed_step'], status['error_code']) == ('descriptors', 'member_model_verify_stalled'), status
    assert time.monotonic() - started < 10
    joined = [p for p in status['participants'] if p['node_id'] == REMOTE]
    assert joined and joined[0]['detail'] in {'model_verifying', 'model_compiling'}, status['participants']
