# SPDX-License-Identifier: AGPL-3.0-only
"""The persisted transport ledger is opt-in on the live serve and bound to its deployment."""
from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from mycelium_demo import cli
from mycelium_live import supervisor as supervisor_module
from mycelium_product_spine.state import TRANSPORT_HISTORY_FILE
from tests.live.test_product_application_internet import _Qualification, _Route
from tests.a14_acceptance.test_transport_history_persistence import (
    DEPLOYMENT,
    _document,
    _observation,
)


def _base(tmp_path: Path) -> list[str]:
    return ["--operator-plan", str(tmp_path / "one.json"),
            "--seed-state-root", str(tmp_path / "seed")]


def test_supervisor_forwards_the_flag_only_when_given(tmp_path, monkeypatch):
    calls: list[dict] = []
    monkeypatch.setattr(supervisor_module, "run_physical_server",
                        lambda **kwargs: calls.append(kwargs) or 0)
    assert supervisor_module.main(_base(tmp_path)) == 0
    assert "persist_transport_history" not in calls[-1]
    assert supervisor_module.main([*_base(tmp_path), "--persist-transport-history"]) == 0
    assert calls[-1]["persist_transport_history"] is True


def test_the_registry_serve_refuses_the_flag(tmp_path):
    argv = ["--operator-plan", str(tmp_path / "one.json"),
            "--operator-plan", str(tmp_path / "two.json"),
            "--seed-state-root", str(tmp_path / "seed"), "--persist-transport-history"]
    with pytest.raises(SystemExit, match="--persist-transport-history is valid only"):
        supervisor_module.main(argv)


def test_the_demo_cli_forwards_the_flag_to_the_live_server(tmp_path):
    forwarded: list[list[str]] = []
    argv = ["serve", "--mode", "live", "--operator-plan", str(tmp_path / "plan.json"),
            "--seed-state-root", str(tmp_path / "seed"), "--persist-transport-history"]
    assert cli.main(argv, live_server_main=lambda a: forwarded.append(list(a)) or 0,
                    environ={}) == 0
    assert forwarded[0][-1] == "--persist-transport-history"


def test_the_fixture_serve_refuses_the_live_only_flag(tmp_path):
    with pytest.raises(SystemExit) as raised:
        cli.main(["serve", "--mode", "fixture", "--persist-transport-history"], environ={})
    assert raised.value.code != 0


def test_the_product_application_binds_the_ledger_to_the_deployment(tmp_path):
    root = tmp_path / "state"
    root.mkdir(mode=0o700)
    os.chmod(root, 0o700)
    app = supervisor_module._optional_product_application(
        _Route(_document(_observation(6, "direct"))), _Qualification(),
        state_root=root, transport_binding=DEPLOYMENT)
    app._publish()
    assert (root / TRANSPORT_HISTORY_FILE).stat().st_mode & 0o777 == 0o600


def test_live_stack_binding_requires_the_flag_and_a_state_root(tmp_path):
    graph = SimpleNamespace(deployment_id=DEPLOYMENT)
    assert supervisor_module._transport_history_binding(
        graph, product_state_root=tmp_path, persist=False) is None
    assert supervisor_module._transport_history_binding(
        graph, product_state_root=tmp_path, persist=True) == DEPLOYMENT
    with pytest.raises(ValueError, match="a14_transport_history_state_root_required"):
        supervisor_module._transport_history_binding(
            graph, product_state_root=None, persist=True)
    with pytest.raises(ValueError, match="a14_transport_history_deployment_unavailable"):
        supervisor_module._transport_history_binding(
            SimpleNamespace(), product_state_root=tmp_path, persist=True)
