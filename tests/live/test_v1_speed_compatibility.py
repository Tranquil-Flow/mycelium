# SPDX-License-Identifier: AGPL-3.0-only
"""V1 OFF wire projection stays legacy; speed evidence is explicitly opted in."""
from types import SimpleNamespace

import pytest

import physical_inference_node as node
from mycelium_live.route import _public_interruptibility


def snapshot(tmp_path, monkeypatch, mode, enabled):
    monkeypatch.setenv('MYCELIUM_TOKEN_PATH', 'on' if enabled else 'off')
    runtime = dict(mode='stage_local_kv',backend='mlx',states={},active_state_count=0,
        active_kv_bytes=0,materialization=mode,
        work_unit='transformer_stage' if mode=='per_stage' else 'transformer_layer',
        maximum_observed_work_unit_ms=125.,observed_work_unit_count=1)
    service=object.__new__(node.PhysicalNodeService)
    service.state='CONFIGURED'
    service.runtime=SimpleNamespace(kv_snapshot=lambda:runtime)
    service.capacity=service.transport=service.sidecar=None
    service.runtime_identity={'backend':'mlx','dtype':'float32','quantization':'int8-weight-only'}
    service.artifact_root=tmp_path
    service._last_cancellation=None
    service._signed_result=lambda event,details=None:{'event':event,'details':details}
    return service._snapshot({})['details'],runtime


@pytest.mark.parametrize('setting',[None,'off'])
def test_off_serving_factory_retains_v1_layer_work_unit(monkeypatch,setting):
    from mycelium_router import mlx_runtime
    if setting is None:monkeypatch.delenv('MYCELIUM_TOKEN_PATH',raising=False)
    else:monkeypatch.setenv('MYCELIUM_TOKEN_PATH',setting)
    captured={}
    monkeypatch.setattr(node,'_route_decode_mode',lambda *args:'stage_local_kv')
    monkeypatch.setattr(mlx_runtime,'MLXRuntimePort',lambda *args,**kwargs:captured.update(kwargs) or captured)
    service=object.__new__(node.PhysicalNodeService)
    service._clock=SimpleNamespace(now=lambda:0.)
    service.node_id='desk';service.requested_decode_mode=None
    placement=SimpleNamespace(runtime_backend='mlx',placement_id='p')
    result=service._build_runtime_port(placement,SimpleNamespace(),SimpleNamespace(proof={}))
    assert result['materialization']=='per_layer'


def test_off_signed_snapshot_retains_exact_v1_interruptibility_shape(tmp_path,monkeypatch):
    details,raw=snapshot(tmp_path,monkeypatch,'per_layer',False)
    expected=dict(runtime_backend='mlx',decode_mode='stage_local_kv',work_unit='transformer_layer',
        maximum_observed_work_unit_ms=125.,observed_work_unit_count=1,
        maximum_total_cleanup_ms=4200,physical_proof_required=True,
        backend_candidate=True,cooperative_bound_candidate=True)
    assert details['interruptibility']==expected
    assert _public_interruptibility(details['interruptibility'])==expected
    assert 'materialization' not in details['runtime'] and 'work_unit' not in details['runtime']
    assert raw['materialization']=='per_layer' and raw['work_unit']=='transformer_layer'  # Do not mutate the runtime.


@pytest.mark.parametrize('enabled',[False,True])
def test_explicit_stage_measurement_is_never_mislabeled_as_legacy(tmp_path,monkeypatch,enabled):
    # The isolated benchmark explicitly constructs per_stage in both control arms.
    details,_=snapshot(tmp_path,monkeypatch,'per_stage',enabled)
    assert details['interruptibility']['work_unit']=='transformer_stage'
    assert details['interruptibility']['materialization']=='per_stage'
    assert details['runtime']['materialization']=='per_stage'
