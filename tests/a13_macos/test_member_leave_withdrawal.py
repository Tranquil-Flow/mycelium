# SPDX-License-Identifier: AGPL-3.0-or-later
"""PV10 member side: the signed planning notice and final withdrawal around a smooth leave.

``Leave smoothly`` tells the owner (signed, over the member channel) which requests are still running here so the
owner can use its existing A7 KV-standby / A6 replay path instead of waiting for loss detection, waits for the owner's
answer or a bounded timeout, drains, and then sends one signed final withdrawal before it disconnects.  ``Leave now``
sends nothing: it is what the owner sees when the internet drops.
"""
from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest

from mycelium_a13_macos.member_leave import (LEAVE_SMOOTH_STALL_S, OWNER_ACK_TIMEOUT_S, LeaveError, LeaveSteps,
                                             MemberLeave, bounded, leave_status)
from test_member_leave import Clock, Recorder, _leave, _names


class Announcing(Recorder):
    """Recorder whose steps also include the signed final withdrawal."""

    def __init__(self, *args, announce_fails=False, **kw) -> None:
        super().__init__(*args, **kw)
        self.outcomes: list[str] = []
        self.announce_fails = announce_fails

    def steps(self) -> LeaveSteps:
        base = super().steps()

        def announce(outcome: str) -> None:
            self.log.append('announce_withdrawal')
            self.outcomes.append(outcome)
            if self.announce_fails:
                raise LeaveError('owner_ack_timeout')

        return LeaveSteps(fence=base.fence, inflight=base.inflight, handoff=base.handoff, unload=base.unload,
                          publish_withdrawal=base.publish_withdrawal, withdraw_consent=base.withdraw_consent,
                          disconnect=base.disconnect, cancel_work=base.cancel_work, announce_withdrawal=announce)


def test_steps_without_an_announce_seam_are_unchanged():
    recorder = Recorder(inflight=(0,))
    assert recorder.steps().announce_withdrawal is None


# -- ordering -------------------------------------------------------------------------------------------------------

def test_smooth_leave_signs_the_final_after_consent_withdrawal_and_before_disconnecting(tmp_path):
    recorder = Announcing(inflight=(1, 0), handoff=True)
    result = _leave(tmp_path, recorder)[0].run('smooth')
    assert _names(recorder.log) == ['fence', 'handoff', 'unload', 'publish_withdrawal', 'withdraw_consent',
                                    'announce_withdrawal', 'disconnect', 'cancel_work']
    assert recorder.outcomes == ['drained'] and result['state'] == 'left' and result['fallback'] is None


def test_smooth_leave_that_stalls_says_forced_in_the_signed_final(tmp_path):
    recorder = Announcing(inflight=(2,))
    leave, clock = _leave(tmp_path, recorder)
    result = leave.run('smooth')
    assert result['fallback'] == 'stall' and recorder.outcomes == ['forced']
    assert _names(recorder.log)[-4:] == ['withdraw_consent', 'announce_withdrawal', 'disconnect', 'cancel_work']
    assert 'unload' not in recorder.log


def test_leave_now_sends_no_withdrawal(tmp_path):
    recorder = Announcing(inflight=(3,))
    _leave(tmp_path, recorder)[0].run('instant')
    assert 'announce_withdrawal' not in recorder.log and recorder.outcomes == []
    assert _names(recorder.log) == ['fence', 'withdraw_consent', 'disconnect', 'cancel_work']


def test_a_restart_that_finishes_an_interrupted_leave_sends_no_withdrawal(tmp_path):
    import json
    (tmp_path).mkdir(exist_ok=True)
    (tmp_path / 'leave-state.json').write_text(json.dumps({
        'protocol': 'mycelium.a13_member_leave.v1', 'mode': 'smooth', 'state': 'draining', 'started_at': 1.0,
        'fallback': None, 'steps': [], 'handoff': 'unavailable'}))
    recorder = Announcing()
    clock = Clock()
    result = MemberLeave(tmp_path, recorder.steps(), clock=clock, sleep=clock.sleep).resume()
    assert result['fallback'] == 'restart' and 'announce_withdrawal' not in recorder.log


def test_an_unreachable_owner_never_stops_the_leave(tmp_path):
    recorder = Announcing(inflight=(0,), announce_fails=True)
    result = _leave(tmp_path, recorder)[0].run('smooth')
    assert result['state'] == 'left'
    assert {row['name']: row['code'] for row in result['steps'] if not row['ok']} == {
        'announce_withdrawal': 'owner_ack_timeout'}
    assert _names(recorder.log)[-2:] == ['disconnect', 'cancel_work']


# -- bounded wait for the owner ------------------------------------------------------------------------------------------

def test_the_owner_wait_is_bounded():
    assert 1 <= OWNER_ACK_TIMEOUT_S <= 10
    release = threading.Event()
    started = time.monotonic()
    with pytest.raises(LeaveError) as caught:
        bounded(lambda: release.wait(30), 0.2)
    release.set()
    assert caught.value.code == 'owner_ack_timeout' and time.monotonic() - started < 3
    assert bounded(lambda: 7, 1) == 7
    with pytest.raises(LeaveError) as caught:
        bounded(lambda: (_ for _ in ()).throw(RuntimeError('boom')), 1)
    assert caught.value.code == 'owner_notice_failed'


# -- the application adapter ------------------------------------------------------------------------------------------------

class FakeClient:
    def __init__(self, handoff=None, fail=None) -> None:
        self.sent: list[dict] = []
        self.handoff = handoff or {}
        self.fail = fail

    def send(self, notice):
        self.sent.append(notice)
        if self.fail:
            raise self.fail
        return {'accepted': True, 'phase': notice['phase'], 'recorded': 'leaving' if notice['phase'] == 'planning'
                else 'left_smoothly', 'revoked': notice['phase'] == 'final',
                'handoff': {rid: self.handoff.get(rid, 'loss_detection') for rid in notice['in_flight']}}


def stand_in(tmp_path, client, *, generation=4, inflight=('r1', 'r2')):
    left = []
    peer = SimpleNamespace(set_contribution_enabled=lambda **k: None, record_voluntary_leave=lambda: left.append(True))
    authority = SimpleNamespace(peer=peer, member=SimpleNamespace(generation=generation, node_id='node-m'),
                                stop_control=lambda: None, _runtime=SimpleNamespace(root=tmp_path, model_unloaded=True))
    app = SimpleNamespace(guard=threading.RLock(), root=tmp_path, authority=authority, _leave_client=client,
                          _cancel_acquisition=lambda: None, _left=False, _leave_fence=False, left_marks=left,
                          installation_role='target')
    app._inflight_ids = list(inflight)
    return app


@pytest.fixture
def ledger(monkeypatch):
    from mycelium_a13_macos import adaptive_drain
    rows = {'ids': []}
    monkeypatch.setattr(adaptive_drain, 'live_reservations',
                        lambda root: [{'request_id': rid, 'reservation_id': f'res-{rid}', 'kv_bytes': 1,
                                       'lease_expires_at': 9e18, 'status': 'COMMITTED'} for rid in rows['ids']])
    return rows


def test_handoff_names_the_running_requests_and_is_done_only_when_the_owner_took_them_all(tmp_path, ledger):
    from mycelium_a13_macos.member_leave import app_steps
    ledger['ids'] = ['r1', 'r2', 'r3']
    client = FakeClient({'r1': 'a7_kv_standby', 'r2': 'a6_replay', 'r3': 'not_in_flight'})
    app = stand_in(tmp_path, client)
    steps = app_steps(app)
    assert steps.handoff is not None                      # the member-side seam now exists
    assert steps.handoff() is True
    [planning] = client.sent
    assert planning['phase'] == 'planning' and planning['generation'] == 4 and planning['outcome'] is None
    assert planning['in_flight'] == ['r1', 'r2', 'r3']
    assert leave_status_decisions(app) == {'r1': 'a7_kv_standby', 'r2': 'a6_replay', 'r3': 'not_in_flight'}


def test_handoff_is_refused_when_any_request_is_left_to_loss_detection(tmp_path, ledger):
    from mycelium_a13_macos.member_leave import app_steps
    ledger['ids'] = ['r1', 'r2']
    client = FakeClient({'r1': 'a7_kv_standby'})          # r2 stays with loss detection
    assert app_steps(stand_in(tmp_path, client)).handoff() is False


def test_planning_is_sent_even_with_nothing_running_so_the_owner_can_plan_the_leave(tmp_path, ledger):
    from mycelium_a13_macos.member_leave import app_steps
    ledger['ids'] = []
    client = FakeClient()
    assert app_steps(stand_in(tmp_path, client)).handoff() is True
    assert client.sent[0]['in_flight'] == []


def test_handoff_ids_are_bounded_and_unusual_ids_are_not_sent(tmp_path, ledger):
    from mycelium_a13_macos.member_leave import app_steps
    from mycelium_a13_macos.member_withdrawal import MAX_IN_FLIGHT
    ledger['ids'] = [f'r{i}' for i in range(MAX_IN_FLIGHT + 9)] + ['bad id with spaces']
    client = FakeClient()
    app_steps(stand_in(tmp_path, client)).handoff()
    assert len(client.sent[0]['in_flight']) == MAX_IN_FLIGHT
    assert 'bad id with spaces' not in client.sent[0]['in_flight']


def test_an_owner_that_does_not_answer_makes_the_handoff_fail_and_the_member_keeps_draining(tmp_path, ledger):
    from mycelium_a13_macos.member_leave import app_steps
    ledger['ids'] = ['r1']
    steps = app_steps(stand_in(tmp_path, FakeClient(fail=RuntimeError('unreachable'))))
    with pytest.raises(LeaveError) as caught:
        steps.handoff()
    assert caught.value.code == 'owner_notice_failed'


def test_a_failed_handoff_is_recorded_failed_and_the_drain_still_runs_within_the_stall_bound(tmp_path):
    recorder = Announcing(inflight=(1, 0), handoff=True, fail={'handoff'})
    result = _leave(tmp_path, recorder)[0].run('smooth')
    assert result['handoff'] == 'failed' and result['state'] == 'left' and result['fallback'] is None
    assert recorder.log.count('inflight') == 2


def test_the_final_is_signed_with_the_outcome_and_marks_the_peer_as_having_left(tmp_path, ledger):
    from mycelium_a13_macos.member_leave import app_steps
    client = FakeClient()
    app = stand_in(tmp_path, client)
    steps = app_steps(app)
    steps.announce_withdrawal('drained')
    [final] = client.sent
    assert final['phase'] == 'final' and final['outcome'] == 'drained' and final['in_flight'] == []
    steps.disconnect()
    assert app.left_marks == [True]                        # a voluntary leave opens the re-join path at once
    assert app._left is True


def leave_status_decisions(app):
    return app._leave_handoff_decisions


def test_status_carries_the_handoff_decisions(tmp_path, ledger):
    from mycelium_a13_macos.member_leave import app_steps
    ledger['ids'] = ['r1']
    app = stand_in(tmp_path, FakeClient({'r1': 'a6_replay'}))
    app_steps(app).handoff()
    app._leave = MemberLeave(tmp_path, Recorder().steps())
    app._leave.run('instant')
    assert leave_status(app)['handoff_decisions'] == {'r1': 'a6_replay'}


# -- real signed round trip -----------------------------------------------------------------------------------------------

def test_a_smooth_leave_over_the_real_signed_channel_is_left_smoothly_at_the_owner(tmp_path, ledger):
    from mycelium_a13_macos.app import DemoApp
    from mycelium_a13_macos.demo_app import ensure_exchange
    from mycelium_a13_macos.local_contributor import create_local_contributor
    from mycelium_a13_macos.member_leave import app_steps
    from mycelium_a13_macos.member_withdrawal import MemberWithdrawalClient
    from test_app_authority import CONSENT
    owner = DemoApp(tmp_path / 'owner', check_platform=False).start()
    actor = None
    try:
        owner.action('/api/create', {'consent': CONSENT})
        actor = create_local_contributor(owner)
        exchange = ensure_exchange(owner)
        asked = []
        exchange.withdrawals.set_planner(lambda n, g, ids: asked.append((n, g, tuple(ids))) or {'r1': 'a7_kv_standby'})
        node, generation = actor.member.node_id, actor.member.generation
        ledger['ids'] = ['r1']
        mine = tmp_path / 'member'
        mine.mkdir()
        app = stand_in(mine, MemberWithdrawalClient(actor.peer), generation=generation)
        app.authority.member = actor.member
        leave = MemberLeave(mine, app_steps(app), poll_s=0.01)

        def release():                                  # the owner released the request: the ledger drains
            time.sleep(0.2)
            ledger['ids'] = []
        threading.Thread(target=release, daemon=True).start()
        result = leave.run('smooth')
        assert result['state'] == 'left' and result['handoff'] == 'done' and result['fallback'] is None
        assert asked == [(node, generation, ('r1',))]
        row = {m['node_id']: m for m in owner.status()['authority']['members']}[node]
        assert row['leave']['kind'] == 'left_smoothly' and row['leave']['drained'] is True
        assert row['lifecycle_state'] == 'STOPPED'
    finally:
        if actor is not None:
            actor.stop()
        owner.close()
