# SPDX-License-Identifier: AGPL-3.0-only
"""Closed network-mode contract: internet (default) | lan | local."""
from __future__ import annotations

import argparse
import ipaddress

import pytest

from mycelium_platform_neutral import network_mode as nm


def test_enum_is_closed_and_internet_is_default() -> None:
    assert {m.value for m in nm.NetworkMode} == {"internet", "lan", "local"}
    assert nm.DEFAULT_NETWORK_MODE is nm.NetworkMode.INTERNET
    assert nm.NETWORK_MODE_ENV == "MYCELIUM_NETWORK_MODE"


@pytest.mark.parametrize("value", ["internet", "lan", "local"])
def test_parse_accepts_exact_values(value: str) -> None:
    assert nm.parse_network_mode(value).value == value


@pytest.mark.parametrize(
    "value",
    ["", "LAN", " lan", "lan ", "lan\n", "wan", "internet,lan", "mesh", None, 3, b"lan"],
)
def test_parse_rejects_everything_else(value: object) -> None:
    with pytest.raises(nm.NetworkModeError):
        nm.parse_network_mode(value)  # type: ignore[arg-type]


def test_network_mode_error_is_a_value_error() -> None:
    assert issubclass(nm.NetworkModeError, ValueError)


def test_env_unset_is_internet_and_set_is_parsed() -> None:
    assert nm.network_mode_from_env({}) is nm.NetworkMode.INTERNET
    assert nm.network_mode_from_env({nm.NETWORK_MODE_ENV: "lan"}) is nm.NetworkMode.LAN
    assert nm.network_mode_from_env({nm.NETWORK_MODE_ENV: "local"}) is nm.NetworkMode.LOCAL


@pytest.mark.parametrize("value", ["", "Lan", "wan", " internet"])
def test_env_rejects_unknown_or_empty_values(value: str) -> None:
    with pytest.raises(nm.NetworkModeError):
        nm.network_mode_from_env({nm.NETWORK_MODE_ENV: value})


def test_resolve_prefers_cli_then_env_then_default() -> None:
    env = {nm.NETWORK_MODE_ENV: "lan"}
    assert nm.resolve_network_mode("local", env) is nm.NetworkMode.LOCAL
    assert nm.resolve_network_mode(None, env) is nm.NetworkMode.LAN
    assert nm.resolve_network_mode(None, {}) is nm.NetworkMode.INTERNET
    with pytest.raises(nm.NetworkModeError):
        nm.resolve_network_mode("bogus", {})
    with pytest.raises(nm.NetworkModeError):
        nm.resolve_network_mode(None, {nm.NETWORK_MODE_ENV: "bogus"})


def test_mode_properties() -> None:
    internet, lan, local = nm.NetworkMode.INTERNET, nm.NetworkMode.LAN, nm.NetworkMode.LOCAL
    assert (internet.uses_hosted_discovery, lan.uses_hosted_discovery, local.uses_hosted_discovery) == (
        True, False, False)
    assert (internet.sidecar_local_only, lan.sidecar_local_only, local.sidecar_local_only) == (
        False, False, True)
    assert (internet.sidecar_lan, lan.sidecar_lan, local.sidecar_lan) == (False, True, False)
    assert (internet.allows_private_origins, lan.allows_private_origins, local.allows_private_origins) == (
        False, True, False)


def test_stage_runtime_lan_sidecar_is_declared_unsupported_in_this_lane() -> None:
    assert nm.SIDECAR_LAN_SUPPORTED is False


@pytest.mark.parametrize("host", ["10.0.0.5", "172.16.0.1", "172.31.255.255", "192.168.1.2"])
def test_rfc1918_accepts_private_ranges(host: str) -> None:
    assert nm.is_rfc1918(host) is True


@pytest.mark.parametrize(
    "host",
    ["172.32.0.1", "100.64.0.1", "169.254.1.1", "127.0.0.1", "0.0.0.0", "8.8.8.8", "::1", "fd00::1",
     "not-an-ip", "", "010.0.0.1", "192.168.1.2/24"],
)
def test_rfc1918_rejects_everything_else(host: str) -> None:
    assert nm.is_rfc1918(host) is False


def test_first_private_lan_address_takes_first_rfc1918_candidate() -> None:
    assert nm.first_private_lan_address(["127.0.0.1", "8.8.8.8", "192.168.4.7", "10.1.1.1"]) == "192.168.4.7"
    assert nm.first_private_lan_address(["127.0.0.1", "8.8.8.8"]) is None
    assert nm.first_private_lan_address([]) is None


def test_first_private_lan_address_default_uses_injected_route_lookup() -> None:
    assert nm.first_private_lan_address(route_lookup=lambda: "10.9.8.7") == "10.9.8.7"
    assert nm.first_private_lan_address(route_lookup=lambda: None) is None
    assert nm.first_private_lan_address(route_lookup=lambda: "203.0.113.9") is None
    ipaddress.ip_address("10.9.8.7")


def test_argparse_helper_defaults_to_none_and_rejects_unknown() -> None:
    parser = argparse.ArgumentParser(exit_on_error=True)
    nm.add_network_mode_argument(parser)
    assert parser.parse_args([]).network_mode is None
    assert parser.parse_args(["--network-mode", "lan"]).network_mode == "lan"
    with pytest.raises(SystemExit):
        parser.parse_args(["--network-mode", "wan"])
