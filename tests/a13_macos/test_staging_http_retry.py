# SPDX-License-Identifier: AGPL-3.0-only
"""Staging reads reuse bounded retry; acknowledgements gain no replay authority."""
import pytest
from mycelium_a13_macos import managed_control as control
from test_control_http_retry import client_fixture

READS = ['/managed-control/staging/read', '/managed-control/staging/cleanup/read']


@pytest.mark.parametrize('path', READS)
@pytest.mark.parametrize('status', [502, 503, 504, 530])
def test_staging_read_retries_identical_bytes(monkeypatch, path, status):
    client, calls, clock = client_fixture(monkeypatch, status=status)
    envelope = {'request_id': 'same', 'sequence': 1, 'expires_at': 130, 'signed': 'fixture'}
    assert client._request(path, envelope) == {'accepted': True}
    assert len(calls) == 2 and calls[0][:2] == calls[1][:2]
    assert clock.waits == [.1]
    assert control._REQUEST_DEADLINE.get() is None


@pytest.mark.parametrize('path', READS)
def test_staging_outage_keeps_existing_attempt_budget(monkeypatch, path):
    client, calls, clock = client_fixture(monkeypatch, failures=1000)
    with pytest.raises(control.ManagedControlError) as caught:
        client._request(path, {'signed': 'fixture'})
    assert len(calls) == 8
    assert clock.waits == [.1, .2, .4, .8, 1., 1., 1.]
    assert clock.now <= 110
    assert caught.value.status == 503 and caught.value.request_path == path
    assert len({data for _, data, _ in calls}) == 1


@pytest.mark.parametrize('path', READS)
def test_staging_retry_obeys_callers_earlier_deadline(monkeypatch, path):
    client, calls, clock = client_fixture(monkeypatch, failures=1000)
    with control.request_deadline(clock.now + .15):
        with pytest.raises(control.ManagedControlError):
            client._request(path, {'signed': 'fixture'})
        assert control._REQUEST_DEADLINE.get() == 100.15
    assert len(calls) == 2 and clock.now <= 100.15
    assert all(timeout <= .15 + 1e-10 for _, _, timeout in calls)
    assert control._REQUEST_DEADLINE.get() is None


@pytest.mark.parametrize('path', READS)
def test_canonical_relay_refusal_surfaces_without_retry(monkeypatch, path):
    client, calls, clock = client_fixture(monkeypatch, status=502,
        body=b'{"error":{"code":"relay_backend_unavailable"}}', content_type='application/json')
    with pytest.raises(control.ManagedControlError) as caught:
        client._request(path, {'signed': 'fixture'})
    assert caught.value.code == 'relay_backend_unavailable'
    assert caught.value.status == 502 and caught.value.request_path == path
    assert len(calls) == 1 and not clock.waits


@pytest.mark.parametrize('path', ['/managed-control/staging/ack', '/managed-control/staging/cleanup/ack'])
def test_staging_ack_is_not_replayed(monkeypatch, path):
    client, calls, clock = client_fixture(monkeypatch)
    with pytest.raises(control.ManagedControlError):
        client._request(path, {'signed': 'fixture'})
    assert len(calls) == 1 and not clock.waits
