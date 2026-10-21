# SPDX-License-Identifier: AGPL-3.0-only
"""A `seed_http_seed_time_invalid` refusal names its window in the daemon log.

Two-Mac run #19 stopped with `local_api_http_409:seed_http_seed_time_invalid`
on the laptop's `/api/join/window/poll`, and neither the local API body nor the
daemon log (the app's stdout/stderr) carried the window numbers that
`mycelium_seed.http._seed_time_invalid` attaches to the error: the peer layer
re-raises a code-only error `from` the SeedHTTPError, and the app's 409 path
kept only codes. The public body stays a closed code; the daemon log gets one
bounded JSON line with the numbers (no secrets, no exception text).
"""
import json

import pytest

from mycelium_a13_macos.app import DemoApp, bounded_action_failure
from mycelium_a13_macos.peer import RemoteMacMemberError
from mycelium_seed.http import SeedHTTPError, _seed_time_invalid

from test_app import Authority, login, request

CONSENT = {'peer_visibility': True, 'power': 'ac_only', 'storage_bytes': 2_000_000_000,
           'network': 'unmetered_only', 'background': False, 'diagnostics': False,
           'thermal': 'stop_on_serious'}


def _wrapped_refusal():
    """The real shape: peer._translate re-raises the code `from` the seed error."""
    try:
        raise _seed_time_invalid(now=1000.0, issued=1031.5, expires=1091.5)
    except SeedHTTPError as exc:
        try:
            raise RemoteMacMemberError('seed_http_seed_time_invalid') from exc
        except RemoteMacMemberError as wrapped:
            return wrapped


def _events(text):
    out = []
    for line in text.splitlines():
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            out.append(value)
    return out


def test_the_private_failure_record_carries_the_chained_window():
    record = bounded_action_failure('/api/join/window/poll', _wrapped_refusal(),
                                    'seed_http_seed_time_invalid')
    window = record['seed_window']
    assert window['now'] == 1000.0
    assert window['issued_at'] == 1031.5
    assert window['expires_at'] == 1091.5
    assert window['seconds_before_issued'] == pytest.approx(31.5)
    assert window['seconds_after_expires'] == pytest.approx(-91.5)


def test_a_window_is_only_numbers_from_an_allowlist():
    error = SeedHTTPError('seed_http_seed_time_invalid')
    error.window = {'now': 1.0, 'issued_at': float('nan'), 'expires_at': True,
                    'token': 'fixture secret should not appear', 'seconds_before_issued': 'x'}
    record = bounded_action_failure('/api/join/window/poll', error, 'seed_http_seed_time_invalid')
    assert record['seed_window'] == {'now': 1.0, 'issued_at': None, 'expires_at': None,
                                     'seconds_before_issued': None}
    assert 'fixture secret' not in json.dumps(record)


def test_failures_without_a_window_carry_none():
    record = bounded_action_failure('/api/create', RuntimeError('x'), 'action_not_completed')
    assert 'seed_window' not in record


def test_the_daemon_log_names_the_window_of_a_seed_time_refusal(tmp_path, capfd):
    class Refusing(Authority):
        def create_swarm(self, consent):
            raise _wrapped_refusal()

    root = tmp_path / 'diagnostic-state'
    a = DemoApp(root, authority_factory=Refusing, check_platform=False).start()
    try:
        h = login(a)
        status, _headers, raw = request(a, '/api/create', 'POST', {'consent': CONSENT}, h)
        assert status == 409
        assert json.loads(raw) == {'error': 'seed_http_seed_time_invalid'}
        record = json.loads((root / 'last-action-error.private.json').read_text())
        assert record['seed_window']['issued_at'] == 1031.5
    finally:
        a.close()
    captured = capfd.readouterr()
    events = [e for e in _events(captured.out + captured.err)
              if e.get('event') == 'seed_time_refused']
    assert len(events) == 1
    event = events[0]
    assert event['path'] == '/api/create'
    assert event['public_code'] == 'seed_http_seed_time_invalid'
    assert event['max_clock_skew_seconds'] == 30.0
    window = event['seed_window']
    assert window['now'] == 1000.0 and window['issued_at'] == 1031.5
    assert window['expires_at'] == 1091.5
    assert window['seconds_before_issued'] == pytest.approx(31.5)
    assert window['seconds_after_expires'] == pytest.approx(-91.5)


def test_other_refusals_add_no_daemon_log_line(tmp_path, capfd):
    class BadCreate(Authority):
        def create_swarm(self, consent):
            raise RuntimeError('fixture secret should not appear')

    root = tmp_path / 'other-state'
    a = DemoApp(root, authority_factory=BadCreate, check_platform=False).start()
    try:
        h = login(a)
        assert request(a, '/api/create', 'POST', {'consent': CONSENT}, h)[0] == 409
    finally:
        a.close()
    captured = capfd.readouterr()
    assert not [e for e in _events(captured.out + captured.err)
                if e.get('event') == 'seed_time_refused']
    assert 'fixture secret' not in captured.out + captured.err
