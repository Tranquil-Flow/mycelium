# SPDX-License-Identifier: AGPL-3.0-only
"""Preference HTTP auth, explicit-OFF precedence, and the existing GUI quit path."""
import json
import os
import pytest

from tests.a13_macos.test_demo_run_model import app
from test_app import login, request


def test_fast_mode_http_requires_session_and_round_trips_saved_setting(app):
    status, _, _ = request(app, '/api/fast-mode', 'POST', {'enabled': True}, headers={'Origin': app.origin})
    assert status == 401
    assert not (app.root / 'fast-mode.json').exists()
    headers = login(app)
    status, _, raw = request(app, '/api/fast-mode', 'POST', {'enabled': True}, headers=headers)
    assert status == 200
    assert json.loads(raw) == {'enabled': True, 'source': 'saved', 'locked': False}
    status, _, raw = request(app, '/api/status', headers=headers)
    assert status == 200 and json.loads(raw)['fast_mode']['enabled'] is True


def test_explicit_off_overrides_app_default_on(app, monkeypatch):
    from mycelium_a13_macos.fast_mode import bind_owner_process
    from mycelium_live.token_path_activation import token_path_enabled
    monkeypatch.delenv('MYCELIUM_TOKEN_PATH', raising=False)
    app.action('/api/fast-mode', {'enabled': True})
    with bind_owner_process(app):
        assert token_path_enabled()
        with monkeypatch.context() as m:
            m.setenv('MYCELIUM_TOKEN_PATH', 'off')
            assert not token_path_enabled()
            assert not app.status()['fast_mode']['enabled']
        assert token_path_enabled()


def test_existing_gui_quit_action_closes_private_server_and_retains_preference(app):
    app.action('/api/fast-mode', {'enabled': True})
    headers = login(app)
    status, _, raw = request(app, '/api/quit', 'POST', {}, headers=headers)
    assert status == 200 and json.loads(raw)['stopping'] is True
    assert app.closed.wait(5)
    app.close()  # joins the close fence; no arbitrary sleeps or process kills
    assert app.server is None and not app.running
    assert (app.root / 'fast-mode.json').exists()
    assert not app.launch_file.exists()
