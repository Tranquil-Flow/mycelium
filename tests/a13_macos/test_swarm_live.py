# SPDX-License-Identifier: AGPL-3.0-only
"""Live swarm projection: what a visitor may see of a published swarm's route, helpers and host.

Sources are the owner app's real documents (owner-run status, ``adaptive_owner.adaptive_status``, the registry
``/api/v1/recovery/status`` runtime document of ``mycelium_a6_recovery``, observatory transport observations of
``routeExplorer/contracts``).  FAKE (labelled): the documents below are shaped like those sources.  A source that is
missing, off or malformed is reported as ``available: false`` with a closed reason; nothing is invented.
"""
import json

import pytest

from mycelium_a13_macos import swarm_live

RUN = {'state': 'serving', 'step': 'serving', 'failed_step': None, 'error_code': None, 'route_ready': True,
       'run_id': 'run-0123456789abcdef', 'started_at': 1_800_000_000, 'registry_origin': 'http://127.0.0.1:9',
       'participants': [{'node_id': 'a' * 20, 'label': 'This Mac (owner)', 'layers': [0, 14], 'state': 'serving',
                         'detail': None},
                        {'node_id': 'b' * 20, 'label': 'MacBook helper', 'layers': [14, 28], 'state': 'withdrawn',
                         'detail': 'helper withdrew its capacity'}]}
ADAPTIVE = {'enabled': True, 'game_mode': False, 'stopped': False, 'state': 'drain', 'mode': 'drain',
            'reason': 'memory_pressure', 'generation': 3, 'capacity_scale': 0.5, 'new_admissions_allowed': False,
            'expires_in_s': 12.0, 'withdraw_overdue': True, 'reserve_bytes': 1}


def recovery(*records, breaker='closed'):
    return {'protocol': 'x', 'binding': {}, 'requests': list(records),
            'breaker': {'state': breaker}}


def record(request_id, *, terminal=None, reconciled=None, attempt=0, cleanup=False, phase='running'):
    return {'request_id': request_id, 'path_id': 'p', 'attempt': 1, 'path_attempt': 0, 'phase': phase,
            'committed_token_count': 3, 'recovery_attempt': attempt, 'terminal': terminal,
            'cleanup_complete': cleanup, 'reconciled': reconciled}


def transport(*kinds):
    return {'segments': [{'edge_kind': 'transport_observation', 'transport': kind} for kind in kinds]
            + [{'edge_kind': 'logical_execution', 'transport': 'direct'}]}


def project(**sources):
    base = dict(owner_run=RUN, adaptive=None, recovery=None, observatory=None, busy=False)
    base.update(sources)
    return swarm_live.project(**base)


def test_route_section_names_the_publication_state_and_never_the_registry_origin():
    live = project()
    assert live['route'] == {'available': True, 'state': 'serving', 'step': 'serving', 'route_ready': True,
                             'failed_step': None, 'error_code': None, 'run': 'run-0123456', 'started_at': 1_800_000_000}
    assert 'registry_origin' not in json.dumps(live) and 'node_id' not in json.dumps(live)


def test_a_failed_run_is_failed_with_its_closed_code_not_offline():
    live = project(owner_run=dict(RUN, state='failed', step='activate', failed_step='activate',
                                  error_code='owner_run_activate_failed', route_ready=False))
    assert live['route']['state'] == 'failed' and live['route']['error_code'] == 'owner_run_activate_failed'
    assert live['route']['failed_step'] == 'activate'


def test_no_owner_run_is_offline_and_helpers_unavailable():
    live = project(owner_run=None)
    assert live['route']['state'] == 'offline'
    assert live['helpers'] == {'available': False, 'reason': 'no_run'}


def test_helpers_show_label_layers_state_and_a_withdrawal_honestly():
    helpers = project()['helpers']
    assert helpers['available'] is True and helpers['count'] == 2 and helpers['withdrawn'] == 1
    assert helpers['rows'][0] == {'label': 'This Mac (owner)', 'node': 'aaaaaaaaaaaa', 'layers': [0, 14],
                                  'state': 'serving', 'detail': None, 'withdrawn': False}
    assert helpers['rows'][1]['withdrawn'] is True and helpers['rows'][1]['detail'] == 'helper withdrew its capacity'


def test_hostile_text_in_a_participant_is_bounded_and_markup_free():
    rows = project(owner_run=dict(RUN, participants=[
        {'node_id': 'c' * 20, 'label': '<img src=x onerror=alert(1)>' * 20, 'layers': [0, 1], 'state': 'x' * 500,
         'detail': 'y' * 500}]))['helpers']['rows']
    assert len(rows[0]['label']) <= 48 and len(rows[0]['state']) <= 48 and len(rows[0]['detail']) <= 120
    assert '<' not in rows[0]['label']


def test_adaptive_host_status_is_projected_for_the_entry_mac_and_off_is_unavailable_not_green():
    host = project(adaptive=ADAPTIVE)['host']
    assert host == {'available': True, 'state': 'drain', 'mode': 'drain', 'reason': 'memory_pressure',
                    'capacity_scale': 0.5, 'new_admissions_allowed': False, 'withdraw_overdue': True,
                    'stopped': False, 'scope': 'entry_mac'}
    assert project(adaptive={'enabled': False, 'state': 'off'})['host'] == {'available': False, 'reason': 'adaptive_off'}
    assert project(adaptive=None)['host'] == {'available': False, 'reason': 'adaptive_source_missing'}


def test_recovery_outcomes_count_resumed_and_aborted_separately():
    live = project(recovery=recovery(
        record('r1', terminal='completed', reconciled='resumed', attempt=1, cleanup=True),
        record('r2', terminal='failed', reconciled='aborted', attempt=2, cleanup=True),
        record('r3', attempt=1, phase='recovering'),
        record('r4', terminal='completed', reconciled='already_terminal', cleanup=True),
        record('r5', terminal='completed', cleanup=True), breaker='open'))
    assert live['recovery'] == {'available': True, 'requests': 5, 'recovered': 1, 'aborted': 1, 'recovering': 1,
                                'already_terminal': 1, 'max_attempt': 2, 'breaker': 'open'}


def test_a_malformed_recovery_document_is_unavailable_not_zeros():
    for bad in ('x', {'requests': 'no', 'breaker': {}}, {'requests': [{'terminal': 1}], 'breaker': {'state': 'closed'}}):
        assert project(recovery=bad)['recovery'] == {'available': False, 'reason': 'recovery_document_invalid'}
    assert project(recovery=None)['recovery'] == {'available': False, 'reason': 'recovery_source_unreachable'}


def test_transport_counts_only_real_transport_observations():
    live = project(observatory=transport('direct', 'relay', 'relay', 'unknown'))
    assert live['transport'] == {'available': True, 'direct': 1, 'relay': 2, 'unknown': 1}
    assert project(observatory={'segments': []})['transport'] == {'available': False, 'reason': 'no_transport_observation'}
    assert project(observatory=None)['transport'] == {'available': False, 'reason': 'observatory_unreachable'}


def test_sessions_state_one_prompt_at_a_time_and_whether_it_is_busy():
    assert project(busy=True)['sessions'] == {'available': True, 'max_concurrent': 1, 'busy': True,
                                              'policy': 'one prompt at a time per swarm; others get "busy"'}


class App:
    def __init__(self, run=RUN):
        self.run = run
        self.owner_run = type('R', (), {'status': lambda s: run})()
        self.chat = type('C', (), {'state': lambda s: {'busy': True}})()


def test_live_state_reads_each_source_and_survives_every_source_failing():
    reads = []

    def reader(path):
        reads.append(path)
        return recovery() if 'recovery' in path else transport('relay')

    live = swarm_live.live_state(App(), adaptive=lambda: ADAPTIVE, read_json=reader)
    assert sorted(reads) == ['/api/v1/observatory/snapshot', '/api/v1/recovery/status']
    assert live['recovery']['available'] and live['transport']['relay'] == 1 and live['sessions']['busy'] is True

    def boom(*_):
        raise OSError('down')

    dead = swarm_live.live_state(App(), adaptive=boom, read_json=boom)
    assert dead['route']['state'] == 'serving'
    assert dead['host']['available'] is False and dead['recovery']['available'] is False
    assert dead['transport']['available'] is False


def test_live_state_uses_the_registry_origin_only_for_the_reader_and_caches_briefly():
    calls = []
    app = App()
    clock = [0.0]
    reader_for = lambda origin: (lambda path: calls.append((origin, path)) or recovery())  # noqa: E731
    first = swarm_live.live_state(app, reader_factory=reader_for, clock=lambda: clock[0])
    swarm_live.live_state(app, reader_factory=reader_for, clock=lambda: clock[0])
    assert {origin for origin, _ in calls} == {'http://127.0.0.1:9'}
    assert len(calls) == 2  # two paths, second call served from the cache
    clock[0] += 60
    swarm_live.live_state(app, reader_factory=reader_for, clock=lambda: clock[0])
    assert len(calls) == 4
    assert first['route']['available'] is True
