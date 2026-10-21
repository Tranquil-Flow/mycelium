# SPDX-License-Identifier: AGPL-3.0-or-later
"""PV10: a member's signed withdrawal over the member channel.

``planning`` (Leave smoothly starts: the member names the requests still running on it) and ``final`` (the member is
about to stop) are signed with the membership key and verified against the CURRENT member record.  The owner shows
``Left smoothly`` only from a verified final; a member that simply stops answering is ``Left suddenly`` (liveness);
the owner's own claim of "smooth" for a vanished Mac is never possible because the label comes only from this record.
"""
from __future__ import annotations

import copy
import json

import pytest

from mycelium_a13_macos.app import DemoApp
from mycelium_a13_macos.demo_app import ensure_exchange
from mycelium_a13_macos.local_contributor import create_local_contributor
from mycelium_a13_macos.managed_control import ManagedControlError
from mycelium_a13_macos.run_exchange import RunExchange
from test_app_authority import CONSENT
from test_member_state_report import Clock


def notice(generation, *, phase='planning', clock=None, in_flight=(), outcome=None, **changes):
    from mycelium_a13_macos.member_withdrawal import NOTICE
    value = {'protocol': NOTICE, 'phase': phase, 'issued_at': (clock.now if clock else __import__('time').time()),
             'generation': generation, 'in_flight': list(in_flight), 'outcome': outcome}
    value.update(changes)
    return value


@pytest.fixture
def world(tmp_path):
    app = DemoApp(tmp_path / 'owner', check_platform=False).start()
    actor = None
    try:
        app.action('/api/create', {'consent': CONSENT})
        actor = create_local_contributor(app)
        clock = Clock()
        exchange = RunExchange(app.authority.control_mailbox, clock=clock).install()
        yield app, actor, exchange, clock
    finally:
        if actor is not None:
            actor.stop()
        app.close()


def client(actor):
    from mycelium_a13_macos.member_withdrawal import MemberWithdrawalClient
    return MemberWithdrawalClient(actor.peer)


def post_raw(app, envelope):
    from mycelium_a13_macos.member_withdrawal import PATH_WITHDRAW
    return app.authority.control_mailbox.handle_http('POST', PATH_WITHDRAW, envelope)


# -- schema ---------------------------------------------------------------------------------------------------------

def test_notice_schema_is_closed_and_bounded():
    from mycelium_a13_macos.member_withdrawal import MAX_IN_FLIGHT, validate_notice
    assert validate_notice(notice(3, in_flight=['request:a', 'r2']))['phase'] == 'planning'
    assert validate_notice(notice(3, phase='final', outcome='drained'))['outcome'] == 'drained'
    assert validate_notice(notice(3, phase='final', outcome='forced'))['outcome'] == 'forced'
    bad = [
        {**notice(3), 'extra': 1},
        {**notice(3), 'foreground_app': 'Zoom'},
        notice(3, phase='goodbye'),
        notice(3, phase='final'),                                   # a final names how it ended
        notice(3, phase='final', outcome='maybe'),
        notice(3, phase='planning', outcome='drained'),             # planning has no outcome yet
        notice(3, in_flight=['has space']),
        notice(3, in_flight=['x' * 200]),
        notice(3, in_flight=[1]),
        notice(3, in_flight=[f'r{i}' for i in range(MAX_IN_FLIGHT + 1)]),
        notice(3, in_flight=['dup', 'dup']),
        notice(-1), notice(True), notice(3, issued_at=float('nan')),
        {k: v for k, v in notice(3).items() if k != 'issued_at'},
    ]
    for value in bad:
        with pytest.raises(ValueError, match='member_withdrawal_invalid'):
            validate_notice(value)


# -- the owner's record --------------------------------------------------------------------------------------------

def test_signed_final_withdrawal_is_recorded_as_left_smoothly_and_revokes_that_generation(world):
    app, actor, exchange, clock = world
    node, generation = actor.member.node_id, actor.member.generation
    revoked = []
    exchange.withdrawals.set_on_final(lambda n, g: revoked.append((n, g)))
    assert exchange.withdrawals.view(node, generation) is None                       # nothing known: nothing claimed
    ack = client(actor).send(notice(generation, phase='final', outcome='drained', clock=clock))
    assert ack['accepted'] is True and ack['phase'] == 'final' and ack['recorded'] == 'left_smoothly'
    assert revoked == [(node, generation)]                                           # the owner acts on the signed self-revoke
    view = exchange.withdrawals.view(node, generation)
    assert view['kind'] == 'left_smoothly' and view['drained'] is True and view['generation'] == generation
    assert view['line'].startswith('Left smoothly - signed withdrawal received ')
    assert view['signed'] is True


def test_forced_final_is_still_a_signed_withdrawal_but_says_work_was_cut_off(world):
    app, actor, exchange, clock = world
    node, generation = actor.member.node_id, actor.member.generation
    client(actor).send(notice(generation, phase='final', outcome='forced', clock=clock))
    view = exchange.withdrawals.view(node, generation)
    assert view['kind'] == 'left_smoothly' and view['drained'] is False
    assert 'cut off' in view['line']


def test_a_final_with_a_failing_revoke_is_still_acknowledged_and_recorded(world):
    app, actor, exchange, clock = world
    node, generation = actor.member.node_id, actor.member.generation

    def boom(*args):
        raise RuntimeError('seed busy')
    exchange.withdrawals.set_on_final(boom)
    ack = client(actor).send(notice(generation, phase='final', outcome='drained', clock=clock))
    assert ack['accepted'] is True and ack['revoked'] is False
    assert exchange.withdrawals.view(node, generation)['kind'] == 'left_smoothly'


def test_the_real_app_wires_the_self_revoke_and_a_captured_final_cannot_be_replayed(world):
    app, actor, exchange, clock = world
    node, generation = actor.member.node_id, actor.member.generation
    wired = ensure_exchange(app)                       # the app's own exchange: self-revoke is wired without a test hook
    from mycelium_a13_macos.member_withdrawal import MemberWithdrawalClient
    captured = MemberWithdrawalClient(actor.peer).envelope(notice(generation, phase='final', outcome='drained'))
    ack = MemberWithdrawalClient(actor.peer).send(notice(generation, phase='final', outcome='drained'))
    assert ack['revoked'] is True
    row = {m['node_id']: m for m in app.authority.coordinator.members()}[node]
    assert row['lifecycle_state'] == 'STOPPED' and row['generation'] == generation + 1
    assert wired.withdrawals.view(node, generation + 1)['kind'] == 'left_smoothly'
    with pytest.raises(ManagedControlError):           # the member record advanced: the old signed copy is refused
        post_raw(app, captured)


def test_tampered_unsigned_or_misbound_notices_are_refused(world):
    app, actor, exchange, clock = world
    node, generation = actor.member.node_id, actor.member.generation
    envelope = client(actor).envelope(notice(generation, phase='final', outcome='drained', clock=clock))
    forged = copy.deepcopy(envelope)
    forged['statement']['notice']['outcome'] = 'forced'
    with pytest.raises(ManagedControlError) as caught:
        post_raw(app, forged)
    assert caught.value.code == 'managed_control_signature_invalid'
    unsigned = {k: v for k, v in envelope.items() if k != 'signature'}
    with pytest.raises(ManagedControlError):
        post_raw(app, unsigned)
    with pytest.raises(ManagedControlError):
        post_raw(app, {'statement': {}})
    with pytest.raises(ManagedControlError) as caught:
        post_raw(app, client(actor).envelope(notice(generation + 1, phase='final', outcome='drained', clock=clock)))
    assert caught.value.code == 'member_withdrawal_generation_mismatch'
    from mycelium_a13_macos.member_withdrawal import MAX_NOTICE_AGE_SECONDS
    with pytest.raises(ManagedControlError) as caught:
        post_raw(app, client(actor).envelope({**notice(generation), 'issued_at': clock.now - MAX_NOTICE_AGE_SECONDS - 5}))
    assert caught.value.code == 'member_withdrawal_stale'
    with pytest.raises(ManagedControlError) as caught:
        post_raw(app, client(actor).envelope({**notice(generation), 'issued_at': clock.now + 600}))
    assert caught.value.code == 'member_withdrawal_from_future'
    assert exchange.withdrawals.view(node, generation) is None                       # none of it was recorded


def test_the_withdrawal_route_is_not_the_state_report_route():
    from mycelium_a13_macos.member_state import PATH_STATE
    from mycelium_a13_macos.member_withdrawal import PATH_WITHDRAW
    assert PATH_STATE != PATH_WITHDRAW


# -- planning: the swarm plans for the leave ---------------------------------------------------------------------------

def test_planning_marks_the_member_leaving_and_asks_the_planner_about_each_request(world):
    app, actor, exchange, clock = world
    node, generation = actor.member.node_id, actor.member.generation
    asked = []

    def planner(n, g, request_ids):
        asked.append((n, g, tuple(request_ids)))
        return {'r1': 'a7_kv_standby', 'r2': 'a6_replay', 'r3': 'not_in_flight'}

    exchange.withdrawals.set_planner(planner)
    ack = client(actor).send(notice(generation, in_flight=['r1', 'r2', 'r3', 'r4'], clock=clock))
    assert asked == [(node, generation, ('r1', 'r2', 'r3', 'r4'))]
    assert ack['accepted'] is True and ack['recorded'] == 'leaving'
    # an answer the planner did not give is the honest default: loss detection, the member keeps draining
    assert ack['handoff'] == {'r1': 'a7_kv_standby', 'r2': 'a6_replay', 'r3': 'not_in_flight', 'r4': 'loss_detection'}
    view = exchange.withdrawals.view(node, generation)
    assert view['kind'] == 'leaving' and view['handoff'] == ack['handoff']
    assert view['line'].startswith('Leaving smoothly')
    # a retried planning notice with the same requests is answered from the record, not planned twice
    client(actor).send(notice(generation, in_flight=['r1', 'r2', 'r3', 'r4'], clock=clock))
    assert len(asked) == 1


def test_a_broken_or_lying_planner_degrades_to_loss_detection(world):
    app, actor, exchange, clock = world
    generation = actor.member.generation
    exchange.withdrawals.set_planner(lambda *a: (_ for _ in ()).throw(RuntimeError('router gone')))
    ack = client(actor).send(notice(generation, in_flight=['r1'], clock=clock))
    assert ack['handoff'] == {'r1': 'loss_detection'}
    exchange.withdrawals.set_planner(lambda *a: {'r1': 'teleported', 'other': 'a7_kv_standby'})
    exchange.withdrawals.forget_all()
    ack = client(actor).send(notice(generation, in_flight=['r1'], clock=clock))
    assert ack['handoff'] == {'r1': 'loss_detection'}                                 # unknown decision / unasked id dropped


def test_planning_without_a_planner_still_records_leaving_and_defers_to_loss_detection(world):
    app, actor, exchange, clock = world
    node, generation = actor.member.node_id, actor.member.generation
    ack = client(actor).send(notice(generation, in_flight=['r1'], clock=clock))
    assert ack['handoff'] == {'r1': 'loss_detection'} and exchange.withdrawals.view(node, generation)['kind'] == 'leaving'


# -- left suddenly: liveness, never a claim ----------------------------------------------------------------------------

def test_a_silent_member_without_a_withdrawal_is_left_suddenly_after_the_liveness_bound(world):
    from mycelium_a13_macos.member_withdrawal import LOST_AFTER_SECONDS
    app, actor, exchange, clock = world
    node, generation = actor.member.node_id, actor.member.generation
    assert exchange.withdrawals.view(node, generation) is None                       # never heard: unknown, not "left"
    exchange.withdrawals.observe_contact(node)
    clock.now += LOST_AFTER_SECONDS - 1
    assert exchange.withdrawals.view(node, generation) is None
    clock.now += 2
    view = exchange.withdrawals.view(node, generation)
    assert view['kind'] == 'left_suddenly' and view['signed'] is False
    assert view['line'].startswith('Left suddenly - no withdrawal received')
    exchange.withdrawals.observe_contact(node)                                       # the internet came back: no verdict
    assert exchange.withdrawals.view(node, generation) is None


def test_a_planned_leave_that_goes_silent_before_its_final_is_left_suddenly_not_smoothly(world):
    from mycelium_a13_macos.member_withdrawal import LOST_AFTER_SECONDS
    app, actor, exchange, clock = world
    node, generation = actor.member.node_id, actor.member.generation
    client(actor).send(notice(generation, in_flight=[], clock=clock))
    exchange.withdrawals.observe_contact(node)
    assert exchange.withdrawals.view(node, generation)['kind'] == 'leaving'
    clock.now += LOST_AFTER_SECONDS + 5
    view = exchange.withdrawals.view(node, generation)
    assert view['kind'] == 'left_suddenly' and 'planned' in view['line']


def test_the_exchange_counts_every_signed_read_as_contact(world):
    app, actor, exchange, clock = world
    from mycelium_a13_macos.run_exchange import RunExchangeClient
    node = actor.member.node_id
    RunExchangeClient(actor.peer).read()
    assert exchange.withdrawals.last_contact(node) == pytest.approx(clock.now, abs=1)


def test_a_rejoin_supersedes_the_old_record(world):
    app, actor, exchange, clock = world
    node, generation = actor.member.node_id, actor.member.generation
    client(actor).send(notice(generation, phase='final', outcome='drained', clock=clock))
    assert exchange.withdrawals.view(node, generation + 1)['kind'] == 'left_smoothly'     # the revoked generation's roster row
    assert exchange.withdrawals.view(node, generation + 2) is None                         # re-admitted: a new generation


def test_final_records_survive_an_owner_restart_but_planning_and_contact_do_not(world, tmp_path):
    from mycelium_a13_macos.member_withdrawal import MemberWithdrawalChannel
    app, actor, exchange, clock = world
    node, generation = actor.member.node_id, actor.member.generation
    store = tmp_path / 'withdrawals.json'
    persisted = MemberWithdrawalChannel(app.authority.control_mailbox, clock=clock, store_path=store).install()
    client(actor).send(notice(generation, phase='final', outcome='drained', clock=clock))
    assert json.loads(store.read_text())
    assert oct(store.stat().st_mode & 0o777) == '0o600'
    again = MemberWithdrawalChannel(app.authority.control_mailbox, clock=clock, store_path=store)
    assert again.view(node, generation + 1)['kind'] == 'left_smoothly'
    assert persisted is not again


def test_the_record_store_is_bounded(world):
    from mycelium_a13_macos.member_withdrawal import MAX_RECORDS
    app, actor, exchange, clock = world
    for index in range(MAX_RECORDS + 20):
        exchange.withdrawals.record_final(f'node-{index}', 1, drained=True, revoked=True)
    assert len(exchange.withdrawals.records()) <= MAX_RECORDS


# -- owner status and workspace -------------------------------------------------------------------------------------------

def test_status_members_carry_the_leave_view(tmp_path):
    app = DemoApp(tmp_path / 'owner', check_platform=False).start()
    actor = None
    try:
        app.action('/api/create', {'consent': CONSENT})
        actor = create_local_contributor(app)
        exchange = ensure_exchange(app)
        node, generation = actor.member.node_id, actor.member.generation
        rows = {m['node_id']: m for m in app.status()['authority']['members']}
        assert rows[node]['leave'] is None
        from mycelium_a13_macos.member_withdrawal import MemberWithdrawalClient
        MemberWithdrawalClient(actor.peer).send(notice(generation, phase='final', outcome='drained'))
        rows = {m['node_id']: m for m in app.status()['authority']['members']}
        assert rows[node]['lifecycle_state'] == 'STOPPED' and rows[node]['generation'] == generation + 1
        assert rows[node]['leave']['kind'] == 'left_smoothly'
        assert rows[node]['leave']['line'].startswith('Left smoothly')
    finally:
        if actor is not None:
            actor.stop()
        app.close()


def test_status_never_breaks_when_the_leave_view_fails(tmp_path, monkeypatch):
    app = DemoApp(tmp_path / 'owner', check_platform=False).start()
    actor = None
    try:
        app.action('/api/create', {'consent': CONSENT})
        actor = create_local_contributor(app)
        exchange = ensure_exchange(app)
        monkeypatch.setattr(type(exchange.withdrawals), 'view', lambda *a, **k: (_ for _ in ()).throw(RuntimeError('x')))
        row = {m['node_id']: m for m in app.status()['authority']['members']}[actor.member.node_id]
        assert row['leave'] is None
    finally:
        if actor is not None:
            actor.stop()
        app.close()


def test_workspace_renders_the_leave_line():
    from pathlib import Path
    text = (Path(__file__).resolve().parents[2] / 'mycelium_a13_macos' / 'web' / 'app.js').read_text(encoding='utf-8')
    assert 'member.leave' in text and 'member.leave.line' in text
    assert "left_suddenly" in text and "left_smoothly" in text      # shown with its kind so the two read differently
