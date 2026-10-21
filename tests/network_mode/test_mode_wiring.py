# SPDX-License-Identifier: AGPL-3.0-only
"""Network-mode wiring in the node, seed and supervisor CLIs (pure, no sockets)."""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

from mycelium_platform_neutral.network_mode import NetworkMode

node_main = importlib.import_module("mycelium_node.__main__")
seed_main = importlib.import_module("mycelium_seed.__main__")
process = importlib.import_module("mycelium_node.process")

LAN_IP = "192.168.7.20"


def lan_address() -> str:
    return LAN_IP


def no_lan_address() -> None:
    return None


def node_args(*extra: str):
    return node_main._parser().parse_args(
        ["--data-dir", "d", "--node-id", "n1", "--sidecar-path", "s", *extra]
    )


# -- node ---------------------------------------------------------------------

def test_node_internet_default_keeps_advertise_required_with_todays_failure(capsys) -> None:
    with pytest.raises(SystemExit) as exit_info:
        node_main.run(["--data-dir", "d", "--node-id", "n1", "--sidecar-path", "s"])
    assert exit_info.value.code == node_main.EXIT_PREFLIGHT_FAILURE
    assert capsys.readouterr().err == "node_preflight_failed\n"


def test_node_internet_runtime_network_is_exactly_todays_arguments() -> None:
    args = node_args("--advertise", "https://n1.test/control")
    network = node_main._runtime_network(args, environ={}, stage_runtime=True)
    assert network.mode is NetworkMode.INTERNET
    assert network.advertise == ["https://n1.test/control"]
    assert (network.sidecar_local_only, network.sidecar_lan) == (False, False)


def test_node_internet_without_advertise_is_rejected() -> None:
    with pytest.raises(ValueError):
        node_main._runtime_network(node_args(), environ={}, stage_runtime=True)


def test_node_unknown_mode_is_rejected_from_cli_and_env() -> None:
    with pytest.raises(SystemExit):
        node_args("--network-mode", "wan")
    args = node_args("--advertise", "https://n1.test/control")
    with pytest.raises(ValueError):
        node_main._runtime_network(
            args, environ={"MYCELIUM_NETWORK_MODE": "wan"}, stage_runtime=True
        )


def test_node_env_selects_mode_and_cli_overrides_it() -> None:
    args = node_args("--advertise", "http://192.168.7.5:8766")
    env = {"MYCELIUM_NETWORK_MODE": "local"}
    assert node_main._runtime_network(args, environ=env, stage_runtime=True).mode is NetworkMode.LOCAL
    network = node_main._runtime_network(
        node_args("--advertise", "http://192.168.7.5:8766", "--network-mode", "internet"),
        environ=env,
        stage_runtime=True,
    )
    assert network.mode is NetworkMode.INTERNET


def test_node_local_mode_sets_sidecar_local_only() -> None:
    args = node_args("--advertise", "http://127.0.0.1:8766", "--network-mode", "local")
    network = node_main._runtime_network(args, environ={}, stage_runtime=True)
    assert (network.sidecar_local_only, network.sidecar_lan) == (True, False)


def test_node_lan_auto_advertises_first_private_address_for_control_only_peer() -> None:
    args = node_args("--network-mode", "lan")
    network = node_main._runtime_network(
        args, environ={}, stage_runtime=False, lan_address=lan_address
    )
    assert network.advertise == [f"http://{LAN_IP}:8766"]
    assert network.mode is NetworkMode.LAN


def test_node_lan_advertise_port_and_explicit_advertise_win() -> None:
    port = node_main._runtime_network(
        node_args("--network-mode", "lan", "--advertise-port", "9100"),
        environ={}, stage_runtime=False, lan_address=lan_address,
    )
    assert port.advertise == [f"http://{LAN_IP}:9100"]
    explicit = node_main._runtime_network(
        node_args("--network-mode", "lan", "--advertise", "http://192.168.7.9:1234"),
        environ={}, stage_runtime=False, lan_address=lan_address,
    )
    assert explicit.advertise == ["http://192.168.7.9:1234"]


def test_node_lan_without_a_private_address_fails_closed() -> None:
    with pytest.raises(ValueError):
        node_main._runtime_network(
            node_args("--network-mode", "lan"),
            environ={}, stage_runtime=False, lan_address=no_lan_address,
        )


def test_node_lan_stage_runtime_peer_is_refused_until_the_sidecar_has_a_lan_mode() -> None:
    with pytest.raises(ValueError):
        node_main._runtime_network(
            node_args("--network-mode", "lan"),
            environ={}, stage_runtime=True, lan_address=lan_address,
        )


def test_node_advertise_port_is_validated() -> None:
    for bad in ("0", "65536", "-1", "x"):
        with pytest.raises(SystemExit):
            node_args("--network-mode", "lan", "--advertise-port", bad)


def test_node_lan_dry_run_is_accepted_for_control_only_peer(tmp_path: Path, capsys) -> None:
    # a source-only peer has no sidecar and no stage runtime: lan membership works today
    assert node_main._parser().parse_args(
        ["--data-dir", "d", "--node-id", "n1", "--peer-class", "android_termux_iroh",
         "--network-mode", "lan", "--dry-run"]
    ).advertise is None


# -- process command construction -----------------------------------------------

def _command(tmp_path: Path, **overrides):
    script = tmp_path / "svc.py"
    script.write_text("")
    sidecar = tmp_path / "sidecar"
    sidecar.write_text("")
    artifacts = tmp_path / "a"
    artifacts.mkdir()
    sockets = tmp_path / "s"
    sockets.mkdir()
    params = dict(
        python_executable=sys.executable, service_script=script, run_id="r", deployment_id="d",
        node_id="n", artifact_root=artifacts, socket_root=sockets, sidecar_binary=sidecar,
        sidecar_local_only=False,
    )
    params.update(overrides)
    return process.build_physical_node_command(**params), params


def test_internet_command_is_unchanged_and_has_no_lan_flag(tmp_path: Path) -> None:
    command, _ = _command(tmp_path)
    assert "--sidecar-lan" not in command and "--sidecar-local-only" not in command
    assert command[-2:] == ("--command-timeout", "30.0")


def test_lan_flag_is_appended_only_when_requested(tmp_path: Path) -> None:
    command, _ = _command(tmp_path, sidecar_lan=True)
    assert command[-1] == "--sidecar-lan"


def test_lan_and_local_only_are_mutually_exclusive(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        _command(tmp_path, sidecar_lan=True, sidecar_local_only=True)


# -- supervisor (physical_inference_node) ---------------------------------------

def test_supervisor_cli_accepts_sidecar_lan_exclusively(monkeypatch) -> None:
    pin = importlib.import_module("physical_inference_node")
    base = ["x", "--run-id", "r", "--deployment-id", "d", "--node-id", "n",
            "--artifact-root", "a", "--socket-root", "s", "--sidecar-binary", "b"]
    monkeypatch.setattr(sys, "argv", base)
    assert pin._parse_args().sidecar_lan is False
    monkeypatch.setattr(sys, "argv", base + ["--sidecar-lan"])
    assert pin._parse_args().sidecar_lan is True
    monkeypatch.setattr(sys, "argv", base + ["--sidecar-lan", "--sidecar-local-only"])
    with pytest.raises(SystemExit):
        pin._parse_args()


def test_supervisor_sidecar_argv_gets_lan_flag_only_in_lan_mode(tmp_path: Path) -> None:
    pin = importlib.import_module("physical_inference_node")

    def argv(**extra):
        return pin.NativeSidecarProcess(
            binary=tmp_path / "sc", socket_root=tmp_path, local_only=False,
            queue_capacity=8, startup_timeout=1.0, loopback=False, **extra,
        )._argv(5)

    assert "--lan" not in argv()
    assert argv(lan=True)[-1] == "--lan"
    with pytest.raises(pin.NodeCommandError):
        pin.NativeSidecarProcess(
            binary=tmp_path / "sc", socket_root=tmp_path, local_only=True,
            queue_capacity=8, startup_timeout=1.0, loopback=False, lan=True,
        )


# -- seed -------------------------------------------------------------------------

def seed_args(*extra: str):
    return seed_main._parser().parse_args(["--data-dir", "d", *extra])


def test_seed_internet_default_is_todays_arguments() -> None:
    args = seed_args()
    mode = seed_main._resolve_network(args, environ={})
    assert mode is NetworkMode.INTERNET
    assert (args.bind, args.advertised_url, args.port) == ("127.0.0.1", None, 8765)
    args = seed_args("--bind", "0.0.0.0", "--advertised-url", "http://203.0.113.5:8765")
    seed_main._resolve_network(args, environ={})
    assert (args.bind, args.advertised_url) == ("0.0.0.0", "http://203.0.113.5:8765")


def test_seed_lan_defaults_to_the_private_interface() -> None:
    args = seed_args("--network-mode", "lan")
    seed_main._resolve_network(args, environ={}, lan_address=lan_address)
    assert (args.bind, args.advertised_url) == (LAN_IP, None)


def test_seed_lan_wildcard_bind_derives_the_advertised_url() -> None:
    args = seed_args("--network-mode", "lan", "--bind", "0.0.0.0")
    seed_main._resolve_network(args, environ={}, lan_address=lan_address)
    assert args.advertised_url == f"http://{LAN_IP}:8765"


def test_seed_lan_rejects_proxy_loopback_public_and_missing_interface() -> None:
    for extra in (
        ("--trusted-https-proxy", "--advertised-url", "https://x.example"),
        ("--bind", "127.0.0.1"),
        ("--bind", "203.0.113.5"),
        ("--bind", "0.0.0.0", "--port", "0"),
        ("--bind", "0.0.0.0", "--advertised-url", "http://203.0.113.5:8765"),
    ):
        with pytest.raises(ValueError):
            seed_main._resolve_network(
                seed_args("--network-mode", "lan", *extra), environ={}, lan_address=lan_address
            )
    with pytest.raises(ValueError):
        seed_main._resolve_network(
            seed_args("--network-mode", "lan"), environ={}, lan_address=no_lan_address
        )


def test_seed_local_rejects_non_loopback_bind_and_env_selects_mode() -> None:
    args = seed_args("--network-mode", "local")
    assert seed_main._resolve_network(args, environ={}) is NetworkMode.LOCAL
    assert args.bind == "127.0.0.1"
    with pytest.raises(ValueError):
        seed_main._resolve_network(
            seed_args("--network-mode", "local", "--bind", LAN_IP), environ={}
        )
    args = seed_args()
    assert seed_main._resolve_network(
        args, environ={"MYCELIUM_NETWORK_MODE": "lan"}, lan_address=lan_address
    ) is NetworkMode.LAN


def test_seed_unknown_mode_rejected() -> None:
    with pytest.raises(SystemExit):
        seed_args("--network-mode", "wan")
    with pytest.raises(ValueError):
        seed_main._resolve_network(seed_args(), environ={"MYCELIUM_NETWORK_MODE": "wan"})
