# SPDX-License-Identifier: AGPL-3.0-only
"""A failed reconnect must leave the rejected statement's window in the run dir.

2026-09-16 65r1 failed on `seed_http_seed_time_invalid` and left nothing but the
code: the tunnels were dead hours later and the identity statement that was
rejected is never returned to the caller, so the cause had to be reconstructed by
reading source (four hypotheses, none provable from disk). This pins that the
evidence is written before the error propagates.
"""
import threading

import pytest

from mycelium_a13_macos.endpoint_recovery import endpoint_card, reconnect_peer
from mycelium_node.durable_membership import load_membership_state, save_membership_state
from mycelium_qualification.signing import generate_ed25519_signer
from mycelium_seed.http import SeedHTTPError

OLD, NEW = 'https://old.example.com', 'https://new.example.com'


class _Client:
    def identity(self, *, now):
        error = SeedHTTPError('seed_http_seed_time_invalid')
        error.window = {'now': 1000.0, 'issued_at': 1003.0, 'expires_at': 1063.0,
                        'seconds_before_issued': 3.0, 'seconds_after_expires': -63.0}
        raise error


class _Peer:
    def __init__(self, root):
        self.state_root = root
        self._lock = threading.RLock()
        self.stopped = False

    def _clock(self):
        return 1000.0

    def _make_client(self, *, membership):
        assert membership['seed_url'] == NEW
        return _Client()

    def stop(self):
        self.stopped = True


def _peer_with_membership(tmp_path):
    signer = generate_ed25519_signer(endpoint_id='recovery-endpoint')
    peer = _Peer(tmp_path)
    save_membership_state(tmp_path, {
        'protocol': 'mycelium.node.membership_state.v1',
        'node_id': 'remote-mac-recovery', 'swarm_id': 'swarm-recovery',
        'seed_node_id': 'seed-recovery', 'seed_url': OLD,
        'seed_key_digest': signer.verification_key_digest,
        'seed_key_records': [signer.public_key_record()],
        'endpoint_id': signer.endpoint_id, 'incarnation': 'inc-1',
        'membership_generation': 1, 'restart_count': 0,
    })
    card = endpoint_card(signer=signer, swarm_id='swarm-recovery',
                         seed_node_id='seed-recovery', previous_origin=OLD,
                         public_origin=NEW, now=1000.0)
    return peer, card


def test_a_rejected_identity_statement_is_persisted_before_the_error_escapes(tmp_path):
    peer, card = _peer_with_membership(tmp_path)
    with pytest.raises(SeedHTTPError, match='seed_http_seed_time_invalid'):
        reconnect_peer(peer, card=card, acknowledged=True)

    from mycelium_a13_macos.authority import _load_private_json
    record = _load_private_json(tmp_path / 'endpoint-recovery-diagnostic.json')
    assert record is not None
    assert record['code'] == 'seed_http_seed_time_invalid'
    assert record['public_origin'] == NEW
    assert record['window']['issued_at'] == 1003.0
    assert record['window']['seconds_before_issued'] == 3.0
    # The relocation must NOT have been persisted: the peer never trusted NEW.
    assert load_membership_state(tmp_path)['seed_url'] == OLD
    assert peer.stopped is False


def test_a_successful_reconnect_writes_no_diagnostic(tmp_path):
    peer, card = _peer_with_membership(tmp_path)

    class _Good(_Client):
        def identity(self, *, now):
            return {'seed_url': NEW, 'swarm_id': 'swarm-recovery',
                    'seed_node_id': 'seed-recovery'}

    peer._make_client = lambda *, membership: _Good()
    peer.start = lambda: {'started': True}
    reconnect_peer(peer, card=card, acknowledged=True)
    from mycelium_a13_macos.authority import _load_private_json
    assert _load_private_json(tmp_path / 'endpoint-recovery-diagnostic.json') is None
    assert load_membership_state(tmp_path)['seed_url'] == NEW
