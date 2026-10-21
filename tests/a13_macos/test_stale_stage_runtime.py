# SPDX-License-Identifier: AGPL-3.0-only
"""V1.2 D5: a leftover stage runtime of the previous run must not fail the next run's bootstrap.

A13 app-flow gate (V1.1 candidate): ``demo_restart`` failed once with ``stage_runtime_active`` right after a re-join.
``TargetApplication.stage_offer`` refuses while ``_runtime`` is attached; the runtime of the last run stays attached
after its worker exited until a cleanup request detaches it, and the owner cannot request cleanup for a work offer that
already expired (``request_stage_cleanup`` needs a live offer; offers live at most 300 s).  A member that never saw the
owner's ``stopped`` phase (Stop then Start within seconds) therefore met its next bootstrap offer with a stale runtime.
The agent now detaches the previous run's runtime once and retries; any other error, or a runtime that cannot be
detached, is still reported by name.
"""
from test_demo_target import App, Gate, announcement, offer
from mycelium_a13_macos.demo_target import DemoTargetAgent
import pytest


class StageRuntimeActive(Exception):
    def __init__(self):
        super().__init__('stage_runtime_active')
        self.code = 'stage_runtime_active'


@pytest.fixture
def world(tmp_path, monkeypatch):
    (tmp_path / 'target').mkdir()
    app = App(tmp_path)
    app.stale = True
    original = app.action

    def action(path, body):
        if path == '/api/staging/accept' and app.stale:
            app.actions.append((path, body))
            raise StageRuntimeActive()
        if path == '/api/control/stop' and not getattr(app, 'cannot_detach', False):
            app.stale = False
        return original(path, body) if path != '/api/staging/accept' else (app.actions.append((path, body)) or {})
    app.action = action
    monkeypatch.setattr('mycelium_a13_macos.installation_descriptor.installation_descriptor',
                        lambda app, **k: {'statement': {'target_node_id': 'node-b'}, 'signed': True})
    state = {'a': announcement('observe', bootstrap_offer_id='boot-1'), 'reports': []}
    agent = DemoTargetAgent(app, consent=Gate(), source=lambda: state['a'], sink=state['reports'].append,
                            report_interval=0)
    app.authority.offer = offer('boot-1')
    return app, agent


def test_a_stale_runtime_is_detached_once_and_the_bootstrap_proceeds(world):
    app, agent = world
    assert agent.tick()['state'] == 'bootstrap_running'
    assert [p for p, _ in app.actions] == ['/api/staging/accept', '/api/control/stop', '/api/staging/accept',
                                           '/api/control/start']


def test_a_runtime_that_cannot_be_detached_is_still_reported_by_name(world):
    app, agent = world
    app.cannot_detach = True
    status = agent.tick()
    assert (status['state'], status['detail']) == ('error', 'stage_runtime_active')
    assert [p for p, _ in app.actions].count('/api/staging/accept') == 2     # one retry, never a loop


def test_another_staging_error_is_not_retried_or_hidden(world):
    app, agent = world
    original = app.action

    def action(path, body):
        if path == '/api/staging/accept':
            raise ValueError('stage_not_authorized')
        return original(path, body)
    app.action = action
    status = agent.tick()
    assert (status['state'], status['detail']) == ('error', 'stage_not_authorized')
    assert '/api/control/stop' not in [p for p, _ in app.actions]
