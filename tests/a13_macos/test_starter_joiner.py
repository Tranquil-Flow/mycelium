# SPDX-License-Identifier: AGPL-3.0-only
"""A GUI owner's starter announcement works without env setup on the joiner."""
from mycelium_a13_macos.app import DemoApp
from mycelium_a13_macos.run_model import load_choice
from tests.a13_macos.test_demo_target import world, whole, announcement, Acquisition, A9


def test_joiner_adopts_announced_starter_via_real_picker(whole, monkeypatch):
    app, agent, gate, state, calls = whole
    monkeypatch.delenv('MYCELIUM_A13_RUN_MODEL', raising=False)
    app._acquisition_active = False
    app.owner_run = None
    def reopen():
        app.acquisition = Acquisition(A9['model_id'], A9['resolved_commit'])
    app._open_acquisition = reopen
    original = app.action
    def action(path, body):
        if path == '/api/run-model':
            DemoApp._choose_run_model(app, body)
        return original(path, body)
    app.action = action
    state['a'] = announcement('collect', slice_mode=False, model={**A9, 'choice': 'a9-route'})
    assert agent.tick()['state'] == 'downloading_model'
    assert load_choice(app.root) == 'a9-route'
    assert [p for p, _ in app.actions] == ['/api/run-model', '/api/model/download']
    app.acquisition.state = 'prepared'
    assert agent.tick()['state'] == 'reported'
    assert len(calls) == 1 and 'slice_source' not in calls[0]
