# SPDX-License-Identifier: AGPL-3.0-only
"""The closed network-mode contract shared by seed, node, supervisor and directory.

``internet``  Today's behaviour and the default: iroh N0 preset (n0 relays and
              n0 pkarr/DNS address lookup), public HTTPS origins.
``lan``       Private-network mode: no relay, no n0 DNS/pkarr lookup, peers are
              reached on RFC 1918 addresses, private ``http(s)`` directory and
              entry origins are accepted.  Nothing in this mode contacts a
              hosted service.
``local``     Loopback only (the sidecar ``--local-only`` test mode).

The value is selected with ``--network-mode`` or ``MYCELIUM_NETWORK_MODE``;
unknown values are rejected, never coerced.  Nothing here opens a socket except
``route_lookup_ipv4``, a UDP route lookup that sends no packet.
"""
from __future__ import annotations

import argparse
import ipaddress
import os
import socket
from collections.abc import Callable, Iterable, Mapping
from enum import Enum

NETWORK_MODE_ENV = "MYCELIUM_NETWORK_MODE"

# The Rust sidecar has no LAN bind mode yet (docs/design/lan-direct-transport.md).
# Until it does, a stage-runtime peer must refuse ``lan`` before launch instead
# of failing inside the sidecar.  Membership-only peers are unaffected.
SIDECAR_LAN_SUPPORTED = False

_RFC1918 = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
)


class NetworkModeError(ValueError):
    """Raised for any value outside the closed network-mode set."""


class NetworkMode(str, Enum):
    INTERNET = "internet"
    LAN = "lan"
    LOCAL = "local"

    @property
    def uses_hosted_discovery(self) -> bool:
        """True only where n0 relays / n0 DNS lookup are in play."""
        return self is NetworkMode.INTERNET

    @property
    def sidecar_local_only(self) -> bool:
        return self is NetworkMode.LOCAL

    @property
    def sidecar_lan(self) -> bool:
        return self is NetworkMode.LAN

    @property
    def allows_private_origins(self) -> bool:
        return self is NetworkMode.LAN


DEFAULT_NETWORK_MODE = NetworkMode.INTERNET


def parse_network_mode(value: object) -> NetworkMode:
    if not isinstance(value, str):
        raise NetworkModeError("network mode is invalid")
    for mode in NetworkMode:
        if value == mode.value:
            return mode
    raise NetworkModeError("network mode is invalid")


def network_mode_from_env(environ: Mapping[str, str] | None = None) -> NetworkMode:
    env = os.environ if environ is None else environ
    if NETWORK_MODE_ENV not in env:
        return DEFAULT_NETWORK_MODE
    return parse_network_mode(env[NETWORK_MODE_ENV])


def resolve_network_mode(
    cli_value: str | None, environ: Mapping[str, str] | None = None
) -> NetworkMode:
    """CLI value wins over the environment, which wins over the default."""
    if cli_value is not None:
        return parse_network_mode(cli_value)
    return network_mode_from_env(environ)


def _argparse_mode(value: str) -> str:
    try:
        return parse_network_mode(value).value
    except NetworkModeError:
        raise argparse.ArgumentTypeError("network mode is invalid") from None


def add_network_mode_argument(parser: argparse.ArgumentParser) -> None:
    """Add ``--network-mode``; the namespace value is ``None`` when not given."""
    parser.add_argument(
        "--network-mode",
        type=_argparse_mode,
        default=None,
        help=(
            "internet (default), lan (private network, no relay/n0 lookup) or "
            f"local (loopback); falls back to {NETWORK_MODE_ENV}"
        ),
    )


def is_rfc1918(host: object) -> bool:
    if not isinstance(host, str) or not host:
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.version == 4 and any(address in network for network in _RFC1918)


def route_lookup_ipv4() -> str | None:
    """Primary outbound IPv4 via a UDP route lookup; no packet is sent."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("192.0.2.1", 9))  # TEST-NET-1: route table only
        return probe.getsockname()[0]
    except OSError:
        return None
    finally:
        probe.close()


def first_private_lan_address(
    candidates: Iterable[str] | None = None,
    *,
    route_lookup: Callable[[], str | None] = route_lookup_ipv4,
) -> str | None:
    """First RFC 1918 address among ``candidates`` (default: the primary route)."""
    if candidates is None:
        found = route_lookup()
        candidates = () if found is None else (found,)
    for candidate in candidates:
        if is_rfc1918(candidate):
            return candidate
    return None
