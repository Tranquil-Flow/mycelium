# SPDX-License-Identifier: AGPL-3.0-or-later
"""PV10 owner side: a member's planning notice reaches the existing A7 KV-standby / A6 replay recovery.

The owner never trusts the member's list: it acts only on requests it knows are running through that member's
placement.  A request covered by an A7 standby goes through the existing ``accept_source_lost`` /
``cut_over_lost_source`` pair (which itself falls back to A6 full-context replay when the cutover is not eligible); any
other request goes through the existing ``report_lost_source`` (A6).  Nothing in A6/A7 is changed - this module is a
caller of their public seams, with the member alive instead of dead.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from mycelium_router.fakes import InMemoryClientSink
from tests.a7_acceptance.test_entry_cutover import STANDBY_ID, _Harness, _MAX_NEW, _reference_tokens
from tests.a7_acceptance.test_source_lost_claim import _decode_until, _finish, _wiring


def handoff(harness, **kw):
    from mycelium_a13_macos.leave_handoff import LeaveHandoff
    return LeaveHandoff(router=harness.entry, wiring=_wiring(harness), **kw)


# -- A7: the member is alive and covered by a standby ------------------------------------------------------------------

def test_a_covered_request_leaves_through_the_a7_kv_successor_without_replay(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)
    request_id = 'request:leave-a7'
    sink = InMemoryClientSink()
    _decode_until(harness, request_id, sink, 2)
    assert harness.source_port.killed is False                       # the leaving member is still alive

    decisions = handoff(harness).plan('node-b', 1, [request_id])

    assert decisions == {request_id: 'a7_kv_standby'}
    record = harness.entry.entry.get_request(request_id)
    assert record.kv_successor_cutover['outcome'] == 'fenced_kv_successor'
    assert record.kv_successor_cutover['recovery_prefill'] is False
    assert record.manifest.ordered_hops[1].placement_id == STANDBY_ID
    _finish(harness, request_id)
    assert harness.entry.request_status(request_id) == 'COMPLETED'
    assert sink.token_ids == _reference_tokens(harness.case)           # exact delivery, no duplicate or missing token
    assert sink.token_indexes == list(range(_MAX_NEW))
    assert harness.standby_port.kv_snapshot()['prefill_operation_count'] == 0


def test_an_incompatible_standby_is_reported_honestly_as_the_a6_replay_it_became(tmp_path: Path) -> None:
    harness = _Harness(tmp_path, standby_dtype='float16')
    request_id = 'request:leave-a6-after-a7'
    sink = InMemoryClientSink()
    _decode_until(harness, request_id, sink, 2)

    decisions = handoff(harness).plan('node-b', 1, [request_id])

    record = harness.entry.entry.get_request(request_id)
    assert record.kv_successor_cutover['outcome'] == 'full_context_replay'
    assert record.kv_successor_cutover['kv_continuity_claimed'] is False
    assert decisions == {request_id: 'a6_replay'}                      # never claims KV continuity it did not have


# -- the owner does not trust the member's list ---------------------------------------------------------------------------

def test_requests_that_do_not_cross_the_leaving_member_are_not_touched(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)
    request_id = 'request:leave-other'
    sink = InMemoryClientSink()
    _decode_until(harness, request_id, sink, 2)
    old = harness.entry.entry.get_request(request_id).manifest

    decisions = handoff(harness).plan('node-c', 1, [request_id, 'request:unknown'])

    assert decisions == {request_id: 'not_in_flight', 'request:unknown': 'not_in_flight'}
    assert harness.entry.entry.get_request(request_id).manifest is old   # a member cannot force recovery of others' work
    assert harness.entry.entry.get_request(request_id).kv_successor_cutover is None


def test_a_finished_request_is_not_in_flight(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)
    request_id = 'request:leave-done'
    harness.run(request_id, kill_after=None)
    assert harness.entry.request_status(request_id) == 'COMPLETED'
    assert handoff(harness).plan('node-b', 1, [request_id]) == {request_id: 'not_in_flight'}


def test_the_planner_never_raises_and_is_bounded(tmp_path: Path) -> None:
    from mycelium_a13_macos.member_withdrawal import MAX_IN_FLIGHT
    harness = _Harness(tmp_path)
    plan = handoff(harness)
    ids = [f'request:{i}' for i in range(MAX_IN_FLIGHT + 10)]
    decisions = plan.plan('node-b', 1, ids)
    assert len(decisions) <= MAX_IN_FLIGHT and set(decisions.values()) == {'not_in_flight'}
    broken = SimpleNamespace(request_status=lambda rid: (_ for _ in ()).throw(RuntimeError('router down')))
    from mycelium_a13_macos.leave_handoff import LeaveHandoff
    assert LeaveHandoff(router=broken).plan('node-b', 1, ['r1']) == {'r1': 'loss_detection'}


# -- A6: no standby covers the member ---------------------------------------------------------------------------------------

class FakeEntry:
    def __init__(self, *, status='DECODING', accept=True, nodes=('node-a', 'node-b')) -> None:
        self.reports: list[tuple] = []
        self._status, self._accept = status, accept
        placements = [SimpleNamespace(placement_id=f'pl-{n}', node_id=n) for n in nodes]
        graph = SimpleNamespace(stages=[SimpleNamespace(placements=[p]) for p in placements])
        hops = tuple(SimpleNamespace(placement_id=p.placement_id) for p in placements)
        self._record = SimpleNamespace(graph=graph, manifest=SimpleNamespace(ordered_hops=hops), status=status,
                                       kv_successor_cutover=None)

    def request_status(self, request_id):
        return self._status

    def get_request(self, request_id):
        return self._record

    def report_lost_source(self, request_id, placement_id, *, reason='node_process_exited'):
        self.reports.append((request_id, placement_id, reason))
        return self._accept


def test_an_uncovered_request_leaves_through_the_a6_replay_seam() -> None:
    from mycelium_a13_macos.leave_handoff import LeaveHandoff
    entry = FakeEntry()
    router = SimpleNamespace(entry=entry, request_status=entry.request_status)
    decisions = LeaveHandoff(router=router).plan('node-b', 2, ['r1'])
    assert decisions == {'r1': 'a6_replay'}
    assert entry.reports == [('r1', 'pl-node-b', 'member_leaving')]


def test_a_request_still_prefilling_stays_with_loss_detection_and_the_member_keeps_draining() -> None:
    from mycelium_a13_macos.leave_handoff import LeaveHandoff
    entry = FakeEntry(accept=False, status='PREFILL')
    router = SimpleNamespace(entry=entry, request_status=entry.request_status)
    assert LeaveHandoff(router=router).plan('node-b', 2, ['r1']) == {'r1': 'loss_detection'}


def test_a7_is_tried_first_and_a6_only_when_no_standby_covers_the_member() -> None:
    from mycelium_a13_macos.leave_handoff import LeaveHandoff
    entry = FakeEntry()
    router = SimpleNamespace(entry=entry, request_status=entry.request_status)
    calls: list[str] = []

    class Wiring:
        controller = SimpleNamespace(covered_source_placement=lambda rid, node: 'pl-node-b' if rid == 'covered' else None)

        def accept_source_lost(self, rid, node):
            calls.append(f'accept:{rid}')
            return {'accepted': True, 'cutover_pending': True}

        def cut_over_lost_source(self, _router, rid, node):
            calls.append(f'cutover:{rid}')
            entry._record.kv_successor_cutover = {'outcome': 'fenced_kv_successor'}
            return True

    decisions = LeaveHandoff(router=router, wiring=Wiring()).plan('node-b', 2, ['covered', 'plain'])
    assert decisions == {'covered': 'a7_kv_standby', 'plain': 'a6_replay'}
    assert calls == ['accept:covered', 'cutover:covered']
    assert [r[0] for r in entry.reports] == ['plain']


# -- wiring into the owner exchange ---------------------------------------------------------------------------------------------

def test_the_owner_exchange_asks_the_apps_leave_handoff_for_the_planning_notice(tmp_path) -> None:
    from mycelium_a13_macos.app import DemoApp
    from mycelium_a13_macos.demo_app import ensure_exchange
    from mycelium_a13_macos.local_contributor import create_local_contributor
    from mycelium_a13_macos.member_withdrawal import MemberWithdrawalClient
    from test_app_authority import CONSENT
    app = DemoApp(tmp_path / 'owner', check_platform=False).start()
    actor = None
    try:
        app.action('/api/create', {'consent': CONSENT})
        actor = create_local_contributor(app)
        exchange = ensure_exchange(app)
        client = MemberWithdrawalClient(actor.peer)
        from test_member_withdrawal import notice
        ack = client.send(notice(actor.member.generation, in_flight=['r1']))
        assert ack['handoff'] == {'r1': 'loss_detection'}                  # no router attached to this app: honest default
        app.leave_handoff = SimpleNamespace(plan=lambda n, g, ids: {'r1': 'a7_kv_standby'})
        exchange.withdrawals.forget_all()
        ack = client.send(notice(actor.member.generation, in_flight=['r1']))
        assert ack['handoff'] == {'r1': 'a7_kv_standby'}
    finally:
        if actor is not None:
            actor.stop()
        app.close()
