# SPDX-License-Identifier: AGPL-3.0-only
"""Dedicated launcher binds the saved switch; advanced runs also fence changes."""
from pathlib import Path
from types import SimpleNamespace
import signal
import sys

import pytest
from tests.a13_macos.test_demo_run_model import app
from mycelium_a13_macos.app import DemoError


@pytest.mark.parametrize('state', ['prepared', 'publishing', 'staging', 'awaiting_activation', 'starting', 'api_running'])
def test_advanced_workflow_also_refuses_switch(app, state):
    app._chain_workflow = SimpleNamespace(status=lambda: {'state': state})
    try:
        with pytest.raises(DemoError, match='fast_mode_run_active'):
            app.action('/api/fast-mode', {'enabled': True})
    finally:
        app._chain_workflow = None


def test_launcher_restores_process_setting_after_quit(tmp_path, monkeypatch, capsys):
    from mycelium_a13_macos import __main__ as entry
    from mycelium_a13_macos.authority import _atomic_private_json
    from mycelium_a13_macos.fast_mode import FILE, PROTOCOL, APP_DEFAULT_ENV
    from mycelium_live.token_path_activation import token_path_enabled
    import os
    tmp_path.chmod(0o700)
    _atomic_private_json(tmp_path / FILE, {'protocol': PROTOCOL, 'enabled': True})
    monkeypatch.delenv('MYCELIUM_TOKEN_PATH', raising=False)
    monkeypatch.delenv(APP_DEFAULT_ENV, raising=False)
    seen = []
    class Closed:
        def is_set(self):
            seen.append(token_path_enabled())
            return True
    class App:
        installation_role = 'owner'
        closed = Closed()
        def __init__(self, root): self.root = root
        def start(self): return self
        def close(self): seen.append('closed')
    monkeypatch.setattr(entry, 'DemoApp', App)
    monkeypatch.setattr(entry, 'preflight', lambda: {})
    monkeypatch.setattr(entry, 'model_contract', lambda: {})
    monkeypatch.setattr(sys, 'argv', ['app', '--headless', '--state-root', str(tmp_path)])
    handlers = {n: signal.getsignal(n) for n in (signal.SIGINT, signal.SIGTERM)}
    try:
        assert entry.main() == 0, capsys.readouterr().err
    finally:
        for n, handler in handlers.items(): signal.signal(n, handler)
    assert seen == [True, 'closed']
    assert APP_DEFAULT_ENV not in os.environ
