# SPDX-License-Identifier: AGPL-3.0-only
"""Phone wire deadline translation; scripted carrier, not native execution."""
import copy
import time

import pytest

from mycelium_live import phone_stage_session as phone
from test_phone_stage_session import Carrier, session


def bind_payload(deadline_ms):
    return {'request_id': 'request', 'control': {
        'deployment_id': 'dep', 'deployment_epoch': 1,
        'qualification_digest': 'sha256:'+'a'*64,
        'command_id': 'request:request:attempt:1', 'publisher_generation': 1,
        'absolute_deadline_ms': deadline_ms, 'request_attempt': 1,
        'path_id': 'path', 'path_attempt': 0, 'path_digest': 'sha256:'+'b'*64,
        'topology_generation': 1, 'cancellation_generation': 0,
    }}


def test_bind_adds_remaining_budget_without_rewriting_subject(monkeypatch):
    clock = [1_000.0]
    monkeypatch.setattr(phone.time, 'monotonic', lambda: clock[0])
    c = Carrier()
    s = session(c)
    try:
        s.send(command_id='hello', command='hello', payload={})
        original_factory = s._client_factory
        def delayed_connect_factory(**kwargs):
            client = original_factory(**kwargs)
            original_connect = client.connect
            def connect(**kw):
                original_connect(**kw)
                clock[0] += 0.25
            client.connect = connect
            return client
        s._client_factory = delayed_connect_factory
        payload = bind_payload(1_002_000)
        before = copy.deepcopy(payload)
        s.send(command_id='bind', command='bind_request_control', payload=payload)
        wire = c.calls[-1]['payload']
        assert set(wire) == {'request_id', 'control', 'execution_budget_ms'}
        assert wire['execution_budget_ms'] == 1_750
        assert wire['control'] == payload['control']
        assert payload == before
        clock[0] += 0.25
        s.send(command_id='bind-again', command='bind_request_control', payload=payload)
        assert c.calls[-1]['payload']['execution_budget_ms'] == 1_250
        assert c.calls[-1]['payload']['control'] == wire['control']
    finally:
        s.close()


@pytest.mark.parametrize('deadline', [999_000, 1_000_000])
def test_expired_bind_never_opens_a_carrier(monkeypatch, deadline):
    monkeypatch.setattr(phone.time, 'monotonic', lambda: 1_000.0)
    c = Carrier()
    s = session(c)
    try:
        s.send(command_id='hello', command='hello', payload={})
        with pytest.raises(TimeoutError, match='request_execution_deadline_exceeded'):
            s.send(command_id='expired', command='bind_request_control', payload=bind_payload(deadline))
        assert len(c.calls) == 1 and len(c.clients) == 1
    finally:
        s.close()


def test_unix_deadline_is_not_silently_accepted_as_host_monotonic(monkeypatch):
    monkeypatch.setattr(phone.time, 'monotonic', lambda: 1_000.0)
    c = Carrier()
    s = session(c)
    try:
        s.send(command_id='hello', command='hello', payload={})
        with pytest.raises(ValueError, match='phone_execution_budget_invalid'):
            s.send(command_id='wall', command='bind_request_control',
                   payload=bind_payload(int(time.time()*1000)+10_000))
        assert len(c.calls) == 1 and len(c.clients) == 1
    finally:
        s.close()
