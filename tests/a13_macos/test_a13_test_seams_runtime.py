# SPDX-License-Identifier: AGPL-3.0-only
"""V1.2: the member clock-offset test seam can also be set at RUNTIME, only if the gate was open at launch.

The A13 app-flow gate needed a member app restart to apply ``MYCELIUM_A13_TEST_CLOCK_OFFSET_SECONDS``; the restart itself
(a member re-admitted without its worker) confounded the skew clause.  With ``MYCELIUM_A13_TEST_SEAMS=1`` set AT LAUNCH,
the member app also accepts ``POST /api/test-seams/clock-offset {"offset_seconds": <number|null>}`` on its loopback
control server (same session/origin/action checks as every other action).  Without the launch gate the route does not
exist (404); the owner role refuses it; implausible values never activate it.  Setting it again changes it, ``null``
clears it.
"""
import json
import time

import pytest

from mycelium_a13_macos.target_app import TargetApplication
from test_a13_test_seams import GATE, OFFSET, _clean_env, _peer_factory, _target_app  # noqa: F401  (autouse env cleanup)
from test_app import login, request

ROUTE = '/api/test-seams/clock-offset'


def _shift(target):
    return target.peer._clock() - time.time()


# -- the member application ---------------------------------------------------------------------------------------

def test_the_gate_open_at_launch_lets_the_offset_be_set_changed_and_cleared_at_runtime(tmp_path, monkeypatch):
    monkeypatch.setenv(GATE, '1')
    target = TargetApplication(tmp_path / 'a', _test_peer_factory=_peer_factory)
    assert abs(_shift(target)) < 1.0 and target.test_clock_offset_seconds is None
    assert target.set_test_clock_offset(9.0) == {'active': True, 'clock_offset_seconds': 9.0}
    assert 8.0 < _shift(target) < 10.0 and target.test_clock_offset_seconds == 9.0
    target.set_test_clock_offset(-4.5)
    assert -5.5 < _shift(target) < -3.5
    assert target.set_test_clock_offset(None) == {'active': False, 'clock_offset_seconds': None}
    assert abs(_shift(target)) < 1.0 and target.test_clock_offset_seconds is None


def test_without_the_gate_at_launch_the_runtime_setter_is_refused_even_if_the_env_is_set_later(tmp_path, monkeypatch):
    target = TargetApplication(tmp_path / 'a', _test_peer_factory=_peer_factory)
    monkeypatch.setenv(GATE, '1')                      # too late: the decision is made at launch
    monkeypatch.setenv(OFFSET, '9')
    with pytest.raises(ValueError, match='test_seams_unavailable'):
        target.set_test_clock_offset(9.0)
    assert abs(_shift(target)) < 1.0 and target.test_clock_offset_seconds is None


def test_a_gate_set_to_anything_but_1_at_launch_does_not_open_the_setter(tmp_path, monkeypatch):
    monkeypatch.setenv(GATE, 'true')
    target = TargetApplication(tmp_path / 'a', _test_peer_factory=_peer_factory)
    with pytest.raises(ValueError, match='test_seams_unavailable'):
        target.set_test_clock_offset(9.0)


@pytest.mark.parametrize('bad', [float('nan'), float('inf'), 3601.0, -3601.0, True, '9', [9], {'a': 1}])
def test_an_implausible_offset_never_activates_the_seam(tmp_path, monkeypatch, bad):
    monkeypatch.setenv(GATE, '1')
    target = TargetApplication(tmp_path / 'a', _test_peer_factory=_peer_factory)
    with pytest.raises(ValueError, match='test_seam_offset_invalid'):
        target.set_test_clock_offset(bad)
    assert abs(_shift(target)) < 1.0 and target.test_clock_offset_seconds is None


def test_the_owners_own_local_contributor_never_opens_the_setter(tmp_path, monkeypatch):
    monkeypatch.setenv(GATE, '1')

    class Local(TargetApplication):
        HONOURS_TEST_CLOCK_SEAM = False
    local = Local(tmp_path / 'l', _test_peer_factory=_peer_factory)
    with pytest.raises(ValueError, match='test_seams_unavailable'):
        local.set_test_clock_offset(9.0)


# -- the HTTP control server ---------------------------------------------------------------------------------------

def test_the_route_is_absent_without_the_launch_gate(tmp_path, monkeypatch):
    monkeypatch.setenv(OFFSET, '9')                    # the offset alone is inert, and so is the route
    app = _target_app(tmp_path)
    try:
        headers = login(app)
        assert request(app, ROUTE, 'POST', {'offset_seconds': 9.0}, headers)[0] == 404
        assert request(app, ROUTE, 'POST', {'offset_seconds': 9.0})[0] == 404       # not even authentication is asked
        assert app.status()['test_seams_active'] is False
    finally:
        app.close()


def test_with_the_launch_gate_the_route_sets_and_clears_the_offset_and_status_shows_it(tmp_path, monkeypatch):
    monkeypatch.setenv(GATE, '1')
    app = _target_app(tmp_path)
    try:
        headers = login(app)
        status, _, raw = request(app, ROUTE, 'POST', {'offset_seconds': 9.0}, headers)
        assert status == 200, raw
        assert json.loads(raw) == {'active': True, 'clock_offset_seconds': 9.0}
        assert 8.0 < app.authority.peer._clock() - time.time() < 10.0
        view = json.loads(request(app, '/api/status', headers=headers)[2])
        assert view['test_seams_active'] is True and view['test_seams']['clock_offset_seconds'] == 9.0
        status, _, raw = request(app, ROUTE, 'POST', {'offset_seconds': None}, headers)
        assert status == 200 and json.loads(raw) == {'active': False, 'clock_offset_seconds': None}
        assert abs(app.authority.peer._clock() - time.time()) < 1.0
    finally:
        app.close()


def test_the_route_keeps_every_normal_action_check(tmp_path, monkeypatch):
    monkeypatch.setenv(GATE, '1')
    app = _target_app(tmp_path)
    try:
        headers = login(app)
        assert request(app, ROUTE, 'POST', {'offset_seconds': 9.0})[0] == 401                      # no session
        assert request(app, ROUTE, 'POST', {'offset_seconds': 9.0}, {'Cookie': headers['Cookie']})[0] == 403   # no origin
        assert request(app, ROUTE, 'POST', {'offset_seconds': 9.0}, {**headers, 'Origin': 'https://evil.invalid'})[0] == 403
        assert request(app, ROUTE, 'POST', {'offset_seconds': 'nine'}, headers)[0] == 400
        assert request(app, ROUTE, 'POST', {'offset_seconds': 9.0, 'extra': 1}, headers)[0] == 400
        assert request(app, ROUTE, 'POST', {}, headers)[0] == 400
        assert request(app, ROUTE, 'POST', {'offset_seconds': 99999.0}, headers)[0] == 400
        assert request(app, ROUTE, 'GET', headers=headers)[0] == 404
        assert abs(app.authority.peer._clock() - time.time()) < 1.0
    finally:
        app.close()


def test_the_owner_role_answers_the_route_with_a_refusal_even_with_the_gate(tmp_path, monkeypatch):
    from mycelium_a13_macos.app import DemoApp
    monkeypatch.setenv(GATE, '1')
    app = DemoApp(tmp_path / 'owner', check_platform=False).start()
    try:
        headers = login(app)
        assert request(app, ROUTE, 'POST', {'offset_seconds': 9.0}, headers)[0] in (403, 404)
        assert app.status()['test_seams_active'] is False
    finally:
        app.close()
