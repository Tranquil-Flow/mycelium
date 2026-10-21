"""PV21 speed-aware placement: calibration records, split choice, single-Mac fast path, explanation.

Pure arithmetic and unit seams only: no device, network, route or model download. The one
MLX test times a few-KB synthetic layer written to a temp dir; it is skipped without MLX.
"""
from __future__ import annotations

import json
import math
from types import SimpleNamespace as NS

import pytest

from mycelium_a13_macos import layer_split as ls
from mycelium_a13_macos import placement as pl
from mycelium_a13_macos.layer_split import LayerProfile, SplitRefused

GiB = 1 << 30
MODEL = ('Qwen/Qwen3-14B', 'c' * 40)


def profile(layers=28, layer_bytes=GiB, embedding=GiB // 2, output=GiB // 2, tied=False):
    return LayerProfile(model_type='qwen3', num_layers=layers, layer_bytes=(layer_bytes,) * layers,
                        embedding_bytes=embedding, output_bytes=output, tie_word_embeddings=tied)


def cal(decode_ms, prefill_ms_per_token=1.0, *, basis='measured', chip='Apple M4 Pro', model=MODEL):
    return {'protocol': pl.CALIBRATION_PROTOCOL, 'basis': basis,
            'method': pl.METHOD_MEASURED if basis == 'measured' else pl.METHOD_ESTIMATED,
            'chip': chip, 'model_id': model[0], 'resolved_commit': model[1], 'layer_bytes': GiB,
            'decode_ms_per_layer': decode_ms, 'prefill_tokens_per_second_per_layer': 1000.0 / prefill_ms_per_token,
            'prefill_tokens': 128, 'samples': 12 if basis == 'measured' else 0, 'wall_ms': 900.0,
            'measured_at': 1.0}


def member(node, gib, decode_ms, *, label=None, rtt=None, basis='measured', prefill=1.0, **extra):
    row = {'node_id': node, 'memory_bytes': gib * GiB, 'step_calibration': cal(decode_ms, prefill, basis=basis)}
    if label:
        row['label'] = label
    if rtt is not None:
        row['link_rtt_ms'] = rtt
    row.update(extra)
    return row


def counts(doc):
    return [s['end'] - s['start'] for s in doc['stages']]


# -- calibration records -------------------------------------------------------

def test_calibration_record_is_closed_bounded_and_labelled():
    good = cal(10.0)
    assert pl.validate_step_calibration(good) == good
    for broken in (
            {**good, 'extra': 1}, {k: v for k, v in good.items() if k != 'chip'},
            {**good, 'basis': 'guessed'}, {**good, 'method': pl.METHOD_ESTIMATED},  # measured must name the measuring method
            {**good, 'decode_ms_per_layer': 0}, {**good, 'decode_ms_per_layer': float('nan')},
            {**good, 'decode_ms_per_layer': -1.0}, {**good, 'prefill_tokens': 0},
            {**good, 'chip': 'x' * 200}, {**good, 'protocol': 'other'}, 'text', None):
        with pytest.raises(ValueError, match='step_calibration_invalid'):
            pl.validate_step_calibration(broken)


def test_chip_estimate_is_labelled_estimated_and_scales_with_the_chip():
    pro = pl.estimate_step_calibration('Apple M4 Pro', profile(), model_id=MODEL[0], resolved_commit=MODEL[1], now=5.0)
    base = pl.estimate_step_calibration('Apple M2', profile(), model_id=MODEL[0], resolved_commit=MODEL[1], now=5.0)
    assert pro['basis'] == 'estimated' and pro['method'] == pl.METHOD_ESTIMATED and pro['samples'] == 0
    # memory bound decode: 273 GB/s vs 100 GB/s
    assert base['decode_ms_per_layer'] / pro['decode_ms_per_layer'] == pytest.approx(273 / 100, rel=0.02)
    assert pro['prefill_tokens_per_second_per_layer'] > base['prefill_tokens_per_second_per_layer']
    # the documented formula: layer bytes / (bandwidth x 0.6)
    assert pro['decode_ms_per_layer'] == pytest.approx(GiB / (273e9 * 0.6) * 1e3, rel=1e-3)
    assert pl.estimate_step_calibration('Intel Core i7', profile(), model_id=MODEL[0], resolved_commit=MODEL[1]) is None
    assert pl.chip_figures('Apple M4 Max')[0] > pl.chip_figures('Apple M4 Pro')[0]  # longest chip name wins, not "M4"


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def test_measurement_reports_the_median_step_and_stays_bounded():
    clock = FakeClock()
    durations = iter([0.5] + [0.020, 0.021, 0.019, 0.020, 0.500] * 40)  # a slow warm-up, then a stray 0.5 s stall every fifth step
    calls = {'decode': 0, 'prefill': 0}

    def decode():
        calls['decode'] += 1
        clock.now += next(durations)

    def prefill(tokens):
        calls['prefill'] += 1
        clock.now += 0.2

    out = pl.measure_step_calibration(decode, prefill, chip='Apple M4 Pro', model_id=MODEL[0],
                                      resolved_commit=MODEL[1], layer_bytes=GiB, prefill_tokens=128,
                                      budget_seconds=2.0, max_decode_steps=48, clock=clock, now=9.0)
    assert out['basis'] == 'measured' and out['method'] == pl.METHOD_MEASURED
    assert out['decode_ms_per_layer'] == pytest.approx(20.0)  # the median ignores the stall
    assert out['prefill_tokens_per_second_per_layer'] == pytest.approx(128 / 0.2)
    assert out['samples'] == calls['decode'] - 1 <= 48
    assert clock.now <= 2.0 + 0.5 + 0.2  # budget respected (plus at most the step in flight)
    assert pl.validate_step_calibration(out) == out


def test_calibrate_member_prefers_measurement_then_estimate_then_nothing():
    steps = (lambda: None, lambda n: None, GiB)
    measured = pl.calibrate_member(None, profile(), chip='Apple M4 Pro', model_id=MODEL[0], resolved_commit=MODEL[1],
                                   probe=lambda: steps)
    assert measured['basis'] == 'measured'

    def no_weights():
        raise ValueError('step_calibration_no_weights')
    fallback = pl.calibrate_member(None, profile(), chip='Apple M4 Pro', model_id=MODEL[0],
                                   resolved_commit=MODEL[1], probe=no_weights)
    assert fallback['basis'] == 'estimated'
    assert pl.calibrate_member(None, None, chip='Apple M4 Pro', model_id=MODEL[0], resolved_commit=MODEL[1],
                               probe=no_weights) is None
    assert pl.calibrate_member(None, profile(), chip='Mystery', model_id=MODEL[0], resolved_commit=MODEL[1],
                               probe=no_weights) is None


def test_mlx_probe_times_a_real_layer_from_weights_already_on_disk(tmp_path):
    mx = pytest.importorskip('mlx.core')
    root = tmp_path / 'snap'
    root.mkdir()
    (root / 'config.json').write_text(json.dumps({'model_type': 'qwen3', 'num_hidden_layers': 2}))
    mx.save_safetensors(str(root / 'model.safetensors'), {
        'model.embed_tokens.weight': mx.ones((16, 32)),
        'model.layers.0.mlp.up_proj.weight': mx.ones((64, 32)), 'model.layers.0.mlp.down_proj.weight': mx.ones((32, 64)),
        'model.layers.0.input_layernorm.weight': mx.ones((32,)),
        'model.layers.1.mlp.up_proj.weight': mx.ones((64, 32)), 'model.norm.weight': mx.ones((32,))})
    decode, prefill, layer_bytes = pl.mlx_layer_probe(root)
    assert layer_bytes == (64 * 32 + 32 * 64 + 32) * 4  # one layer only (layer 0), read from the local header
    out = pl.calibrate_member(root, None, chip='Apple M4', model_id=MODEL[0], resolved_commit=MODEL[1],
                              budget_seconds=0.5)
    assert out['basis'] == 'measured' and out['layer_bytes'] == layer_bytes and out['samples'] >= 4
    with pytest.raises(Exception):
        pl.mlx_layer_probe(tmp_path / 'absent')  # no weights -> no silent download, the caller estimates


# -- placement -------------------------------------------------------------------

def test_split_follows_per_layer_speed_and_explains_itself():
    # the demo shape: a fast big Mac and a 1.7x slower laptop; neither alone can hold the 28 GiB model.
    doc = pl.choose_placement([member('a-owner', 48, 10.0, label='the Mac mini'),
                               member('b-laptop', 32, 17.0, label='the MacBook')], profile())
    assert counts(doc) == [18, 10]
    block = doc['placement']
    assert block['mode'] == 'split' and block['applied'] and block['reason'] == 'model_does_not_fit_one_mac'
    assert block['explanation'] == 'Split 18/10 because the MacBook is 1.7x slower per layer.'
    assert doc['weighting'] == 'measured_step_time'
    assert ls.split_ranges(doc) == [[0, 18], [18, 28]]  # still a digest-checked layer_split document


def test_split_minimizes_the_slowest_stage_not_the_memory_share():
    memory_split = ls.memory_proportional_split(
        [{'node_id': 'a-owner', 'memory_bytes': 48 * GiB}, {'node_id': 'b-laptop', 'memory_bytes': 32 * GiB}], profile())
    speed = pl.choose_placement([member('a-owner', 48, 10.0), member('b-laptop', 32, 17.0)], profile())
    # naive memory split ignores that the laptop is slower; the speed split's bottleneck is no worse
    assert counts(speed) != counts(memory_split)
    def bottleneck(c):
        return max(c[0] * 10.0, c[1] * 17.0 + 0.5 * 17.0)
    assert bottleneck(counts(speed)) <= bottleneck(counts(memory_split))


def test_equal_speeds_split_evenly_and_say_so():
    doc = pl.choose_placement([member('a-one', 48, 10.0), member('b-two', 48, 10.0)], profile(), )
    assert counts(doc) == [14, 14] or counts(doc) == [15, 13]  # the output head sits on the last stage
    assert 'about the same speed' in doc['placement']['explanation']


def test_memory_budget_binds_before_speed_and_is_reported():
    # laptop budget 1/2 x 16 GiB = 8 GiB -> 6 layers + the 0.5 GiB output head + 1 GiB overhead; speed alone wants 10
    doc = pl.choose_placement([member('a-owner', 48, 10.0, label='the Mac mini'),
                               member('b-laptop', 16, 17.0, label='the MacBook')], profile())
    assert counts(doc) == [22, 6]
    assert doc['placement']['memory_bound'] is True and doc['placement']['reason'] == 'memory_bound'
    assert 'memory' in doc['placement']['explanation']
    for stage in doc['stages']:
        assert stage['cost_bytes'] <= stage['budget_bytes']


def test_standing_cap_and_three_quarter_guard_lower_the_budget():
    capped = pl.choose_placement([member('a-owner', 48, 10.0), member('b-laptop', 32, 10.0, max_memory_bytes=9 * GiB)],
                                 profile())
    assert counts(capped)[1] == 7  # 9 GiB cap - 1 GiB overhead - 0.5 GiB output head
    # a policy asking for 100% of RAM is still held to the 3/4-RAM guard
    greedy = ls.SplitPolicy(memory_fraction_numerator=1, memory_fraction_denominator=1)
    doc = pl.choose_placement([member('a-owner', 48, 10.0), member('b-laptop', 16, 10.0)], profile(), greedy)
    assert doc['stages'][1]['budget_bytes'] == 12 * GiB


def test_a_model_no_member_can_hold_is_refused_by_name():
    with pytest.raises(SplitRefused) as raised:
        pl.choose_placement([member('a-small', 8, 10.0), member('b-small', 8, 10.0)], profile())
    assert raised.value.code == 'member_cannot_hold_share'


def test_link_rtt_is_part_of_the_prediction_per_hop():
    base = pl.choose_placement([member('a-owner', 48, 10.0, rtt=0.0), member('b-laptop', 32, 17.0, rtt=0.0)], profile())
    slow = pl.choose_placement([member('a-owner', 48, 10.0, rtt=0.0), member('b-laptop', 32, 17.0, rtt=24.0)], profile())
    delta = slow['placement']['prediction']['split_token_ms'] - base['placement']['prediction']['split_token_ms']
    assert delta == pytest.approx(24.0, abs=0.01)  # one RTT per token for a two-stage ring
    assert slow['placement']['prediction']['hop_one_way_ms'] == [12.0, 12.0]
    # a direct pair measurement beats the via-owner sum
    direct = pl.choose_placement([member('a-owner', 48, 10.0, rtt=0.0), member('b-laptop', 32, 17.0, rtt=24.0)], profile(),
                                 pair_rtt_ms={('a-owner', 'b-laptop'): 4.0})
    assert direct['placement']['prediction']['hop_one_way_ms'] == [2.0, 2.0]


def test_a_costly_link_moves_layers_off_the_far_member():
    near = member('b-near', 48, 10.0, rtt=1.0)
    three = pl.choose_placement([member('a-owner', 48, 10.0, rtt=0.0), near, member('c-far', 48, 10.0, rtt=200.0)],
                                profile())
    # ring: a->near 0.5, near->far 100, far->a 100 (one-way): the stages that pay for the far link get fewer layers
    assert counts(three)[1] <= counts(three)[0] and counts(three)[2] <= counts(three)[0]
    assert sum(counts(three)) == 28 and min(counts(three)) >= 1


def test_prefill_time_breaks_a_decode_tie_in_either_direction():
    p = profile(layers=20, output=0, embedding=0)  # no head: the decode tie below is exact
    # decode 10 vs 5 ms/layer: 6/14 and 7/13 both have a 70 ms slowest stage
    slow_prefill_first = pl.choose_placement(
        [member('a-one', 128, 10.0, prefill=4.0), member('b-two', 128, 5.0, prefill=1.0)], p, force_split=True)
    assert counts(slow_prefill_first) == [6, 14]
    slow_prefill_second = pl.choose_placement(
        [member('a-one', 128, 10.0, prefill=1.0), member('b-two', 128, 5.0, prefill=4.0)], p, force_split=True)
    assert counts(slow_prefill_second) == [7, 13]


def test_fast_path_when_the_model_fits_the_strongest_mac_and_is_faster():
    doc = pl.choose_placement([member('a-owner', 128, 10.0, label='the Mac mini', rtt=0.0),
                               member('b-laptop', 32, 17.0, label='the MacBook', rtt=6.0)], profile())
    block = doc['placement']
    assert block['mode'] == 'single' and block['host'] == 'a-owner' and block['replicas'] == ['b-laptop']
    assert block['reason'] == 'fits_one_mac_faster'
    assert block['explanation'].startswith('Fast path chosen: fits on the Mac mini')
    assert 'the other Mac stays available' in block['explanation']
    assert block['prediction']['single_tokens_per_second'] > block['prediction']['split_tokens_per_second']
    # no one-host route exists yet: reported, not applied, and the best split is still what compiles
    assert pl.SINGLE_HOST_ROUTE_SUPPORTED is False and block['applied'] is False
    assert block['note'] == 'single_host_route_not_supported_yet_running_best_split'
    assert len(doc['stages']) == 2 and ls.split_ranges(doc)


def test_fast_path_is_not_chosen_when_the_split_is_predicted_faster():
    # only the slow Mac can hold the whole model; the fast one (too small for it) can carry most layers
    doc = pl.choose_placement([member('a-fast', 40, 8.0), member('b-slow', 128, 40.0)], profile())
    assert doc['placement']['mode'] == 'split'
    prediction = doc['placement']['prediction']
    assert prediction['single_token_ms'] > prediction['split_token_ms']
    assert counts(doc)[0] > counts(doc)[1]


def test_force_always_split_overrides_the_fast_path():
    members = [member('a-owner', 128, 10.0, label='the Mac mini'), member('b-laptop', 32, 17.0, label='the MacBook')]
    doc = pl.choose_placement(members, profile(), force_split=True)
    block = doc['placement']
    assert block['mode'] == 'split' and block['applied'] and block['force_split'] is True
    assert block['reason'] == 'forced_always_split' and 'always split' in block['explanation']
    assert 'Split' in block['explanation'] and 'host' not in block


def test_estimated_calibration_is_labelled_in_the_decision():
    doc = pl.choose_placement([member('a-owner', 48, 10.0, basis='estimated'), member('b-laptop', 32, 17.0)], profile())
    assert doc['weighting'] == 'estimated_step_time' and doc['placement']['basis'] == 'estimated'
    assert '(estimated)' in doc['placement']['explanation']
    assert {m['basis'] for m in doc['placement']['members']} == {'estimated', 'measured'}


def test_without_a_calibration_for_every_member_the_split_is_memory_only():
    plain = [{'node_id': 'a-owner', 'memory_bytes': 48 * GiB}, {'node_id': 'b-laptop', 'memory_bytes': 32 * GiB}]
    doc = pl.choose_placement([member('a-owner', 48, 10.0), plain[1]], profile())
    expected = ls.memory_proportional_split(plain, profile())
    assert doc['stages'] == expected['stages'] and doc['weighting'] == 'memory'
    assert doc['placement']['reason'] == 'no_calibration'


def test_placement_is_deterministic_and_digest_covers_the_decision():
    members = [member('a-owner', 48, 10.0), member('b-laptop', 32, 17.0)]
    one, two = pl.choose_placement(members, profile()), pl.choose_placement(list(members), profile())
    assert one == two and one['split_digest'] == two['split_digest']
    tampered = json.loads(json.dumps(one))
    tampered['placement']['explanation'] = 'edited'
    with pytest.raises(SplitRefused):
        ls.split_ranges(tampered)


def test_calibrated_rows_trust_only_validated_bound_records_and_estimate_from_chip():
    prof = profile()
    stale = cal(10.0, model=('Other/Model', 'd' * 40))
    rows = pl.calibrated_rows(
        [{'node_id': 'a', 'memory_bytes': 1, 'step_calibration': stale, 'chip': 'Apple M4 Pro'},
         {'node_id': 'b', 'memory_bytes': 1, 'step_calibration': {'bogus': 1}, 'chip': 'Apple M2'},
         {'node_id': 'c', 'memory_bytes': 1, 'step_calibration': cal(12.0)}],
        prof, model_id=MODEL[0], resolved_commit=MODEL[1])
    assert [r['step_calibration']['basis'] for r in rows] == ['estimated', 'estimated', 'measured']
    assert pl.calibrated_rows([{'node_id': 'a', 'memory_bytes': 1}], prof, model_id=MODEL[0], resolved_commit=MODEL[1]) is None


# -- latency decomposition hook ------------------------------------------------

def test_token_latency_decomposition_stacks_per_stage_and_exposes_the_residual():
    records = [
        {'stage': 0, 'kind': 'compute', 'start_ns': 0, 'end_ns': 100},
        {'stage': 0, 'kind': 'serialize', 'start_ns': 100, 'end_ns': 110},
        {'stage': 0, 'kind': 'send', 'start_ns': 110, 'end_ns': 150},
        {'stage': 1, 'kind': 'receive', 'start_ns': 150, 'end_ns': 160},
        {'stage': 1, 'kind': 'compute', 'start_ns': 160, 'end_ns': 400}]
    out = pl.decompose_token_latency(records, wall_ns=420)
    assert out['stages']['0']['compute'] == 100 and out['stages']['1']['compute'] == 240
    assert out['by_kind']['send'] == 40 and out['accounted_ns'] == 400
    assert out['residual_ns'] == 20 and out['residual_fraction'] == pytest.approx(20 / 420, abs=1e-4)
    for bad in ({'stage': 0, 'kind': 'gpu', 'start_ns': 0, 'end_ns': 1}, {'stage': 0, 'kind': 'send', 'start_ns': 5, 'end_ns': 1},
                {'stage': -1, 'kind': 'send', 'start_ns': 0, 'end_ns': 1}, {'stage': 0, 'kind': 'send', 'start_ns': 0.5, 'end_ns': 1}):
        with pytest.raises(ValueError, match='latency_record_invalid'):
            pl.decompose_token_latency([bad])


# -- owner setting ---------------------------------------------------------------

def test_owner_setting_defaults_to_auto_persists_and_the_environment_wins(tmp_path):
    assert pl.load_setting(tmp_path, environ={}) == 'auto'
    pl.save_setting(tmp_path, 'always_split')
    assert pl.load_setting(tmp_path, environ={}) == 'always_split'
    assert pl.load_setting(tmp_path, environ={pl.SETTING_ENVIRONMENT: 'auto'}) == 'auto'
    with pytest.raises(ValueError, match='placement_mode_invalid'):
        pl.save_setting(tmp_path, 'sometimes')
    (tmp_path / pl.SETTING_FILE).write_text('{"protocol":"x"}')
    assert pl.load_setting(tmp_path, environ={}) == 'auto'  # unreadable state never breaks a run
