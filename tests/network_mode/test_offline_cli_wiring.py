# SPDX-License-Identifier: AGPL-3.0-only
"""--offline in the seed CLI and ``mycelium_demo serve`` (pure, no sockets)."""
from __future__ import annotations

import importlib
import os
from types import SimpleNamespace

import pytest

from mycelium_demo import cli
from mycelium_platform_neutral.offline import OFFLINE_ENV

seed_main = importlib.import_module("mycelium_seed.__main__")
LAN_IP = "192.168.7.20"


def lan_address() -> str:
    return LAN_IP


def seed_args(*extra: str):
    return seed_main._parser().parse_args(["--data-dir", "d", *extra])


# -- seed ----------------------------------------------------------------------

def test_seed_internet_default_leaves_the_environment_untouched() -> None:
    environ: dict[str, str] = {}
    seed_main._resolve_network(seed_args(), environ=environ)
    assert environ == {}


def test_seed_offline_flag_is_exported() -> None:
    environ: dict[str, str] = {}
    seed_main._resolve_network(seed_args("--offline"), environ=environ)
    assert environ[OFFLINE_ENV] == "1"


@pytest.mark.parametrize("mode", ["lan", "local"])
def test_seed_lan_and_local_imply_offline(mode: str) -> None:
    environ: dict[str, str] = {}
    seed_main._resolve_network(
        seed_args("--network-mode", mode), environ=environ, lan_address=lan_address
    )
    assert environ[OFFLINE_ENV] == "1"


def test_seed_implied_offline_can_be_overridden() -> None:
    environ: dict[str, str] = {}
    seed_main._resolve_network(
        seed_args("--network-mode", "lan", "--no-offline"), environ=environ,
        lan_address=lan_address,
    )
    assert environ[OFFLINE_ENV] == "0"
    environ = {"MYCELIUM_NETWORK_MODE": "lan", OFFLINE_ENV: "0"}
    seed_main._resolve_network(seed_args(), environ=environ, lan_address=lan_address)
    assert environ[OFFLINE_ENV] == "0"


# -- serve ---------------------------------------------------------------------

def run_serve(*extra: str, environ: dict[str, str] | None = None, mode: str = "fixture"):
    env = {"PATH": "/usr/bin"} if environ is None else environ
    runs: list[dict] = []
    live: list[list[str]] = []

    def runner(command, *, cwd, env, check):
        runs.append(dict(env))
        return SimpleNamespace(returncode=0)

    argv = ["serve", "--mode", mode, *extra]
    if mode == "live":
        argv += ["--operator-plan", "plan.json", "--seed-state-root", "seed"]
    code = cli.main(
        argv, process_runner=runner, live_server_main=lambda a: live.append(list(a)) or 0,
        environ=env, fixture_runtime_available=lambda _p: True,
    )
    return code, env, runs, live


def test_serve_internet_default_leaves_the_environment_untouched() -> None:
    _, env, runs, _ = run_serve()
    assert env == {"PATH": "/usr/bin"}
    assert runs == [{"PATH": "/usr/bin", "VITE_OBSERVATORY_SOURCE_MODE": "fixture"}]


def test_serve_offline_flag_is_exported_to_the_child_environment() -> None:
    _, env, runs, _ = run_serve("--offline")
    assert env[OFFLINE_ENV] == "1" and runs[0][OFFLINE_ENV] == "1"


@pytest.mark.parametrize("mode", ["lan", "local"])
def test_serve_lan_and_local_imply_offline(mode: str) -> None:
    _, env, _, _ = run_serve("--network-mode", mode)
    assert env[OFFLINE_ENV] == "1"


def test_serve_implied_offline_is_overridable_by_flag_and_environment() -> None:
    _, env, _, _ = run_serve("--network-mode", "lan", "--no-offline")
    assert env[OFFLINE_ENV] == "0"
    _, env, _, _ = run_serve(environ={"MYCELIUM_NETWORK_MODE": "lan", OFFLINE_ENV: "0"})
    assert env[OFFLINE_ENV] == "0"
    _, env, _, _ = run_serve(environ={"MYCELIUM_NETWORK_MODE": "lan"})
    assert env[OFFLINE_ENV] == "1"


def test_serve_live_sets_the_switch_and_forwards_neither_flag_to_the_supervisor() -> None:
    code, env, _, live = run_serve("--offline", "--network-mode", "lan", mode="live")
    assert code == 0 and env[OFFLINE_ENV] == "1"
    assert len(live) == 1
    assert "--offline" not in live[0] and "--no-offline" not in live[0]
    assert "--network-mode" not in live[0]


def test_serve_rejects_an_unknown_offline_value_without_a_traceback() -> None:
    with pytest.raises(SystemExit) as caught:
        run_serve(environ={"PATH": "/usr/bin", OFFLINE_ENV: "maybe"})
    assert caught.value.code == 2


def test_serve_without_an_injected_environment_uses_the_process_environment(monkeypatch) -> None:
    monkeypatch.delenv(OFFLINE_ENV, raising=False)
    monkeypatch.delenv("MYCELIUM_NETWORK_MODE", raising=False)
    code = cli.main(
        ["serve", "--mode", "fixture", "--offline"],
        process_runner=lambda *a, **k: SimpleNamespace(returncode=0),
        fixture_runtime_available=lambda _p: True,
    )
    assert code == 0 and os.environ[OFFLINE_ENV] == "1"
    monkeypatch.delenv(OFFLINE_ENV, raising=False)
