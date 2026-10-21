# SPDX-License-Identifier: AGPL-3.0-only
"""Source-shaped native telemetry through the real public projection; not physical proof."""
import copy
import json
from pathlib import Path

import pytest

from test_peer_counter_projection_single_observation import NODE_ID, _route

FIXTURE = json.loads((Path(__file__).parents[1] / 'fixtures/native-phone-interruptibility.json').read_text())


def project(monkeypatch, raw):
    route = _route(monkeypatch)
    route._last_snapshots[NODE_ID] = {'details': {
        'runtime': {'mode': 'complete_context_replay', 'active_state_count': 0},
        'interruptibility': copy.deepcopy(raw),
    }}
    before = copy.deepcopy(route._last_snapshots)
    public = route.public_status()['peers'][0]['interruptibility']
    assert route._last_snapshots == before, 'public reduction must not rewrite signed evidence'
    return public


def test_native_replay_projection_is_closed_and_preserves_unmeasured_bound(monkeypatch):
    assert project(monkeypatch, FIXTURE['loaded']) == FIXTURE['public_loaded']


def test_checked_out_native_runtime_projects_unknown_not_fake_zero_metrics(monkeypatch):
    assert project(monkeypatch, FIXTURE['busy']) is None


@pytest.mark.parametrize('change', [
    {'maximum_total_cleanup_ms': 8000},
    {'cooperative_bound_candidate': True},
    {'cooperative_interruption': False},
    {'shared_process_termination_used': True},
])
def test_native_projection_does_not_promote_unsupported_claims(monkeypatch, change):
    assert project(monkeypatch, {**FIXTURE['loaded'], **change}) is None


@pytest.mark.parametrize('observed', [True, -1, float('nan'), float('inf'), 10**1000])
def test_malformed_native_measurement_stays_unknown(monkeypatch, observed):
    assert project(monkeypatch, {**FIXTURE['loaded'], 'maximum_observed_work_unit_ms': observed}) is None


def test_unknown_native_variant_does_not_publish_private_extensions(monkeypatch):
    raw = {**FIXTURE['loaded'], 'work_unit': 'future_native_variant', 'private_detail': 'private'}
    assert project(monkeypatch, raw) is None


def test_desktop_projection_is_unchanged(monkeypatch):
    raw = {'work_unit': 'transformer_layer', 'sentinel': 'existing-desktop-contract'}
    assert project(monkeypatch, raw) == raw
