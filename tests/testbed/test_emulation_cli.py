# SPDX-License-Identifier: AGPL-3.0-only
"""CLI parsing only; runtime emulation is covered by its real chain suite."""
from types import SimpleNamespace

import pytest

from mycelium_testbed import emulation


@pytest.mark.parametrize("steps", [1, 13])
def test_generation_steps_option_forwards_integer_budget(monkeypatch, tmp_path, steps):
    observed = []

    def run(**kwargs):
        observed.append(kwargs)
        return SimpleNamespace(to_document=lambda: {"cli_test": True})

    monkeypatch.setattr(emulation, "run_emulation", run)
    assert emulation.main(["run", "--run-dir", str(tmp_path),
                           "--generation-steps", str(steps)]) == 0
    assert len(observed) == 1
    assert observed[0]["token_steps"] == steps
    assert observed[0]["run_dir"] == tmp_path


def test_generation_steps_rejects_non_numeric_budget_before_execution(monkeypatch, tmp_path, capsys):
    def forbidden(**kwargs):
        pytest.fail("invalid budget reached emulation")

    monkeypatch.setattr(emulation, "run_emulation", forbidden)
    with pytest.raises(SystemExit) as stopped:
        emulation.main(["run", "--run-dir", str(tmp_path),
                        "--generation-steps", "not-a-number"])
    assert stopped.value.code == 2
    assert "invalid int value" in capsys.readouterr().err


def test_cli_default_generation_budget_is_unchanged(monkeypatch, tmp_path):
    observed = []

    def run(**kwargs):
        observed.append(kwargs)
        return SimpleNamespace(to_document=lambda: {"cli_test": True})

    monkeypatch.setattr(emulation, "run_emulation", run)
    assert emulation.main(["run", "--run-dir", str(tmp_path)]) == 0
    assert observed[0]["token_steps"] == emulation.TOKEN_STEPS
