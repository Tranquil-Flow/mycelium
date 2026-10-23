# SPDX-License-Identifier: AGPL-3.0-only
"""Actual tiny MLX composition of the native benchmark serving factory.

The assignment loader is replaced with already-authenticated tiny fixture stages;
factory selection, model execution, KV lifecycle and evidence are production code.
Not a two-process, physical, or performance qualification.
"""
import json
import time
from types import SimpleNamespace

import runtime_loader
import pytest
from scripts import tokenpath_ab_bench
from scripts.tokenpath_ab_bench import load_native_runtime
from test_router_mlx_runtime import _run_two_stage, runtime_case  # noqa: F401


@pytest.fixture(autouse=True)
def private_bench_admission(tmp_path, monkeypatch):
    # This in-process tiny-model test must not read a live demo's marker.
    # Keep the real admission predicate, including its refusal behavior.
    root = tmp_path / 'private-bench-admission'
    root.mkdir()
    check_busy = tokenpath_ab_bench.check_busy
    monkeypatch.setattr(tokenpath_ab_bench, 'check_busy', lambda: check_busy(root))
    return root


def test_native_token_factory_preserves_private_busy_refusal(private_bench_admission, monkeypatch):
    (private_bench_admission / 'DEVICE.busy').write_text('owned test marker')
    def forbidden_load(*args, **kwargs):
        raise AssertionError('busy admission must precede loading')
    monkeypatch.setattr(runtime_loader, 'load_assignment_stage', forbidden_load)
    with pytest.raises(tokenpath_ab_bench.Busy, match=r'^DEVICE\.busy$'):
        load_native_runtime(None, {})


def test_native_token_factory_executes_stage_units_and_names_evidence(runtime_case, tmp_path, monkeypatch):
    case = runtime_case
    by_assignment = {a['assignment_id']: loaded for a, loaded in zip(case.assignments, case.loaded)}
    calls = []

    def already_loaded(assignment, report, *, load_generation):
        assert report == {'fixture': 'already-authenticated-stage'}
        calls.append((assignment['assignment_id'], load_generation))
        return by_assignment[assignment['assignment_id']]

    monkeypatch.setattr(runtime_loader, 'load_assignment_stage', already_loaded)
    ports = []
    for i, assignment in enumerate(case.assignments):
        ap, rp = tmp_path/f'assignment-{i}.json', tmp_path/f'report-{i}.json'
        ap.write_text(json.dumps(assignment))
        rp.write_text(json.dumps({'fixture': 'already-authenticated-stage'}))
        node = SimpleNamespace(node_id=assignment['node_id'], graph=case.graph,
                               _clock=SimpleNamespace(now=time.monotonic))
        ports.append(load_native_runtime(node, {'assignment': str(ap), 'report': str(rp)}))
    assert calls == [(a['assignment_id'], 17) for a in case.assignments]
    # The qualification constructor is deliberately not flipped by integration.
    assert all(port.materialization == 'per_layer' for port in case.ports)
    ids = (1, 2, 3)
    try:
        for step in range(3):
            _, token = _run_two_stage(case, ids, phase='PREFILL' if step == 0 else 'DECODE',
                path_id='native-token-serving', token_index=-1 if step == 0 else step,
                position=0 if step == 0 else step + 2, ports=tuple(ports))
            ids = (token,)
        for port in ports:
            evidence = port.kv_snapshot()
            assert evidence['materialization'] == 'per_stage'
            assert evidence['work_unit'] == 'transformer_stage'
            assert evidence['observed_work_unit_count'] == 3
            assert evidence['maximum_observed_work_unit_ms'] > 0
            assert len(port.executions) == 3
            assert all(row['success'] for row in port.executions)
    finally:
        for port in ports:
            port.cancel('native-token-serving')
    assert all(port.kv_snapshot()['active_state_count'] == 0 for port in ports)
