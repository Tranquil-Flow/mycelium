# SPDX-License-Identifier: AGPL-3.0-or-later
"""PV22: owner-issued API keys for the private-swarm OpenAI-compatible API.

Real code under test: ``mycelium_a13_macos.openai_keys.KeyStore`` on a real private directory.  The clock is injected.
"""
from __future__ import annotations

import json
import os
import stat

import pytest

from mycelium_a13_macos.openai_keys import KeyStore, KeyStoreError, QuotaExceeded


class Clock:
    def __init__(self, now=1_800_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def store(tmp_path, clock):
    return KeyStore(tmp_path / 'openai-api', clock=clock)


def test_create_shows_the_secret_once_and_stores_only_a_hash(store, tmp_path):
    record, secret = store.create(label='laptop')
    assert secret.startswith('myc-sk-') and len(secret) >= 40
    assert record['label'] == 'laptop' and record['id'].startswith('key_')
    assert secret not in json.dumps(store.list())
    assert 'hash' not in record and 'secret' not in record
    for path in (tmp_path / 'openai-api').rglob('*'):
        if path.is_file():
            assert secret.encode() not in path.read_bytes()
    assert record['prefix'] == secret[:11] and secret not in record['prefix']


def test_files_are_owner_only(store, tmp_path):
    store.create(label='a')
    root = tmp_path / 'openai-api'
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    files = [p for p in root.iterdir() if p.is_file()]
    assert files, 'keys must be on disk'
    for path in files:
        assert stat.S_IMODE(path.stat().st_mode) == 0o600 and path.stat().st_uid == os.getuid()


def test_authenticate_accepts_only_the_exact_live_secret(store):
    record, secret = store.create(label='a')
    assert store.authenticate(secret)['id'] == record['id']
    assert store.authenticate(secret + 'x') is None
    assert store.authenticate(secret[:-1]) is None
    assert store.authenticate('') is None and store.authenticate(None) is None
    store.revoke(record['id'])
    assert store.authenticate(secret) is None


def test_revoke_keeps_a_listed_tombstone_and_is_idempotent(store, clock):
    record, _ = store.create(label='a')
    clock.now += 5
    first = store.revoke(record['id'])
    assert first['revoked_at'] == clock.now
    clock.now += 5
    assert store.revoke(record['id'])['revoked_at'] == first['revoked_at']
    assert [k['revoked_at'] for k in store.list()] == [first['revoked_at']]
    with pytest.raises(KeyStoreError) as caught:
        store.revoke('key_nope')
    assert caught.value.code == 'key_unknown'


def test_keys_survive_reopen_and_stay_distinct(store, tmp_path, clock):
    a, secret_a = store.create(label='a')
    b, secret_b = store.create(label='b')
    assert secret_a != secret_b and a['id'] != b['id']
    again = KeyStore(tmp_path / 'openai-api', clock=clock)
    assert [k['label'] for k in again.list()] == ['a', 'b']
    assert again.authenticate(secret_b)['id'] == b['id']


@pytest.mark.parametrize('label', ['', ' ', 'x' * 65, 'a\nb', 7, None, 'tab\there'])
def test_label_is_required_and_bounded(store, label):
    with pytest.raises(KeyStoreError) as caught:
        store.create(label=label)
    assert caught.value.code == 'key_label_invalid'


@pytest.mark.parametrize('quota', [
    {'requests_per_window': 0}, {'requests_per_window': -1}, {'requests_per_window': True},
    {'requests_per_window': 1.5}, {'tokens_per_window': 0}, {'tokens_per_window': 10**12},
    {'window_seconds': 59}, {'window_seconds': 10**9}, {'window_seconds': True}])
def test_quota_values_are_validated(store, quota):
    with pytest.raises(KeyStoreError) as caught:
        store.create(label='q', **quota)
    assert caught.value.code == 'key_quota_invalid'


def test_key_count_is_bounded(store):
    for index in range(32):
        store.create(label=f'k{index}')
    with pytest.raises(KeyStoreError) as caught:
        store.create(label='one-too-many')
    assert caught.value.code == 'key_limit_reached'


def test_request_quota_counts_per_window_and_resets(store, clock):
    record, _ = store.create(label='a', requests_per_window=2, window_seconds=60)
    store.settle(store.admit(record['id'], 10), 3)
    store.settle(store.admit(record['id'], 10), 3)
    with pytest.raises(QuotaExceeded) as caught:
        store.admit(record['id'], 10)
    assert caught.value.kind == 'requests' and caught.value.retry_after_s == 60
    clock.now += 45
    with pytest.raises(QuotaExceeded) as caught:
        store.admit(record['id'], 10)
    assert caught.value.retry_after_s == 15
    clock.now += 15
    store.settle(store.admit(record['id'], 10), 1)
    usage = store.list()[0]['usage']
    assert usage['requests'] == 1 and usage['tokens'] == 1


def test_token_quota_reserves_the_requested_maximum_then_settles_to_actual(store):
    record, _ = store.create(label='a', tokens_per_window=100, window_seconds=3600)
    first = store.admit(record['id'], 60)
    with pytest.raises(QuotaExceeded) as caught:
        store.admit(record['id'], 60)  # 60 reserved + 60 > 100
    assert caught.value.kind == 'tokens'
    store.settle(first, 20)  # only 20 were produced
    second = store.admit(record['id'], 60)  # 20 used + 60 <= 100
    store.settle(second, 60)
    assert store.list()[0]['usage']['tokens'] == 80
    with pytest.raises(QuotaExceeded):
        store.admit(record['id'], 21)


def test_a_request_larger_than_the_whole_token_quota_is_not_retryable(store):
    record, _ = store.create(label='a', tokens_per_window=50)
    with pytest.raises(QuotaExceeded) as caught:
        store.admit(record['id'], 51)
    assert caught.value.kind == 'tokens' and caught.value.retry_after_s is None


def test_release_refunds_a_request_that_never_ran(store):
    record, _ = store.create(label='a', requests_per_window=1, tokens_per_window=100)
    store.release(store.admit(record['id'], 80))
    usage = store.list()[0]['usage']
    assert usage['requests'] == 0 and usage['tokens'] == 0
    store.settle(store.admit(record['id'], 80), 5)


def test_quota_is_per_key(store):
    a, _ = store.create(label='a', requests_per_window=1)
    b, _ = store.create(label='b', requests_per_window=1)
    store.settle(store.admit(a['id'], 1), 1)
    store.settle(store.admit(b['id'], 1), 1)
    with pytest.raises(QuotaExceeded):
        store.admit(a['id'], 1)


def test_unlimited_key_has_no_quota(store):
    record, _ = store.create(label='free')
    assert record['quota'] == {'requests_per_window': None, 'tokens_per_window': None, 'window_seconds': 3600}
    for _ in range(50):
        store.settle(store.admit(record['id'], 4096), 4096)


def test_usage_survives_reopen(store, tmp_path, clock):
    record, _ = store.create(label='a', requests_per_window=2)
    store.settle(store.admit(record['id'], 5), 5)
    again = KeyStore(tmp_path / 'openai-api', clock=clock)
    assert again.list()[0]['usage']['requests'] == 1
    again.settle(again.admit(record['id'], 5), 5)
    with pytest.raises(QuotaExceeded):
        again.admit(record['id'], 5)


def test_revoked_key_cannot_be_admitted(store):
    record, _ = store.create(label='a')
    store.revoke(record['id'])
    with pytest.raises(KeyStoreError) as caught:
        store.admit(record['id'], 1)
    assert caught.value.code == 'key_revoked'


def test_a_tampered_or_world_readable_store_is_refused(tmp_path, clock):
    store = KeyStore(tmp_path / 'openai-api', clock=clock)
    store.create(label='a')
    path = tmp_path / 'openai-api' / 'keys.json'
    os.chmod(path, 0o644)
    with pytest.raises(KeyStoreError) as caught:
        KeyStore(tmp_path / 'openai-api', clock=clock)
    assert caught.value.code == 'key_store_unsafe'
    os.chmod(path, 0o600)
    path.write_bytes(b'{not json')
    with pytest.raises(KeyStoreError) as caught:
        KeyStore(tmp_path / 'openai-api', clock=clock)
    assert caught.value.code == 'key_store_corrupt'
