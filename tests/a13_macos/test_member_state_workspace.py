# SPDX-License-Identifier: AGPL-3.0-or-later
"""PV11: the owner's status and workspace show each member's reported A16 state with its freshness; PV1 wiring."""
from __future__ import annotations

import inspect
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from mycelium_a13_macos.app import DemoApp
from mycelium_a13_macos.demo_app import ensure_exchange
from mycelium_a13_macos.local_contributor import create_local_contributor
from test_app_authority import CONSENT
from test_member_state_report import Clock, report, send

APP_JS = Path(__file__).resolve().parents[2] / 'mycelium_a13_macos' / 'web' / 'app.js'


@pytest.fixture
def world(tmp_path):
    app = DemoApp(tmp_path / 'owner', check_platform=False).start()
    actor = None
    try:
        app.action('/api/create', {'consent': CONSENT})
        actor = create_local_contributor(app)
        exchange = ensure_exchange(app)
        yield app, actor, exchange
    finally:
        if actor is not None:
            actor.stop()
        app.close()


def members(app):
    return {m['node_id']: m for m in app.status()['authority']['members']}


def test_status_members_show_unknown_until_the_member_reports(world):
    app, actor, exchange = world
    app.local_contributor = None            # treat the member as a remote Mac: the owner knows only what it reports
    row = members(app)[actor.member.node_id]
    state = row['member_state']
    assert state['state'] == 'unknown' and state['sharing'] is None and 'unknown' in state['line'].lower()
    send(actor, report(actor.member.generation, sharing={'state': 'paused', 'reason': 'host_memory_pressure',
                                                          'adaptive_enabled': True}))
    state = members(app)[actor.member.node_id]['member_state']
    assert state['state'] == 'reported' and state['sharing']['state'] == 'paused'
    assert state['line'].startswith('Paused sharing - reported ') and state['line'].endswith(' s ago')
    assert state['reason_text'] == 'memory is low'


def test_status_view_is_for_the_current_generation_only(world):
    from mycelium_a13_macos.member_state import validate_state_report
    app, actor, exchange = world
    app.local_contributor = None
    node, generation = actor.member.node_id, actor.member.generation
    other = generation + 1                  # a report that belongs to another membership generation than the roster's
    exchange.member_states.accept(node, other, validate_state_report(report(other)))
    assert members(app)[node]['member_state']['state'] == 'unknown'


def test_owners_own_contributor_is_shown_from_its_local_facts(world):
    app, actor, exchange = world
    row = members(app)[actor.member.node_id]
    assert row['member_state']['state'] == 'reported' and row['member_state']['source'] == 'local'
    assert row['member_state']['age_seconds'] == 0 and row['member_state']['sharing']['state'] == 'normal'


def test_status_never_breaks_when_the_view_fails(world, monkeypatch):
    app, actor, exchange = world
    monkeypatch.setattr(type(exchange.member_states), 'view', lambda *a, **k: (_ for _ in ()).throw(RuntimeError('x')))
    app.local_contributor = None
    row = members(app)[actor.member.node_id]
    assert row['member_state']['state'] == 'unknown' and 'line' in row['member_state']


def test_workspace_renders_the_reported_state_and_its_freshness():
    text = APP_JS.read_text(encoding='utf-8')
    assert 'member.member_state' in text and 'member_state.line' in text
    assert 'reason_text' in text
    # never builds the sentence from raw ids and never shows anything but the allow-listed fields
    assert 'foreground' not in text.lower().split('member.member_state')[1][:600]


# -- member wiring ------------------------------------------------------------------------------------------------------

def stand_in(actor, root, **extra):
    return SimpleNamespace(installation_role='target', authority=SimpleNamespace(peer=actor.peer, member=actor.member),
                           root=root, acquisition=None, local_contributor=None, demo_target=None, **extra)


def test_app_tick_reports_on_reconnect_then_stays_quiet_then_answers_a_request(world, tmp_path):
    from mycelium_a13_macos import member_state
    app, actor, exchange = world
    app.local_contributor = None
    me = stand_in(actor, tmp_path / 'member')
    me.root.mkdir()
    assert member_state.app_tick(me) == 'reconnect'
    view = exchange.member_states.view(actor.member.node_id, actor.member.generation)
    assert view['state'] == 'reported' and view['purpose'] == 'reconnect'
    assert member_state.app_tick(me) is None
    assert member_state.reporter_status(me)['sent'] == 1
    me.demo_target = SimpleNamespace(announcement={'member_state_requested': True})
    assert member_state.app_tick(me) == 'requested'


def test_app_tick_never_raises_and_keeps_a_bounded_error(world, tmp_path):
    from mycelium_a13_macos import member_state
    app, actor, exchange = world
    me = stand_in(actor, tmp_path / 'member')
    me.root.mkdir()
    me.authority = SimpleNamespace(peer=SimpleNamespace(staging_control_client=lambda **k: (_ for _ in ()).throw(RuntimeError('boom')),
                                                         staging_control_snapshot=lambda: {}), member=actor.member)
    assert member_state.app_tick(me) is None
    assert member_state.reporter_status(me)['last_error'] == 'member_state_send_failed'
    owner_role = SimpleNamespace(installation_role='owner', authority=None)
    assert member_state.app_tick(owner_role) is None


def test_the_standing_loop_drives_the_reporter_without_waiting_for_consent():
    source = inspect.getsource(DemoApp._standing_loop)
    assert 'member_state' in source and 'app_tick' in source
    # a failure of the report never stops the standing step
    assert source.index('_standing_tick') < source.index('app_tick')
