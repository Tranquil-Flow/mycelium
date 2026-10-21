# SPDX-License-Identifier: AGPL-3.0-or-later
"""PV1: each member sends the owner a signed state report; the owner verifies, stores the latest per member and
rebuilds its view from the reports after its own restart.  PV11 is in test_member_state_workspace.py."""
from __future__ import annotations

import copy
import json
import time

import pytest

from mycelium_a13_macos.app import DemoApp
from mycelium_a13_macos.local_contributor import create_local_contributor
from mycelium_a13_macos.managed_control import ManagedControlError
from mycelium_a13_macos.run_exchange import RunExchange
from test_app_authority import CONSENT


class Clock:
    def __init__(self, now=None):
        self.now = time.time() if now is None else now

    def __call__(self):
        return self.now


def body(generation, **changes):
    value = {'generation': generation,
             'sharing': {'state': 'normal', 'reason': 'none', 'adaptive_enabled': True},
             'worker': {'state': 'idle'},
             'model': {'model_id': 'Qwen/Qwen3-0.6B', 'resolved_commit': 'c' * 40, 'verification': 'verified'},
             'caps': {'storage_bytes': 10 ** 9, 'max_memory_bytes': 2 * 10 ** 9},
             'service_mode': None}
    value.update(changes)
    return value


def report(generation, *, clock=None, purpose='periodic', **changes):
    from mycelium_a13_macos.member_state import STATE_REPORT
    return {'protocol': STATE_REPORT, 'purpose': purpose,
            'issued_at': (clock or time.time)(), **body(generation, **changes)}


# -- schema -------------------------------------------------------------------------------------------------

def test_report_schema_is_closed_bounded_and_names_no_apps():
    from mycelium_a13_macos.member_state import validate_state_report
    good = report(3)
    assert validate_state_report(good)['sharing']['state'] == 'normal'
    paused = report(3, sharing={'state': 'paused', 'reason': 'owner_game_mode', 'adaptive_enabled': False})
    assert validate_state_report(paused)['sharing']['reason'] == 'owner_game_mode'
    bad = [
        {**good, 'extra': 1},
        {**good, 'foreground_app': 'Zoom'},
        report(3, sharing={'state': 'paused', 'reason': 'Zoom', 'adaptive_enabled': True}),   # reason is a closed code
        report(3, sharing={'state': 'paused', 'reason': 'none', 'adaptive_enabled': True, 'apps': ['Zoom']}),
        report(3, sharing={'state': 'hacked', 'reason': 'none', 'adaptive_enabled': True}),
        report(3, worker={'state': 'exploding'}),
        report(3, worker={'state': 'running', 'app': 'Zoom'}),
        report(3, model={'model_id': 'm', 'resolved_commit': 'c', 'verification': 'maybe'}),
        report(3, model={'model_id': 'x' * 500, 'resolved_commit': 'c', 'verification': 'verified'}),
        report(3, caps={'storage_bytes': -1, 'max_memory_bytes': 1}),
        report(3, caps={'storage_bytes': 1}),
        report(3, service_mode='overnight'),
        report(-1), report(True), {**good, 'issued_at': float('nan')}, {**good, 'purpose': 'because'},
        {k: v for k, v in good.items() if k != 'issued_at'},
    ]
    for value in bad:
        with pytest.raises(ValueError, match='member_state_report_invalid'):
            validate_state_report(value)
    for mode in ('interactive', 'batch', None):
        assert validate_state_report(report(3, service_mode=mode))['service_mode'] == mode


# -- owner side over the real signed local-TLS control plane --------------------------------------------------

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


def send(actor, value):
    from mycelium_a13_macos.member_state import MemberStateClient
    return MemberStateClient(actor.peer).report(value)


def post_raw(app, envelope):
    from mycelium_a13_macos.member_state import PATH_STATE
    return app.authority.control_mailbox.handle_http('POST', PATH_STATE, envelope)


def test_signed_report_is_verified_stored_and_viewed_with_freshness(world):
    app, actor, exchange, clock = world
    node, generation = actor.member.node_id, actor.member.generation
    assert exchange.member_states.view(node, generation)['state'] == 'unknown'
    ack = send(actor, report(generation, clock=clock, purpose='reconnect',
                             sharing={'state': 'paused', 'reason': 'owner_game_mode', 'adaptive_enabled': False},
                             worker={'state': 'warming'}))
    assert ack['accepted'] is True
    clock.now += 12
    view = exchange.member_states.view(node, generation)
    assert view['state'] == 'reported' and view['age_seconds'] == pytest.approx(12, abs=0.5)
    assert view['sharing'] == {'state': 'paused', 'reason': 'owner_game_mode', 'adaptive_enabled': False}
    assert view['worker'] == {'state': 'warming'} and view['generation'] == generation
    assert view['model']['verification'] == 'verified' and view['caps']['storage_bytes'] == 10 ** 9
    assert view['line'] == 'Paused sharing - reported 12 s ago'
    assert view['reason_text'] == 'paused on that Mac'
    # Latest wins, an older replay never overwrites a newer one.
    send(actor, report(generation, clock=lambda: clock.now + 5, sharing={'state': 'normal', 'reason': 'none', 'adaptive_enabled': True}))
    send(actor, report(generation, clock=lambda: clock.now - 5, sharing={'state': 'reduced', 'reason': 'host_memory_pressure', 'adaptive_enabled': True}))
    assert exchange.member_states.view(node, generation)['sharing']['state'] == 'normal'


def test_view_goes_stale_then_says_so(world):
    app, actor, exchange, clock = world
    node, generation = actor.member.node_id, actor.member.generation
    send(actor, report(generation, clock=clock))
    clock.now += 40
    assert exchange.member_states.view(node, generation)['state'] == 'reported'
    clock.now += 200
    view = exchange.member_states.view(node, generation)
    assert view['state'] == 'stale' and view['age_seconds'] > 200
    assert view['line'].startswith('Stale - last report 4 min ago') and 'Sharing' in view['line']
    assert view['sharing']['state'] == 'normal'  # the last known value stays visible, labelled stale


def test_unsigned_tampered_or_wrong_member_reports_are_refused_with_bounded_codes(world):
    app, actor, exchange, clock = world
    from mycelium_a13_macos.member_state import MemberStateClient
    node, generation = actor.member.node_id, actor.member.generation
    envelope = MemberStateClient(actor.peer).envelope(report(generation, clock=clock))
    # tampered content under a valid signature
    forged = copy.deepcopy(envelope)
    forged['statement']['report']['sharing']['state'] = 'paused'
    with pytest.raises(ManagedControlError) as caught:
        post_raw(app, forged)
    assert caught.value.code == 'managed_control_signature_invalid'
    # unsigned
    unsigned = {k: v for k, v in envelope.items() if k != 'signature'}
    with pytest.raises(ManagedControlError) as caught:
        post_raw(app, unsigned)
    assert caught.value.code in {'managed_control_signature_invalid', 'managed_control_envelope_invalid'}
    # garbage
    with pytest.raises(ManagedControlError):
        post_raw(app, {'statement': {}})
    assert exchange.member_states.view(node, generation)['state'] == 'unknown'


def test_stale_future_and_wrong_generation_reports_are_refused(world):
    app, actor, exchange, clock = world
    from mycelium_a13_macos.member_state import MemberStateClient, MAX_REPORT_AGE_SECONDS
    node, generation = actor.member.node_id, actor.member.generation
    client = MemberStateClient(actor.peer)
    for issued, code in ((clock.now - MAX_REPORT_AGE_SECONDS - 5, 'member_state_stale'),
                         (clock.now + 600, 'member_state_from_future')):
        with pytest.raises(ManagedControlError) as caught:
            post_raw(app, client.envelope({**report(generation), 'issued_at': issued}))
        assert caught.value.code == code
    # a report claiming another generation than the signed binding
    with pytest.raises(ManagedControlError) as caught:
        post_raw(app, client.envelope(report(generation + 1, clock=clock)))
    assert caught.value.code == 'member_state_generation_mismatch'
    assert exchange.member_states.view(node, generation)['state'] == 'unknown'
    # skew inside the bound is accepted
    send(actor, {**report(generation), 'issued_at': clock.now + 10})
    assert exchange.member_states.view(node, generation)['state'] == 'reported'


def test_old_generation_report_after_rejoin_is_refused_and_view_ignores_it(world):
    app, actor, exchange, clock = world
    from mycelium_a13_macos.member_state import MemberStateClient
    node, generation = actor.member.node_id, actor.member.generation
    client = MemberStateClient(actor.peer)
    old = client.envelope(report(generation, clock=clock))
    send(actor, report(generation, clock=clock))
    # Re-admission: the owner now knows a newer generation; the stored report belongs to the old one.
    assert exchange.member_states.view(node, generation + 1)['state'] == 'unknown'
    # A revoked / superseded member's signed report is refused by the mailbox's current-member check.
    app.action('/api/revoke', {'node_id': node, 'generation': generation})
    with pytest.raises(ManagedControlError):
        post_raw(app, old)
    with pytest.raises(Exception):
        send(actor, report(generation, clock=clock))


def test_store_is_bounded_per_member_and_per_swarm(world):
    app, actor, exchange, clock = world
    store = exchange.member_states
    from mycelium_a13_macos.member_state import MAX_MEMBERS, validate_state_report
    for index in range(MAX_MEMBERS + 20):
        store.accept(f'node-{index}', 1, validate_state_report(report(1, clock=clock)))
    assert len(store.latest()) <= MAX_MEMBERS


def test_owner_restart_rebuilds_from_reports_and_never_assumes(world):
    app, actor, exchange, clock = world
    from mycelium_a13_macos.member_state import MemberStateReporter, MemberStateClient
    node, generation = actor.member.node_id, actor.member.generation
    paused = {'state': 'paused', 'reason': 'owner_game_mode', 'adaptive_enabled': True}
    facts = lambda: body(generation, sharing=paused)
    reporter = MemberStateReporter(facts=facts, sink=MemberStateClient(actor.peer).report, clock=clock)
    assert reporter.tick() == 'reconnect'
    assert exchange.member_states.view(node, generation)['sharing']['state'] == 'paused'
    # The owner process restarts: a new exchange on the same mailbox has no report and does not assume one.
    restarted = RunExchange(app.authority.control_mailbox, clock=clock).install()
    view = restarted.member_states.view(node, generation)
    assert view['state'] == 'unknown' and view['sharing'] is None and view['since'] is not None
    assert 'unknown' in view['line'].lower()
    # The member's next report (periodic, or at once when the owner asks) rebuilds it.
    assert reporter.tick(requested=True) == 'requested'
    rebuilt = restarted.member_states.view(node, generation)
    assert rebuilt['state'] == 'reported' and rebuilt['sharing'] == paused


def test_owner_asks_a_silent_member_for_a_report_in_the_announcement(world):
    app, actor, exchange, clock = world
    node, generation = actor.member.node_id, actor.member.generation
    assert exchange.announcement(node)['member_state_requested'] is True       # nothing known yet (e.g. after a restart)
    send(actor, report(generation, clock=clock))
    assert exchange.announcement(node)['member_state_requested'] is False
    clock.now += 100
    assert exchange.announcement(node)['member_state_requested'] is True       # gone stale
    assert 'member_state_requested' in exchange.announcement()


# -- member-side schedule (no network: injected sink and clock) ---------------------------------------------------

def make_reporter(**kw):
    from mycelium_a13_macos.member_state import MemberStateReporter
    clock = kw.pop('clock', Clock(1000.0))
    sent = []
    state = {'facts': body(1), 'fail': False}

    def sink(value):
        if state['fail']:
            raise RuntimeError('owner_unreachable')
        sent.append(value)
        return {'accepted': True}
    reporter = MemberStateReporter(facts=lambda: copy.deepcopy(state['facts']), sink=sink, clock=clock, **kw)
    return reporter, sent, state, clock


def test_reporter_sends_on_reconnect_then_periodically_then_on_change():
    reporter, sent, state, clock = make_reporter()
    assert reporter.tick() == 'reconnect' and sent[-1]['purpose'] == 'reconnect'
    assert sent[-1]['issued_at'] == clock.now and sent[-1]['generation'] == 1
    clock.now += 5
    assert reporter.tick() is None and len(sent) == 1                     # nothing changed, not due
    state['facts'] = body(1, sharing={'state': 'paused', 'reason': 'owner_game_mode', 'adaptive_enabled': True})
    assert reporter.tick() is None                                       # change is debounced...
    clock.now += 6
    assert reporter.tick() == 'change' and sent[-1]['sharing']['state'] == 'paused'
    clock.now += 31
    assert reporter.tick() == 'periodic'
    clock.now += 2
    assert reporter.tick() is None
    assert len(sent) == 3


def test_reporter_reconnects_after_failure_and_on_a_new_generation():
    reporter, sent, state, clock = make_reporter()
    state['fail'] = True
    assert reporter.tick() is None and sent == [] and reporter.status()['last_error'] == 'member_state_send_failed'
    clock.now += 1
    assert reporter.tick() is None                                       # backing off, not hammering
    state['fail'] = False
    clock.now += 30
    assert reporter.tick() == 'reconnect' and sent[-1]['purpose'] == 'reconnect'
    assert reporter.status()['last_error'] is None
    state['facts'] = body(2)                                             # re-joined: generation + 1
    clock.now += 6
    assert reporter.tick() == 'reconnect' and sent[-1]['generation'] == 2


def test_reporter_failure_codes_are_bounded():
    reporter, sent, state, clock = make_reporter()
    state['fail'] = True
    reporter.tick()
    assert reporter.status()['last_error'] == 'member_state_send_failed'
    reporter2, _, state2, _ = make_reporter()
    state2['facts'] = {'generation': 1}                                  # facts the validator refuses
    reporter2.tick()
    assert reporter2.status()['last_error'] == 'member_state_report_invalid'


# -- facts collected from the app ---------------------------------------------------------------------------------

def test_collect_facts_projects_adaptive_state_without_app_names(tmp_path):
    from test_adaptive_owner import make_app, ON
    from mycelium_a13_macos.member_state import collect_facts, validate_state_report
    app, clock = make_app(tmp_path)
    value = collect_facts(app)
    assert value['sharing'] == {'state': 'normal', 'reason': 'none', 'adaptive_enabled': False}
    app.action('/api/adaptive/policy', {**ON, 'game_mode': True})
    value = collect_facts(app)
    assert value['sharing'] == {'state': 'paused', 'reason': 'owner_game_mode', 'adaptive_enabled': True}
    assert value['worker']['state'] in {'running', 'idle', 'warming'}
    assert value['generation'] is None                                   # not a member: nothing to report for
    validate_state_report({'protocol': 'mycelium.v2_member_state_report.v1', 'purpose': 'periodic',
                           'issued_at': time.time(), **{**value, 'generation': 1}})
    text = json.dumps(value)
    assert 'Zoom' not in text and 'app_name' not in text


def test_collect_facts_maps_stop_and_reduced(tmp_path):
    from test_adaptive_owner import make_app, ON
    from mycelium_a13_macos.member_state import collect_facts
    app, clock = make_app(tmp_path)
    app.action('/api/adaptive/policy', ON)
    app.action('/api/adaptive/stop', {})
    value = collect_facts(app)
    assert value['sharing']['state'] == 'paused' and value['sharing']['reason'] == 'owner_stop'


# -- service mode field (carried by the same channel; the modes code is on lane/rel2-modes) -------------------------

def test_announcement_carries_the_owner_service_mode_and_report_carries_the_applied_one(world):
    app, actor, exchange, clock = world
    assert exchange.announcement()['service_mode'] is None
    exchange.set_service_mode_provider(lambda: 'batch')
    assert exchange.announcement(actor.member.node_id)['service_mode'] == 'batch'
    exchange.set_service_mode_provider(lambda: 'nonsense')
    assert exchange.announcement()['service_mode'] is None               # a closed value or nothing
    exchange.set_service_mode_provider(lambda: (_ for _ in ()).throw(RuntimeError('x')))
    assert exchange.announcement()['service_mode'] is None
    node, generation = actor.member.node_id, actor.member.generation
    send(actor, report(generation, clock=clock, service_mode='batch'))
    assert exchange.member_states.view(node, generation)['service_mode'] == 'batch'


@pytest.mark.xfail(strict=True, reason='TODO(rel2 merge): lane/rel2-modes owns service-mode.json; once '
                   'mycelium_router.service_modes is on this branch the member must apply the owner mode from the '
                   'announcement (MemberStateReporter owner_service_mode hook) - then remove this marker')
def test_member_applies_the_owner_service_mode_to_its_own_service_mode_file(tmp_path):
    from mycelium_router import service_modes as sm
    from mycelium_a13_macos.member_state import apply_owner_service_mode
    root = tmp_path / 'member'
    root.mkdir()
    apply_owner_service_mode(type('App', (), {'root': root})(), 'batch')
    assert sm.read_service_mode(root) == 'batch'
