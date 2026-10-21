# SPDX-License-Identifier: AGPL-3.0-only
"""V1.2 D1 (part 2): a stage offer left by a member identity that no longer exists does not block the next run.

A13 app-flow gate (V1.1 candidate): a member app restarted while its previous stage offer awaited cleanup.  The restart
is a re-admission (newer membership generation and incarnation); the offer, bound to the old identity, can no longer be
read, cleaned or retired by anyone (``_current_offer`` and ``_member_guard`` compare the whole binding), so the owner's
next ``publish_offer`` for that node answered ``offer_already_pending`` until the offer's TTL (up to 300 s) ran out.

A newer identity of the same node supersedes the stale offer: the replaced offer is kept in the bounded history as
``superseded`` -- never as a cleanup acknowledgement.  The same identity, or an older one, still gets
``offer_already_pending``.
"""
import json
import sqlite3

import pytest

from mycelium_a13_macos.managed_control import ManagedControlError
from test_control_pump import _world
from test_managed_control import RUN_ID, DEPLOYMENT_ID


def _publish(box, member, archive=b'bytes'):
    return box.publish_offer(target=member, run_id=RUN_ID, deployment_id=DEPLOYMENT_ID, archive=archive, ttl_seconds=60)


def _age_stored_offer(box, node_id, *, generations=-1, incarnation='remote-incarnation-old'):
    """Rewrite the stored offer's binding as if an earlier identity had published it."""
    with sqlite3.connect(str(box.database)) as db:
        (metadata,) = db.execute('SELECT metadata FROM managed_stage_offers WHERE node_id=?', (node_id,)).fetchone()
        offer = json.loads(metadata)
        offer['membership_generation'] += generations
        if incarnation is not None:
            offer['member_incarnation'] = incarnation
        db.execute('UPDATE managed_stage_offers SET metadata=? WHERE node_id=?', (json.dumps(offer, sort_keys=True), node_id))
    return offer


def _history(box):
    with sqlite3.connect(str(box.database)) as db:
        return [json.loads(row[0]) for row in db.execute('SELECT receipt FROM managed_stage_history')]


def test_an_offer_of_an_older_identity_is_superseded_and_recorded_not_cleaned(tmp_path):
    with _world(tmp_path / 'world') as w:
        box = w['authority']._control_mailbox
        node = w['member']['node_id']
        _publish(box, w['member'], b'first')
        stale = _age_stored_offer(box, node, generations=-1)
        fresh = _publish(box, w['member'], b'second')               # must not raise offer_already_pending
        assert box.pending_offer(w['member']) == fresh
        receipts = _history(box)
        assert len(receipts) == 1
        receipt = receipts[0]
        assert receipt['offer'] == stale
        assert receipt['superseded'] is True and receipt['cleanup_ack'] is None   # never a cleanup claim
        assert receipt['superseded_by'] == {'membership_generation': w['member']['generation'],
                                            'member_incarnation': w['member']['incarnation']}


def test_a_superseded_offer_cannot_be_retired_as_cleaned(tmp_path):
    with _world(tmp_path / 'world') as w:
        box = w['authority']._control_mailbox
        node = w['member']['node_id']
        _publish(box, w['member'], b'first')
        stale = _age_stored_offer(box, node, generations=-1)
        _publish(box, w['member'], b'second')
        with pytest.raises(ManagedControlError):
            box.retire_offer(stale)


def test_the_same_identity_still_cannot_replace_a_live_offer(tmp_path):
    with _world(tmp_path / 'world') as w:
        box = w['authority']._control_mailbox
        _publish(box, w['member'], b'first')
        with pytest.raises(ManagedControlError, match='offer_already_pending'):
            _publish(box, w['member'], b'second')
        assert _history(box) == []


def test_an_offer_of_a_newer_identity_is_never_replaced_by_an_older_one(tmp_path):
    with _world(tmp_path / 'world') as w:
        box = w['authority']._control_mailbox
        node = w['member']['node_id']
        _publish(box, w['member'], b'first')
        _age_stored_offer(box, node, generations=+1, incarnation='remote-incarnation-newer')
        with pytest.raises(ManagedControlError, match='offer_already_pending'):
            _publish(box, w['member'], b'second')
        assert _history(box) == []


def test_a_full_history_still_fails_closed_for_a_superseded_offer(tmp_path, monkeypatch):
    with _world(tmp_path / 'world') as w:
        box = w['authority']._control_mailbox
        node = w['member']['node_id']
        _publish(box, w['member'], b'first')
        _age_stored_offer(box, node, generations=-1)
        with sqlite3.connect(str(box.database)) as db:
            db.executemany('INSERT INTO managed_stage_history (offer_id, receipt) VALUES (?, ?)',
                           [(f'filler-{i}', '{}') for i in range(256)])
        with pytest.raises(ManagedControlError, match='offer_history_capacity_exhausted'):
            _publish(box, w['member'], b'second')
