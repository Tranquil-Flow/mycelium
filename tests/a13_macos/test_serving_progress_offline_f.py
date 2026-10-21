# SPDX-License-Identifier: AGPL-3.0-only
"""Frozen-F serving observer controls; synthetic clock/status, no native qualification.

These tests do not claim a genuine challenge-progress source exists at this seam.
"""
from types import SimpleNamespace

import pytest

from mycelium_a13_macos import demo_chain
from mycelium_a13_macos.owner_run import OwnerRunError
from mycelium_a13_macos.workflow import ManagedServingWorkflow


class Clock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def time(self):
        return 1000.0 + self.now


class Context:
    run_id = 'current-run'
    deployment_id = 'current-deployment'

    def __init__(self, clock, *, participants=()):
        self.clock = clock
        self.state = {'participants': list(participants), 'local_node': 'owner-node'}
        self.result = {}
        self.polls = 0

    def progress(self, _message):
        pass

    def wait_until(self, predicate, *, budget, code, interval):
        assert budget == float('inf') and code == 'route_qualification_timeout'
        while True:
            self.polls += 1
            assert self.polls < 20, 'unbounded synthetic observer'
            result = predicate()
            if result:
                return result
            self.clock.now += interval


def setup(monkeypatch, *, participants=(), report_factory=None, qualified=False):
    clock = Clock()
    monkeypatch.setattr(demo_chain.time, 'monotonic', clock.monotonic)
    monkeypatch.setattr(demo_chain.time, 'time', clock.time)
    workflow = ManagedServingWorkflow(authority=None, controller=None, launch=None)
    workflow._state = 'starting'
    reports = report_factory or (lambda _clock: {})
    exchange = SimpleNamespace(reports=lambda: reports(clock))
    app = SimpleNamespace(_chain_workflow=workflow, run_exchange=exchange)
    chain = demo_chain.AppDemoChain(app, budgets={'serving': 3.0}, interval=1.0)
    ctx = Context(clock, participants=participants)
    return clock, workflow, chain, ctx


def assert_timeout(chain, ctx):
    with pytest.raises(OwnerRunError) as caught:
        chain._step_serving(ctx)
    assert caught.value.code == 'route_qualification_timeout'
    assert ctx.clock.now == 3.0


def test_actual_stall_expires_on_original_no_progress_window(monkeypatch):
    _, _, chain, ctx = setup(monkeypatch)
    assert_timeout(chain, ctx)


def test_repeated_heartbeat_only_report_never_renews_window(monkeypatch):
    def reports(clock):
        return {'owner-node': {'run_id': 'current-run', 'deployment_id': 'current-deployment',
                               'state': 'work_running', 'detail': None, 'progress': None,
                               'received_at': clock.time(), 'issued_at': clock.time()}}
    _, _, chain, ctx = setup(monkeypatch, participants=('owner-node',), report_factory=reports)
    assert_timeout(chain, ctx)


def test_wrong_run_report_cannot_renew_window(monkeypatch):
    def reports(clock):
        return {'owner-node': {'run_id': 'older-run', 'deployment_id': 'current-deployment',
                               'state': 'work_running', 'detail': 'phase_' + str(int(clock.now)),
                               'progress': None, 'received_at': clock.time()}}
    _, _, chain, ctx = setup(monkeypatch, participants=('owner-node',), report_factory=reports)
    assert_timeout(chain, ctx)


def test_api_running_without_qualification_is_rejected(monkeypatch):
    _, workflow, chain, ctx = setup(monkeypatch)
    workflow._state = 'api_running'
    workflow._startup = {'startup_qualification_complete': False,
                         'unavailable_deployment_count': 1, 'url': 'http://127.0.0.1:55555/'}
    with pytest.raises(OwnerRunError) as caught:
        chain._step_serving(ctx)
    assert caught.value.code == 'route_qualification_failed'
    assert ctx.result == {}
