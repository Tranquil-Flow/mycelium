"""Model-size suggestion (PV7): which registered models fit the swarm's usable memory."""
import pytest

from mycelium_a13_macos import model_resources as R
from mycelium_a13_macos.model_suggestion import assess_model, member_rows_usable, suggest_models
from mycelium_a13_macos.run_model import CHOICES, choice

GiB = 1 << 30
OWNER, AIR, MINI = 'node-a-owner', 'node-b-air', 'node-c-mini'


def rows(*specs):
    return [dict(node_id=n, memory_bytes=m * GiB, **extra) for n, m, extra in specs]


def names(result, state):
    return [c['name'] for c in result['choices'] if c['fit']['state'] == state]


def test_owner_48_plus_air_8_fits_everything_but_the_measured_7b_route():
    result = suggest_models(CHOICES, rows((OWNER, 48, {}), (AIR, 8, {})))
    # The 7B measured group peak (8.48 GB per member) exceeds what an 8 GiB Mac lets one app use.
    assert names(result, 'too_large') == ['qwen2.5-7b']
    assert result['suggested'] == 'qwen3-14b'
    assert [c['name'] for c in result['choices']][0] == 'qwen3-14b'  # largest first


def test_largest_first_ordering_and_one_line_reason_each():
    result = suggest_models(CHOICES, rows((OWNER, 48, {}), (AIR, 8, {})))
    sizes = [choice(c['name']).snapshot_bytes for c in result['choices']]
    assert sizes == sorted(sizes, reverse=True)
    for c in result['choices']:
        assert '\n' not in c['fit']['line'] and 10 < len(c['fit']['line']) < 220
    fit = next(c for c in result['choices'] if c['name'] == 'qwen3-14b')['fit']
    assert fit['line'].startswith("fits: needs ") and " GB of your swarm's " in fit['line']
    assert fit['needs_bytes'] <= fit['usable_bytes']
    assert [m['layers'] for m in fit['members']] == [37, 3]


def test_two_8_gib_macs_fit_only_the_small_models():
    result = suggest_models(CHOICES, rows((OWNER, 8, {}), (AIR, 8, {})))
    assert names(result, 'fits') == ['qwen3-0.6b', 'qwen2.5-0.5b']
    assert result['suggested'] == 'qwen3-0.6b'
    too = next(c for c in result['choices'] if c['name'] == 'qwen3-14b')['fit']
    assert too['state'] == 'too_large' and too['line'].startswith('too big: needs ')
    assert " GB of your swarm's " in too['line'] and 'add a Mac' in too['line']
    assert too['needs_bytes'] > too['usable_bytes']


def test_owner_reserve_is_taken_off_the_owner_mac():
    base = suggest_models(CHOICES, rows((OWNER, 48, {}), (AIR, 8, {})))
    reserved = suggest_models(CHOICES, rows((OWNER, 48, {'reserve_bytes': 40 * GiB}), (AIR, 8, {})))
    assert reserved['suggested'] != base['suggested']
    assert reserved['usable_bytes'] == base['usable_bytes'] - 40 * GiB * 3 // 4 or reserved['usable_bytes'] < base['usable_bytes']
    assert 'qwen3-14b' in names(reserved, 'too_large')


def test_a_known_member_memory_cap_limits_its_share():
    capped = suggest_models(CHOICES, rows((OWNER, 48, {}), (AIR, 8, {'max_memory_bytes': 3 * GiB})))
    assert 'qwen3-14b' in names(capped, 'too_large') and 'qwen3-8b' in names(capped, 'too_large')
    line = next(c for c in capped['choices'] if c['name'] == 'qwen3-14b')['fit']['line']
    assert 'memory limit' in line or 'memory' in line


def test_usable_memory_is_the_app_ceiling_minus_reserve_and_never_above_the_cap():
    usable = member_rows_usable(rows((OWNER, 48, {'reserve_bytes': 8 * GiB}), (AIR, 8, {'max_memory_bytes': 4 * GiB}),
                                     (MINI, 16, {})))
    assert usable == {OWNER: R.member_memory_ceiling(48 * GiB) - 8 * GiB, AIR: 4 * GiB,
                      MINI: R.member_memory_ceiling(16 * GiB)}


def test_fewer_than_two_macs_is_unknown_not_refused():
    result = suggest_models(CHOICES, rows((OWNER, 48, {})))
    assert {c['fit']['state'] for c in result['choices']} == {'unknown'}
    assert result['suggested'] is None
    line = result['choices'][0]['fit']['line']
    assert 'invite' in line.lower() and "GB" in line
    assert suggest_models(CHOICES, [])['suggested'] is None


def test_more_than_eight_macs_use_the_eight_largest_and_unknown_memory_is_skipped():
    many = rows(*[(f'node-{i:02d}', 8 + i, {}) for i in range(10)]) + [{'node_id': 'node-x', 'memory_bytes': None}]
    result = suggest_models(CHOICES, many)
    assert result['members'] == 8


def test_assess_model_matches_the_planner_for_a_single_model():
    fit = assess_model(choice('qwen3-8b').model, rows((OWNER, 48, {}), (AIR, 8, {})))
    plan = R.plan_for_members(choice('qwen3-8b').model, [{'node_id': OWNER, 'memory_bytes': 48 * GiB},
                                                          {'node_id': AIR, 'memory_bytes': 8 * GiB}],
                              owner_node_id=OWNER)
    assert fit['state'] == 'fits'
    assert fit['needs_bytes'] == sum(m['max_rss_bytes'] for m in plan['members'])


def test_free_disk_known_for_a_member_is_checked():
    r = rows((OWNER, 48, {}), (AIR, 8, {'available_storage_bytes': 100_000_000}))
    fit = assess_model(choice('qwen3-8b').model, r)
    assert fit['state'] == 'too_large' and 'disk' in fit['line']
