"""Adversarial HTTP-boundary fixtures, never physical evidence."""
from copy import deepcopy
import pytest
from scripts import run_a5_product_gate as gate
from tests.a5_acceptance.test_replica_contracts import _qualification_payload


def exercise(monkeypatch, *, final_kv=0, omit_final_peer=False, missing_work=False):
    qualification = _qualification_payload()
    placements = ['primary', 'placement-fixture-replica', 'placement-fixture-stage-1']
    nodes = {p: f'node-{i}' for i, p in enumerate(placements)}
    requests = [{'request_id': str(i), 'placement_ids': [p, placements[2]]} for i, p in enumerate(placements[:2])]
    def runtime(active):
        return {'protocol': 'fixture-admission', 'deployment_id': 'deployment-fixture',
                'deployment_epoch': 1, 'topology_version': 1, 'graph_digest': 'fixture',
                'queue': {'depth': 0, 'active_request_ids': ['0', '1'] if active else [], 'maximum_active_requests': 2},
                'placements': [{'placement_id': p, 'node_id': nodes[p], 'active_reservations': int(active)} for p in placements],
                'requests': deepcopy(requests)}
    def live(work, kv):
        return {'route_alive': True, 'deployment_id': 'deployment-fixture', 'topology_version': 1,
                'replica_track_qualification': [qualification],
                'peers': [{'node_id': n, 'frames_sent': work, 'frames_received': work,
                           'applied_operation_count': work, 'active_kv_state_count': kv,
                           'placement_counters': {p: {'prefill_operation_count': work, 'decode_operation_count': work,
                               'active_state_count': kv, 'active_kv_bytes': kv * 64} for p, owner in nodes.items()
                               if owner == n and not (missing_work and work and p == placements[1])}}
                          for n in nodes.values()]}
    final = live(4, final_kv)
    if omit_final_peer:
        final['peers'].pop()
    snapshots = iter([live(0, 0), runtime(False), runtime(True), live(4, 1), runtime(False), final])
    monkeypatch.setattr(gate, 'public_json', lambda *args: next(snapshots))
    class Session:
        count = 0
        def __init__(self, _):
            self.index = Session.count; Session.count += 1
            self._qualification = {'binding': {'model_id': 'fixture'}}
        def submit(self, **kwargs):
            return {'request_id': str(self.index)}
        def stream_summary(self, accepted):
            return {'request_id': accepted['request_id'], 'terminal': 'completed'}
    monkeypatch.setattr(gate, 'ProductSession', Session)
    return gate.run_gate('http://127.0.0.1:1', maximum_new_tokens=2)


def test_clean_control(monkeypatch):
    assert exercise(monkeypatch)['cleanup_zero_delta'] is True


@pytest.mark.parametrize('kwargs', [{'final_kv': 1}, {'omit_final_peer': True}])
def test_positive_gate_rejects_unproven_cleanup(monkeypatch, kwargs):
    with pytest.raises(gate.GateError, match='cleanup'):
        exercise(monkeypatch, **kwargs)


def test_node_aggregate_work_cannot_substitute_for_placement_work(monkeypatch):
    with pytest.raises(gate.GateError, match='placement_work'):
        exercise(monkeypatch, missing_work=True)
