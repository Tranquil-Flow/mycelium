# SPDX-License-Identifier: AGPL-3.0-only
"""Owner per-lifetime request quota recovery (durable journal semantics).

Harness nature: real ManagedControlOwner durable journal plus a real signing
ManagedControlClient that is never dialed (base_url https://127.0.0.1:1) with
instance-stubbed enqueue/result server outcomes, mirroring the parent-verified
outputs/a13-macos-evidence/glm-retention-repro.py harness. No mailbox, TLS
listener, model execution, network, or seed state is involved; managed_control
server bytes are read-only for this slice and are NOT exercised here.
"""
import json
import time
from pathlib import Path

import pytest

from mycelium_a13_macos import managed_control as mc
from mycelium_a13_macos.control_session import ManagedControlOwner
from mycelium_a13_macos.authority import _atomic_private_json
from mycelium_qualification.signing import generate_ed25519_signer
from physical_inference_qualification import ControllerError

RUN_ID = 'glm-quota-run'
DEPLOYMENT_ID = 'glm-quota-deployment'


def _world(tmp_path, *, max_commands=8, max_requests=100, node_id='glm-target-quota'):
    signer = generate_ed25519_signer(endpoint_id='glm-owner-endpoint')
    member = {
        'node_id': node_id,
        'endpoint_id': 'endpoint-' + node_id,
        'verification_key_digest': signer.verification_key_digest,
        'generation': 1,
        'incarnation': 'incarnation-1',
        'peer_class': 'mac_mlx_iroh',
        'lifecycle_state': 'NEW',
        'lease_expires_at': time.time() + 3600.0,
    }
    client = mc.ManagedControlClient(base_url='https://127.0.0.1:1',  # never dialed
        signer=signer, trusted_owner_key_record=signer.public_key_record(),
        swarm_id='glm-swarm-quota', owner_node_id='glm-owner-node')
    owner = ManagedControlOwner(tmp_path, client=client,
        current_member=lambda _node_id: member,
        max_commands=max_commands, max_requests=max_requests)
    return member, client, owner


def _rebuild(tmp_path, client, *, max_commands=8, max_requests=100):
    return ManagedControlOwner(tmp_path, client=client,
        current_member=lambda _node_id: _MEMBER[0], max_commands=max_commands,
        max_requests=max_requests)


_MEMBER = []


def _make(tmp_path, **kwargs):
    member, client, owner = _world(tmp_path, **kwargs)
    _MEMBER.clear()
    _MEMBER.append(member)
    return member, client, owner


def _session(owner, member):
    return owner.session(node_id=member['node_id'], run_id=RUN_ID,
        deployment_id=DEPLOYMENT_ID, timeout_seconds=30)


class Server:
    """Instance-level stub of the two owner-facing server operations."""

    def __init__(self, *, enqueue=None, result=None):
        self.enqueue_stub = enqueue
        self.result_stub = result
        self.sequences = []
        self.enqueue_calls = []

    def install(self, client):
        def enqueue(**kwargs):
            self.enqueue_calls.append(kwargs)
            self.sequences.append(kwargs['sequence'])
            outcome = self.enqueue_stub
            if outcome is None:
                return {'state': 'pending'}
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome(**kwargs)

        def result(**kwargs):
            self.sequences.append(kwargs['sequence'])
            outcome = self.result_stub(kwargs) if callable(self.result_stub) else self.result_stub
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        client.enqueue = enqueue
        client.result = result


def _completed(state='completed'):
    return {'state': state, 'native_response': {'ok': True}}


def _pending_then(n, outcomes, final):
    states = iter(['pending'] * n + [final])

    def result(kwargs):
        state = next(states)
        return _completed() if state == 'completed' else {'state': state}

    return result


def test_result_waiting_consumes_no_durable_write_budget(tmp_path):
    member, client, owner = _make(tmp_path, max_requests=8)
    server = Server(result=_pending_then(7, None, 'completed'))
    server.install(client)
    try:
        s = _session(owner, member)
        assert s.send(command_id='wait-1', command='hello', payload={}) == {'ok': True}
        journal = json.loads(owner.path.read_text())
        intents_after_wait = journal['write_intents']
        # Seven result polls must not consume the per-lifetime write budget...
        # ...and a NEW command still succeeds where the pre-fix journal bricked.
        server.result_stub=_completed()  # the first command's finite poll iterator is exhausted
        assert s.send(command_id='wait-2', command='hello', payload={}) == {'ok': True}
        journal = json.loads(owner.path.read_text())
        assert journal['write_intents'] == intents_after_wait + 1 == 2
        assert len(set(server.sequences)) == len(server.sequences)  # never reused
    finally:
        owner.close()


def test_sequence_is_monotonic_and_never_reused_across_restart(tmp_path):
    member, client, owner = _make(tmp_path)
    server = Server(result=_pending_then(70, None, 'completed'))  # crosses one block
    server.install(client)
    try:
        _session(owner, member).send(command_id='mono-1', command='hello', payload={})
        assert len(set(server.sequences)) == len(server.sequences)
        ceiling = json.loads(owner.path.read_text())['sequence_ceiling']
        assert ceiling > max(server.sequences)  # reservation is durable
    finally:
        owner.close()
    server2 = Server(result=_pending_then(3, None, 'completed'))
    server2.install(client)
    owner = _rebuild(tmp_path, client)
    try:
        _session(owner, member).send(command_id='mono-2', command='hello', payload={})
        # Across the restart boundary: strictly monotonic, nothing reused.
        assert min(server2.sequences) > max(server.sequences)
        assert len(set(server.sequences + server2.sequences)) == len(server.sequences + server2.sequences)
    finally:
        owner.close()


def test_write_intents_still_bounded_and_reads_survive_exhaustion(tmp_path):
    member, client, owner = _make(tmp_path, max_requests=8)
    # A valid v2 journal that already consumed six requests leaves two write slots.
    old=json.loads(owner.path.read_text());owner.close()
    old={k:v for k,v in old.items() if k not in {'sequence_ceiling','write_intents'}}
    old.update(protocol='mycelium.a13.owner_control_journal.v2',sequence=6)
    _atomic_private_json(owner.path,old)
    owner=_rebuild(tmp_path,client,max_requests=8)
    per_command = {'c1': _completed(), 'c2': 'pending'}

    def result(kwargs):
        state = per_command[kwargs['native_command']['command_id']]
        if state == 'pending':
            per_command['c2'] = _completed()
            return {'state': 'pending'}
        return state

    server = Server(result=result)
    server.install(client)
    try:
        s = _session(owner, member)
        assert s.send(command_id='c1', command='hello', payload={}) == {'ok': True}
        # A pending result followed by completion must not consume a write slot.
        assert s.send(command_id='c2', command='hello', payload={}) == {'ok': True}
        journal = json.loads(owner.path.read_text())
        assert journal['write_intents'] == 8
        # The bound still exists for NEW write intents.
        with pytest.raises(ControllerError, match='managed_owner_request_limit_exceeded'):
            s.send(command_id='c3', command='hello', payload={})
        journal = json.loads(owner.path.read_text())
        assert 'c3' not in journal['commands']  # refusal precedes any intent persist
        assert [c for c in server.enqueue_calls if c['native_command']['command_id'] == 'c3'] == []
        # Reconcile of already-persisted work survives exhaustion (anti-brick).
        assert owner.reconcile('c1') == {'ok': True}
    finally:
        owner.close()


def test_unresolved_enqueue_still_fences_new_commands(tmp_path):
    member, client, owner = _make(tmp_path)

    def unreachable(**_kwargs):
        raise TimeoutError('connection lost')

    server = Server(enqueue=unreachable, result=_completed())
    server.install(client)
    try:
        s = _session(owner, member)
        with pytest.raises(ControllerError, match='managed_control_reconciliation_required'):
            s.send(command_id='stuck-1', command='hello', payload={})
        with pytest.raises(ControllerError, match='managed_control_reconciliation_required'):
            s.send(command_id='stuck-2', command='hello', payload={})
    finally:
        owner.close()


def test_expired_unknown_enqueue_retains_uncertainty_and_fence_across_restart(tmp_path):
    member,client,owner=_make(tmp_path)
    server=Server(enqueue=mc.ManagedControlError('managed_control_message_expired'),
        result=mc.ManagedControlError('managed_control_command_unknown'))
    server.install(client)
    try:
        s=_session(owner,member)
        with pytest.raises(ControllerError,match='managed_control_reconciliation_required'):
            s.send(command_id='ghost-1',command='hello',payload={})
        row=json.loads(owner.path.read_text())['commands']['ghost-1']
        assert row['enqueue_confirmed'] is False and not row.get('abandoned',False)
        assert row['digest'] and row['enqueue_request']
        with pytest.raises(ControllerError,match='managed_control_reconciliation_required'):
            s.send(command_id='after-ghost',command='hello',payload={})
    finally:owner.close()
    owner=_rebuild(tmp_path,client)
    try:
        with pytest.raises(ControllerError,match='managed_control_reconciliation_required'):
            owner.reconcile('ghost-1')
        current=json.loads(owner.path.read_text())['commands']['ghost-1']
        assert current==row
        with pytest.raises(ControllerError,match='managed_control_reconciliation_required'):
            _session(owner,member).send(command_id='after-restart',command='hello',payload={})
        assert all(call['native_command']['command_id']=='ghost-1' for call in server.enqueue_calls)
    finally:owner.close()


def test_lost_expired_enqueue_recorded_server_side_adopted_without_reissue(tmp_path):
    member, client, owner = _make(tmp_path)
    expired = mc.ManagedControlError('managed_control_message_expired')
    server = Server(enqueue=expired, result=_completed())
    server.install(client)
    try:
        s = _session(owner, member)
        # The enqueue attempt's reply was lost and its TTL lapsed, but the
        # server DID record it: one fresh read-only lookup adopts observed
        # truth; the native command is never reissued.
        assert s.send(command_id='shadow-1', command='hello', payload={}) == {'ok': True}
        assert len(server.enqueue_calls) == 1
        row = json.loads(owner.path.read_text())['commands']['shadow-1']
        assert row['enqueue_confirmed'] is True and 'abandoned' not in row
    finally:
        owner.close()
    owner = _rebuild(tmp_path, client)
    try:
        assert owner.reconcile('shadow-1') == {'ok': True}  # restart adopts again
    finally:
        owner.close()




def test_positive_expiry_recovery_over_real_tls_and_native_worker(tmp_path,monkeypatch):
    from test_control_session import connected,owner as make_owner,session as native_session
    with connected(tmp_path/'real') as (world,worker):
        sender=make_owner(tmp_path/'sender',world)
        enqueue=world['owner_client'].enqueue
        native_send=worker.send
        commands=[];enqueues=[]
        def tracked_native(**kwargs):
            commands.append(kwargs['command_id'])
            return native_send(**kwargs)
        def lost_reply(**kwargs):
            enqueues.append(kwargs['native_command']['command_id'])
            result=enqueue(**kwargs)
            if kwargs['native_command']['command_id']=='expiry-wire-hello':
                # Fault injection at the response boundary AFTER real enqueue.
                raise mc.ManagedControlError('managed_control_message_expired')
            return result
        monkeypatch.setattr(worker,'send',tracked_native)
        monkeypatch.setattr(world['owner_client'],'enqueue',lost_reply)
        try:
            native=native_session(sender,world)
            reply=native.send(command_id='expiry-wire-hello',command='hello',payload={})
            assert reply['ok'] is True and reply['route_ready'] is False
            assert enqueues.count('expiry-wire-hello')==1
            assert commands.count('expiry-wire-hello')==1
            native.close()
            assert native.stop_response['ok'] is True
        finally:sender.close()


def test_v1_journal_migration_preserves_legacy_unknown_body(tmp_path):
    member,client,owner=_make(tmp_path)
    s=_session(owner,member)
    old={'protocol':'mycelium.a13.owner_control_journal.v1',
        'authority':owner._state['authority'],'sequence':9,
        'commands':{'legacy':{'binding':s.binding,'digest':'sha256:'+'a'*64}}}
    owner.close();_atomic_private_json(owner.path,old)
    owner=_rebuild(tmp_path,client)
    try:
        journal=json.loads(owner.path.read_text())
        assert journal['protocol']=='mycelium.a13.owner_control_journal.v3'
        assert journal['sequence_ceiling']>9
        assert journal['commands']['legacy']['enqueue_request'] is None
        with pytest.raises(ControllerError,match='managed_control_legacy_reconciliation_required'):
            owner.reconcile('legacy')
    finally:owner.close()


def test_v2_journal_migrates_without_reset_or_reuse(tmp_path):
    member, client, owner = _make(tmp_path)
    authority = {'swarm_id': client.swarm_id, 'owner_node_id': client.owner_node_id,
                 'key_digest': client.signer.verification_key_digest}
    v2 = {'protocol': 'mycelium.a13.owner_control_journal.v2', 'authority': authority,
          'sequence': 5, 'commands': {}}
    owner.close()
    _atomic_private_json(owner.path,v2)
    owner = _rebuild(tmp_path, client, max_commands=6, max_requests=6)
    server = Server(result=_completed())
    server.install(client)
    try:
        journal = json.loads(owner.path.read_text())
        assert journal['protocol'] == 'mycelium.a13.owner_control_journal.v3'
        assert journal['write_intents'] == 5  # old refusal boundary carried exactly
        assert journal['sequence_ceiling'] > 5  # reserved strictly above old sequence
        assert _session(owner, member).send(command_id='mig-1', command='hello', payload={}) == {'ok': True}
        assert server.sequences[0] > 5  # never reset, never reused
        with pytest.raises(ControllerError, match='managed_owner_request_limit_exceeded'):
            _session(owner, member).send(command_id='mig-2', command='hello', payload={})
    finally:
        owner.close()
