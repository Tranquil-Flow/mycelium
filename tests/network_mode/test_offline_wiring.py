# SPDX-License-Identifier: AGPL-3.0-only
"""--offline wiring in the node and the A13 launcher (pure, no sockets)."""
from __future__ import annotations

import importlib
import sys

import pytest

from mycelium_platform_neutral.offline import OFFLINE_ENV

node_main = importlib.import_module("mycelium_node.__main__")


def node_args(*extra: str):
    return node_main._parser().parse_args(
        [
            "--data-dir", "d", "--node-id", "n1", "--sidecar-path", "s",
            "--advertise", "https://n1.test/control", *extra,
        ]
    )


def test_node_internet_default_leaves_the_environment_untouched() -> None:
    environ: dict[str, str] = {}
    node_main._runtime_network(node_args(), environ=environ, stage_runtime=True)
    assert environ == {}


def test_node_offline_flag_is_exported_for_deep_code() -> None:
    environ: dict[str, str] = {}
    node_main._runtime_network(node_args("--offline"), environ=environ, stage_runtime=True)
    assert environ[OFFLINE_ENV] == "1"


def test_node_lan_mode_implies_offline() -> None:
    environ: dict[str, str] = {}
    node_main._runtime_network(
        node_args("--network-mode", "lan"), environ=environ, stage_runtime=False
    )
    assert environ[OFFLINE_ENV] == "1"


def test_node_lan_mode_can_be_explicitly_overridden() -> None:
    environ: dict[str, str] = {}
    node_main._runtime_network(
        node_args("--network-mode", "lan", "--no-offline"), environ=environ, stage_runtime=False
    )
    assert environ[OFFLINE_ENV] == "0"
    environ = {OFFLINE_ENV: "0", "MYCELIUM_NETWORK_MODE": "lan"}
    node_main._runtime_network(node_args(), environ=environ, stage_runtime=False)
    assert environ[OFFLINE_ENV] == "0"


@pytest.mark.parametrize("argv,expected", [
    (["--offline", "--preflight"], "1"),
    (["--preflight"], None),
])
def test_a13_launcher_exports_the_offline_switch(monkeypatch, argv, expected) -> None:
    launcher = importlib.import_module("mycelium_a13_macos.__main__")
    monkeypatch.delenv(OFFLINE_ENV, raising=False)
    monkeypatch.delenv("MYCELIUM_NETWORK_MODE", raising=False)
    monkeypatch.setattr(launcher, "preflight", lambda: {"ok": True})
    monkeypatch.setattr(sys, "argv", ["launcher", *argv])
    import os

    assert launcher.main() == 0
    assert os.environ.get(OFFLINE_ENV) == expected
    monkeypatch.delenv(OFFLINE_ENV, raising=False)
