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


def test_choices_are_every_registered_model_with_pinned_snapshot_bytes():
    names = [c.name for c in run_model.CHOICES]
    assert names == ['qwen2.5-0.5b', 'qwen3-0.6b', 'qwen2.5-7b', 'qwen3-8b', 'qwen3-14b']
    # The picker lists exactly the registered models (the A9 route model is the 0.5B contract).
    assert {c.model for c in run_model.CHOICES} == set(REGISTERED_MODELS)
    sizes = {c.name: c.snapshot_bytes for c in run_model.CHOICES}
    # HF tree totals of the pinned revisions (every file of the snapshot).
    assert sizes == {'qwen2.5-0.5b': 999_604_126, 'qwen3-0.6b': 1_519_209_243, 'qwen2.5-7b': 15_242_807_270,
                     'qwen3-8b': 16_397_461_266, 'qwen3-14b': 29_552_613_776}
    for c in run_model.CHOICES:  # the choice size is the registered shape's snapshot size
        from mycelium_a13_macos.model_resources import model_shape
        assert model_shape(c.model).snapshot_bytes == c.snapshot_bytes
    assert run_model.choice('qwen3-0.6b').model is QWEN3_0_6B_MODEL


def test_demo_default_is_14b_when_it_fits_else_largest_fitting_qwen3():
    assert run_model.demo_default_choice(storage_ceiling=40_000_000_000) == 'qwen3-14b'
    # The 7B-derived proposal alone could not hold the whole 14B snapshot; the
    # consent ceiling is now derived per model (model_resources.py), so the
    # 14B owner requirement (snapshot + 10%) fits and 14B is the default.
    assert RESOURCE_PROPOSAL.additional_disk_bytes < 29_552_613_776
    assert run_model.storage_ceiling() == 32_568_709_325
    assert run_model.demo_default_choice() == 'qwen3-14b'
    assert run_model.demo_default_choice(storage_ceiling=20_000_000_000) == 'qwen3-8b'
    assert run_model.demo_default_choice(storage_ceiling=2_000_000_000) == 'qwen3-0.6b'
    assert run_model.demo_default_choice(storage_ceiling=1_000) is None


def test_choice_persists_in_private_state_and_env_var_still_wins(tmp_path):
    root = tmp_path / 'state'; root.mkdir(mode=0o700)
    assert run_model.app_run_model(root, environ={})[:2] == ('qwen3-14b', 'demo_default')
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
    assert status['model_contract']['model_id'] == 'Qwen/Qwen3-14B'
    assert status['run_model']['selected'] == 'qwen3-14b'
    assert status['run_model']['source'] == 'demo_default'
    assert status['run_model']['locked'] is False
    assert [c['name'] for c in status['run_model']['choices']] == [
        'qwen2.5-0.5b', 'qwen3-0.6b', 'qwen2.5-7b', 'qwen3-8b', 'qwen3-14b']
    assert {c['name']: c['fits_storage_ceiling'] for c in status['run_model']['choices']} == {
        'qwen2.5-0.5b': True, 'qwen3-0.6b': True, 'qwen2.5-7b': True, 'qwen3-8b': True, 'qwen3-14b': True}
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


# -- PV6 picker: every registered model, per-member need, refused while a run is active ------------

def test_picker_lists_label_size_and_per_member_disk_and_ram_for_every_model(app):
    choices = {c['name']: c for c in app.status()['run_model']['choices']}
    assert set(choices) == {'qwen2.5-0.5b', 'qwen3-0.6b', 'qwen2.5-7b', 'qwen3-8b', 'qwen3-14b'}
    for c in choices.values():
        assert c['label'] and c['snapshot_bytes'] > 0 and c['member_disk_bytes'] > 0 and c['member_rss_bytes'] > 0
    assert choices['qwen3-14b']['member_rss_bytes'] > choices['qwen3-8b']['member_rss_bytes'] \
        > choices['qwen3-0.6b']['member_rss_bytes']


def test_choosing_a_qwen25_model_pins_its_contract_and_survives_restart(app):
    from mycelium_a13_macos.serving import CANDIDATE_MODEL
    app.action('/api/run-model', {'choice': 'qwen2.5-7b'})
    assert app.run_model_contract() is CANDIDATE_MODEL
    assert app.status()['model_contract']['model_id'] == 'Qwen/Qwen2.5-7B-Instruct'
    root = app.root
    app.close()
    again = DemoApp(root, authority_factory=_Authority, check_platform=False).start()
    try:
        assert again.status()['run_model']['selected'] == 'qwen2.5-7b'
    finally:
        again.close()


def test_model_change_refused_while_an_owner_run_is_active(app):
    class Run:
        def active(self): return True
    app.owner_run = Run()
    with pytest.raises(DemoError, match='owner_run_active'):
        app.action('/api/run-model', {'choice': 'qwen3-0.6b'})
    assert app.status()['run_model']['selected'] == 'qwen3-14b'
    assert not (app.root / 'run-model.json').exists()


def test_environment_override_still_selects_and_locks_every_env_name(tmp_path, monkeypatch):
    for env, model in (('candidate', 'Qwen/Qwen2.5-7B-Instruct'), ('a9-route', 'Qwen/Qwen2.5-0.5B-Instruct'),
                       ('qwen3-8b', 'Qwen/Qwen3-8B')):
        name, source, contract = run_model.app_run_model(tmp_path, environ={'MYCELIUM_A13_RUN_MODEL': env})
        assert (name, source, contract.model_id) == (env, 'environment', model)
        doc = run_model.document(tmp_path, environ={'MYCELIUM_A13_RUN_MODEL': env})
        assert doc['locked'] is True and doc['source'] == 'environment'


# -- PV7 suggestion on the owner status and the download gate -------------------------------------

GiB = 1 << 30


def _members(app, monkeypatch, *memory_gib):
    import time
    monkeypatch.setattr('mycelium_a13_macos.app.host_memory_bytes', lambda: 48 * GiB)
    reports = {f'node-member-{i}': {'generation': 1, 'expires_at': time.time() + 600,
                                    'device': {'memory_bytes': m * GiB}} for i, m in enumerate(memory_gib)}
    monkeypatch.setattr(app, 'member_capability_reports', lambda: reports)


def test_status_marks_what_fits_the_joined_macs_and_suggests_the_largest(app, monkeypatch):
    _members(app, monkeypatch, 8)
    picker = app.status()['run_model']
    fits = {c['name']: c['fit']['state'] for c in picker['choices']}
    assert fits == {'qwen3-14b': 'fits', 'qwen3-8b': 'fits', 'qwen2.5-7b': 'too_large',
                    'qwen3-0.6b': 'fits', 'qwen2.5-0.5b': 'fits'}
    line = next(c for c in picker['choices'] if c['name'] == 'qwen3-14b')['fit']['line']
    assert line.startswith("fits: needs ") and " GB of your swarm's " in line
    assert picker['suggestion']['name'] == 'qwen3-14b' and picker['suggestion']['members'] == 2
    assert picker['suggestion']['line'].startswith('Largest that fits: Qwen3-14B')


def test_owner_reserve_and_expired_reports_are_taken_into_account(app, monkeypatch):
    from mycelium_a13_macos import adaptive_host
    _members(app, monkeypatch, 8, 64)
    reports = app.member_capability_reports()
    reports['node-member-1']['expires_at'] = 1  # expired: not counted
    original = adaptive_host.read_adaptive_policy
    monkeypatch.setattr(adaptive_host, 'read_adaptive_policy',
                        lambda root: dict(original(root), reserve_bytes=44 * GiB))
    picker = app.status()['run_model']
    assert picker['suggestion']['members'] == 2
    assert {c['name']: c['fit']['state'] for c in picker['choices']}['qwen3-14b'] == 'too_large'


def test_without_a_joined_mac_every_fit_is_unknown_and_nothing_is_refused(app, monkeypatch):
    _members(app, monkeypatch)
    picker = app.status()['run_model']
    assert {c['fit']['state'] for c in picker['choices']} == {'unknown'}
    assert picker['suggestion']['name'] is None and 'Invite' in picker['suggestion']['line']
    app.action('/api/run-model', {'choice': 'qwen3-14b'})  # choosing is never refused by fit


def test_choosing_a_model_that_does_not_fit_is_allowed_but_flagged(app, monkeypatch):
    _members(app, monkeypatch, 8)
    monkeypatch.setattr('mycelium_a13_macos.app.host_memory_bytes', lambda: 8 * GiB)
    doc = app.action('/api/run-model', {'choice': 'qwen3-14b'})
    assert doc['selected'] == 'qwen3-14b'
    assert next(c for c in doc['choices'] if c['name'] == 'qwen3-14b')['fit']['state'] == 'too_large'


def test_download_of_a_model_that_does_not_fit_the_joined_macs_is_refused(app, monkeypatch):
    _members(app, monkeypatch, 8)
    monkeypatch.setattr('mycelium_a13_macos.app.host_memory_bytes', lambda: 8 * GiB)
    app.action('/api/run-model', {'choice': 'qwen3-14b'})
    with pytest.raises(DemoError, match='model_does_not_fit_swarm'):
        app.model_fit_gate()
    app.action('/api/run-model', {'choice': 'qwen3-0.6b'})
    app.model_fit_gate()  # fits: no refusal


def test_fit_gate_does_not_apply_to_an_environment_override_or_a_single_mac(app, monkeypatch):
    _members(app, monkeypatch, 8)
    monkeypatch.setattr('mycelium_a13_macos.app.host_memory_bytes', lambda: 8 * GiB)
    monkeypatch.setenv('MYCELIUM_A13_RUN_MODEL', 'qwen3-14b')
    app.model_fit_gate()  # harness override: flagged only, never refused
    monkeypatch.delenv('MYCELIUM_A13_RUN_MODEL')
    _members(app, monkeypatch)
    app.model_fit_gate()  # nothing joined yet: fit unknown
