# SPDX-License-Identifier: AGPL-3.0-only
"""Consent-wall propagation and independently enforced fake-clock continuation."""
from dataclasses import asdict
from types import SimpleNamespace

import pytest

from mycelium_a13_macos import demo_chain, standing_consent
from mycelium_a13_macos.capability import MemberReports, signed_capability_link_report
from mycelium_a13_macos.demo_target import DemoTargetAgent
from mycelium_a13_macos.pairing import PairingError
from mycelium_qualification.signing import generate_ed25519_signer
from test_capability_link import DEVICE, LINK, NOW
from test_demo_chain import LOCAL, REMOTE, Members, make, wait
from test_execution_ceiling import Clock
from test_standing_consent import CAPS, GRANT, POLICY


def grant(root, wall):
    return standing_consent.grant_standing_consent(
        root, {**GRANT, 'caps': {**CAPS, 'max_wall_seconds': wall}}, policy=POLICY, now=NOW)


def test_signed_wall_cap_reaches_owner_and_cannot_be_tampered():
    signer = generate_ed25519_signer(endpoint_id='mac-a')
    report = signed_capability_link_report(signer=signer, node_id=REMOTE, purpose='pre_run',
        generation=3, device=DEVICE, link=LINK, now=NOW, caps={'max_wall_seconds': 600})
    inbox = MemberReports(lookup=lambda node: {'generation': 3, 'lifecycle_state': 'RUNNING',
        'verification_key_digest': signer.verification_key_digest}, clock=lambda: NOW)
    inbox.submit(report)
    assert inbox.latest()[REMOTE]['caps']['max_wall_seconds'] == 600
    report['caps']['max_wall_seconds'] = 1800
    with pytest.raises(PairingError, match='capability_report_signature_invalid'):
        inbox.submit(report)


@pytest.mark.parametrize('wall', [True, 0, 59, 3601, '600'])
def test_signed_wall_cap_rejects_invalid_or_out_of_consent_range(wall):
    signer = generate_ed25519_signer(endpoint_id='mac-a')
    with pytest.raises(PairingError, match='capability_report_invalid'):
        signed_capability_link_report(signer=signer, node_id=REMOTE, purpose='pre_run',
            generation=3, device=DEVICE, link=LINK, now=NOW, caps={'max_wall_seconds': wall})


def test_local_planning_caps_include_consented_wall(tmp_path):
    grant(tmp_path, 600)
    assert standing_consent.planning_caps(tmp_path)['max_wall_seconds'] == 600


@pytest.mark.parametrize('required,expected', [(30, 30), (60, 60), (600, 600)])
def test_member_uses_exact_announced_wall_not_its_larger_grant(tmp_path, required, expected):
    consent = grant(tmp_path, 1800)
    limits = standing_consent.standing_limits(consent, required={
        'max_rss_bytes': 1024, 'max_disk_bytes': 1024,
        'max_wall_seconds': required, 'max_commands': 5000})
    assert limits.max_wall_seconds == expected


def test_stale_larger_wall_assignment_is_refused_not_silently_shortened(tmp_path):
    consent = grant(tmp_path, 600)
    with pytest.raises(standing_consent.StandingConsentError, match='member_wall_cap_below_share'):
        standing_consent.standing_limits(consent, required={
            'max_rss_bytes': 1024, 'max_disk_bytes': 1024, 'max_wall_seconds': 1800})


def test_whole_snapshot_without_resource_row_still_supplies_announced_wall():
    import threading
    agent = DemoTargetAgent.__new__(DemoTargetAgent)
    agent._lock = threading.RLock()
    agent.announcement = {'run_id': 'r', 'work_limits': {'max_wall_seconds': 600}}
    assert agent.member_limits({'run_id': 'r'}) == {'max_wall_seconds': 600}
    assert agent.member_limits({'run_id': 'other'}) is None


@pytest.mark.parametrize('wall,rollover,ceiling', [(600, 480, 570), (60, 30, 30)])
def test_both_guards_use_smallest_worker_even_if_runwide_is_larger(wall, rollover, ceiling):
    clock = Clock()
    chain = demo_chain.AppDemoChain(SimpleNamespace(), budgets={'rollover_margin': 120})
    chain._monotonic = clock
    ctx = SimpleNamespace(state={'execution_clock_started_at': 1000,
        'work_limits': {'max_wall_seconds': 1800},
        'work_nodes': {LOCAL: {'work_limits': {'max_wall_seconds': 1800}},
                       REMOTE: {'work_limits': {'max_wall_seconds': wall}}}})
    clock.now = 1000 + rollover - .01
    assert not chain.rollover_due(ctx)
    clock.now = 1000 + rollover
    assert chain.rollover_due(ctx)
    clock.now = 1000 + ceiling - .01
    assert chain.ceiling_reached(ctx) is None
    clock.now = 1000 + ceiling
    assert chain.ceiling_reached(ctx) == 'execution_ceiling_reached'


def configured(tmp_path, local_wall=1800, remote_wall=600):
    app, ops, members, run = make(tmp_path, local_wall=local_wall)
    reports = app.member_capability_reports
    app.member_capability_reports = lambda: {n: {**r, 'caps': (
        {} if remote_wall is None else {'max_wall_seconds': remote_wall})} for n, r in reports().items()}
    clock = Clock()
    run._clock = run.chain._monotonic = clock
    run._ceiling_poll_s = .01
    app.owner_run = run
    return app, ops, members, run, clock


@pytest.mark.parametrize('with_resources', [False, True])
@pytest.mark.parametrize('local_wall,remote_wall,expected', [
    (1800, 600, {LOCAL: 1800, REMOTE: 600}),
    (60, 3600, {LOCAL: 60, REMOTE: 1800}),
    (1800, None, {LOCAL: 1800, REMOTE: 60}),
    # Device run 2026-10-26 (attempt 7): the owner holds no standing grant. Its own worker must use the
    # owner's own proposal, not the 60 s unknown-member fallback (which expired it at serving).
    (None, 600, {LOCAL: 1800, REMOTE: 600}),
])
def test_compile_records_and_publishes_effective_limits(tmp_path, local_wall, remote_wall, expected, with_resources):
    app, ops, members, run, clock = configured(tmp_path, local_wall, remote_wall)
    if with_resources:
        from mycelium_a13_macos import model_resources as resources
        compile = ops.start_compile
        def compiled(app):
            compile(app)
            rows = resources.plan_for_members(app.acquisition.prepared_source().model,
                [{'node_id': LOCAL, 'memory_bytes': 48 << 30},
                 {'node_id': REMOTE, 'memory_bytes': 16 << 30}])['members']
            app._chain_compile_state['resource_plan'] = {
                'members': [resources.public_member_resources(row) for row in rows]}
        ops.start_compile = compiled
    try:
        run.start()
        status = wait(run, lambda s: s['state'] in {'serving', 'failed'})
        assert status['state'] == 'serving', status
        ctx = run._ctx
        for node, seconds in expected.items():
            assert ctx.state['work_nodes'][node]['work_limits']['max_wall_seconds'] == seconds
            assert app.run_exchange.announcement(node)['work_limits']['max_wall_seconds'] == seconds
        assert ctx.state['work_limits']['max_wall_seconds'] == min(expected.values())
        if remote_wall is None:
            assert ctx.state['wall_cap_fallback_nodes'] == [REMOTE]
            compile_step = next(s for s in status['steps'] if s['name'] == 'compile')
            assert '60 seconds' in compile_step['detail']
        if local_wall is None:
            assert LOCAL not in ctx.state['wall_cap_fallback_nodes']
    finally:
        run.stop(); run.join(5)
        assert not run._thread.is_alive()


class EnforcingMembers(Members):
    """A member owns its timer; owner monitor cannot redefine/extend it."""
    def __init__(self, app, exchange, clock, consent):
        super().__init__(app, exchange, [LOCAL, REMOTE])
        self.clock, self.consent = clock, consent
        self.active = None
        self.started = []
        self.stopped = []
        self.expired = []

    def enforce(self):
        if self.active is not None:
            run_id, started, limits = self.active
            # The native worker's independent ten-second cleanup reserve.
            if self.clock() >= started + limits['max_wall_seconds'] - 10:
                self.expired.append(run_id)
                self.app._chain_workflow.state = 'failed'
                raise RuntimeError('runtime_cold_restart_required')

    def tick(self):
        a = self.exchange.announcement(REMOTE)
        workflow = self.app._chain_workflow
        if a['phase'] in {'work', 'serving'} and workflow is not None and workflow.state != 'prepared':
            if self.active is None:
                # Model the member's independent consent enforcement, including
                # the original bug: even if owner asks 1800, this Mac dies at 600.
                required = a['work_limits']
                limits = asdict(standing_consent.standing_limits(self.consent))
                limits['max_wall_seconds'] = min(limits['max_wall_seconds'], required['max_wall_seconds'])
                self.active = (a['run_id'], self.clock(), limits)
                self.started.append(self.active)
            self.enforce()
        super().tick()

    def retire(self):
        self.enforce()
        if self.active is not None:
            self.stopped.append((self.active[0], self.clock()))
            self.active = None


@pytest.mark.parametrize('wall,rollover_at', [(600, 480), (60, 30)])
def test_enforcing_member_survives_full_rollover_and_original_deadline(tmp_path, wall, rollover_at):
    app, ops, members, run, clock = configured(tmp_path, remote_wall=wall)
    consent = {'caps': {**CAPS, 'max_wall_seconds': wall}}
    member = EnforcingMembers(app, app.run_exchange, clock, consent)
    run.chain._agent_factory = lambda *a, **k: member
    original_stop = ops.stop
    def stop(app):
        member.retire()
        return original_stop(app)
    ops.stop = stop
    failures = []
    original_fail = run._fail
    def fail(*args, **kwargs):
        failures.append(args[2])
        return original_fail(*args, **kwargs)
    run._fail = fail
    try:
        run.start()
        assert wait(run, lambda s: s['state'] in {'serving', 'failed'})['state'] == 'serving'
        old = run._ctx
        assert member.active is not None
        clock.now = old.state['execution_clock_started_at'] + rollover_at - .01
        member.enforce()
        assert run.status()['state'] == 'serving'
        clock.now += .01
        run._end_at_ceiling(old)
        status = wait(run, lambda s: s['state'] == 'failed' or (
            s['run_id'] != old.run_id and s['state'] == 'serving'))
        assert status['state'] == 'serving', status
        assert status['rollover']['completed'] == 1
        assert len(member.started) == 2 and member.stopped == [(old.run_id, clock.now)]
        assert member.started[0][2]['max_wall_seconds'] == wall
        # Cross the first worker's native deadline while the successor is still
        # safely before its own rollover. No real-minute waits.
        clock.now = member.started[0][1] + wall - 10
        member.enforce()
        assert run.status()['state'] == 'serving'
        assert member.expired == [] and failures == []
    finally:
        run.stop(); run.join(5)
        assert not run._thread.is_alive()


@pytest.mark.parametrize('local', [False, True])
def test_agent_refuses_wall_cap_reduced_after_compilation(tmp_path, local):
    from mycelium_a13_macos.demo_target import StandingConsentGate, OwnerRunConsent
    grant(tmp_path, 600)
    agent = DemoTargetAgent.__new__(DemoTargetAgent)
    agent.app = SimpleNamespace(root=tmp_path)
    agent.consent = OwnerRunConsent(lambda: True) if local else StandingConsentGate()
    # No resource row: whole-snapshot workers still need the wall check.
    assert agent._share_refusal({'work_limits': {'max_wall_seconds': 1800}}) == 'member_wall_cap_below_share'
    assert agent._share_refusal({'work_limits': {'max_wall_seconds': 600}}) is None


def test_owner_surfaces_standing_wall_refusal_instead_of_waiting():
    from mycelium_a13_macos.owner_run import OwnerRunError
    chain = demo_chain.AppDemoChain(SimpleNamespace())
    chain._reports = lambda ctx: {REMOTE: {'state': 'reported', 'detail': 'member_wall_cap_below_share'}}
    ctx = SimpleNamespace(state={'participants': [LOCAL, REMOTE]})
    with pytest.raises(OwnerRunError, match='member_wall_cap_below_share'):
        chain._all_in(ctx, {'work_running'})


def test_enforcing_fake_really_expires_without_owner_rollover(tmp_path):
    app, ops, members, run, clock = configured(tmp_path)
    member = EnforcingMembers(app, app.run_exchange, clock, {'caps': CAPS})
    app._chain_workflow = SimpleNamespace(state='api_running')
    member.active = ('old', clock(), {'max_wall_seconds': 600})
    clock.now += 590
    with pytest.raises(RuntimeError, match='runtime_cold_restart_required'):
        member.enforce()
    assert member.expired == ['old']
