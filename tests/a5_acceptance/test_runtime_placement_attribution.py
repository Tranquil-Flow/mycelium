"""Tiny actual tensor operations; no physical or useful-model claim."""
from dataclasses import replace
import pytest
from test_router_numpy_qwen_kv import _case, _work_item
from mycelium_router.payloads import encode_token_ids
from test_router_mlx_runtime import runtime_case, _fresh_ports, _work_item as mlx_item


def test_numpy_placement_work_counts_only_applied_operations(tmp_path):
    case = _case(tmp_path, 'qwen2')
    port = case.ports[0]
    item = _work_item(case, 0, encode_token_ids((1, 2, 3)),
                      request_id='request:attribution', path_id='path:attribution',
                      lease_expires_at=1_000_000_000_000.0)
    assert port.execute(item).success
    assert port.execute(item).success  # Idempotent replay must not count work twice.
    counters = port.kv_snapshot()['placement_counters']
    assert set(counters) == {item.placement_id}
    assert counters[item.placement_id]['prefill_operation_count'] == 1
    assert counters[item.placement_id]['decode_operation_count'] == 0
    assert counters[item.placement_id]['active_state_count'] == 1
    assert counters[item.placement_id]['active_kv_bytes'] > 0
    wrong = replace(item, placement_id='unbound')
    assert not port.execute(wrong).success
    assert port.kv_snapshot()['placement_counters'] == counters


def test_mlx_placement_work_is_not_inferred_from_admission(runtime_case):
    port = _fresh_ports(runtime_case)[0]
    item = mlx_item(runtime_case, 0, encode_token_ids((1, 2, 3)))
    try:
        assert port.execute(item).success
        assert port.execute(item).success
        counters = port.kv_snapshot()['placement_counters'][item.placement_id]
        assert counters['prefill_operation_count'] == 1
        assert counters['decode_operation_count'] == 0
        assert counters['active_state_count'] == 1
        assert counters['active_kv_bytes'] > 0
    finally:
        port.close()
