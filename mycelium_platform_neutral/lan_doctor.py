# SPDX-License-Identifier: AGPL-3.0-only
"""``doctor --lan``: is this machine ready to run a swarm with no hosted service?

Pure logic over injected probes; the CLI wires the real ones.  Every failing
item carries a one-sentence fix in plain English.  The report names environment
variables and flags, never their values (a URL may carry credentials).
"""
from __future__ import annotations

import ipaddress
from collections.abc import Callable, Mapping, Sequence
from typing import Any
from urllib.parse import urlsplit

from . import network_mode as network_mode_module
from .network_mode import NetworkMode, NetworkModeError
from .offline import OFFLINE_ENV, OfflineSettingError, offline_enabled

PROTOCOL = "mycelium.lan_doctor.v1"
DEFAULT_PORTS = (8765, 8766)  # seed, node advertise
CHECK_NAMES = ("network_mode", "private_address", "ports", "hosted_endpoints", "offline")

_RELAY_FLAGS = frozenset({"--force-relay", "--sidecar-force-relay", "--relay-url"})
_ORIGIN_FLAGS = frozenset(
    {"--directory-origin", "--public-origin", "--entry-origin", "--origin"}
)
_ENV_PREFIXES = ("MYCELIUM_", "IROH_")
_ENV_NAMES = frozenset({"HF_ENDPOINT", "HF_HUB_ENDPOINT"})
_HOSTED_SUFFIXES = (
    ("trycloudflare.com", "Cloudflare tunnel"),
    ("cloudflare-dns.com", "Cloudflare DNS"),
    ("mycelium.now", "hosted Mycelium service"),
    ("iroh.link", "n0 relay or DNS"),
    ("iroh.network", "n0 relay or DNS"),
    ("n0.computer", "n0 relay or DNS"),
)


def _check(name: str, ok: bool, detail: str, fix: str | None) -> dict[str, Any]:
    return {"name": name, "ok": ok, "detail": detail, "fix": None if ok else fix}


def _hosted_reason(value: str) -> str | None:
    """Why ``value`` (a URL) is a hosted endpoint, or ``None`` for a local one."""
    try:
        parts = urlsplit(value)
        host = (parts.hostname or "").lower()
    except ValueError:
        return "unreadable endpoint"
    if not host:
        return None
    for suffix, reason in _HOSTED_SUFFIXES:
        if host == suffix or host.endswith("." + suffix):
            return reason
    if host == "localhost" or network_mode_module.is_rfc1918(host):
        return None
    try:
        if ipaddress.ip_address(host).is_loopback:
            return None
    except ValueError:
        pass
    return f"public {parts.scheme.upper()} endpoint"


def _flag_findings(flags: Sequence[str]) -> list[str]:
    found: list[str] = []
    tokens = list(flags)
    for index, token in enumerate(tokens):
        name, equals, inline = token.partition("=")
        if name in _RELAY_FLAGS:
            found.append(f"{name} (relay)")
        elif name in _ORIGIN_FLAGS:
            value = inline if equals else (tokens[index + 1] if index + 1 < len(tokens) else "")
            reason = _hosted_reason(value) if "://" in value else None
            if reason is not None:
                found.append(f"{name} ({reason})")
    return found


def _env_findings(environ: Mapping[str, str]) -> list[str]:
    found: list[str] = []
    for name in sorted(environ):
        if not (name.startswith(_ENV_PREFIXES) or name in _ENV_NAMES):
            continue
        value = environ[name]
        if not isinstance(value, str) or "://" not in value:
            continue
        reason = _hosted_reason(value)
        if reason is not None:
            found.append(f"{name} ({reason})")
    return found


def run_lan_doctor(
    *,
    environ: Mapping[str, str],
    network_mode: str | None = None,
    offline: bool | None = None,
    flags: Sequence[str] = (),
    ports: Sequence[int] = DEFAULT_PORTS,
    lan_address: Callable[[], str | None] = network_mode_module.route_lookup_ipv4,
    port_available: Callable[[int], bool],
) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []

    # 1. network mode
    mode: NetworkMode | None
    try:
        mode = network_mode_module.resolve_network_mode(network_mode, environ)
    except NetworkModeError:
        mode = None
        checks.append(_check(
            "network_mode", False, "The network mode has an unknown value.",
            "Set MYCELIUM_NETWORK_MODE (or --network-mode) to one of internet, lan or local; "
            "for a swarm on your own network use lan.",
        ))
    else:
        lan_fix = (
            "Run every Mycelium command with --network-mode lan, or export "
            "MYCELIUM_NETWORK_MODE=lan."
        )
        if mode is NetworkMode.LAN:
            checks.append(_check("network_mode", True, "Network mode is lan.", None))
        elif mode is NetworkMode.LOCAL:
            checks.append(_check(
                "network_mode", False,
                "Network mode is local, which is loopback only: other devices cannot connect.",
                lan_fix,
            ))
        else:
            checks.append(_check(
                "network_mode", False,
                "Network mode is internet (the default), which uses hosted relays and lookup.",
                lan_fix,
            ))

    # 2. a private address to advertise
    address = lan_address()
    if network_mode_module.is_rfc1918(address):
        checks.append(_check(
            "private_address", True, f"This device's private address is {address}.", None
        ))
    else:
        checks.append(_check(
            "private_address", False, "No private network address was found for this device.",
            "Connect to your Wi-Fi or Ethernet network (a 192.168.x.x, 10.x.x.x or 172.16-31.x.x "
            "address) and rerun, or pass --bind and --advertise with the address to use.",
        ))

    # 3. ports (loopback-only test; this does not open a LAN listener)
    busy = [port for port in ports if not port_available(port)]
    if busy:
        checks.append(_check(
            "ports", False,
            "These ports are already in use: " + ", ".join(str(port) for port in busy) + ".",
            "Stop the program using them, or choose free ports with --port (seed) and "
            "--advertise-port (node).",
        ))
    else:
        checks.append(_check(
            "ports", True, "Ports " + ", ".join(str(port) for port in ports) + " are free.", None
        ))

    # 4. nothing hosted configured
    hosted = _env_findings(environ) + _flag_findings(flags)
    if hosted:
        names = ", ".join(hosted)
        checks.append(_check(
            "hosted_endpoints", False, f"Hosted endpoints are configured: {names}.",
            "Remove them (unset the environment variables, drop the flags) so nothing "
            "leaves your network: " + ", ".join(item.split(" ")[0] for item in hosted) + ".",
        ))
    else:
        checks.append(_check(
            "hosted_endpoints", True,
            "No relay, tunnel, hosted directory or public endpoint is configured.", None,
        ))

    # 5. offline switch
    try:
        offline_on = offline_enabled(environ, cli_value=offline, network_mode=mode)
    except (OfflineSettingError, NetworkModeError):
        checks.append(_check(
            "offline", False, f"{OFFLINE_ENV} has an unknown value.",
            f"Set {OFFLINE_ENV} to 1 or 0 (or unset it).",
        ))
    else:
        if offline_on:
            checks.append(_check(
                "offline", True, "Offline mode is on: Hugging Face is never contacted.", None
            ))
        else:
            checks.append(_check(
                "offline", False,
                "Offline mode is off: a missing model would be downloaded from Hugging Face.",
                f"Unset {OFFLINE_ENV}=0 or set {OFFLINE_ENV}=1 (or pass --offline), and copy "
                "the model to the owner's device so it is served over the LAN.",
            ))

    return {"protocol": PROTOCOL, "ok": all(item["ok"] for item in checks), "checks": checks}


def render_report(report: Mapping[str, Any]) -> str:
    lines = ["Mycelium LAN doctor"]
    failing = 0
    for item in report["checks"]:
        if item["ok"]:
            lines.append(f"  [ok]  {item['name']}: {item['detail']}")
        else:
            failing += 1
            lines.append(f"  [fix] {item['name']}: {item['detail']}")
            lines.append(f"        Fix: {item['fix']}")
    lines.append(
        ("READY: the configuration keeps everything on your network. Membership works; inference "
         "between devices needs the sidecar LAN mode, which is not available yet.")
        if report["ok"]
        else f"NOT READY: {failing} of {len(report['checks'])} checks need attention."
    )
    return "\n".join(lines) + "\n"
