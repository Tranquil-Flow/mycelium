# SPDX-License-Identifier: AGPL-3.0-only
"""Per-model, per-split resource proposals (model_resources.py).

Numbers are derived from the registered model shapes and the compiled
memory-proportional split; the Qwen2.5-7B candidate's measured constants stay
exact.  Demo topology: owner M4 Pro 48 GiB + joiner MacBook Air M1 8 GiB.
"""
from pathlib import Path

import pytest

from mycelium_a13_macos import model_resources as R
from mycelium_a13_macos.layer_split import SplitRefused, model_layer_profile
from mycelium_a13_macos.serving import (A9_ROUTE_MODEL, CANDIDATE_MODEL, QWEN3_0_6B_MODEL, QWEN3_8B_MODEL,
                                        QWEN3_14B_MODEL, REGISTERED_MODELS, ResourceProposal)

GiB = 1 << 30
OWNER, AIR = 'node-a-owner', 'node-b-air'
DEMO = [{'node_id': OWNER, 'memory_bytes': 48 * GiB}, {'node_id': AIR, 'memory_bytes': 8 * GiB}]

# The 2026-09-16/19 hand derivation (serving.RESOURCE_PROPOSAL), pinned here
# as literals so a refactor can never silently move them.
LEGACY_7B = ResourceProposal(network_download_bytes=11_535_382, execution_download_bytes=0,
                             additional_disk_bytes=24_854_140_006, max_combined_rss_bytes=16_955_405_108,
                             max_wall_seconds=1_800, run_count=1)


def test_every_registered_model_has_a_shape():
    for model in REGISTERED_MODELS:
        assert R.model_shape(model) is not None, model.model_id


def test_7b_candidate_reproduces_the_legacy_constants_exactly():
    assert R.model_resource_proposal(CANDIDATE_MODEL) == LEGACY_7B
    assert CANDIDATE_MODEL.resource_proposal == LEGACY_7B
    # Per-member limits are the runner's proposal / 2 on any 7B split.
    split = {'stages': [{'node_id': 'laptop', 'start': 0, 'end': 14, 'stage_bytes': 7_615_612_928},
                        {'node_id': 'm4pro', 'start': 14, 'end': 28, 'stage_bytes': 7_615_620_096}],
             'members': []}
    rows = R.member_resources(CANDIDATE_MODEL, split, slice_mode=False)
    assert {r['max_rss_bytes'] for r in rows} == {8_477_702_554} == {16_955_405_108 // 2}
    assert {r['max_disk_bytes'] for r in rows} == {12_427_070_003} == {24_854_140_006 // 2}
    assert R.split_resource_proposal(CANDIDATE_MODEL, split, slice_mode=False) == LEGACY_7B
    assert R.owner_storage_requirement(CANDIDATE_MODEL) == 23_052_975_062
    assert R.model_storage_ceiling(CANDIDATE_MODEL) == 24_854_140_006


def test_named_constants_reproduce_the_7b_recipe():
    assert R.measured_anchor(CANDIDATE_MODEL).group_peak_bytes == 7_371_915_264
    import math
    assert 2 * math.ceil(R.GUARD_HEADROOM * 7_371_915_264) == 16_955_405_108
    assert 2 * math.ceil(R.DISK_HEADROOM * 11_297_336_366) == 24_854_140_006
    # The modelled peak for the measured 7B stage is never below the measurement.
    shape = R.model_shape(CANDIDATE_MODEL)
    resident = math.ceil(7_615_620_096 * R.RESIDENT_FACTORS['int8-weight-only'])
    assert resident >= 3_811_704_832  # measured resident int8 weights of that stage
    kv = 14 * shape.kv_bytes_per_token_per_layer('float32') * R.DEMO_CONTEXT_TOKENS
    assert R.LOAD_PEAK_FACTOR * resident + R.GROUP_FIXED_BYTES + kv > 7_371_915_264


def test_demo_context_cap_covers_the_chat_limits():
    from mycelium_a13_macos.chat import MAX_NEW_TOKENS_CAP, MAX_PROMPT_CHARS
    assert R.DEMO_CONTEXT_TOKENS >= MAX_PROMPT_CHARS + MAX_NEW_TOKENS_CAP + 100


@pytest.mark.parametrize('model,weights', [
    (CANDIDATE_MODEL, 15_231_233_024), (A9_ROUTE_MODEL, 988_065_536),
    (QWEN3_0_6B_MODEL, 1_503_264_768), (QWEN3_8B_MODEL, 16_381_470_720),
    (QWEN3_14B_MODEL, 29_536_614_400)])
def test_shapes_match_the_pinned_safetensors_totals(model, weights):
    shape = R.model_shape(model)
    assert shape.weight_source_bytes == weights
    files = shape.snapshot_bytes - shape.non_weight_bytes
    # The files additionally hold only their JSON headers (< 64 KiB each model).
    assert 0 < files - weights < 64 * 1024


def _local_snapshot(model):
    root = Path.home() / '.cache/huggingface/hub' / ('models--' + model.model_id.replace('/', '--'))
    path = root / 'snapshots' / model.resolved_commit
    return path if (path / 'config.json').is_file() else None


@pytest.mark.parametrize('model', [QWEN3_0_6B_MODEL, QWEN3_8B_MODEL, QWEN3_14B_MODEL])
def test_shape_profile_equals_the_header_profile_of_the_local_snapshot(model):
    path = _local_snapshot(model)
    if path is None:
        pytest.skip('pinned snapshot not in the local HF cache')
    assert R.model_shape(model).layer_profile() == model_layer_profile(path)


def test_qwen3_0_6b_on_owner_plus_air():
    plan = R.plan_for_members(QWEN3_0_6B_MODEL, DEMO, owner_node_id=OWNER)
    owner, air = plan['members']
    assert (owner['start'], owner['end'], air['start'], air['end']) == (0, 27, 27, 28)
    assert owner['stage_source_bytes'] == 1_160_635_904 and air['stage_source_bytes'] == 653_793_792
    assert owner['max_rss_bytes'] == 3_614_068_964 and air['max_rss_bytes'] == 2_026_757_282
    assert air['max_disk_bytes'] == 797_606_656
    assert plan['proposal'].max_combined_rss_bytes == 2 * owner['max_rss_bytes']
    R.check_members(QWEN3_0_6B_MODEL, plan['members'])


def test_qwen3_8b_on_owner_plus_air():
    plan = R.plan_for_members(QWEN3_8B_MODEL, DEMO, owner_node_id=OWNER)
    owner, air = plan['members']
    assert (owner['end'], air['start']) == (34, 34)
    assert air['stage_source_bytes'] == 2_016_453_632
    assert air['max_rss_bytes'] == 3_635_537_813 <= R.member_memory_ceiling(8 * GiB)
    assert owner['max_rss_bytes'] == 19_099_590_582
    R.check_members(QWEN3_8B_MODEL, plan['members'])


def test_qwen3_14b_on_owner_plus_air_is_admitted_with_per_member_limits():
    plan = R.plan_for_members(QWEN3_14B_MODEL, DEMO, owner_node_id=OWNER)
    owner, air = plan['members']
    assert (owner['start'], owner['end'], air['start'], air['end']) == (0, 37, 37, 40)
    assert owner['stage_source_bytes'] == 25_998_907_904 and air['stage_source_bytes'] == 3_537_706_496
    # Air: 1.77 GB resident int8, modelled group peak 4.72 GB, guard 5.43 GB <= 6 GiB.
    assert air['resident_bytes'] == 1_772_390_955
    assert air['load_peak_bytes'] == 4_719_187_030
    assert air['max_rss_bytes'] == 5_427_065_085 <= R.member_memory_ceiling(8 * GiB) == 6 * GiB
    assert air['max_disk_bytes'] == 3_969_910_631
    # Owner: 13.0 GB resident int8, guard 32.6 GB <= 36 GiB on 48 GiB.
    assert owner['max_rss_bytes'] == 32_621_085_758 <= R.member_memory_ceiling(48 * GiB)
    assert plan['proposal'] == ResourceProposal(
        network_download_bytes=29_552_613_776, execution_download_bytes=0,
        additional_disk_bytes=2 * owner['max_disk_bytes'], max_combined_rss_bytes=2 * owner['max_rss_bytes'],
        max_wall_seconds=1800, run_count=1)
    R.check_members(QWEN3_14B_MODEL, plan['members'],
                    {OWNER: {'available_storage_bytes': 200_000_000_000},
                     AIR: {'available_storage_bytes': 21_000_000_000}})


def test_qwen3_14b_the_7b_bound_would_have_refused_the_owner_stage():
    plan = R.plan_for_members(QWEN3_14B_MODEL, DEMO, owner_node_id=OWNER)
    owner = plan['members'][0]
    assert owner['load_peak_bytes'] > LEGACY_7B.max_combined_rss_bytes // 2
    assert owner['max_disk_bytes'] > LEGACY_7B.additional_disk_bytes // 2


def test_storage_ceilings_admit_14b_and_keep_7b_values_expressible():
    ceiling = R.storage_consent_ceiling()
    # Set by Qwen3-14B: a whole-model member's slice disk (>= its owner download need).
    assert ceiling == R.whole_model_member(QWEN3_14B_MODEL)['max_disk_bytes'] == 32_568_709_325
    assert R.owner_storage_requirement(QWEN3_14B_MODEL) == 32_507_875_154 <= ceiling
    assert ceiling >= 29_552_613_776 >= LEGACY_7B.additional_disk_bytes
    assert R.pairing_quota_ceiling() == 32_568_709_325
    assert R.pairing_quota_ceiling() >= R.whole_model_member(QWEN3_8B_MODEL)['max_disk_bytes']


def test_memory_caps_are_realistic_for_an_8gb_air():
    assert R.member_memory_ceiling(8 * GiB) == 6 * GiB
    assert R.memory_cap_options(8 * GiB) == [4 * GiB, 5 * GiB, 6 * GiB]
    assert R.memory_cap_options(48 * GiB) == [24 * GiB, 30 * GiB, 36 * GiB]


def test_member_whose_guard_exceeds_its_memory_is_refused_by_name_with_a_message():
    split = {'stages': [{'node_id': OWNER, 'start': 0, 'end': 30, 'stage_bytes': 21_000_000_000},
                        {'node_id': AIR, 'start': 30, 'end': 40, 'stage_bytes': 8_536_614_400}],
             'members': DEMO}
    rows = R.member_resources(QWEN3_14B_MODEL, split, owner_node_id=OWNER)
    with pytest.raises(R.ResourceRefused) as error:
        R.check_members(QWEN3_14B_MODEL, rows)
    assert isinstance(error.value, SplitRefused)
    assert (error.value.code, error.value.node_id) == ('member_memory_insufficient_for_share', AIR)
    message = error.value.message
    assert 'Qwen3-14B' in message and 'layers 31-40' in message and '8.6 GB of memory' in message
    assert '6.4 GB' in message  # the 3/4-of-RAM ceiling it was measured against


def test_member_without_free_disk_for_its_share_is_refused():
    plan = R.plan_for_members(QWEN3_14B_MODEL, DEMO, owner_node_id=OWNER)
    with pytest.raises(R.ResourceRefused) as error:
        R.check_members(QWEN3_14B_MODEL, plan['members'], {AIR: {'available_storage_bytes': 5_000_000_000}})
    assert (error.value.code, error.value.node_id) == ('member_disk_insufficient_for_share', AIR)
    assert '5.0 GB of free disk' in error.value.message and '7.2 GB' in error.value.message


def test_too_small_mac_is_refused_by_the_split_with_a_message():
    with pytest.raises(SplitRefused) as error:
        R.plan_for_members(QWEN3_14B_MODEL, [{'node_id': OWNER, 'memory_bytes': 16 * GiB},
                                             {'node_id': AIR, 'memory_bytes': 8 * GiB}])
    assert error.value.code == 'member_cannot_hold_share'
    assert 'Qwen3-14B' in R.refusal_message(error.value.code, {'node_id': error.value.node_id},
                                            model=QWEN3_14B_MODEL)


def test_standing_caps_against_a_members_share():
    plan = R.plan_for_members(QWEN3_14B_MODEL, DEMO, owner_node_id=OWNER)
    air = plan['members'][1]
    assert R.check_member_caps(air, {'max_memory_bytes': 6 * GiB, 'storage_bytes': 8_000_000_000}) is None
    assert R.check_member_caps(air, {'max_memory_bytes': 5 * GiB, 'storage_bytes': 8_000_000_000}) \
        == 'member_memory_cap_below_share'
    assert R.check_member_caps(air, {'max_memory_bytes': 6 * GiB, 'storage_bytes': 2_000_000_000}) \
        == 'member_storage_cap_below_share'
    text = R.cap_refusal_message('member_memory_cap_below_share', air, {'max_memory_bytes': 5 * GiB},
                                 model=QWEN3_14B_MODEL)
    assert '5.4 GB of memory per run' in text and 'needs 5.4 GB' in text and 'layers 38-40' in text


def test_work_limits_for_a_member_are_its_own_guard_and_disk_limit():
    plan = R.plan_for_members(QWEN3_14B_MODEL, DEMO, owner_node_id=OWNER)
    air = plan['members'][1]
    assert R.work_limits_for(air, storage_bytes=2_000_000_000) == {
        'max_rss_bytes': 5_427_065_085, 'max_disk_bytes': 2_000_000_000,
        'max_wall_seconds': 1800, 'max_commands': 5000}
    assert R.work_limits_for(air)['max_disk_bytes'] == 3_969_910_631


def test_unregistered_or_malformed_inputs_refuse():
    class Unknown:
        model_id, resolved_commit, runtime_quantization, runtime_dtype = 'x/y', '0' * 40, 'int8-weight-only', 'float32'
    with pytest.raises(R.ResourceRefused, match='model_resources_unregistered'):
        R.model_resource_proposal(Unknown())
    with pytest.raises(R.ResourceRefused, match='model_resources_split_invalid'):
        R.member_resources(QWEN3_14B_MODEL, {'stages': [{'node_id': 'a', 'start': 0, 'end': 39, 'stage_bytes': 1}]})
    with pytest.raises(R.ResourceRefused, match='model_resources_split_invalid'):
        R.member_resources(QWEN3_14B_MODEL, {'stages': []})


def test_consent_document_matches_the_enforced_functions():
    doc = R.consent_document(QWEN3_14B_MODEL, device_memory_bytes=8 * GiB)
    assert doc['storage_consent_ceiling_bytes'] == R.storage_consent_ceiling()
    assert doc['pairing_quota_ceiling_bytes'] == R.pairing_quota_ceiling()
    assert doc['selected']['owner_storage_bytes'] == R.owner_storage_requirement(QWEN3_14B_MODEL)
    assert doc['memory_cap_options'] == R.memory_cap_options(8 * GiB)
    assert doc['memory_cap_ceiling_bytes'] == 6 * GiB
