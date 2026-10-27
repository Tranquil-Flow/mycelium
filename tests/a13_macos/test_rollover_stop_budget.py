# SPDX-License-Identifier: AGPL-3.0-only
"""Owner rollover with real lifecycle code and a clock-driven route close.

No native workers/relay/model: only the serving thread and target receipt delivery
are simulated. A stop request or thread exit alone never supplies a receipt.
"""
from types import SimpleNamespace

import pytest

from mycelium_a13_macos import chain_compile as cc
from mycelium_a13_macos.demo_chain import ChainOps
from mycelium_a13_macos.managed_control import ManagedControlError
from mycelium_a13_macos.workflow import ManagedServingWorkflow
from test_demo_chain import make, wait, LOCAL, REMOTE
from test_execution_ceiling import Clock


class TimedRouteThread:
    def __init__(self, workflow, clock, duration, receipts, *, receipt=True, error=None):
        self.workflow, self.clock, self.duration = workflow, clock, duration
        self.receipts, self.receipt, self.error = receipts, receipt, error
        self.started = None
        self.done = False
        self.joins = []

    def is_alive(self):
        return not self.done

    def join(self, timeout):
        assert self.workflow.stop_event.is_set()
        self.started = self.clock() if self.started is None else self.started
        self.joins.append(timeout)
        self.clock.now += min(timeout, max(0, self.started + self.duration - self.clock()))
        if self.clock() >= self.started + self.duration:
            self.done = True
            self.workflow._state = 'failed' if self.error else 'stopped'
            self.workflow._error = self.error
            if self.receipt:
                self.receipts.update(self.workflow._offers)


@pytest.fixture
def timed_run(tmp_path, monkeypatch):
    app, ops, members, run = make(tmp_path, local_wall=None)
    original_caps = app.member_capability_reports
    app.member_capability_reports = lambda: {
        node: {**row, 'caps': {'max_wall_seconds': 600, 'max_disk_bytes': 2 << 30}}
        for node, row in original_caps().items()}
    clock = Clock()
    run._clock = run.chain._monotonic = clock
    run.chain._sleep = lambda seconds: setattr(clock, "now", clock.now + seconds)
    run._ceiling_poll_s = .01
    app.owner_run = run
    monkeypatch.setattr(cc, 'time', SimpleNamespace(monotonic=clock))
    run.start()
    assert wait(run, lambda s: s['state'] in {'serving', 'failed'})['state'] == 'serving'
    old = run._ctx
    # The real workflow constructor/state shape, not a fake stop-success method.
    workflow = ManagedServingWorkflow(authority=app.authority,
        controller=SimpleNamespace(peers=(LOCAL, REMOTE)), launch=None)
    workflow._state = 'api_running'
    workflow._offers = {n: {'offer_id': 'work-' + n, 'target_node_id': n} for n in (LOCAL, REMOTE)}
    receipts = set()
    def cleanup_receipt(offer):
        if offer['target_node_id'] not in receipts:
            raise ManagedControlError('offer_cleanup_unconfirmed')
        return {'protocol': 'mycelium.controller_remote_cleanup_ack.v1',
                'node_id': offer['target_node_id'], 'staging_root': '/fixture/staging', 'removed': True}
    app.authority.control_mailbox.cleanup_receipt = cleanup_receipt
    # Stop uses the production ChainOps seam; other stages retain the existing
    # package fixture's simulated compilation/member delivery, never real I/O.
    ops.stop = ChainOps().stop
    if hasattr(ChainOps, 'stop_before'):
        ops.stop_before = ChainOps().stop_before
    app._chain_workflow = workflow
    # Production ObservationContext has run_id; the older package fake does not.
    app._observation_context.run_id = old.run_id
    def install(duration, **kwargs):
        thread = TimedRouteThread(workflow, clock, duration, receipts, **kwargs)
        app._chain_serving_thread = thread
        return thread
    def rollover(at=None):
        clock.now = old.state['execution_clock_started_at'] + (600 - 120 if at is None else at)
        run._end_at_ceiling(old)
        return wait(run, lambda s: s['state'] == 'failed' or (
            s['run_id'] != old.run_id and s['state'] == 'serving'))
    yield SimpleNamespace(app=app, ops=ops, run=run, clock=clock, old=old,
                          workflow=workflow, install=install, rollover=rollover)
    # The never-ending route is only a simulated thread: no process to kill.
    app._chain_serving_thread = None
    app._chain_workflow = None
    if hasattr(ops, 'stop_before'):
        del ops.stop_before
    ops.stop = lambda app: None
    run.stop()
    run.join(5)
    assert not run._thread.is_alive()


def test_35_second_close_rolls_over_after_proof(timed_run):
    t = timed_run
    thread = t.install(35)
    status = t.rollover()
    assert status['state'] == 'serving', status
    assert status['rollover']['completed'] == 1
    assert thread.done
    assert t.clock() == thread.started + 35
    assert t.old.result.get('cleanup_unproven') is None
    assert t.run.chain.budgets['rollover_margin'] == 120
    assert t.old.state['work_nodes'][REMOTE]['work_limits']['max_wall_seconds'] == 600


def test_never_ending_close_fails_closed_at_worker_budget_with_code(timed_run):
    t = timed_run
    thread = t.install(float('inf'))
    status = t.rollover()
    assert status['error_code'] == 'rollover_cleanup_unproven'
    assert status['rollover']['completed'] == 0
    assert status['run_id'] == t.old.run_id
    assert status['cleanup_unproven']['stop_error'] == 'rebuild_stop_timeout'
    from mycelium_a13_macos.worker import _CLEANUP_RESERVE_SECONDS
    assert t.clock() <= t.old.state['execution_clock_started_at'] + 600 - _CLEANUP_RESERVE_SECONDS
    assert not thread.done


def test_late_thread_and_proof_after_stop_timeout_can_continue(timed_run):
    t = timed_run
    t.install(35)
    def early_timeout(app, **kwargs):
        return cc.stop_serving(app, timeout=30)
    t.ops.stop_before = early_timeout
    status = t.rollover()
    assert status['state'] == 'serving', status
    assert status['rollover']['completed'] == 1
    assert t.old.result.get('cleanup_unproven') is None


def test_thread_exit_without_target_receipt_never_continues(timed_run):
    t = timed_run
    t.install(35, receipt=False)
    status = t.rollover()
    assert status['error_code'] == 'rollover_cleanup_unproven'
    assert status['rollover']['completed'] == 0
    assert status['cleanup_unproven']['code'] == 'owner_target_cleanup_unproven'


def test_late_workflow_cleanup_failure_is_not_erased(timed_run):
    t = timed_run
    t.install(35, error='router_port_shutdown_cleanup_unproven')
    status = t.rollover()
    assert status['error_code'] == 'rollover_cleanup_unproven'
    assert status['cleanup_unproven']['stop_error'] == 'router_port_shutdown_cleanup_unproven'


def test_late_failure_retains_original_stop_exception(timed_run):
    t = timed_run
    t.install(35, error='router_port_shutdown_cleanup_unproven')
    t.ops.stop_before = lambda app, **kwargs: cc.stop_serving(app, timeout=30)
    status = t.rollover()
    assert status['error_code'] == 'rollover_cleanup_unproven'
    assert status['cleanup_unproven']['stop_error'] == 'rebuild_stop_timeout'
    assert status['cleanup_unproven']['late_stop_error'] == 'router_port_shutdown_cleanup_unproven'


def test_served_workflow_without_offers_cannot_claim_cleanup(timed_run):
    t = timed_run
    t.install(35)
    t.workflow._offers = {}
    status = t.rollover()
    assert status['error_code'] == 'rollover_cleanup_unproven'
    assert status['cleanup_unproven']['code'] == 'owner_target_cleanup_unproven'


def test_stop_wait_is_derived_from_route_close_constants(monkeypatch):
    from mycelium_live import route, router_port, supervisor
    from physical_inference_qualification import NODE_SESSION_TIMEOUT_SECONDS, STAGE_CLEANUP_TIMEOUT_SECONDS
    workflow = ManagedServingWorkflow(authority=None,
        controller=SimpleNamespace(peers=(LOCAL, REMOTE)), launch=None)
    expected = (supervisor.REGISTRY_SERVER_POLL_SECONDS + router_port.ROUTER_CLOSE_TIMEOUT_SECONDS
                + route._MAX_CANCELLATION_AND_CLEANUP_SECONDS + route.ROUTE_CLOSE_MONITOR_JOIN_SECONDS
                + 2 * (2 * NODE_SESSION_TIMEOUT_SECONDS + STAGE_CLEANUP_TIMEOUT_SECONDS))
    assert workflow.close_wait_seconds() == expected
    monkeypatch.setattr(route, 'NODE_SESSION_TIMEOUT_SECONDS', 1000)
    assert workflow.close_wait_seconds() == expected + 2 * 2 * (1000 - NODE_SESSION_TIMEOUT_SECONDS)


def test_late_scheduler_does_not_get_a_fresh_margin(timed_run):
    t = timed_run
    thread = t.install(35)
    status = t.rollover(at=580)
    assert status['error_code'] == 'rollover_cleanup_unproven'
    assert status['cleanup_unproven']['stop_error'] == 'rebuild_stop_timeout'
    assert t.clock() == t.old.state['execution_clock_started_at'] + 590
    assert not thread.done


def test_late_receipt_is_required_before_replacement(timed_run):
    t = timed_run
    thread = t.install(35, receipt=False)
    def advance(seconds):
        assert t.run._ctx is t.old
        assert [c[0] for c in t.ops.calls].count('compile') == 1
        t.clock.now += seconds
        if t.clock() >= thread.started + 40:
            thread.receipts.update(t.workflow._offers)
    t.run.chain._sleep = advance
    status = t.rollover()
    assert status['state'] == 'serving', status
    assert status['rollover']['completed'] == 1
    assert t.clock() >= thread.started + 40


def test_unknown_stop_error_is_redacted_in_status(timed_run):
    t = timed_run
    t.install(float('inf'))
    def bad_stop(app, **kwargs):
        raise ValueError('https://private.invalid/?credential=do-not-publish')
    t.ops.stop_before = bad_stop
    status = t.rollover()
    assert status['cleanup_unproven']['stop_error'] == 'owner_route_stop_failed'
    assert 'private.invalid' not in str(status)
    assert status['rollover']['completed'] == 0


def test_serving_and_compile_joins_share_one_deadline(timed_run):
    t = timed_run
    serving = t.install(35)
    compiling = TimedRouteThread(t.workflow, t.clock, 10, set())
    t.app._chain_compile_thread = compiling
    started = t.clock()
    with pytest.raises(ValueError, match='rebuild_stop_timeout'):
        cc.stop_serving(t.app, timeout=40)
    assert t.clock() == started + 40
    assert serving.done and not compiling.done
    assert t.app._chain_workflow is t.workflow
    t.app._chain_compile_thread = None


@pytest.mark.parametrize('deliver', [True, False])
def test_real_target_cleanup_receipt_not_owner_request_proves_cleanup(tmp_path, deliver):
    from pathlib import Path
    from test_control_pump import _world
    from test_controller_managed_staging import setup_controller
    from test_managed_control import RUN_ID, DEPLOYMENT_ID
    from mycelium_a13_macos.demo_chain import AppDemoChain
    from mycelium_a13_macos.target_app import TargetApplication
    with _world(tmp_path / 'world') as w:
        box = w['authority']._control_mailbox
        controller, peer = setup_controller(tmp_path, w)
        archive, _ = controller._archive_identity_for_peer(peer)
        offer = box.publish_offer(target=w['member'], run_id=RUN_ID,
            deployment_id=DEPLOYMENT_ID, archive=archive)
        target = TargetApplication(w['target'].state_root)
        target.peer = w['target']
        ca = w['authority'].seed_server.material.ca_certificate
        target.stage_offer(offer, max_bytes=len(archive), max_disk_bytes=100_000,
                           authorize=lambda intent: True, _test_ca_certificate=ca)
        workflow = ManagedServingWorkflow(authority=w['authority'], controller=controller, launch=None)
        workflow._offers = {peer.node_id: offer}
        app = SimpleNamespace(authority=w['authority'])
        chain = AppDemoChain(app)
        clock = Clock()
        chain._monotonic = clock
        started = clock()
        delivered = []
        def advance(seconds):
            clock.now += seconds
            if deliver and not delivered and clock() >= started + 35:
                delivered.append(target.cleanup_offer(offer, acknowledged=True, _test_ca_certificate=ca))
        chain._sleep = advance
        assert chain._target_cleanup_proven(workflow, deadline=started + 40) is deliver
        if deliver:
            assert box.cleanup_receipt(offer) == delivered[0]
            assert not Path(peer.staging_root).exists()
        else:
            assert clock() == started + 40
            assert Path(peer.staging_root).exists()
            with pytest.raises(ManagedControlError, match='offer_cleanup_unconfirmed'):
                box.cleanup_receipt(offer)
        target.stop()
