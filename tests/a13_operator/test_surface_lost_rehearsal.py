# SPDX-License-Identifier: AGPL-3.0-only
"""wp8-03 item 4 (priority): the 63r1 class must name itself `surface_lost`.

The failure this rehearses: `native-control.json` is written by the app at startup
(`app.py:294`) and UNLINKED by it on shutdown (`app.py:771`). So its absence means "the app is
not running". Without the guard, `app_rpc.AppClient` surfaces a bare FileNotFoundError, which
chain step 2 reported as `compiler_not_ready` on 2026-09-16 and which cost a physical attempt
(`runs/wp8-02-useful-two-mac/ERRATA-1.md`).

Rehearsing this offline is honest BECAUSE the app's own shutdown path is an unlink: making the
surface disappear is the same observable event a killed daemon produces. What this does not
cover is a real process kill end to end -- stated here rather than glossed.

The property: absence must raise `surface_lost:<label>:<state>:daemon=<evidence>`, with the
daemon's own last markers as evidence, and it must be distinguishable from a merely SLOW app.
"""
import importlib.util
import json
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
OPERATOR = ROOT / 'scripts/a13_operator'
if str(OPERATOR) not in sys.path:
    sys.path.insert(0, str(OPERATOR))

_spec = importlib.util.spec_from_file_location('round14_surface', OPERATOR / 'round14_surface.py')
surface = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(surface)


def _root(tmp_path, present=True):
    root = tmp_path / 'owner-lhr'
    root.mkdir(exist_ok=True)
    control = root / 'native-control.json'
    if present:
        control.write_text(json.dumps({'port': 54321}))
    return root


def _daemon_log(tmp_path, lines):
    path = tmp_path / 'owner-daemon.log'
    path.write_text('\n'.join(lines) + '\n')
    return path


def test_a_present_surface_passes(tmp_path):
    state = surface.require_local(_root(tmp_path), label='owner')
    assert state['present'] is True


def test_a_vanished_surface_names_itself_as_surface_lost(tmp_path):
    """The killed-daemon event: the app unlinked its control file."""
    root = _root(tmp_path)
    assert surface.require_local(root, label='owner')['present'] is True
    (root / 'native-control.json').unlink()
    with pytest.raises(RuntimeError) as caught:
        surface.require_local(root, label='owner')
    message = str(caught.value)
    assert message.startswith('surface_lost:owner:')
    assert '"present": false' in message or '"present":false' in message


def test_the_refusal_is_not_a_bare_missing_file_error(tmp_path):
    """Pins the 63r1 lesson: the named cause, never FileNotFoundError -> compiler_not_ready."""
    root = _root(tmp_path, present=False)
    with pytest.raises(RuntimeError) as caught:
        surface.require_local(root, label='owner')
    assert not isinstance(caught.value, FileNotFoundError)
    assert 'surface_lost' in str(caught.value)
    assert 'compiler_not_ready' not in str(caught.value)


def test_the_apps_own_exit_evidence_rides_along(tmp_path):
    """A lost surface must carry the daemon's last markers, not just 'absent'."""
    root = _root(tmp_path, present=False)
    log = _daemon_log(tmp_path, [
        'LAUNCH 12:00:00 preview70',
        'launcher_started',
        'packaged_app_closed',
        'OWNER_DAEMON_EXIT code=0',
    ])
    with pytest.raises(RuntimeError) as caught:
        surface.require_local(root, label='owner', log_path=log)
    message = str(caught.value)
    assert 'packaged_app_closed' in message
    assert 'OWNER_DAEMON_EXIT' in message


def test_a_dead_app_is_distinguishable_from_a_slow_one(tmp_path):
    """A deadline expiry says so, so a slow app is never reported as a lost surface."""
    root = _root(tmp_path, present=False)
    with pytest.raises(RuntimeError) as caught:
        surface.wait_local(root, deadline_s=0, label='owner')
    assert 'absent_after_0s' in str(caught.value)
    assert str(caught.value).startswith('surface_lost:owner:absent_after_0s:')


def test_the_evidence_is_bounded(tmp_path):
    """The daemon hint must not paste a whole log into the error."""
    root = _root(tmp_path, present=False)
    log = _daemon_log(tmp_path, ['packaged_app_closed ' + 'x' * 5000])
    with pytest.raises(RuntimeError) as caught:
        surface.require_local(root, label='owner', log_path=log)
    message = str(caught.value)
    assert 'packaged_app_closed' in message
    assert len(message) < 1200


def test_a_missing_daemon_log_is_not_itself_a_failure(tmp_path):
    """The evidence is best-effort: no log must still yield a named surface_lost."""
    root = _root(tmp_path, present=False)
    with pytest.raises(RuntimeError) as caught:
        surface.require_local(root, label='owner', log_path=tmp_path / 'never-written.log')
    message = str(caught.value)
    assert message.startswith('surface_lost:owner:')
    assert 'log_error' in message
