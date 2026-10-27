# SPDX-License-Identifier: AGPL-3.0-only
"""Exact owner-selected contract in status and preparation; no inference."""
import json
import pytest
from dataclasses import asdict
from mycelium_a13_macos.app import DemoApp, preflight
from mycelium_a13_macos.serving import CANDIDATE_MODEL, ServingPreparationError, prepare_registry_launch
from tests.a13_macos.test_serving import _prepared, _operator_plan, seed, private_fixture_identity


def test_status_names_exact_selected_model(tmp_path,monkeypatch):
    # Fresh state selects the tested starter, not the unqualified larger models.
    from mycelium_a13_macos.run_model import choice, demo_default_choice
    monkeypatch.delenv('MYCELIUM_A13_RUN_MODEL',raising=False)
    expected=asdict(choice(demo_default_choice()).model)
    assert expected['model_id']=='Qwen/Qwen2.5-0.5B-Instruct'
    assert preflight()['model_contract']==expected
    app=DemoApp(tmp_path/'app').start()
    try:
        assert app.status()['model_contract']==expected
        assert app.status()['role']=='not_yet_serving'
    finally: app.close()


@pytest.mark.parametrize('seed_value',[17,True,None])
def test_nonzero_or_missing_sampling_seed_refused(tmp_path,seed,seed_value):
    prepared=_prepared(tmp_path)
    plan=_operator_plan(tmp_path,prepared,seed=seed)
    document=json.loads(plan.read_text())
    document['controller']['run_plan']['request']['sampling_seed']=seed_value
    plan.write_text(json.dumps(document))
    with pytest.raises(ServingPreparationError,match='operator_plan_decoding_mismatch'):
        prepare_registry_launch(prepared=prepared,operator_plan=plan,
            private_state_root=tmp_path/'private',seed_state_root=seed._seed_root)
    assert not (tmp_path/'private').exists()
