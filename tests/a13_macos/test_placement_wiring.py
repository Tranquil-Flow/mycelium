"""PV21 wiring: placement inputs ride the signed run-exchange report; the compile picks the speed-aware split.

Unit seams only (fake app / actor / consent as in test_demo_target); no device, network or model.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from mycelium_a13_macos import app as appmod
from mycelium_a13_macos import chain_compile as cc
from mycelium_a13_macos import demo_chain
from mycelium_a13_macos import placement as pl
from mycelium_a13_macos import run_exchange as rx
from _nhost_fixtures import COMMIT, tiny_dense_snapshot
from test_demo_target import App, Gate, announcement
from test_placement import GiB, cal

MODEL_ID, SHA = 'Qwen/Qwen3-0.6B', 'c' * 40


def member_report(**extra):
    return {'protocol': rx.MEMBER_REPORT, 'run_id': 'r1', 'deployment_id': 'd1', 'state': 'reported', 'detail': None,
            'model': {'model_id': MODEL_ID, 'resolved_commit': SHA}, 'progress': None, 'descriptor': None,
            'issued_at': 1.0, **extra}


# -- run-exchange report ---------------------------------------------------------

def test_report_carries_a_validated_step_calibration_field():
    record = cal(12.5, model=(MODEL_ID, SHA))
    assert rx.validate_report(member_report(step_calibration=record))['step_calibration'] == record
    assert 'step_calibration' not in rx.validate_report(member_report())  # optional: old members still report
    for bad in ({**record, 'decode_ms_per_layer': -1}, {**record, 'extra': 1}, 'fast', None):
        with pytest.raises(ValueError, match='demo_report_invalid'):
            rx.validate_report(member_report(step_calibration=bad))


def test_unknown_report_fields_are_still_refused():
    with pytest.raises(ValueError, match='demo_report_invalid'):
        rx.validate_report(member_report(speed='fast'))


# -- the member publishes its own calibration, off the tick thread --------------------

@pytest.fixture
def world(tmp_path, monkeypatch):
    (tmp_path / 'target').mkdir()
    app = App(tmp_path)
    state = {'a': announcement('collect'), 'reports': []}
    monkeypatch.setattr('mycelium_a13_macos.installation_descriptor.installation_descriptor',
                        lambda app, **k: {'statement': {'target_node_id': 'node-b'}, 'signed': True})
    return app, state


def test_member_reports_its_calibration_once_per_model(world):
    from mycelium_a13_macos.demo_target import DemoTargetAgent
    app, state = world
    calls = []
    record = cal(21.0, model=(MODEL_ID, SHA))
    agent = DemoTargetAgent(app, consent=Gate(), source=lambda: state['a'], sink=state['reports'].append,
                            report_interval=0, calibrator=lambda a: calls.append(a['model']['model_id']) or record)
    agent.tick()
    agent._calibration_thread.join(5)
    agent.tick()
    assert calls == [MODEL_ID]  # one bounded run, not one per tick
    assert state['reports'][-1]['step_calibration'] == record
    rx.validate_report(state['reports'][-1])  # exactly what the signed exchange will accept
    agent.tick()
    assert calls == [MODEL_ID]


def test_a_failing_calibrator_never_breaks_the_member(world):
    from mycelium_a13_macos.demo_target import DemoTargetAgent
    app, state = world

    def boom(a):
        raise RuntimeError('mlx unavailable')
    agent = DemoTargetAgent(app, consent=Gate(), source=lambda: state['a'], sink=state['reports'].append,
                            report_interval=0, calibrator=boom)
    assert agent.tick()['state'] == 'reported'
    agent._calibration_thread.join(5)
    agent.tick()
    assert all('step_calibration' not in r for r in state['reports'])


def test_without_a_calibrator_the_report_is_unchanged(world):
    from mycelium_a13_macos.demo_target import DemoTargetAgent
    app, state = world
    agent = DemoTargetAgent(app, consent=Gate(), source=lambda: state['a'], sink=state['reports'].append, report_interval=0)
    agent.tick()
    assert state['reports'] and all('step_calibration' not in r for r in state['reports'])


# -- owner glue ---------------------------------------------------------------------

def test_capability_records_add_chip_link_rtt_and_the_members_calibration(tmp_path):
    local, remote = 'node-a', 'node-b'
    record_remote, record_local = cal(17.0, model=(MODEL_ID, SHA)), cal(10.0, model=(MODEL_ID, SHA))
    app = NS(
        root=tmp_path,
        member_capability_reports=lambda: {remote: {
            'generation': 3, 'expires_at': 1000.0,
            'device': {'chip': 'Apple M2', 'memory_bytes': 16 * GiB, 'available_storage_bytes': 5 * GiB},
            'link': {'rtt_ms_median': 18.5}}},
        authority=NS(coordinator=NS(member=lambda node: {'generation': 3})),
        local_contributor=NS(member=NS(node_id=local)),
        run_exchange=NS(reports=lambda: {local: {'step_calibration': record_local}, remote: {'step_calibration': record_remote}}))
    records = demo_chain.member_capability_records(app, [local, remote], now=100.0,
                                                   local_device={'memory_bytes': 48 * GiB, 'chip': 'Apple M4 Pro'})
    assert records[remote]['chip'] == 'Apple M2' and records[remote]['link_rtt_ms'] == 18.5
    assert records[remote]['step_calibration'] == record_remote and records[local]['step_calibration'] == record_local
    assert records[local]['chip'] == 'Apple M4 Pro' and 'link_rtt_ms' not in records[local]  # the owner is the entry


def contract():
    return NS(model_id=MODEL_ID, resolved_commit=SHA, runtime_quantization='int8-weight-only')


def reports(**per_node):
    base = {n: {'memory_bytes': 48 * GiB} for n in ('node-a', 'node-b')}
    for node, extra in per_node.items():
        base[node.replace('_', '-')].update(extra)
    return base


def test_compile_uses_the_speed_aware_split_when_every_member_reported_a_calibration(tmp_path):
    snapshot = tiny_dense_snapshot(tmp_path / 'hf', num_layers=12)
    split = cc.plan_member_split(
        reports(node_a={'step_calibration': cal(10.0, model=(MODEL_ID, SHA))},
                node_b={'step_calibration': cal(30.0, model=(MODEL_ID, SHA))}),
        model_root=snapshot, model_contract=contract(), force_split=True)
    ranges = [[s['start'], s['end']] for s in split['stages']]
    assert split['weighting'] == 'measured_step_time' and split['placement']['mode'] == 'split'
    assert ranges[0][1] - ranges[0][0] > ranges[1][1] - ranges[1][0]  # the 3x faster Mac takes more layers


def test_compile_estimates_from_the_chip_when_a_member_has_no_measurement(tmp_path):
    snapshot = tiny_dense_snapshot(tmp_path / 'hf', num_layers=12)
    split = cc.plan_member_split(reports(node_a={'chip': 'Apple M4 Pro'}, node_b={'chip': 'Apple M2'}),
                                 model_root=snapshot, model_contract=contract(), force_split=True)
    assert split['weighting'] == 'estimated_step_time' and split['placement']['basis'] == 'estimated'


def test_compile_keeps_the_memory_split_without_any_placement_input(tmp_path):
    snapshot = tiny_dense_snapshot(tmp_path / 'hf', num_layers=12)
    split = cc.plan_member_split(reports(), model_root=snapshot, model_contract=contract())
    assert split['weighting'] == 'memory' and 'placement' not in split  # unchanged document, unchanged digest


def test_a_measurement_for_a_different_model_is_not_trusted(tmp_path):
    snapshot = tiny_dense_snapshot(tmp_path / 'hf', num_layers=12)
    stale = cal(10.0, model=('Other/Model', 'd' * 40))
    split = cc.plan_member_split(reports(node_a={'step_calibration': stale}, node_b={'step_calibration': stale}),
                                 model_root=snapshot, model_contract=contract())
    assert split['weighting'] == 'memory'  # no chip to estimate from either


def test_public_decision_is_status_safe_and_json_clean(tmp_path):
    snapshot = tiny_dense_snapshot(tmp_path / 'hf', num_layers=12)
    split = cc.plan_member_split(
        reports(node_a={'step_calibration': cal(10.0, model=(MODEL_ID, SHA))},
                node_b={'step_calibration': cal(30.0, model=(MODEL_ID, SHA))}),
        model_root=snapshot, model_contract=contract())
    view = pl.public_decision(split)
    assert view['mode'] == 'single' and view['explanation'].startswith('Fast path chosen: fits on')
    assert all('memory_bytes' not in m for m in view['members'])
    assert pl.public_decision({}) is None


# -- /api/status and the owner setting -------------------------------------------------

def test_status_block_carries_the_one_sentence_and_the_setting(tmp_path, monkeypatch):
    monkeypatch.delenv(pl.SETTING_ENVIRONMENT, raising=False)
    app = NS(root=tmp_path, _chain_compile_state={'placement': {'explanation': 'Split 18/10 because the MacBook is 1.7x slower per layer.'}})
    block = appmod._placement_status(app)
    assert block['setting'] == 'auto' and block['modes'] == ['auto', 'always_split']
    assert block['decision']['explanation'] == 'Split 18/10 because the MacBook is 1.7x slower per layer.'
    assert block['single_host_route_supported'] is False
    app._chain_compile_state = None
    app._last_placement = {'explanation': 'kept after the run'}
    assert appmod._placement_status(app)['decision']['explanation'] == 'kept after the run'


def test_owner_can_force_always_split_and_the_next_compile_reads_it(tmp_path, monkeypatch):
    monkeypatch.delenv(pl.SETTING_ENVIRONMENT, raising=False)
    owner = NS(installation_role='owner', root=tmp_path)
    assert appmod.DemoApp._choose_placement(owner, {'mode': 'always_split'})['setting'] == 'always_split'
    assert pl.load_setting(tmp_path) == 'always_split'
    assert appmod._placement_status(owner)['setting'] == 'always_split'
    assert appmod.DemoApp._choose_placement(owner, {'mode': 'auto'})['setting'] == 'auto'


def test_placement_setting_is_validated_and_owner_only(tmp_path, monkeypatch):
    monkeypatch.delenv(pl.SETTING_ENVIRONMENT, raising=False)
    owner = NS(installation_role='owner', root=tmp_path)
    for body in ({'mode': 'sometimes'}, {}, {'mode': 'auto', 'x': 1}, {'mode': ['auto']}, 'auto'):
        with pytest.raises(appmod.DemoError, match='placement_mode_invalid'):
            appmod.DemoApp._choose_placement(owner, body)
    with pytest.raises(appmod.DemoError, match='owner_role_required'):
        appmod.DemoApp._choose_placement(NS(installation_role='target', root=tmp_path), {'mode': 'auto'})
    monkeypatch.setenv(pl.SETTING_ENVIRONMENT, 'always_split')
    with pytest.raises(appmod.DemoError, match='placement_locked_by_environment'):
        appmod.DemoApp._choose_placement(owner, {'mode': 'auto'})


def test_ui_has_the_sentence_and_the_always_split_control():
    web = Path(appmod.__file__).parent / 'web'
    html, script = (web / 'index.html').read_text(), (web / 'run.js').read_text()
    for element in ('demo-placement', 'placement-always-split'):
        assert f'id="{element}"' in html and element in script
    assert "'/api/placement'" in script and 'textContent = decision && decision.explanation' in script
