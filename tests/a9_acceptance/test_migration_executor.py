# SPDX-License-Identifier: AGPL-3.0-only
"""Real private filesystem transactions; no live membership or seed writes."""
import json
import multiprocessing as mp
from pathlib import Path

import pytest

from mycelium_platform_neutral import migration
from mycelium_platform_neutral.migration_executor import (
    FileMembershipState, MigrationExecutor, MigrationRequest, MigrationError,
)
from mycelium_platform_neutral.peer_profile import PeerProfile, sign_peer_profile
from mycelium_qualification.signing import generate_ed25519_signer

NOW = 1_000_000.0
BUILD = 'sha256:' + 'a' * 64

def fixture(tmp_path):
    store = FileMembershipState(tmp_path / 'private')
    signer = generate_ed25519_signer(endpoint_id='endpoint-a')
    baseline = {'node-a': {'generation': 5, 'verification_key_digest': signer.verification_key_digest,
                           'incarnation': 'inc-1', 'lease_expires_at': NOW + 600,
                           'revoked': False, 'software_build_digest': BUILD}}
    store.initialize(baseline)
    profile = PeerProfile.from_mapping({
        'protocol': 'mycelium.membership.peer_profile.v2', 'profile_id': 'profile-a',
        'swarm_id': 'swarm-a', 'node_id': 'node-a', 'incarnation': 'inc-2',
        'membership_generation': 6, 'identity_kind': 'native_peer',
        'platform_family': 'macos', 'platform_variant': None, 'architecture': 'arm64',
        'software_build': {'version': '1.0', 'digest': BUILD}, 'transports': [],
        'runtimes': [], 'lease_expires_at': NOW + 600, 'issued_at': NOW - 1,
        'expires_at': NOW + 600,
    })
    envelope = sign_peer_profile(signer=signer, profile=profile)
    return store, envelope, signer

def request(envelope, *, migration_id='migration-a'):
    return MigrationRequest(migration_id=migration_id, profiles=(envelope,))

def test_real_commit_restart_retry_and_adapter_visibility(tmp_path):
    store, envelope, _ = fixture(tmp_path)
    executor = MigrationExecutor(store)
    for stage in ('profiles_signed', 'profiles_verified', 'commit_pending', 'committed'):
        home = tmp_path / stage
        own, signed, _ = fixture(home)
        one = MigrationExecutor(own)
        with pytest.raises(migration.MigrationUnavailable) as stopped:
            one.execute(request(signed), now=NOW, stop_after=stage)
        assert stopped.value.code == 'migration_checkpoint_interrupted'
        assert one.checkpoint() == stage
        assert MigrationExecutor(FileMembershipState(own.root)).execute(request(signed), now=NOW) == 'committed'
        assert FileMembershipState(own.root).read()['node-a']['generation'] == 6
        assert MigrationExecutor(own).execute(request(signed), now=NOW) == 'committed'
    assert migration.execute_identity_preserving_migration(storage=store, request=request(envelope), now=NOW) == 'committed'
    before = (store.root / 'members.json').read_bytes()
    assert executor.execute(request(envelope), now=NOW) == 'committed'
    assert (store.root / 'members.json').read_bytes() == before
    with pytest.raises(MigrationError, match='changed_retry_rejected'):
        executor.execute(request(envelope, migration_id='other-id'), now=NOW)
    changed = json.loads(json.dumps(envelope)); changed['profile']['platform_family'] = 'linux'
    with pytest.raises(MigrationError):
        executor.execute(request(changed), now=NOW)

def test_negative_verification_and_no_partial_write(tmp_path):
    store, envelope, signer = fixture(tmp_path)
    before = (store.root / 'members.json').read_bytes()
    variants = []
    tampered = json.loads(json.dumps(envelope)); tampered['profile']['node_id'] = 'node-b'; variants.append(tampered)
    tampered = json.loads(json.dumps(envelope)); tampered['signature']['signature'] = 'AAAA'; variants.append(tampered)
    other = generate_ed25519_signer(endpoint_id='other'); variants.append(sign_peer_profile(signer=other, profile=PeerProfile.from_mapping(envelope['profile'])))
    stale = json.loads(json.dumps(envelope)); stale['profile']['membership_generation'] = 7
    variants.append(sign_peer_profile(signer=signer, profile=PeerProfile.from_mapping(stale['profile'])))
    for candidate in variants:
        with pytest.raises((MigrationError, ValueError)):
            MigrationExecutor(store).execute(request(candidate), now=NOW)
        assert (store.root / 'members.json').read_bytes() == before
    with pytest.raises(migration.MigrationUnavailable):
        migration.execute_identity_preserving_migration(declared_members={'node-a': 5}, now=NOW)
    assert (store.root / 'members.json').read_bytes() == before
    with pytest.raises(MigrationError, match='duplicate_identity_rejected'):
        MigrationExecutor(store).execute(MigrationRequest('migration-a', (envelope, envelope)), now=NOW)
    assert (store.root / 'members.json').read_bytes() == before

def test_rollback_before_commit_and_recovery_after_publish(tmp_path):
    store, envelope, _ = fixture(tmp_path)
    before = (store.root / 'members.json').read_bytes()
    ex = MigrationExecutor(store)
    with pytest.raises(migration.MigrationUnavailable):
        ex.execute(request(envelope), now=NOW, stop_after='commit_pending')
    assert (store.root / 'members.json').read_bytes() == before
    assert ex.rollback() == 'rolled_back'
    assert (store.root / 'members.json').read_bytes() == before
    assert ex.execute(request(envelope), now=NOW) == 'committed'

def test_batch_atomicity_when_second_signature_fails(tmp_path):
    store, first, _ = fixture(tmp_path)
    signer = generate_ed25519_signer(endpoint_id='endpoint-b')
    members = store.read()
    members['node-b'] = {**members['node-a'], 'verification_key_digest': signer.verification_key_digest}
    store._write('members.json', {'protocol': 'mycelium.private_members.v1', 'members': members})
    profile = {**first['profile'], 'node_id': 'node-b', 'profile_id': 'profile-b'}
    second = sign_peer_profile(signer=signer, profile=PeerProfile.from_mapping(profile))
    original = (store.root / 'members.json').read_bytes()
    broken = json.loads(json.dumps(second)); broken['signature']['signature'] = 'AAAA'
    with pytest.raises(ValueError):
        MigrationExecutor(store).execute(MigrationRequest('migration-a', (first, broken)), now=NOW)
    assert (store.root / 'members.json').read_bytes() == original
    assert not (store.root / 'journal.json').exists()
    assert MigrationExecutor(store).execute(MigrationRequest('migration-a', (first, second)), now=NOW) == 'committed'
    assert {node: value['generation'] for node, value in store.read().items()} == {'node-a': 6, 'node-b': 6}

def test_recovery_after_atomic_publish_and_corrupt_journal(tmp_path):
    store, envelope, _ = fixture(tmp_path)
    ex = MigrationExecutor(store)
    with pytest.raises(migration.MigrationUnavailable):
        ex.execute(request(envelope), now=NOW, stop_after='commit_pending')
    # Emulate a process death after the atomic state rename, before terminal journal fsync.
    target = store._snapshot()[0]
    target['members']['node-a']['generation'] = 6
    target['members']['node-a']['incarnation'] = 'inc-2'
    store._write('members.json', target)
    assert MigrationExecutor(FileMembershipState(store.root)).execute(request(envelope), now=NOW) == 'committed'
    with pytest.raises(MigrationError, match='rollback_not_available'):
        ex.rollback()
    journal = store.root / 'journal.json'
    journal.write_bytes(journal.read_bytes() + b' ')
    with pytest.raises(ValueError):
        ex.execute(request(envelope), now=NOW)

def test_revoked_and_expired_member_never_writes(tmp_path):
    for field, value in (('revoked', True), ('lease_expires_at', NOW - 1)):
        store, envelope, _ = fixture(tmp_path / field)
        current = store.read()
        current['node-a'][field] = value
        store._write('members.json', {'protocol': 'mycelium.private_members.v1', 'members': current})
        before = (store.root / 'members.json').read_bytes()
        with pytest.raises(MigrationError, match='continuity_rejected'):
            MigrationExecutor(store).execute(request(envelope), now=NOW)
        assert (store.root / 'members.json').read_bytes() == before
        assert not (store.root / 'journal.json').exists()

def test_member_profile_cannot_extend_seed_owned_lease(tmp_path):
    store, envelope, signer = fixture(tmp_path)
    before = (store.root / 'members.json').read_bytes()
    declared = {**envelope['profile'], 'lease_expires_at': NOW + 3600,
                'expires_at': NOW + 3600}
    extended = sign_peer_profile(signer=signer, profile=PeerProfile.from_mapping(declared))
    with pytest.raises(MigrationError, match='profile_build_or_lease_mismatch'):
        MigrationExecutor(store).execute(request(extended), now=NOW)
    assert (store.root / 'members.json').read_bytes() == before
    assert not (store.root / 'journal.json').exists()
    assert MigrationExecutor(store).execute(request(envelope), now=NOW) == 'committed'
    assert store.read()['node-a']['lease_expires_at'] == NOW + 600

def test_late_published_recovery_and_exact_terminal_retry(tmp_path):
    store, envelope, _ = fixture(tmp_path)
    ex = MigrationExecutor(store)
    with pytest.raises(migration.MigrationUnavailable):
        ex.execute(request(envelope), now=NOW, stop_after='commit_pending')
    target = store._snapshot()[0]
    target['members']['node-a'].update(generation=6, incarnation='inc-2')
    store._write('members.json', target)  # rename succeeded, terminal journal fsync did not
    published = (store.root / 'members.json').read_bytes()
    assert MigrationExecutor(FileMembershipState(store.root)).execute(request(envelope), now=NOW + 601) == 'committed'
    assert ex.checkpoint() == 'committed'
    assert ex.execute(request(envelope), now=NOW + 601) == 'committed'
    assert (store.root / 'members.json').read_bytes() == published
    with pytest.raises(MigrationError, match='changed_retry_rejected'):
        ex.execute(request(envelope, migration_id='different'), now=NOW + 601)
    with pytest.raises(ValueError, match='peer_profile_now_invalid'):
        ex.execute(request(envelope), now=float('nan'))

def test_late_unpublished_and_mutated_recovery_fail_closed(tmp_path):
    store, envelope, _ = fixture(tmp_path / 'unpublished')
    ex = MigrationExecutor(store)
    with pytest.raises(migration.MigrationUnavailable):
        ex.execute(request(envelope), now=NOW, stop_after='commit_pending')
    source = (store.root / 'members.json').read_bytes()
    with pytest.raises(ValueError, match='peer_profile_expired'):
        ex.execute(request(envelope), now=NOW + 601)
    assert (store.root / 'members.json').read_bytes() == source
    assert ex.checkpoint() == 'commit_pending'
    for field in ('source_digest', 'target_digest', 'validated_at', 'source_state'):
        home = tmp_path / field
        own, signed, _ = fixture(home)
        one = MigrationExecutor(own)
        with pytest.raises(migration.MigrationUnavailable):
            one.execute(request(signed), now=NOW, stop_after='commit_pending')
        target = own._snapshot()[0]
        target['members']['node-a'].update(generation=6, incarnation='inc-2')
        own._write('members.json', target)
        published = (own.root / 'members.json').read_bytes()
        journal_record = own._read('journal.json')
        assert journal_record is not None
        journal = journal_record[0]
        if field == 'source_state':
            journal[field]['members']['node-a']['verification_key_digest'] = BUILD
        elif field == 'validated_at':
            journal[field] = NOW + 601
        else:
            journal[field] = BUILD
        own._write('journal.json', journal)
        with pytest.raises(ValueError):
            one.execute(request(signed), now=NOW + 601)
        assert (own.root / 'members.json').read_bytes() == published
        assert one.checkpoint() == 'commit_pending'
    own, signed, _ = fixture(tmp_path / 'changed-identity')
    one = MigrationExecutor(own)
    assert one.execute(request(signed), now=NOW) == 'committed'
    before = (own.root / 'members.json').read_bytes()
    changed = json.loads(json.dumps(signed))
    changed['profile']['incarnation'] = 'inc-other'
    with pytest.raises(MigrationError, match='changed_retry_rejected'):
        one.execute(request(changed), now=NOW + 601)
    assert (own.root / 'members.json').read_bytes() == before

def test_published_recovery_rejects_changed_target_or_signed_journal(tmp_path):
    for variant in ('target', 'request', 'signature', 'forged-lease'):
        store, envelope, _ = fixture(tmp_path / variant)
        ex = MigrationExecutor(store)
        with pytest.raises(migration.MigrationUnavailable):
            ex.execute(request(envelope), now=NOW, stop_after='commit_pending')
        target = store._snapshot()[0]
        target['members']['node-a'].update(generation=6, incarnation='inc-2')
        store._write('members.json', target)
        journal_record = store._read('journal.json')
        assert journal_record is not None
        journal = journal_record[0]
        if variant == 'target':
            target['members']['node-a']['incarnation'] = 'inc-forged'
            store._write('members.json', target)
        elif variant == 'request':
            journal['request']['migration_id'] = 'changed'
            store._write('journal.json', journal)
        elif variant == 'forged-lease':
            target['members']['node-a']['lease_expires_at'] = NOW + 3600
            store._write('members.json', target)
            from mycelium_platform_neutral.migration_executor import _bytes, _hash
            journal['target_digest'] = _hash(_bytes(target))
            store._write('journal.json', journal)
        else:
            journal['request']['profiles'][0]['signature']['signature'] = 'AAAA'
            store._write('journal.json', journal)
        before = (store.root / 'members.json').read_bytes()
        with pytest.raises(ValueError):
            ex.execute(request(envelope), now=NOW + 601)
        assert (store.root / 'members.json').read_bytes() == before
        assert ex.checkpoint() == 'commit_pending'

def test_legacy_journal_can_resume_before_publish_but_cannot_prove_published_lease(tmp_path):
    for stage in ('source', 'published'):
        store, envelope, _ = fixture(tmp_path / stage)
        ex = MigrationExecutor(store)
        with pytest.raises(migration.MigrationUnavailable):
            ex.execute(request(envelope), now=NOW, stop_after='commit_pending')
        journal_record = store._read('journal.json')
        assert journal_record is not None
        journal = journal_record[0]
        del journal['source_state'], journal['validated_at']
        store._write('journal.json', journal)
        if stage == 'published':
            target = store._snapshot()[0]
            target['members']['node-a'].update(generation=6, incarnation='inc-2')
            store._write('members.json', target)
            before = (store.root / 'members.json').read_bytes()
            with pytest.raises(MigrationError, match='migration_source_proof_missing'):
                ex.execute(request(envelope), now=NOW + 601)
            assert (store.root / 'members.json').read_bytes() == before
            assert ex.checkpoint() == 'commit_pending'
        else:
            assert ex.execute(request(envelope), now=NOW) == 'committed'

def _worker(root, envelope, queue):
    try:
        queue.put(MigrationExecutor(FileMembershipState(root)).execute(request(envelope), now=NOW))
    except Exception as error:
        queue.put(type(error).__name__ + ':' + str(error))

def test_concurrent_process_resume_is_serialized(tmp_path):
    store, envelope, _ = fixture(tmp_path)
    ctx = mp.get_context('spawn'); queue = ctx.Queue()
    workers = [ctx.Process(target=_worker, args=(store.root, envelope, queue)) for _ in range(2)]
    for worker in workers: worker.start()
    for worker in workers:
        worker.join(15)
        if worker.is_alive(): worker.terminate(); worker.join(); pytest.fail('worker hung')
        assert worker.exitcode == 0
    assert [queue.get(timeout=2) for _ in workers] == ['committed', 'committed']
    assert store.read()['node-a']['generation'] == 6


# Each member signs its migration profile on its own host (run_a9_gate member-sign);
# the seed host executes the migration. Allow the seed host to be up to
# MAX_CLOCK_SKEW_SECONDS behind a member's clock; the lease stays strict.
def _profile_issued_at(tmp_path, issued_at, **overrides):
    store, envelope, signer = fixture(tmp_path)
    declared = {**envelope['profile'], 'issued_at': issued_at, **overrides}
    return store, sign_peer_profile(signer=signer, profile=PeerProfile.from_mapping(declared))

@pytest.mark.parametrize('ahead', [0.2, 29.0])
def test_profile_signed_on_a_member_clock_up_to_30_s_ahead_migrates(tmp_path, ahead):
    store, signed = _profile_issued_at(tmp_path, NOW + ahead)
    assert MigrationExecutor(store).execute(request(signed), now=NOW) == 'committed'
    assert store.read()['node-a']['generation'] == 6

def test_profile_signed_on_a_member_clock_31_s_ahead_is_refused(tmp_path):
    store, signed = _profile_issued_at(tmp_path, NOW + 31.0)
    before = (store.root / 'members.json').read_bytes()
    with pytest.raises(MigrationError, match='profile_build_or_lease_mismatch'):
        MigrationExecutor(store).execute(request(signed), now=NOW)
    assert (store.root / 'members.json').read_bytes() == before
    assert not (store.root / 'journal.json').exists()

def test_profile_lease_gets_no_clock_skew_allowance(tmp_path):
    store, signed = _profile_issued_at(tmp_path, NOW - 2.0, lease_expires_at=NOW - 0.5)
    with pytest.raises(MigrationError, match='profile_build_or_lease_mismatch'):
        MigrationExecutor(store).execute(request(signed), now=NOW)
    assert not (store.root / 'journal.json').exists()
