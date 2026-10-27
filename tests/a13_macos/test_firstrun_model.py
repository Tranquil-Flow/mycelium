# SPDX-License-Identifier: AGPL-3.0-only
"""Fresh starter selection must never migrate an existing explicit choice."""
import pytest
from mycelium_a13_macos import run_model
from mycelium_a13_macos.serving import A9_ROUTE_MODEL


def test_starter_identity_size_and_honest_labels(tmp_path):
    starter = run_model.choice('a9-route')
    assert starter.model is A9_ROUTE_MODEL
    assert starter.snapshot_bytes == 999_604_126
    assert 'tested starter' in starter.label
    assert 'tested on two Macs' in starter.label
    for c in run_model.CHOICES:
        if c.name != 'a9-route':
            assert 'not yet tested on two Macs' in c.label
    assert run_model.app_run_model(tmp_path, environ={}) == ('a9-route', 'demo_default', A9_ROUTE_MODEL)


@pytest.mark.parametrize('name', ['qwen3-0.6b', 'qwen3-8b', 'qwen3-14b'])
def test_existing_choice_is_unchanged(tmp_path, name):
    run_model.save_choice(tmp_path, name)
    before = (tmp_path / 'run-model.json').read_bytes()
    assert run_model.app_run_model(tmp_path, environ={})[:2] == (name, 'saved')
    assert (tmp_path / 'run-model.json').read_bytes() == before
    assert run_model.app_run_model(tmp_path, environ={'MYCELIUM_A13_RUN_MODEL': 'a9-route'})[2] is A9_ROUTE_MODEL
