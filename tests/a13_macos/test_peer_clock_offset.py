# SPDX-License-Identifier: AGPL-3.0-only
"""Owner-side qualification of each peer's clock offset (V1.1 item a, live demo RCA 2026-10-20).

The laptop's clock was 9.19 s ahead and nothing on the owner noticed until every chat failed.  The owner now records each
peer's offset from the timestamps the peer signs (request ``issued_at`` vs the owner's receipt time, corrected by half the
peer's measured round trip), shows it in ``/api/status`` and refuses to admit a route over a peer beyond
``MAX_CLOCK_SKEW_SECONDS`` with the bounded code ``peer_clock_skew_exceeded``.
"""
import time
from types import SimpleNamespace

import pytest

from mycelium_a13_macos.clock_offset import PeerClockOffsets
from mycelium_a13_macos.managed_control import _BINDING_FIELDS, MAX_CLOCK_SKEW_SECONDS
from mycelium_a13_macos.run_exchange import (DISCOVERY, MEMBER_REPORT, READ, REPORT, RunExchange,
                                             validate_report)
from test_demo_chain import LOCAL, REMOTE, make, wait

T = 1_000_000.0


class Clock:
    def __init__(self, now=T): self.now = now
    def __call__(self): return self.now


def test_a_peer_clock_ahead_is_measured_and_a_behind_one_is_negative():
    offsets = PeerClockOffsets(clock=Clock())
    offsets.observe('ahead', signed_at=T + 9.19, received_at=T)
    offsets.observe('behind', signed_at=T - 12.5, received_at=T)
    assert offsets.offset('ahead') == pytest.approx(9.19)
    assert offsets.offset('behind') == pytest.approx(-12.5)
    assert offsets.offset('never-heard') is None


def test_the_offset_is_corrected_by_half_the_round_trip():
    # signed at the peer's T+9.19, received 0.4 s later in true time (one way = rtt/2 = 0.2 s)
    offsets = PeerClockOffsets(clock=Clock())
    offsets.observe('a', signed_at=T + 9.19, received_at=T + 0.2, rtt_seconds=0.4)
    assert offsets.offset('a') == pytest.approx(9.19)


def test_a_single_outlier_does_not_move_the_median():
    offsets = PeerClockOffsets(clock=Clock())
    for value in (9.1, 9.2, 9.2, 9.3, 40.0):
        offsets.observe('a', signed_at=T + value, received_at=T)
    assert offsets.offset('a') == pytest.approx(9.2)


def test_exceeded_means_beyond_the_documented_tolerance_either_way():
    offsets = PeerClockOffsets(clock=Clock())
    offsets.observe('ok', signed_at=T + MAX_CLOCK_SKEW_SECONDS - 1, received_at=T)
    offsets.observe('fast', signed_at=T + MAX_CLOCK_SKEW_SECONDS + 1, received_at=T)
    offsets.observe('slow', signed_at=T - MAX_CLOCK_SKEW_SECONDS - 1, received_at=T)
    assert sorted(offsets.exceeded()) == ['fast', 'slow']
    assert offsets.exceeded(['ok']) == {}
    assert offsets.snapshot()['fast']['exceeds_tolerance'] is True
    assert offsets.snapshot()['ok']['exceeds_tolerance'] is False


def test_an_old_observation_is_not_held_against_a_peer_that_fixed_its_clock():
    clock = Clock()
    offsets = PeerClockOffsets(clock=clock, stale_after=120.0)
    offsets.observe('a', signed_at=T + 90, received_at=T)
    clock.now += 121
    assert offsets.offset('a') is None and offsets.exceeded() == {}
    offsets.observe('a', signed_at=clock.now + 0.5, received_at=clock.now)
    assert offsets.offset('a') == pytest.approx(0.5)


def _exchange(clock):
    # The mailbox's signature/ledger checks are not under test here: it hands the statement back as verified.
    mailbox = SimpleNamespace(
        _verify_target=lambda envelope, protocol, fields: (envelope['statement'], {'node_id': envelope['statement']['target_node_id']}),
        _response=lambda protocol, request_id, fields: {'request_id': request_id, **fields})
    return RunExchange(mailbox, clock=clock)


def _statement(node, *, issued_at, **extra):
    return {'statement': {'request_id': 'r', 'issued_at': issued_at, **{key: 'x' for key in _BINDING_FIELDS},
                          'target_node_id': node, 'run_id': DISCOVERY, 'deployment_id': DISCOVERY, **extra}}


def test_the_exchange_records_the_offset_of_every_peer_request():
    clock = Clock()
    exchange = _exchange(clock)
    exchange._read(_statement(REMOTE, issued_at=T + 9.19))
    assert exchange.clock_offsets.offset(REMOTE) == pytest.approx(9.19)


def test_a_report_carries_the_peers_round_trip_for_the_correction():
    clock = Clock(T + 0.2)
    exchange = _exchange(clock)
    report = {'protocol': MEMBER_REPORT, 'run_id': None, 'deployment_id': None, 'state': 'idle', 'detail': None,
              'model': None, 'progress': None, 'descriptor': None, 'issued_at': T + 9.19, 'clock_rtt_seconds': 0.4}
    assert validate_report(report)['clock_rtt_seconds'] == 0.4
    exchange._report(_statement(REMOTE, issued_at=T + 9.19, report=report))
    assert exchange.clock_offsets.offset(REMOTE) == pytest.approx(9.19)
    for bad in (-1, float('nan'), 'x', 4000):
        with pytest.raises(ValueError):
            validate_report({**report, 'clock_rtt_seconds': bad})


def test_a_report_without_a_round_trip_is_still_valid():
    report = {'protocol': MEMBER_REPORT, 'run_id': None, 'deployment_id': None, 'state': 'idle', 'detail': None,
              'model': None, 'progress': None, 'descriptor': None, 'issued_at': time.time()}
    assert validate_report(report)['state'] == 'idle'


def test_the_offsets_survive_a_new_run_clearing_the_reports():
    exchange = _exchange(Clock())
    exchange._read(_statement(REMOTE, issued_at=T + 3))
    exchange.forget_reports()
    assert exchange.clock_offsets.offset(REMOTE) == pytest.approx(3)


def test_start_refuses_a_peer_beyond_the_tolerance_with_a_bounded_code(tmp_path):
    app, ops, members, run = make(tmp_path)
    now = time.time()
    app.run_exchange.clock_offsets.observe(REMOTE, signed_at=now + 45, received_at=now)
    run.start()
    status = wait(run, lambda s: s['state'] in {'serving', 'failed'})
    assert (status['state'], status['failed_step'], status['error_code']) == ('failed', 'descriptors', 'peer_clock_skew_exceeded')
    assert [c[0] for c in ops.calls if c[0] != 'stop'] == []  # no route was prepared over the skewed peer
    message = status['error_message'] or ''
    assert '45' in message and 'clock' in message.lower() and 'ahead' in message


def test_start_admits_a_peer_with_a_nine_second_offset_and_reports_it(tmp_path):
    app, ops, members, run = make(tmp_path)
    now = time.time()
    app.run_exchange.clock_offsets.observe(REMOTE, signed_at=now + 9.19, received_at=now)
    run.start()
    status = wait(run, lambda s: s['state'] in {'serving', 'failed'})
    assert status['state'] == 'serving', status
    assert app.run_exchange.clock_offsets.snapshot()[REMOTE]['offset_seconds'] == pytest.approx(9.19)


def test_api_status_shows_each_members_clock_offset(tmp_path):
    from mycelium_a13_macos.app import DemoApp
    from mycelium_a13_macos.local_contributor import create_local_contributor
    from test_app_authority import CONSENT
    app = DemoApp(tmp_path / 'owner', check_platform=False).start()
    actor = None
    try:
        app.action('/api/create', {'consent': CONSENT})
        actor = create_local_contributor(app)
        exchange = RunExchange(app.authority.control_mailbox)
        app.run_exchange = exchange
        now = time.time()
        exchange.clock_offsets.observe(actor.member.node_id, signed_at=now + 9.19, received_at=now)
        members = {m['node_id']: m for m in app.status()['authority']['members']}
        clock = members[actor.member.node_id]['clock']
        assert clock['offset_seconds'] == pytest.approx(9.19) and clock['exceeds_tolerance'] is False
        assert clock['tolerance_seconds'] == MAX_CLOCK_SKEW_SECONDS
        assert all('clock' in m for m in members.values())  # unmeasured peers say so rather than omit
    finally:
        if actor is not None: actor.stop()
        app.close()
