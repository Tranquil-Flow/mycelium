# SPDX-License-Identifier: AGPL-3.0-only
"""Untrusted provider events cannot rotate identity or leak authz codes."""
import json,subprocess,shlex
from types import SimpleNamespace
from mycelium_a13_macos import public_relay as relay

def test_provider_log_is_reduced_to_public_origin():
    line='tunnel https://one.trycloudflare.com credentials=DO_NOT_RETAIN'
    value=relay.provider_event('cloudflare',line)
    assert value=={'event':'opened','origin':'https://one.trycloudflare.com'}
    assert 'DO_NOT_RETAIN' not in json.dumps(value)
    assert relay.provider_event('localhost_run','https://unverified.lhr.life') is None


def test_changed_origin_withdraws_and_preserves_original(tmp_path):
    t=relay.TemporaryHttpsRelay(tmp_path/'relay',provider='cloudflare')
    class Process:
        def __init__(self):self.terminated=False
        def terminate(self):self.terminated=True
        def poll(self):return None
    t.process=Process();t.proxy=SimpleNamespace(server=SimpleNamespace(backend=('bound',)))
    assert t.observe_origin('https://one.trycloudflare.com') is True
    assert t.observe_origin('https://one.trycloudflare.com') is True
    assert t.observe_origin('https://two.trycloudflare.com') is False
    assert t.public_origin=='https://one.trycloudflare.com'
    assert t.process.terminated and t.proxy.server.backend is None
    assert t.status()['state']=='stopped' and t.status()['error_code']=='relay_origin_changed'

def test_unqualified_provider_is_not_a_supported_candidate(tmp_path):
    import pytest
    with pytest.raises(ValueError,match='relay_provider_invalid'):
        relay.TemporaryHttpsRelay(tmp_path/'relay',provider='localhost_run')
