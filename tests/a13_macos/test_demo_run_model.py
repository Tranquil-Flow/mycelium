# SPDX-License-Identifier: AGPL-3.0-only
"""Demo model choice: pinned Qwen3 contracts, persisted in app state, no env var."""
import json

import pytest

from mycelium_a13_macos import run_model
from mycelium_a13_macos.app import DemoApp, DemoError
from mycelium_a13_macos.serving import (
    REGISTERED_MODELS,
    RESOURCE_PROPOSAL,
    QWEN3_0_6B_MODEL,
    registered_model_contract,
    selected_run_model,
)


def test_qwen3_8b_and_14b_contracts_are_pinned_and_registered():
    from mycelium_a13_macos.serving import QWEN3_8B_MODEL, QWEN3_14B_MODEL
    assert QWEN3_8B_MODEL.model_id == 'Qwen/Qwen3-8B'
    assert QWEN3_8B_MODEL.resolved_commit == 'b968826d9c46dd6066d109eabc6255188de91218'
    assert QWEN3_14B_MODEL.model_id == 'Qwen/Qwen3-14B'
    assert QWEN3_14B_MODEL.resolved_commit == '40c069824f4251a91eefaf281ebe4c544efd3e18'
    for model in (QWEN3_8B_MODEL, QWEN3_14B_MODEL):
        assert model in REGISTERED_MODELS
        assert model.architecture == 'qwen3'
        assert (model.runtime_backend, model.runtime_dtype, model.runtime_quantization) == (
            'mlx', 'float32', 'int8-weight-only')
        assert registered_model_contract(model.model_id, model.resolved_commit) is model


def test_choices_include_tested_starter_and_three_qwen3_sizes():
    names = [c.name for c in run_model.CHOICES]
    assert names == ['a9-route', 'qwen3-0.6b', 'qwen3-8b', 'qwen3-14b']
    sizes = {c.name: c.snapshot_bytes for c in run_model.CHOICES}
    # HF tree totals of the pinned revisions (every file of the snapshot).
    assert sizes == {'a9-route': 999_604_126, 'qwen3-0.6b': 1_519_209_243, 'qwen3-8b': 16_397_461_266,
                     'qwen3-14b': 29_552_613_776}
    assert run_model.choice('qwen3-0.6b').model is QWEN3_0_6B_MODEL


def test_demo_default_is_tested_starter_when_it_fits():
    assert run_model.demo_default_choice(storage_ceiling=40_000_000_000) == 'a9-route'
    # Larger choices remain selectable within their existing consent ceiling.
    assert RESOURCE_PROPOSAL.additional_disk_bytes < 29_552_613_776
    assert run_model.storage_ceiling() == 32_568_709_325
    assert run_model.demo_default_choice() == 'a9-route'
    assert run_model.demo_default_choice(storage_ceiling=20_000_000_000) == 'a9-route'
    assert run_model.demo_default_choice(storage_ceiling=2_000_000_000) == 'a9-route'
    assert run_model.demo_default_choice(storage_ceiling=1_000) is None


def test_choice_persists_in_private_state_and_env_var_still_wins(tmp_path):
    root = tmp_path / 'state'; root.mkdir(mode=0o700)
    assert run_model.app_run_model(root, environ={})[:2] == ('a9-route', 'demo_default')
    run_model.save_choice(root, 'qwen3-0.6b')
    assert (root / 'run-model.json').stat().st_mode & 0o777 == 0o600
    name, source, model = run_model.app_run_model(root, environ={})
    assert (name, source, model) == ('qwen3-0.6b', 'saved', QWEN3_0_6B_MODEL)
    name, source, model = run_model.app_run_model(root, environ={'MYCELIUM_A13_RUN_MODEL': 'a9-route'})
    assert source == 'environment' and model == selected_run_model({'MYCELIUM_A13_RUN_MODEL': 'a9-route'})
    with pytest.raises(ValueError, match='run_model_choice_invalid'):
        run_model.save_choice(root, 'qwen3-32b')
    (root / 'run-model.json').write_text(json.dumps({'protocol': 'x', 'choice': 'qwen3-8b'}))
    (root / 'run-model.json').chmod(0o600)
    with pytest.raises(ValueError, match='run_model_state_invalid'):
        run_model.app_run_model(root, environ={})


def test_selected_run_model_default_is_unchanged_for_non_app_callers():
    from mycelium_a13_macos.serving import CANDIDATE_MODEL
    assert selected_run_model({}) is CANDIDATE_MODEL
    assert selected_run_model({'MYCELIUM_A13_RUN_MODEL': 'qwen3-8b'}).model_id == 'Qwen/Qwen3-8B'
    assert selected_run_model({'MYCELIUM_A13_RUN_MODEL': 'qwen3-14b'}).model_id == 'Qwen/Qwen3-14B'


class _Authority:
    def __init__(self, root): self.created = False
    def start(self): return self
    def status(self): return {'membership_state': 'not_joined', 'swarm_created': False}
    def stop(self): pass
    def renew(self): return self.status()


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.delenv('MYCELIUM_A13_RUN_MODEL', raising=False)
    a = DemoApp(tmp_path / 'private', authority_factory=_Authority, check_platform=False).start()
    yield a
    a.close()


def test_app_route_changes_model_reopens_acquisition_and_survives_restart(app, tmp_path):
    status = app.status()
    assert status['model_contract']['model_id'] == 'Qwen/Qwen2.5-0.5B-Instruct'
    assert status['run_model']['selected'] == 'a9-route'
    assert status['run_model']['source'] == 'demo_default'
    assert status['run_model']['locked'] is False
    assert [c['name'] for c in status['run_model']['choices']] == ['a9-route', 'qwen3-0.6b', 'qwen3-8b', 'qwen3-14b']
    assert {c['name']: c['fits_storage_ceiling'] for c in status['run_model']['choices']} == {
        'a9-route': True, 'qwen3-0.6b': True, 'qwen3-8b': True, 'qwen3-14b': True}
    result = app.action('/api/run-model', {'choice': 'qwen3-0.6b'})
    assert result['selected'] == 'qwen3-0.6b'
    assert app.acquisition.model is QWEN3_0_6B_MODEL
    assert app.acquisition.state_root.name == 'model-source-' + QWEN3_0_6B_MODEL.resolved_commit[:12]
    assert app.status()['model_contract']['model_id'] == 'Qwen/Qwen3-0.6B'
    with pytest.raises(DemoError, match='run_model_choice_invalid'):
        app.action('/api/run-model', {'choice': 'candidate'})
    with pytest.raises(DemoError, match='invalid_request'):
        app.action('/api/run-model', {'choice': 'qwen3-8b', 'extra': 1})
    root = app.root
    app.close()
    again = DemoApp(root, authority_factory=_Authority, check_platform=False).start()
    try:
        assert again.status()['run_model']['selected'] == 'qwen3-0.6b'
        assert again.acquisition.model is QWEN3_0_6B_MODEL
    finally:
        again.close()


def test_model_change_refused_while_download_active_or_env_locked(app, monkeypatch):
    app._acquisition_active = True
    with pytest.raises(DemoError, match='acquisition_already_active'):
        app.action('/api/run-model', {'choice': 'qwen3-0.6b'})
    app._acquisition_active = False
    monkeypatch.setenv('MYCELIUM_A13_RUN_MODEL', 'a9-route')
    assert app.status()['run_model']['locked'] is True
    with pytest.raises(DemoError, match='run_model_locked_by_environment'):
        app.action('/api/run-model', {'choice': 'qwen3-0.6b'})


def test_qwen3_14b_pinned_file_table_matches_hf_and_the_choice_size():
    from mycelium_a13_macos.serving import QWEN3_14B_SNAPSHOT_FILES
    assert sum(size for size, _ in QWEN3_14B_SNAPSHOT_FILES.values()) == 29_552_613_776
    assert run_model.choice('qwen3-14b').snapshot_bytes == 29_552_613_776
    shards = [n for n in QWEN3_14B_SNAPSHOT_FILES if n.endswith('.safetensors')]
    assert shards == ['model-%05d-of-00008.safetensors' % i for i in range(1, 9)]
    import re
    assert all(re.fullmatch(r'sha256:[0-9a-f]{64}', d) for _, d in QWEN3_14B_SNAPSHOT_FILES.values())


def test_local_qwen3_14b_snapshot_sizes_match_the_pinned_table():
    from pathlib import Path
    from mycelium_a13_macos.serving import QWEN3_14B_MODEL, QWEN3_14B_SNAPSHOT_FILES
    snap = Path('~/.cache/huggingface/hub/models--Qwen--Qwen3-14B/snapshots').expanduser() / QWEN3_14B_MODEL.resolved_commit
    if not snap.is_dir():
        pytest.skip('Qwen3-14B snapshot not present on this host')
    present = {p.name: p.stat().st_size for p in snap.iterdir()}
    for name, size in present.items():
        assert QWEN3_14B_SNAPSHOT_FILES[name][0] == size, name
    assert all(n in present for n in QWEN3_14B_SNAPSHOT_FILES if n.endswith('.safetensors'))
