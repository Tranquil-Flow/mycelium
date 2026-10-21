# SPDX-License-Identifier: AGPL-3.0-only
"""Phase 3 / PV33: real signed documents, controlled clock, durable retention."""
import pytest
from test_directory_core import Clock, call, record
from mycelium_swarm_directory import docs
from mycelium_swarm_directory.crypto import Ed25519Signer, verify
from mycelium_swarm_directory.directory import Directory, Limits
from mycelium_swarm_directory.store import SqliteStore

SID = 'sw-aaaaaaaaaaaaaaaaaaaa'

@pytest.fixture
def world(tmp_path):
    clock = Clock()
    store = SqliteStore(tmp_path / 'directory.sqlite3')
    directory = Directory(store, verify, clock=clock)
    owner, visitor = Ed25519Signer.generate(), Ed25519Signer.generate()
    assert call(directory, 'POST', '/records', record(owner))[0] == 200
    yield directory, clock, owner, visitor
    store.close()


def test_decline_cooldown_survives_restart_and_expires_at_boundary(world):
    d, clock, owner, visitor = world
    ask = lambda: docs.access_request(visitor, SID, note='hello', at=clock.t)
    assert call(d, 'POST', '/access-requests', ask())[0] == 200
    assert call(d, 'POST', '/deny', docs.deny(owner, SID, visitor.public_key, at=clock.t))[0] == 200
    d = Directory(d.store, verify, clock=clock)
    assert call(d, 'POST', '/access-requests', ask()) == (429, {'error': 'request_cooldown'})
    mine = call(d, 'POST', '/my-grants', docs.grantee_query(visitor, at=clock.t))[1]
    assert mine['cooldowns'][SID] == clock.t + 86400
    clock.t += 86399
    assert call(d, 'POST', '/access-requests', ask())[0] == 429
    clock.t += 1
    assert call(d, 'POST', '/access-requests', ask())[0] == 200


def test_model_whitelist_rejects_signed_listing_but_reads_old_record(world):
    d, clock, owner, visitor = world
    old = record(owner, seq=2, model='Unknown/typo')
    assert call(d, 'POST', '/records', old) == (400, {'error': 'model_not_registered'})
    row = d.store.get('swarm', SID)
    row['record'] = old
    d.store.put('swarm', SID, row)
    assert call(d, 'GET', '/swarms/' + SID)[1]['swarm']['model'] == 'Unknown/typo'
    assert call(d, 'POST', '/access-requests', docs.access_request(visitor, SID, note='', at=clock.t)) == (400, {'error': 'model_not_registered'})
    assert call(d, 'POST', '/records', record(owner, seq=3))[0] == 200


def test_offline_hide_delist_and_owner_republish_are_reversible(world):
    d, clock, owner, _ = world
    assert d.list_swarms()[0]['listing_state'] == 'listed'
    clock.t += 86400
    assert d.list_swarms()[0]['listing_state'] == 'hidden'
    clock.t += 13 * 86400
    assert d.list_swarms() == []
    # Direct links and signed owner queries retain the record; no tombstone.
    assert call(d, 'GET', '/swarms/' + SID)[1]['swarm']['listing_state'] == 'delisted'
    assert call(d, 'POST', '/owner-inbox', docs.owner_query(owner, SID, at=clock.t))[0] == 200
    assert call(d, 'POST', '/records', record(owner)) == (409, {'error': 'record_seq_stale'})
    assert call(d, 'POST', '/records', record(owner, seq=2))[0] == 200
    assert d.list_swarms()[0]['listing_state'] == 'listed'


def test_offline_heartbeats_do_not_reset_retention_but_live_ones_restore(world):
    d, clock, owner, _ = world
    def beat(status):
        return call(d, 'POST', '/heartbeat', docs.heartbeat(owner, SID, status=status, members=1,
                    served_model='Qwen/Qwen3-0.6B', at=clock.t))
    assert beat('serving')[0] == 200
    clock.t += 91
    assert d.list_swarms()[0]['offline_seconds'] == 1
    clock.t += 86400
    assert beat('offline')[0] == 200
    assert d.list_swarms()[0]['listing_state'] == 'hidden'
    clock.t += 14 * 86400
    assert beat('offline')[0] == 200
    assert d.list_swarms() == []
    clock.t += 1
    assert beat('serving')[0] == 200
    assert d.list_swarms()[0]['listing_state'] == 'listed'


def test_retention_thresholds_are_configurable(world):
    d, clock, owner, _ = world
    d = Directory(d.store, verify, clock=clock, limits=Limits(offline_hide_seconds=5, offline_delist_seconds=10))
    clock.t += 5
    assert d.list_swarms()[0]['listing_state'] == 'hidden'
    clock.t += 5
    assert not d.list_swarms()


def test_cli_retention_config_and_invalid_order(tmp_path):
    from mycelium_swarm_directory.server import build_parser, build_directory
    args=build_parser().parse_args(['--db',str(tmp_path/'configured.sqlite3'),'--offline-hide-seconds','60',
                                  '--offline-delist-seconds','120','--decline-cooldown-seconds','30'])
    d=build_directory(args,environ={})
    try:
        assert (d.limits.offline_hide_seconds,d.limits.offline_delist_seconds,d.limits.decline_cooldown_seconds)==(60,120,30)
        with pytest.raises(ValueError,match='listing_retention_invalid'):
            Directory(d.store,verify,limits=Limits(offline_hide_seconds=120,offline_delist_seconds=60))
    finally:
        d.store.close()


def test_retained_listing_owner_is_recoverable_only_to_original_key(world):
    d,clock,owner,visitor=world
    clock.t+=14*86400
    assert d.list_swarms()==[]
    mine=call(d,'POST','/my-grants',docs.grantee_query(owner,at=clock.t))[1]
    assert [r['swarm_id'] for r in mine['owned_swarms']]==[SID]
    other=call(d,'POST','/my-grants',docs.grantee_query(visitor,at=clock.t))[1]
    assert other['owned_swarms']==[]
    assert call(d,'POST','/records',record(visitor,seq=2))==(403,{'error':'swarm_owner_mismatch'})
    assert d.list_swarms()==[]


def test_record_edit_while_online_does_not_count_online_time_as_offline(world):
    d,clock,owner,_=world
    beat=docs.heartbeat(owner,SID,status='serving',members=1,served_model='Qwen/Qwen3-0.6B',at=clock.t)
    assert call(d,'POST','/heartbeat',beat)[0]==200
    clock.t+=30
    assert call(d,'POST','/records',record(owner,seq=2,name='Renamed'))[0]==200
    clock.t+=61
    assert d.list_swarms()[0]['offline_seconds']==1


def test_owner_inbox_includes_direct_grants_without_exposing_to_strangers(world):
    d, clock, owner, visitor = world
    grant = docs.grant(owner, swarm_id=SID, grantee_key=visitor.public_key, issued_at=clock.t,
                       expires_at=clock.t+3600, max_requests=20, max_tokens=4000)
    assert call(d, 'POST', '/grants', grant)[0] == 200
    inbox = call(d, 'POST', '/owner-inbox', docs.owner_query(owner, SID, at=clock.t))[1]
    assert inbox['grants'] == [grant]
    assert call(d, 'POST', '/owner-inbox', docs.owner_query(visitor, SID, at=clock.t))[0] == 403
