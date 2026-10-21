# SPDX-License-Identifier: AGPL-3.0-only
"""workflow.py slice glue: a slice plan binds member slice prepositions, never whole HF files."""
import pytest

from mycelium_a13_macos import workflow


def test_without_slice_plan_the_hf_binding_is_unchanged(monkeypatch):
    calls = []
    monkeypatch.setattr('mycelium_a13_macos.managed_transfers.bind_hf_prepositions',
                        lambda c, **k: calls.append(('hf', k)) or {**c, 'bound': 'hf'})
    result, plans = workflow.bind_member_artifacts({'peers': []}, prepared='P',
                                                   target_source_roots={'a': '/x'}, slice_plan=None)
    assert result['bound'] == 'hf' and plans is None
    assert calls == [('hf', {'prepared': 'P', 'target_source_roots': {'a': '/x'}})]


def test_slice_plan_binds_each_member_node_plan_and_returns_them(monkeypatch):
    calls = []
    monkeypatch.setattr('mycelium_a13_macos.slice_acquisition.node_slice_plan',
                        lambda plan, node: {'node_id': node, 'node_plan_digest': 'sha256:' + node * 64})
    monkeypatch.setattr('mycelium_a13_macos.slice_acquisition.bind_slice_prepositions',
                        lambda c, **k: calls.append(k) or {**c, 'bound': 'slice'})
    monkeypatch.setattr('mycelium_a13_macos.managed_transfers.bind_hf_prepositions',
                        lambda *a, **k: pytest.fail('whole-file HF binding used in slice mode'))
    controller = {'peers': [{'node_id': 'a'}, {'node_id': 'b'}]}
    result, plans = workflow.bind_member_artifacts(controller, prepared='P',
        target_source_roots={'a': '/slices/a', 'b': '/slices/b'}, slice_plan={'plan': 1})
    assert result['bound'] == 'slice'
    assert set(plans) == {'a', 'b'} and plans['a']['node_id'] == 'a'
    assert calls == [{'node_plans': plans, 'target_slice_roots': {'a': '/slices/a', 'b': '/slices/b'}}]


def test_prepare_serving_workflow_accepts_slice_plan_keyword():
    import inspect
    assert 'slice_plan' in inspect.signature(workflow.prepare_serving_workflow).parameters
