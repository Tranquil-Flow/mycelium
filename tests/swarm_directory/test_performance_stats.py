# SPDX-License-Identifier: AGPL-3.0-only
"""Separate signed telemetry must never change the heartbeat contract."""
import copy
import json

import pytest

from mycelium_swarm_directory import docs
from mycelium_swarm_directory.crypto import Ed25519Signer, verify
from mycelium_swarm_directory.directory import Directory
from mycelium_swarm_directory.store import SqliteStore
from mycelium_swarm_directory.wire import WireError

SWARM = 'sw-aaaaaaaaaaaaaaaaaaaa'


def payload():
    metric = {'p50': 1200, 'p90': 1800, 'n': 2}
    return {'window_seconds': 3600, 'window_limit': 50, 'observed_seconds': 100,
            'requests_24h': 2, 'coverage_seconds': 100, 'serving_since': 1799999900,
            'uptime_seconds': 100, 'mode': 'fast', 'ttft_ms': metric.copy(),
            'decode_millitokens_s': {'p50': 12500, 'p90': 13000, 'n': 2},
            'request_ms': metric.copy(), 'stages': [
                {'index': 0, 'layer_start': 0, 'layer_end': 23, 'compute_ms': metric.copy()},
                {'index': 1, 'layer_start': 23, 'layer_end': 24, 'compute_ms': metric.copy()}],
            'hops': [], 'stage_window_seconds': 3600}


def signed(owner, at=1800000000, **changes):
    from mycelium_swarm_directory import stats_docs
    return stats_docs.stats(owner, SWARM, at=at, values=dict(payload(), **changes))


def call(directory, path, body=None):
    return directory.handle('GET' if body is None else 'POST', path,
                            b'' if body is None else json.dumps(body).encode(), 'test')


def test_old_heartbeat_and_new_stats_are_separate_closed_signed_types():
    from mycelium_swarm_directory import stats_docs
    owner = Ed25519Signer.generate()
    old = docs.heartbeat(owner, SWARM, status='serving', members=2, served_model='Qwen/Qwen3-0.6B', at=1800000000)
    assert docs.check(old, docs.HEARTBEAT, verify) == old
    document = signed(owner)
    assert stats_docs.check(document, verify) == document
    with pytest.raises(WireError):
        docs.check(dict(old, stats=payload()), docs.HEARTBEAT, verify)
    with pytest.raises(WireError, match='signature_invalid'):
        stats_docs.check(dict(document, at=1800000001), verify)


@pytest.mark.parametrize('mutation', [
    lambda p: p.update(prompt='private'),
    lambda p: p.update(mode='secret-hostname'),
    lambda p: p.update(requests_24h=True),
    lambda p: p.update(window_limit=51),
    lambda p: p['ttft_ms'].update(p50=-1),
    lambda p: p['ttft_ms'].update(p50=1.5),
    lambda p: p['ttft_ms'].update(p90=100),
    lambda p: p['ttft_ms'].update(n=0),
    lambda p: p['stages'][0].update(node_id='secret'),
    lambda p: p['stages'][0].update(layer_end=0),
    lambda p: p.update(stages=p['stages'] * 33),
    lambda p: p.update(hops=[{'from_stage': 0, 'to_stage': 2, 'latency_ms': {'p50': 1,'p90': 1,'n': 1}}]),
])
def test_stats_reject_unbounded_or_private_values(mutation):
    from mycelium_swarm_directory import stats_docs
    owner = Ed25519Signer.generate()
    p = payload(); mutation(p)
    with pytest.raises(WireError):
        document = stats_docs.stats(owner, SWARM, at=1800000000, values=p)
        stats_docs.check(document, verify)


@pytest.mark.parametrize('kind,origin', [('activation_rtt',0),('owner_control_rtt_probe',None)])
def test_signed_real_hop_shapes_and_bounds(kind, origin):
    from mycelium_swarm_directory import stats_docs
    owner = Ed25519Signer.generate()
    p = payload()
    hop = {'from_stage':origin,'to_stage':1,'kind':kind,'rtt_ms':125,'samples':5,'age_seconds':2}
    p['hops'] = [hop]
    document = stats_docs.stats(owner, SWARM, at=1800000000, values=p)
    assert stats_docs.check(document,verify) == document
    for field,value in [('rtt_ms',-1),('rtt_ms',True),('samples',0),('age_seconds',91),('kind','private-host'),('to_stage',2)]:
        changed = copy.deepcopy(p); changed['hops'][0][field] = value
        with pytest.raises(WireError): stats_docs.check_values(changed)
    with pytest.raises(WireError): stats_docs.check_values(dict(p,hops=[hop,hop]))


def test_storage_restart_aging_replay_ownership_and_entry_change(tmp_path):
    owner, entry, stranger = [Ed25519Signer.generate() for _ in range(3)]
    clock = [1800000000]
    store = SqliteStore(tmp_path / 'test.sqlite3')
    directory = Directory(store, verify, clock=lambda: clock[0])
    record = docs.swarm_record(owner, swarm_id=SWARM, seq=1, name='Test', model='Qwen/Qwen3-0.6B',
                               entry={'key': entry.public_key, 'origin': 'https://entry.example.org'})
    assert call(directory, '/records', record)[0] == 200
    assert call(directory, '/heartbeat', docs.heartbeat(owner, SWARM, status='serving', members=2,
                         served_model=record['model'], at=clock[0]))[0] == 200
    assert call(directory, '/stats', signed(stranger))[0] == 403
    assert call(directory, '/stats', signed(entry))[0] == 200
    assert call(directory, '/stats', signed(entry))[0] == 409
    assert call(directory, '/stats', signed(entry, at=clock[0] + 301))[0] == 400
    row = call(directory, '/swarms')[1]['swarms'][0]
    assert row['stats']['values'] == payload() and row['stats']['stale'] is False
    store.close()
    store = SqliteStore(tmp_path / 'test.sqlite3')
    directory = Directory(store, verify, clock=lambda: clock[0])
    clock[0] += 91
    row = call(directory, '/swarms/' + SWARM)[1]['swarm']
    assert row['stats']['stale'] is True and row['stats']['age_seconds'] == 91
    assert row['stats']['values']['ttft_ms']['n'] == 2
    replacement = docs.swarm_record(owner, swarm_id=SWARM, seq=2, name='Test', model=record['model'], entry=None)
    assert call(directory, '/records', replacement)[0] == 200
    assert call(directory, '/swarms/' + SWARM)[1]['swarm']['stats'] is None
    assert call(directory, '/stats', signed(entry, at=clock[0]))[0] == 403
    store.close()
