# SPDX-License-Identifier: AGPL-3.0-only
"""V1.1 product TEST SEAM for the A13 app-flow gate's forced member clock-skew case.

The gate must prove that a joined Mac whose clock is ~9 s off still chats (the 2026-10-20 demo failure).  A physical
clock cannot be moved without sudo, so the member role honours ``MYCELIUM_A13_TEST_CLOCK_OFFSET_SECONDS`` -- and ONLY when
``MYCELIUM_A13_TEST_SEAMS=1`` is also set.  The offset shifts the clock the member signs and validates managed-control and
run-exchange timestamps with.  The app logs a loud warning and ``/api/status`` shows ``test_seams_active``.  Without the
gate variable the offset variable is inert, and the owner role never shifts its clock.
"""
import json
import time

import pytest

from mycelium_a13_macos import a13_test_seams as seams
from mycelium_a13_macos.target_app import TargetApplication

OFFSET = 'MYCELIUM_A13_TEST_CLOCK_OFFSET_SECONDS'
GATE = 'MYCELIUM_A13_TEST_SEAMS'


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv(OFFSET, raising=False)
    monkeypatch.delenv(GATE, raising=False)


def _peer_factory(root, **kwargs):
    from mycelium_a13_macos.peer import RemoteMacMember
    return RemoteMacMember(root, **kwargs)


# -- the pure decision ---------------------------------------------------------------------------------------------

def test_the_seam_is_inert_without_the_gate_even_when_the_offset_is_set():
    assert seams.clock_offset_seconds({OFFSET: '9'}) is None
    assert seams.clock_offset_seconds({OFFSET: '9', GATE: '0'}) is None
    assert seams.clock_offset_seconds({OFFSET: '9', GATE: 'true'}) is None  # only the literal 1 opens the gate
    assert seams.clock_offset_seconds({OFFSET: '9', GATE: ''}) is None
    assert seams.member_clock({OFFSET: '9'}) is None


def test_the_gate_alone_does_nothing_without_an_offset():
    assert seams.clock_offset_seconds({GATE: '1'}) is None
    assert seams.member_clock({GATE: '1'}) is None


def test_the_gate_and_the_offset_shift_the_member_clock():
    assert seams.clock_offset_seconds({GATE: '1', OFFSET: '9'}) == 9.0
    assert seams.clock_offset_seconds({GATE: '1', OFFSET: '-12.5'}) == -12.5
    clock = seams.member_clock({GATE: '1', OFFSET: '9'})
    assert 8.5 < clock() - time.time() < 9.5


@pytest.mark.parametrize('bad', ['', 'nan', 'inf', '-inf', 'nine', '1e99', '3601', '-3601', '9 s'])
def test_an_invalid_or_out_of_range_offset_never_activates_the_seam(bad):
    assert seams.clock_offset_seconds({GATE: '1', OFFSET: bad}) is None


# -- the target application ------------------------------------------------------------------------------------------

def test_the_target_member_clock_is_shifted_only_with_both_variables(tmp_path, monkeypatch):
    monkeypatch.setenv(OFFSET, '9')
    plain = TargetApplication(tmp_path / 'a', _test_peer_factory=_peer_factory)
    assert abs(plain.peer._clock() - time.time()) < 1.0  # inert: offset without the gate
    assert plain.test_clock_offset_seconds is None

    monkeypatch.setenv(GATE, '1')
    shifted = TargetApplication(tmp_path / 'b', _test_peer_factory=_peer_factory)
    assert 8.0 < shifted.peer._clock() - time.time() < 10.0
    assert shifted.test_clock_offset_seconds == 9.0


def test_the_default_member_class_is_shifted_too(tmp_path, monkeypatch):
    monkeypatch.setenv(GATE, '1')
    monkeypatch.setenv(OFFSET, '-9')
    shifted = TargetApplication(tmp_path / 'c')
    assert -10.0 < shifted.peer._clock() - time.time() < -8.0


def test_the_shifted_clock_is_the_one_the_control_client_signs_with(tmp_path, monkeypatch):
    """The run exchange and the managed-control client read ``peer._clock``: one clock, shifted once."""
    monkeypatch.setenv(GATE, '1')
    monkeypatch.setenv(OFFSET, '9')
    target = TargetApplication(tmp_path / 'd', _test_peer_factory=_peer_factory)
    assert target.peer._clock_source is not time.time
    assert 8.0 < target.peer._clock_source() - time.time() < 10.0


# -- the app: loud warning + /api/status ---------------------------------------------------------------------------------

def _target_app(tmp_path):
    from mycelium_a13_macos.app import DemoApp, private_write
    from mycelium_a13_macos.authority import _atomic_private_json
    from test_app_authority import CONSENT
    root = tmp_path / 'app'
    root.mkdir(mode=0o700)
    _atomic_private_json(root / 'installation-role.json',
                         {'protocol': 'mycelium.a13_installation_role.v1', 'role': 'target'})
    private_write(root / 'resource-policy.json', json.dumps(CONSENT).encode())
    return DemoApp(root, check_platform=False).start()


def test_status_shows_the_seam_active_and_the_app_warns_loudly(tmp_path, monkeypatch, capsys, caplog):
    monkeypatch.setenv(GATE, '1')
    monkeypatch.setenv(OFFSET, '9')
    with caplog.at_level('WARNING'):
        app = _target_app(tmp_path)
    try:
        status = app.status()
        assert status['test_seams_active'] is True
        assert status['test_seams'] == {'active': True, 'clock_offset_seconds': 9.0}
        out = capsys.readouterr().out
        event = json.loads([line for line in out.splitlines() if 'a13_test_seams_active' in line][0])
        assert event['clock_offset_seconds'] == 9.0 and 'TEST SEAM' in event['warning']
        assert any('TEST SEAM' in record.getMessage() and record.levelname == 'WARNING' for record in caplog.records)
    finally:
        app.close()


def test_status_shows_inactive_without_the_gate_and_stays_silent(tmp_path, monkeypatch, capsys, caplog):
    monkeypatch.setenv(OFFSET, '9')
    with caplog.at_level('WARNING'):
        app = _target_app(tmp_path)
    try:
        status = app.status()
        assert status['test_seams_active'] is False
        assert status['test_seams'] == {'active': False, 'clock_offset_seconds': None}
        assert 'a13_test_seams_active' not in capsys.readouterr().out
        assert not any('TEST SEAM' in record.getMessage() for record in caplog.records)
        assert abs(app.authority.peer._clock() - time.time()) < 1.0
    finally:
        app.close()


def test_the_owner_role_never_shifts_its_clock_and_reports_inactive(tmp_path, monkeypatch):
    from mycelium_a13_macos.app import DemoApp
    monkeypatch.setenv(GATE, '1')
    monkeypatch.setenv(OFFSET, '9')
    app = DemoApp(tmp_path / 'owner', check_platform=False).start()
    try:
        assert app.installation_role == 'owner'
        status = app.status()
        assert status['test_seams_active'] is False
        assert status['test_seams']['clock_offset_seconds'] is None
    finally:
        app.close()


def test_the_owners_own_local_contributor_is_never_shifted_or_announced(tmp_path, monkeypatch, capsys):
    from mycelium_a13_macos.app import DemoApp
    from mycelium_a13_macos.local_contributor import create_local_contributor
    from test_app_authority import CONSENT
    monkeypatch.setenv(GATE, '1')
    monkeypatch.setenv(OFFSET, '9')
    app = DemoApp(tmp_path / 'owner', check_platform=False).start()
    actor = None
    try:
        app.action('/api/create', {'consent': CONSENT})
        actor = create_local_contributor(app)
        assert actor.test_clock_offset_seconds is None
        assert abs(actor.peer._clock() - time.time()) < 1.0
        assert 'a13_test_seams_active' not in capsys.readouterr().out
    finally:
        if actor:
            actor.stop()
        app.close()


@pytest.mark.parametrize('offset', [9.0, -9.0])
def test_a_seamed_member_still_exchanges_and_the_owner_measures_the_forced_offset(tmp_path, offset):
    """The point of the seam: the owner's own view shows ~the forced offset and the exchange still completes."""
    from mycelium_a13_macos.app import DemoApp
    from mycelium_a13_macos.local_contributor import create_local_contributor
    from mycelium_a13_macos.run_exchange import RunExchange, RunExchangeClient
    from test_app_authority import CONSENT
    app = DemoApp(tmp_path / 'owner', check_platform=False).start()
    actor = None
    try:
        app.action('/api/create', {'consent': CONSENT})
        actor = create_local_contributor(app)
        exchange = RunExchange(app.authority.control_mailbox).install()
        app.run_exchange = exchange
        # what TargetApplication installs on a member when both variables are set
        actor.peer._clock_source = seams.member_clock({GATE: '1', OFFSET: str(offset)})
        client = RunExchangeClient(actor.peer)
        assert client.read()['phase'] == 'idle'
        measured = exchange.clock_offsets.view(actor.member.node_id)
        assert abs(measured['offset_seconds'] - offset) < 1.5
        assert measured['exceeds_tolerance'] is False
    finally:
        if actor:
            actor.stop()
        app.close()
