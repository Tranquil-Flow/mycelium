# SPDX-License-Identifier: AGPL-3.0-only
"""A visitor can stop their own prompt: a requester-signed cancel, bound to the request id.

FAKE (labelled): ``CancelChat`` is a duck-typed ChatService stand-in that records cancels; the guard, ledger,
gateway and wire documents are the shipped code.  The browser end is in test_hosted_ui_browser.py.
"""
import json

import pytest

from mycelium_swarm_directory import docs
from mycelium_swarm_directory.crypto import Ed25519Signer, verify
from mycelium_swarm_directory.entry import EntryLimits, ROUTES
from mycelium_swarm_directory.wire import WireError

from test_entry_guard import FakeChat, grant_for, send, world  # noqa: F401  (fixture re-export)

PREFIX = '/swarm-entry/v1/'
SWARM = 'sw-aaaaaaaaaaaaaaaaaaaa'
RID = 'r' * 24
REASON = 'cancelled_by_requester'


class CancelChat(FakeChat):
    """FAKE: records cancels; ``finished`` makes cancel answer False like a request that already ended."""

    def __init__(self):
        super().__init__()
        self.cancelled = []
        self.finished = False

    def cancel(self, request_id, code='chat_cancelled'):
        self.cancelled.append((request_id, code))
        return not self.finished


def cancel(world, requester, request_id=RID, *, client='ip:1', **over):
    document = docs.entry_cancel(requester, over.pop('swarm_id', SWARM), request_id, at=over.pop('at', world['clock'].t),
                                 **over)
    return world['gateway'].handle(PREFIX + 'cancel', json.dumps(document).encode(), client)


@pytest.fixture
def started(world):
    """Carol has a prompt in flight (request id RID)."""
    chat = CancelChat()
    world['gateway'].chat = lambda: chat
    carol = Ed25519Signer.generate()
    grant = grant_for(world, carol)
    assert send(world, carol, grant)[0] == 200
    return {**world, 'chat': chat, 'carol': carol, 'grant': grant}


def test_cancel_is_a_closed_route_and_a_closed_document():
    assert 'cancel' in ROUTES
    assert docs.ENTRY_CANCEL == 'mycelium.swarm.entry_cancel.v1'
    carol = Ed25519Signer.generate()
    document = docs.entry_cancel(carol, SWARM, RID, at=1_800_000_000)
    assert set(document) == {'protocol', 'swarm_id', 'key', 'request_id', 'at', 'nonce', 'sig'}
    assert docs.check(document, docs.ENTRY_CANCEL, verify) == document


@pytest.mark.parametrize('bad', ['short', 'a/b' + 'c' * 20, 'x' * 65, '', 7, None])
def test_a_malformed_request_id_is_refused_in_the_document(bad):
    carol = Ed25519Signer.generate()
    document = docs.entry_cancel(carol, SWARM, bad, at=1_800_000_000)
    with pytest.raises(WireError) as raised:
        docs.check(document, docs.ENTRY_CANCEL, verify)
    assert raised.value.code == 'entry_request_invalid'


def test_the_requester_who_sent_the_prompt_cancels_it(started):
    status, value = cancel(started, started['carol'])
    assert (status, value) == (200, {'cancelled': True, 'request_id': RID})
    assert started['chat'].cancelled == [(RID, REASON)]


def test_another_key_cannot_cancel_it_not_even_the_swarm_owner(started):
    mallory = Ed25519Signer.generate()
    assert cancel(started, mallory) == (403, {'error': 'entry_cancel_not_requester'})
    assert cancel(started, started['owner']) == (403, {'error': 'entry_cancel_not_requester'})
    assert started['chat'].cancelled == []


def test_an_unknown_request_id_is_refused_before_the_backend(started):
    assert cancel(started, started['carol'], 'q' * 24) == (404, {'error': 'entry_request_unknown'})
    assert started['chat'].cancelled == []


def test_a_cancel_signed_for_one_request_cannot_be_pointed_at_another(started):
    document = docs.entry_cancel(started['carol'], SWARM, 'q' * 24, at=started['clock'].t)
    document['request_id'] = RID  # the id is inside the signature
    status, value = started['gateway'].handle(PREFIX + 'cancel', json.dumps(document).encode(), 'ip:1')
    assert status in (400, 403) and value == {'error': 'signature_invalid'}
    assert started['chat'].cancelled == []


def test_replay_stale_and_foreign_swarm_are_refused(started):
    document = docs.entry_cancel(started['carol'], SWARM, RID, at=started['clock'].t)
    raw = json.dumps(document).encode()
    assert started['gateway'].handle(PREFIX + 'cancel', raw, 'ip:1')[0] == 200
    assert started['gateway'].handle(PREFIX + 'cancel', raw, 'ip:1') == (409, {'error': 'entry_nonce_replayed'})
    assert started['chat'].cancelled == [(RID, REASON)]
    assert cancel(started, started['carol'], at=started['clock'].t - 3600) == (400, {'error': 'entry_clock_skew'})
    assert cancel(started, started['carol'], swarm_id='sw-bbbbbbbbbbbbbbbbbbbb') == (404, {'error': 'entry_swarm_mismatch'})


def test_cancelling_something_that_already_ended_says_so(started):
    started['chat'].finished = True
    assert cancel(started, started['carol']) == (200, {'cancelled': False, 'request_id': RID})


def test_a_backend_without_cancel_is_a_closed_error_not_a_crash(world):
    carol = Ed25519Signer.generate()
    assert send(world, carol, grant_for(world, carol))[0] == 200  # plain FakeChat: no cancel
    assert cancel(world, carol) == (501, {'error': 'entry_cancel_unsupported'})


def test_stopping_your_own_prompt_needs_no_grant_and_no_send_budget(started):
    started['guard'].limits = EntryLimits(per_key_per_minute=1)
    assert send(started, started['carol'], started['grant'])[1] == {'error': 'entry_rate_limited_key'}  # key budget spent
    started['ledger'].revoke(started['grant']['grant_id'])
    assert cancel(started, started['carol'])[0] == 200


def test_cancels_have_their_own_per_address_budget(started):
    started['guard'].limits = EntryLimits(cancels_per_ip_per_minute=2)
    statuses = [cancel(started, started['carol'], client='ip:9')[0] for _ in range(3)]
    assert statuses == [200, 200, 429]
    assert cancel(started, started['carol'], client='ip:10')[0] == 200
