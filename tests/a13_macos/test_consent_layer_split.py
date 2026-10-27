# SPDX-License-Identifier: AGPL-3.0-only
"""Consent-aware contiguous placement; no physical/model execution."""
from itertools import combinations
from types import SimpleNamespace

import pytest

from mycelium_a13_macos import model_resources as R
from mycelium_a13_macos.chain_compile import plan_member_split, member_resource_plan
from mycelium_a13_macos.layer_split import split_ranges
from mycelium_a13_macos.serving import A9_ROUTE_MODEL

GiB = 1 << 30


def members():
    return [{'node_id': 'a-laptop', 'memory_bytes': 16 * GiB,
             'caps': {'storage_bytes': 2_000_000_000, 'max_memory_bytes': 4 * GiB}},
            {'node_id': 'b-owner', 'memory_bytes': 48 * GiB,
             'caps': {'storage_bytes': 2_000_000_000, 'max_memory_bytes': 4 * GiB}}]


def test_real_default_caps_rebalance_the_23_layer_owner_share():
    rows = members()
    plan = R.plan_for_members(A9_ROUTE_MODEL, rows, slice_mode=False)
    assert [r['max_disk_bytes'] for r in plan['members']] != [1505687777, 2227449134]
    assert all(R.check_member_caps(r, m['caps']) is None for r, m in zip(plan['members'], rows))
    assert plan['split']['stages'][0]['end'] > 1
    assert split_ranges(plan['split']) == [[0, plan['split']['stages'][0]['end']],
                                          [plan['split']['stages'][0]['end'], 24]]
    assert plan == R.plan_for_members(A9_ROUTE_MODEL, rows, slice_mode=False)
    # Preserve the document shape and old digest rules, not a new wire contract.
    assert all(set(m) == {'node_id', 'memory_bytes'} for m in plan['split']['members'])


def test_memory_caps_move_layers_instead_of_refusing_a_feasible_split():
    rows = members()
    for row in rows:
        row['caps'] = {'max_memory_bytes': 2_200_000_000, 'storage_bytes': 4_000_000_000}
    plan = R.plan_for_members(A9_ROUTE_MODEL, rows, slice_mode=False)
    assert all(R.check_member_caps(r, m['caps']) is None for r, m in zip(plan['members'], rows))


@pytest.mark.parametrize('field,cap,code', [
    ('storage_bytes', 1_400_000_000, 'member_storage_cap_below_share'),
    ('max_memory_bytes', 1_200_000_000, 'member_memory_cap_below_share'),
])
def test_impossible_split_refuses_with_plain_named_resource_message(field, cap, code):
    rows = members()
    for row in rows:
        row['caps'][field] = cap
    with pytest.raises(R.ResourceRefused) as error:
        R.plan_for_members(A9_ROUTE_MODEL, rows, slice_mode=False)
    assert error.value.code == code
    assert error.value.node_id in {'a-laptop', 'b-owner'}
    assert 'agreed to at most' in error.value.message and 'needs' in error.value.message


def test_impossible_known_cap_takes_precedence_over_memory_weight_budget():
    from mycelium_a13_macos.serving import QWEN3_14B_MODEL
    rows = [{'node_id': 'a', 'memory_bytes': 16 * GiB, 'caps': {'max_memory_bytes': GiB}},
            {'node_id': 'b', 'memory_bytes': 8 * GiB, 'caps': {'max_memory_bytes': GiB}}]
    with pytest.raises(R.ResourceRefused) as error:
        R.plan_for_members(QWEN3_14B_MODEL, rows)
    assert error.value.code == 'member_memory_cap_below_share'
    assert 'agreed to at most' in error.value.message


def test_compile_planner_consumes_caps_and_whole_snapshot_mode(monkeypatch):
    from mycelium_a13_macos import chain_compile
    monkeypatch.setattr(chain_compile, 'model_layer_profile', lambda root: R.require_shape(A9_ROUTE_MODEL).layer_profile())
    reports = {m['node_id']: m for m in members()}
    split = plan_member_split(reports, model_root='unused', model_contract=A9_ROUTE_MODEL, slice_mode=False)
    plan = member_resource_plan(A9_ROUTE_MODEL, split, reports=reports, slice_mode=False)
    assert all(R.check_member_caps(r, reports[r['node_id']]['caps']) is None for r in plan['members'])


def test_prepublication_resource_plan_rechecks_known_caps():
    old = R.plan_for_members(A9_ROUTE_MODEL, [{k: v for k, v in m.items() if k != 'caps'} for m in members()],
                             slice_mode=False)
    with pytest.raises(R.ResourceRefused, match='member_storage_cap_below_share'):
        member_resource_plan(A9_ROUTE_MODEL, old['split'], reports={m['node_id']: m for m in members()},
                             slice_mode=False)


def test_three_member_search_matches_exhaustive_contiguous_oracle():
    from itertools import product
    from mycelium_a13_macos.layer_split import LayerProfile, SplitPolicy, SplitRefused, memory_proportional_split
    profile = LayerProfile('qwen2', 6, (1, 3, 2, 5, 1, 4), 3, 2, True)
    rows = [{'node_id': f'n{i}', 'memory_bytes': 100 * (i + 1)} for i in range(3)]
    policy = SplitPolicy(runtime_overhead_bytes=0)
    for caps in product((5, 9, 14, 25), repeat=3):
        def check(stage, member):
            if stage['stage_bytes'] > caps[int(member['node_id'][1:])]:
                raise SplitRefused('member_storage_cap_below_share', member['node_id'])
        feasible = []
        for a, b in combinations(range(1, 6), 2):
            sizes = (3 + sum(profile.layer_bytes[:a]), sum(profile.layer_bytes[a:b]),
                     sum(profile.layer_bytes[b:]) + 5)
            if all(size <= cap for size, cap in zip(sizes, caps)):
                feasible.append([[0, a], [a, b], [b, 6]])
        if feasible:
            split = memory_proportional_split(rows, profile, policy, stage_check=check)
            assert split_ranges(split) in feasible
            assert split == memory_proportional_split(rows, profile, policy, stage_check=check)
        else:
            with pytest.raises(SplitRefused, match='member_storage_cap_below_share'):
                memory_proportional_split(rows, profile, policy, stage_check=check)


def test_owner_and_remote_caps_survive_capability_collection(tmp_path, monkeypatch):
    from mycelium_a13_macos import demo_chain, standing_consent, app as app_module
    caps = {'storage_bytes': 2_000_000_000, 'max_memory_bytes': 4 * GiB}
    monkeypatch.setattr(standing_consent, 'load_standing_consent', lambda root: {'caps': caps, 'state': 'granted'})
    monkeypatch.setattr(app_module, 'read_resource_policy', lambda root: {'storage_bytes': 1_900_000_000})
    app = SimpleNamespace(root=tmp_path,
        local_contributor=SimpleNamespace(member=SimpleNamespace(node_id='b-owner')),
        authority=SimpleNamespace(coordinator=SimpleNamespace(member=lambda node: {'generation': 1})),
        member_capability_reports=lambda: {'a-laptop': {'device': {'memory_bytes': 16 * GiB},
            'generation': 1, 'expires_at': 100, 'caps': caps}})
    records = demo_chain.member_capability_records(app, ['a-laptop', 'b-owner'], now=1,
                                                  local_device={'memory_bytes': 48 * GiB})
    assert records['a-laptop']['caps'] == caps
    assert records['b-owner']['caps'] == {**caps, 'storage_bytes': 1_900_000_000}
