"""Private, restartable local A9 migration transaction. NOT a seed/node cutover.

The storage root is owned by a trusted caller; initialize is a deliberate adapter
operation, not a path for accepting self-declared migration authority. Profiles
are always reverified against the durable pinned keys, including after restart.
"""
from __future__ import annotations

import fcntl
import hashlib
import os
from pathlib import Path
import stat
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Mapping

from mycelium_qualification.evidence import canonical_json_loads
from .migration import LegacyMemberIdentity, ContinuityEvidence, evaluate_identity_continuity, MigrationUnavailable
from .peer_profile import verify_peer_profile
from .primitives import canonical_bytes, digest, segment, bounded_int, finite

STAGES = ('profiles_signed', 'profiles_verified', 'commit_pending', 'committed')
MAX_FILE = 1024 * 1024

class MigrationError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)

def _hash(content: bytes) -> str:
    return 'sha256:' + hashlib.sha256(content).hexdigest()

def _bytes(value: Any) -> bytes:
    content = canonical_bytes(value, 'migration_noncanonical')
    if len(content) > MAX_FILE:
        raise MigrationError('migration_too_large')
    return content

class FileMembershipState:
    """Private same-filesystem atomic adapter. Never points at the live seed DB."""
    def __init__(self, root: Path):
        self.root = Path(root)
        if self.root.is_symlink() or (self.root.exists() and not self.root.is_dir()):
            raise MigrationError('unsafe_storage_root')
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.root.stat().st_mode & 0o077:
            raise MigrationError('unsafe_storage_permissions')

    def _path(self, name: str) -> Path:
        path = self.root / name
        if path.is_symlink():
            raise MigrationError('unsafe_storage_link')
        return path

    @contextmanager
    def lock(self):
        path = self._path('transaction.lock')
        fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise MigrationError('unsafe_storage_lock')
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    def _read(self, name: str) -> tuple[dict, bytes] | None:
        path = self._path(name)
        try:
            fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
        except FileNotFoundError:
            return None
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE or info.st_mode & 0o077:
                raise MigrationError('unsafe_storage_file')
            with os.fdopen(fd, 'rb', closefd=False) as stream:
                raw = stream.read(MAX_FILE + 1)
            if len(raw) > MAX_FILE:
                raise MigrationError('migration_file_oversized')
            value = canonical_json_loads(raw, path=name)
            if not isinstance(value, dict):
                raise MigrationError('migration_file_invalid')
            return value, raw
        finally:
            os.close(fd)

    def _write(self, name: str, value: dict):
        self._path(name)  # refuse symlink, including pre-existing target
        content = _bytes(value)
        fd, temporary = tempfile.mkstemp(prefix='.migration-', dir=self.root)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self._path(name))
            directory = os.open(self.root, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def initialize(self, members: Mapping[str, Any]):
        """Trusted provisioning only; cannot replace existing durable state."""
        with self.lock():
            if self._read('members.json') is not None:
                raise MigrationError('membership_already_initialized')
            _validate_members(members)
            self._write('members.json', {'protocol': 'mycelium.private_members.v1', 'members': dict(members)})

    def _snapshot(self) -> tuple[dict, bytes]:
        found = self._read('members.json')
        if found is None:
            raise MigrationUnavailable('membership_storage_not_initialized')
        state, raw = found
        if set(state) != {'protocol', 'members'} or state['protocol'] != 'mycelium.private_members.v1':
            raise MigrationError('membership_state_invalid')
        _validate_members(state['members'])
        return state, raw

    def read(self) -> dict:
        with self.lock():
            return self._snapshot()[0]['members']

@dataclass(frozen=True)
class MigrationRequest:
    migration_id: str
    profiles: tuple[Mapping[str, Any], ...]

    def payload(self) -> dict:
        segment(self.migration_id, 'migration_id_invalid')
        if not isinstance(self.profiles, tuple) or not 1 <= len(self.profiles) <= 64:
            raise MigrationError('migration_profiles_invalid')
        return canonical_json_loads(_bytes({'migration_id': self.migration_id,
                                            'profiles': list(self.profiles)}), path='request')

def _validate_members(members: Any):
    if not isinstance(members, dict) or not 1 <= len(members) <= 64:
        raise MigrationError('membership_inventory_invalid')
    for key, value in members.items():
        segment(key, 'migration_node_id_invalid')
        if not isinstance(value, dict) or set(value) != {'generation', 'verification_key_digest', 'incarnation', 'lease_expires_at', 'revoked', 'software_build_digest'}:
            raise MigrationError('membership_record_invalid')
        bounded_int(value['generation'], 'membership_generation_invalid', minimum=1)
        digest(value['verification_key_digest'], 'membership_key_invalid')
        digest(value['software_build_digest'], 'membership_build_invalid')
        segment(value['incarnation'], 'membership_incarnation_invalid')
        if type(value['revoked']) is not bool or type(value['lease_expires_at']) not in (int, float):
            raise MigrationError('membership_record_invalid')
        _bytes(value)
    if len({v['verification_key_digest'] for v in members.values()}) != len(members):
        raise MigrationError('duplicate_identity_rejected')

def _target(request: dict, baseline: dict, now: float) -> dict:
    profiles = request['profiles']
    nodes = [segment(envelope.get('profile', {}).get('node_id'), 'migration_node_id_invalid')
             for envelope in profiles if isinstance(envelope, dict) and isinstance(envelope.get('profile'), dict)]
    if len(set(nodes)) != len(nodes):
        raise MigrationError('duplicate_identity_rejected')
    if len(profiles) != len(baseline['members']):
        raise MigrationError('migration_inventory_mismatch')
    members = {key: dict(value) for key, value in baseline['members'].items()}
    seen: set[str] = set()
    for envelope in profiles:
        # Validate envelope's declared node ID only to select the durable pinned key.
        # That value gains no authority until verify_peer_profile authenticates it.
        if not isinstance(envelope, dict) or not isinstance(envelope.get('profile'), dict):
            raise MigrationError('profile_envelope_invalid')
        node = segment(envelope['profile'].get('node_id'), 'migration_node_id_invalid')
        if node in seen:
            raise MigrationError('duplicate_identity_rejected')
        seen.add(node)
        if node not in members:
            raise MigrationError('migration_unknown_member')
        current = members[node]
        profile = verify_peer_profile(envelope, now=now,
                                      expected_key_digest=current['verification_key_digest'],
                                      expected_node_id=node)
        legacy = LegacyMemberIdentity(node_id=node, verification_key_digest=current['verification_key_digest'],
            incarnation=current['incarnation'], generation=current['generation'],
            lease_expires_at=current['lease_expires_at'], revoked=current['revoked'],
            software_build_digest=current['software_build_digest'])
        decision = evaluate_identity_continuity(legacy, ContinuityEvidence(
            declared_node_id=profile.node_id, signer_key_digest=current['verification_key_digest'],
            signature_valid=True, new_incarnation=profile.incarnation,
            profile_binds_generation=profile.membership_generation - 1,
            software_build_digest=profile.software_build_digest), now=now)
        if decision.state != 'preserved' or profile.membership_generation != decision.committed_generation:
            raise MigrationError('continuity_rejected')
        if (profile.software_build_digest != current['software_build_digest']
                or profile.lease_expires_at < now or profile.issued_at > now
                or profile.lease_expires_at > profile.expires_at
                or profile.lease_expires_at > current['lease_expires_at']):
            raise MigrationError('profile_build_or_lease_mismatch')
        current.update(generation=decision.committed_generation, incarnation=profile.incarnation,
                       lease_expires_at=profile.lease_expires_at)
    return {'protocol': baseline['protocol'], 'members': members}

def _verify_published(payload: dict, state: dict, now: float) -> None:
    """Recheck signed post-state when recovering the rename→journal crash gap."""
    if len(payload['profiles']) != len(state['members']):
        raise MigrationError('migration_inventory_mismatch')
    seen: set[str] = set()
    for envelope in payload['profiles']:
        if not isinstance(envelope, dict) or not isinstance(envelope.get('profile'), dict):
            raise MigrationError('profile_envelope_invalid')
        node = segment(envelope['profile'].get('node_id'), 'migration_node_id_invalid')
        if node in seen or node not in state['members']:
            raise MigrationError('duplicate_or_unknown_identity')
        seen.add(node)
        member = state['members'][node]
        profile = verify_peer_profile(envelope, now=now,
            expected_key_digest=member['verification_key_digest'], expected_node_id=node)
        if (profile.membership_generation != member['generation']
                or profile.incarnation != member['incarnation']
                or profile.software_build_digest != member['software_build_digest']
                or profile.lease_expires_at != member['lease_expires_at']):
            raise MigrationError('published_state_mismatch')

class MigrationExecutor:
    def __init__(self, storage: FileMembershipState):
        if not isinstance(storage, FileMembershipState):
            raise MigrationUnavailable('membership_storage_adapter_required')
        self.storage = storage

    def checkpoint(self) -> str | None:
        with self.storage.lock():
            found = self.storage._read('journal.json')
            return found[0]['checkpoint'] if found else None

    def execute(self, request: MigrationRequest, *, now: float, stop_after: str | None = None) -> str:
        if not isinstance(request, MigrationRequest):
            raise MigrationUnavailable('verified_migration_request_required')
        if stop_after is not None and stop_after not in STAGES:
            raise MigrationError('checkpoint_invalid')
        payload = request.payload()
        request_digest = _hash(_bytes(payload))
        finite(now, 'peer_profile_now_invalid', minimum=0.0)
        with self.storage.lock():
            baseline, raw = self.storage._snapshot()
            existing = self.storage._read('journal.json')
            if existing:
                journal = existing[0]
                fields = {'protocol', 'request', 'request_digest', 'source_digest', 'target_digest', 'checkpoint'}
                # Older local journals may still resume against their unchanged source,
                # but cannot prove a published target's seed-owned lease after expiry.
                if set(journal) not in (fields, fields | {'source_state', 'validated_at'}) or journal['protocol'] != 'mycelium.private_migration_journal.v1':
                    raise MigrationError('journal_invalid')
                if journal['request_digest'] != _hash(_bytes(journal['request'])) or journal['request_digest'] != request_digest:
                    raise MigrationError('changed_retry_rejected')
                source = journal['source_digest']; target_digest = journal['target_digest']
                if journal['checkpoint'] not in (*STAGES, 'rolled_back'):
                    raise MigrationError('journal_checkpoint_invalid')
                if _hash(raw) == target_digest and journal['checkpoint'] in ('commit_pending', 'committed'):
                    # Crash after replace but before committed checkpoint: terminalize.
                    if 'source_state' not in journal:
                        raise MigrationError('migration_source_proof_missing')
                    source_state = journal['source_state']
                    if (not isinstance(source_state, dict)
                            or set(source_state) != {'protocol', 'members'}
                            or source_state['protocol'] != baseline['protocol']):
                        raise MigrationError('journal_source_invalid')
                    _validate_members(source_state['members'])
                    if _hash(_bytes(source_state)) != source:
                        raise MigrationError('journal_source_changed')
                    validated_at = finite(journal['validated_at'], 'journal_validation_time_invalid', minimum=0.0)
                    if validated_at > now:
                        raise MigrationError('journal_validation_time_invalid')
                    if _hash(_bytes(_target(payload, source_state, validated_at))) != target_digest:
                        raise MigrationError('migration_target_changed')
                    _verify_published(payload, baseline, validated_at)
                    journal['checkpoint'] = 'committed'
                    self.storage._write('journal.json', journal)
                    return 'committed'
                if _hash(raw) != source:
                    raise MigrationError('membership_concurrent_change')
                if journal['checkpoint'] == 'committed':
                    raise MigrationError('committed_state_missing')
                checkpoint = journal['checkpoint']
            else:
                target = _target(payload, baseline, now)  # reject before first journal byte
                source = _hash(raw); target_digest = _hash(_bytes(target))
                journal = {'protocol': 'mycelium.private_migration_journal.v1',
                           'request': payload, 'request_digest': request_digest,
                           'source_digest': source, 'target_digest': target_digest,
                           'source_state': baseline, 'validated_at': now,
                           'checkpoint': 'profiles_signed'}
                self.storage._write('journal.json', journal)
                checkpoint = 'profiles_signed'
            # Revalidate signatures and continuity at every restart. The journal
            # cannot turn an unsigned assertion into authority.
            target = _target(payload, baseline, now)
            if _hash(_bytes(target)) != target_digest:
                raise MigrationError('migration_target_changed')
            if stop_after == checkpoint:
                raise MigrationUnavailable('migration_checkpoint_interrupted')
            for stage in STAGES[1:]:
                if checkpoint in STAGES and STAGES.index(checkpoint) >= STAGES.index(stage):
                    continue
                if stage == 'committed':
                    self.storage._write('members.json', target)
                journal['checkpoint'] = stage
                self.storage._write('journal.json', journal)
                if stop_after == stage:
                    raise MigrationUnavailable('migration_checkpoint_interrupted')
            return 'committed'

    def rollback(self) -> str:
        with self.storage.lock():
            found = self.storage._read('journal.json')
            if not found:
                raise MigrationError('migration_not_started')
            journal = found[0]
            if journal['checkpoint'] in ('committed', 'rolled_back'):
                raise MigrationError('rollback_not_available')
            _, raw = self.storage._snapshot()
            if _hash(raw) != journal['source_digest']:
                raise MigrationError('rollback_not_available')
            journal['checkpoint'] = 'rolled_back'
            self.storage._write('journal.json', journal)
            return 'rolled_back'
