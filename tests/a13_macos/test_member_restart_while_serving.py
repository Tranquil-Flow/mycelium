# SPDX-License-Identifier: AGPL-3.0-only
"""V1.2 D1 (part 1): a joined Mac restarted while the owner run serves ends the run honestly and promptly.

A13 app-flow gate (V1.1 candidate): the member app was restarted (the clock-skew seam needed it) while the owner run
served.  The restart is a re-admission (newer generation and incarnation) of a Mac whose native worker is gone: its
fresh agent reports ``reported / work_offer_not_published`` and never re-stages, so every command the route sent to the
new incarnation expired unclaimed, the next chat ended ``request_cleanup_unproven`` after ~70 s and the run kept
saying ``serving``.  Re-attaching would need the stage re-offered and the model re-loaded (a new run); the smallest
correct behaviour is that the owner notices and ends the run with ``member_restarted`` so Stop/Start works at once.

A re-admission with the worker alive (relay rotation) keeps reporting ``serving`` and must not end the run; a Mac that
is not serving but whose identity never changed is not a restart.
"""
import time
from types import SimpleNamespace

from test_demo_chain import LOCAL, REMOTE, make, wait

BOUND = {'generation': 3, 'incarnation': 'inc-3', 'verification_key_digest': 'sha256:' + 'a' * 64, 'endpoint_id': 'ep-r'}


def serving_run(tmp_path, grace=0.0):
    app, ops, members, run = make(tmp_path)
    run.chain.budgets['member_restart_grace'] = grace
    run.start()
    assert wait(run, lambda s: s['state'] in {'serving', 'failed'})['state'] == 'serving'
    # What the real observation context records and what the coordinator says now.
    app._observation_context.members = tuple({'node_id': n, **BOUND} for n in (LOCAL, REMOTE))
    current = {n: {'node_id': n, **BOUND} for n in (LOCAL, REMOTE)}
    app.authority.coordinator.member = lambda node: dict(current[node])
    return app, ops, members, run, current


def remote_report(app, run, state, detail=None):
    status = run.status()
    app.run_exchange.submit_local(REMOTE, {
        'protocol': 'mycelium.a13_demo_member_report.v1', 'run_id': status['run_id'],
        'deployment_id': status['deployment_id'], 'state': state, 'detail': detail, 'model': None,
        'progress': None, 'descriptor': None, 'issued_at': time.time()})


def restart_member(app, run, current, members):
    """The member app was restarted: newer identity, fresh agent that no longer serves."""
    members['m'].nodes = [LOCAL]            # the old agent is gone; only the owner's own contributor reports
    current[REMOTE] = {**current[REMOTE], 'generation': 4, 'incarnation': 'inc-4'}
    remote_report(app, run, 'reported', 'work_offer_not_published')


def test_the_baseline_protocol_name_matches_the_exchange():
    from mycelium_a13_macos.run_exchange import MEMBER_REPORT
    assert MEMBER_REPORT == 'mycelium.a13_demo_member_report.v1'


def test_a_restarted_member_ends_the_run_with_member_restarted(tmp_path):
    app, ops, members, run, current = serving_run(tmp_path)
    restart_member(app, run, current, members)
    status = wait(run, lambda s: s['state'] in {'failed', 'stopped'})
    assert (status['state'], status['error_code'], status['failed_step']) == ('failed', 'member_restarted', 'serving')
    assert ops.calls[-1] == ('stop',)


def test_a_new_run_can_start_right_after_a_member_restart(tmp_path):
    app, ops, members, run, current = serving_run(tmp_path)
    restart_member(app, run, current, members)
    wait(run, lambda s: s['state'] == 'failed')
    members['m'].nodes = [LOCAL, REMOTE]    # the restarted app answers again
    run.start()
    status = wait(run, lambda s: s['state'] in {'serving', 'failed'})
    assert status['state'] == 'serving', status


def test_the_run_is_not_ended_before_the_grace_has_passed(tmp_path):
    app, ops, members, run, current = serving_run(tmp_path, grace=30.0)
    restart_member(app, run, current, members)
    for _ in range(5):
        assert run.status()['state'] == 'serving'
        time.sleep(0.01)


def test_a_readmission_with_the_worker_still_serving_does_not_end_the_run(tmp_path):
    app, ops, members, run, current = serving_run(tmp_path)
    current[REMOTE] = {**current[REMOTE], 'generation': 4, 'incarnation': 'inc-4'}   # relay rotation
    remote_report(app, run, 'serving')
    for _ in range(5):
        assert run.status()['state'] == 'serving'
        time.sleep(0.01)


def test_a_member_that_stopped_serving_without_a_new_identity_is_not_called_restarted(tmp_path):
    app, ops, members, run, current = serving_run(tmp_path)
    members['m'].nodes = [LOCAL]
    remote_report(app, run, 'reported', 'work_not_accepted')
    for _ in range(5):
        assert run.status()['state'] == 'serving'
        time.sleep(0.01)


def test_the_owners_own_contributor_is_never_called_restarted(tmp_path):
    app, ops, members, run, current = serving_run(tmp_path)
    run_status = run.status()
    run._ctx.state['local_node'] = LOCAL
    current[LOCAL] = {**current[LOCAL], 'generation': 9, 'incarnation': 'inc-9'}
    app.run_exchange.submit_local(LOCAL, {
        'protocol': 'mycelium.a13_demo_member_report.v1', 'run_id': run_status['run_id'],
        'deployment_id': run_status['deployment_id'], 'state': 'reported', 'detail': None, 'model': None,
        'progress': None, 'descriptor': None, 'issued_at': time.time()})
    members['m'].nodes = []
    for _ in range(5):
        assert run.status()['state'] == 'serving'
        time.sleep(0.01)
