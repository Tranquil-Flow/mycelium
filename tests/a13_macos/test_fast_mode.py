# SPDX-License-Identifier: AGPL-3.0-only
"""Owner preference, process/child propagation, and idle-only mutation."""
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest
from mycelium_a13_macos.app import DemoError
from tests.a13_macos.test_demo_run_model import app


def test_default_off_and_private_persistence(app):
    assert app.status()['fast_mode'] == {'enabled': False, 'source': 'default', 'locked': False}
    assert app.action('/api/fast-mode', {'enabled': True})['enabled'] is True
    path = app.root / 'fast-mode.json'
    assert path.stat().st_mode & 0o777 == 0o600
    from mycelium_a13_macos.fast_mode import document
    assert document(app.root, environ={}) == {'enabled': True, 'source': 'saved', 'locked': False}
    assert app.action('/api/fast-mode', {'enabled': False})['enabled'] is False


@pytest.mark.parametrize('enabled', ['on', 'off'])
def test_environment_overrides_saved_preference_and_locks_switch(app, monkeypatch, enabled):
    app.action('/api/fast-mode', {'enabled': True})
    before = (app.root / 'fast-mode.json').read_bytes()
    monkeypatch.setenv('MYCELIUM_TOKEN_PATH', enabled)
    assert app.status()['fast_mode'] == {'enabled': enabled == 'on', 'source': 'environment', 'locked': True}
    with pytest.raises(DemoError, match='fast_mode_locked_by_environment'):
        app.action('/api/fast-mode', {'enabled': False})
    assert (app.root / 'fast-mode.json').read_bytes() == before


@pytest.mark.parametrize('state', ['running', 'serving', 'stopping', 'failing'])
def test_no_switch_during_run(app, state):
    app.owner_run = SimpleNamespace(active=lambda: True, status=lambda: {'state': state})
    try:
        with pytest.raises(DemoError, match='fast_mode_run_active'):
            app.action('/api/fast-mode', {'enabled': True})
        assert not (app.root / 'fast-mode.json').exists()
    finally:
        app.owner_run = None


@pytest.mark.parametrize('body', [{}, {'enabled': 'on'}, {'enabled': 1}, {'enabled': True, 'extra': 1}])
def test_invalid_preference_refused(app, body):
    with pytest.raises(DemoError, match='invalid_request'):
        app.action('/api/fast-mode', body)


def test_joiner_cannot_choose_owner_fast_mode(app):
    app.installation_role = 'target'
    try:
        with pytest.raises(DemoError, match='owner_role_required'):
            app.action('/api/fast-mode', {'enabled': True})
    finally:
        app.installation_role = 'owner'


def test_process_binding_reaches_all_readers_and_children(app, monkeypatch):
    from mycelium_a13_macos.fast_mode import bind_owner_process
    from mycelium_live.token_path_activation import token_path_enabled, require_token_path_owner
    from mycelium_a13_macos.token_owner import ManagedRouteTokenOwner
    from mycelium_a13_macos.child_environment import source_mode_environment
    monkeypatch.delenv('MYCELIUM_TOKEN_PATH', raising=False)
    before = dict(os.environ)
    with bind_owner_process(app):
        assert token_path_enabled() is False
        app.action('/api/fast-mode', {'enabled': True})
        assert token_path_enabled() is True
        with pytest.raises(ValueError, match='token_path_activation_unprovisioned'):
            require_token_path_owner(None)
        owner = ManagedRouteTokenOwner(signer=None, current_member=None, mailbox=None,
                    evidence_root=app.root / 'evidence', process_deadline_monotonic_s=1)
        assert owner.enabled is True and owner.required is True
        script = 'from mycelium_live.token_path_activation import token_path_enabled; print(token_path_enabled())'
        # Source-mode and frozen children: explicit copied env versus unchanged inheritance.
        for frozen in (False, True):
            with monkeypatch.context() as m:
                m.setattr(sys, 'frozen', frozen, raising=False)
                child = subprocess.run([sys.executable, '-B', '-c', script], env=source_mode_environment(),
                                       capture_output=True, text=True, timeout=10)
            assert child.returncode == 0, child.stderr
            assert child.stdout.strip() == 'True'
        app.action('/api/fast-mode', {'enabled': False})
        assert token_path_enabled() is False
    assert dict(os.environ) == before


def test_corrupt_state_is_not_silently_off(app):
    from mycelium_a13_macos.fast_mode import document
    path = app.root / 'fast-mode.json'
    path.write_text('{"enabled": "on"}')
    path.chmod(0o600)
    with pytest.raises(ValueError, match='fast_mode_state_invalid'):
        document(app.root, environ={})
