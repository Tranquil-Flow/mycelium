# SPDX-License-Identifier: AGPL-3.0-only
"""SQLite retention/replay tests; no server, process, model or device."""
from contextlib import closing
import sqlite3
import pytest
from mycelium_a13_macos.managed_control import ManagedControlMailbox, ManagedControlError
from mycelium_qualification.signing import generate_ed25519_signer
from mycelium_seed.coordinator import SeedCoordinator
from mycelium_invite.sqlite_registry import SqliteInviteRegistry


@pytest.fixture
def mailbox(tmp_path):
    coordinator = SeedCoordinator(swarm_id='swarm', seed_node_id='owner', incarnation='owner-incarnation',
        seed_url='https://localhost:9999',
        signer=generate_ed25519_signer(endpoint_id='owner'),
        invite_registry=SqliteInviteRegistry(tmp_path/'seed.sqlite3'))
    m = ManagedControlMailbox(tmp_path/'control'/'mailbox.sqlite3', coordinator=coordinator, clock=lambda:1000.)
    yield m
    m.close()


def insert(m, db, sequence, *, signer='signer', at=None):
    at = float(sequence if at is None else at)
    statement = dict(request_id=f'request-{sequence}', nonce=f'nonce-{sequence}', sequence=sequence,
        protocol='poll', issued_at=at, expires_at=at+30)
    assert m._cached_or_guard(db, signer_digest=signer, statement=statement,
        request_digest=f'digest-{sequence}') is None
    m._record_auth(db, signer_digest=signer, statement=statement, request_digest=f'digest-{sequence}',
        response={'statement': {'issued_at': at}})
    return statement


def test_long_poll_stream_is_bounded_and_old_signed_replays_stay_rejected(mailbox):
    m = mailbox
    with closing(m._connect()) as db:
        db.execute('BEGIN IMMEDIATE')
        old = insert(m, db, 1)
        for sequence in range(2, 4001): last = insert(m, db, sequence)
        db.commit()
        assert db.execute('SELECT count(*) FROM managed_auth_requests').fetchone()[0] <= 512
        assert db.execute('SELECT 1 FROM managed_auth_requests WHERE sequence=1').fetchone() is None
        for statement in (old, {**old, 'request_id':'other'}, {**old, 'nonce':'different'}):
            with pytest.raises(ManagedControlError, match='sequence_replayed'):
                m._cached_or_guard(db, signer_digest='signer', statement=statement, request_digest='old')
        assert m._cached_or_guard(db, signer_digest='signer', statement=last,
            request_digest='digest-4000') == {'statement': {'issued_at':4000.}}
        with pytest.raises(ManagedControlError, match='conflicting_retry'):
            m._cached_or_guard(db, signer_digest='signer', statement=last, request_digest='changed')
        with pytest.raises(ManagedControlError, match='nonce_replayed'):
            m._cached_or_guard(db, signer_digest='signer', statement={**last, 'request_id':'new', 'sequence':4001},
                request_digest='changed')
        # Re-signing with a NEW sequence cannot reuse a pruned nonce or id.
        with pytest.raises(ManagedControlError, match='nonce_replayed'):
            m._cached_or_guard(db, signer_digest='signer',
                statement={**old, 'request_id':'fresh', 'sequence':5000}, request_digest='fresh')
        with pytest.raises(ManagedControlError, match='conflicting_retry'):
            m._cached_or_guard(db, signer_digest='signer',
                statement={**old, 'nonce':'fresh', 'sequence':5000}, request_digest='fresh')
    # Reopening cannot reset the retained MAX(sequence).
    reopened = ManagedControlMailbox(m.database, coordinator=m.coordinator)
    with closing(reopened._connect()) as db:
        with pytest.raises(ManagedControlError, match='sequence_replayed'):
            reopened._cached_or_guard(db, signer_digest='signer', statement=old, request_digest='old')
    reopened.close()


def test_unexpired_retry_window_survives_sequence_pressure_and_empty_poll_update(mailbox):
    m = mailbox
    with closing(m._connect()) as db:
        db.execute('BEGIN IMMEDIATE')
        first = insert(m, db, 1, at=100)
        for sequence in range(2, 800): insert(m, db, sequence, at=100)
        m._replace_auth_response(db, signer_digest='signer', statement=first, request_digest='digest-1',
            response={'delivered':'command'})
        assert m._cached_or_guard(db, signer_digest='signer', statement=first,
            request_digest='digest-1') == {'delivered':'command'}
        db.rollback()


def command(db, name, run, state, updated):
    db.execute('INSERT INTO managed_commands (command_id,envelope_digest,envelope_json,target_node_id,'
        'target_endpoint_id,target_key_digest,membership_generation,member_incarnation,run_id,deployment_id,'
        'owner_sequence,owner_nonce,command_expires_at,state,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
        (name,'digest','{}','node','endpoint','key',1,'incarnation',run,'deployment',1,'nonce',100,state,updated))


def test_only_finished_old_terminal_commands_are_pruned(mailbox):
    m=mailbox
    with closing(m._connect()) as db:
        command(db,'old','finished','completed',100)
        command(db,'live','live','completed',100)
        command(db,'unresolved','finished','reconciliation_required',100)
        command(db,'pending','finished','pending',100)
        command(db,'recent','finished','failed',99999)
    m.finish_run(run_id='finished', deployment_id='deployment')
    m.prune(now=100000)
    with closing(m._connect()) as db:
        assert {r[0] for r in db.execute('SELECT command_id FROM managed_commands')} == {
            'live','unresolved','pending','recent'}


def test_control_poll_scan_uses_binding_index(mailbox):
    with closing(mailbox._connect()) as db:
        plan = db.execute("EXPLAIN QUERY PLAN SELECT * FROM managed_commands WHERE target_node_id=? "
            "AND target_endpoint_id=? AND target_key_digest=? AND membership_generation=? AND member_incarnation=? "
            "AND run_id=? AND deployment_id=? AND state='pending' ORDER BY owner_sequence LIMIT 129",
            ('n','e','k',1,'i','r','d')).fetchall()
        assert any('SEARCH' in r[3] for r in plan), [tuple(r) for r in plan]
        assert not any('SCAN managed_commands' in r[3] for r in plan)


def test_capacity_never_evicts_an_unexpired_retry(mailbox, monkeypatch):
    from mycelium_a13_macos import mailbox_retention as retention
    monkeypatch.setattr(retention, 'AUTH_CAPACITY', 300)
    with closing(mailbox._connect()) as db:
        for seq in range(1, 301): insert(mailbox, db, seq, at=100)
        db.execute('BEGIN IMMEDIATE')
        with pytest.raises(ManagedControlError, match='capacity_exhausted'):
            insert(mailbox, db, 301, at=100)
        db.rollback()
        assert db.execute('SELECT count(*) FROM managed_auth_requests').fetchone()[0] == 300


def test_historical_filter_is_bounded_and_collisions_fail_closed(mailbox, monkeypatch):
    from mycelium_a13_macos import mailbox_retention as retention
    monkeypatch.setattr(retention, '_positions', lambda *a: [1,2,3,4])
    with closing(mailbox._connect()) as db:
        retention.historical(db, 'signer', 'nonce', 'old', remember=True)
        assert retention.historical(db, 'signer', 'nonce', 'old')
        with pytest.raises(ManagedControlError, match='nonce_replayed'):
            mailbox._cached_or_guard(db, signer_digest='signer', statement={
                'request_id':'new', 'nonce':'colliding', 'sequence':1}, request_digest='digest')
        assert db.execute('SELECT sum(length(bits)) FROM managed_replay_history').fetchone()[0] == retention.PAGE_BYTES


def test_claim_and_reconciliation_retry_rows_survive_retention(mailbox):
    with closing(mailbox._connect()) as db:
        db.execute('BEGIN IMMEDIATE')
        first = insert(mailbox, db, 1)
        command(db, 'claimed', 'live', 'claimed', 1)
        db.execute("UPDATE managed_commands SET target_key_digest='signer',claimed_request_id='request-1'")
        for seq in range(2, 1000): insert(mailbox, db, seq)
        assert mailbox._cached_or_guard(db, signer_digest='signer', statement=first,
            request_digest='digest-1') is not None
        db.commit()


def test_teardown_only_retires_after_successful_stop(tmp_path):
    from types import SimpleNamespace
    from test_demo_chain import FakeApp, Ops
    from mycelium_a13_macos.demo_chain import AppDemoChain
    app = FakeApp(tmp_path)
    calls = []
    app.authority.control_mailbox.finish_run = lambda **kw: calls.append(kw)
    ops = Ops(app)
    chain = AppDemoChain(app, ops=ops)
    ctx = SimpleNamespace(state={}, result={}, run_id='run', deployment_id='deployment')
    chain.teardown(ctx, failed=False)
    assert calls == [{'run_id':'run','deployment_id':'deployment'}]
    calls.clear()
    def broken(app): raise RuntimeError('cleanup_unknown')
    ops.stop = broken
    chain.teardown(ctx, failed=True)
    assert calls == []


def test_startup_signer_discovery_does_not_walk_legacy_history(mailbox, monkeypatch):
    from mycelium_a13_macos import mailbox_retention as retention
    with closing(mailbox._connect()) as db:
        db.executemany('INSERT INTO managed_auth_requests '
            '(signer_key_digest,request_id,request_digest,sequence,nonce,protocol,response_json) '
            'VALUES (?,?,?,?,?,?,?)',
            [('legacy',str(n),'digest',n,str(n),'poll','{}') for n in range(10000)])
    # Isolate signer discovery from the separately bounded per-signer deletion.
    monkeypatch.setattr(retention,'prune_auth',lambda *a:0)
    connect=mailbox._connect
    steps=[]
    def instrumented():
        db=connect()
        db.set_progress_handler(lambda: steps.append(1) or int(len(steps)>10),100)
        return db
    monkeypatch.setattr(mailbox,'_connect',instrumented)
    mailbox.prune(now=100000)
    assert len(steps)<=10
