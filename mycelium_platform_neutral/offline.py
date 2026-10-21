# SPDX-License-Identifier: AGPL-3.0-only
"""The offline model-source switch.

With the switch on, nothing in the product contacts Hugging Face: a model that
is not already on a local or LAN source fails closed with ``offline_message``.

Resolution order, first match wins:

1. an explicit CLI value (``--offline`` / ``--no-offline``);
2. ``MYCELIUM_OFFLINE`` (``1``/``true``/``yes``/``on`` or ``0``/``false``/``no``/``off``);
3. implied by the network mode: ``lan`` and ``local`` are offline, ``internet`` is not.

With none of these set the default is online, exactly as before.  Unknown
values are rejected, never coerced.  Nothing here opens a socket.
"""
from __future__ import annotations

import argparse
import os
from collections.abc import MutableMapping, Mapping

from .network_mode import NETWORK_MODE_ENV, NetworkMode, parse_network_mode

OFFLINE_ENV = "MYCELIUM_OFFLINE"
OFFLINE_ERROR_CODE = "offline_model_source_required"

_TRUE = frozenset({"1", "true", "yes", "on"})
_FALSE = frozenset({"0", "false", "no", "off"})


class OfflineSettingError(ValueError):
    """Raised for an unknown ``MYCELIUM_OFFLINE`` value."""


class OfflineModelSourceError(RuntimeError):
    """A Hugging Face contact was refused because offline mode is on."""

    code = OFFLINE_ERROR_CODE

    def __init__(self, what: str = "the model files") -> None:
        super().__init__(offline_message(what))


def offline_message(what: str = "the model files") -> str:
    return (
        f"Offline mode is on, so Hugging Face is not contacted ({what}). "
        "Use a local or LAN model source instead: join the swarm over the LAN "
        "so the owner's device serves the model bytes, or copy the model into "
        "this device's Hugging Face cache folder (~/.cache/huggingface/hub, or "
        "$HF_HOME/hub) and try again. To allow internet downloads anyway, set "
        f"{OFFLINE_ENV}=0 or pass --no-offline."
    )


def _parse_switch(value: str) -> bool:
    lowered = value.lower()
    if lowered in _TRUE:
        return True
    if lowered in _FALSE:
        return False
    raise OfflineSettingError(f"{OFFLINE_ENV} must be 1 or 0")


def offline_enabled(
    environ: Mapping[str, str] | None = None,
    *,
    cli_value: bool | None = None,
    network_mode: NetworkMode | str | None = None,
) -> bool:
    if cli_value is not None:
        return bool(cli_value)
    env = os.environ if environ is None else environ
    if OFFLINE_ENV in env:
        return _parse_switch(env[OFFLINE_ENV])
    if network_mode is None and NETWORK_MODE_ENV in env:
        network_mode = env[NETWORK_MODE_ENV]
    if network_mode is None:
        return False
    mode = (
        network_mode
        if isinstance(network_mode, NetworkMode)
        else parse_network_mode(network_mode)
    )
    return mode is not NetworkMode.INTERNET


def require_online_model_source(
    what: str = "the model files", environ: Mapping[str, str] | None = None
) -> None:
    if offline_enabled(environ):
        raise OfflineModelSourceError(what)


def add_offline_argument(parser: argparse.ArgumentParser) -> None:
    """Add ``--offline`` / ``--no-offline``; the namespace value is ``None`` when unset."""
    parser.add_argument(
        "--offline",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=(
            "never contact Hugging Face; models must come from the owner's LAN "
            f"or the local cache (implied by --network-mode lan/local; {OFFLINE_ENV})"
        ),
    )


def apply_offline(
    cli_value: bool | None,
    network_mode: NetworkMode | str | None,
    environ: MutableMapping[str, str] | None = None,
) -> bool:
    """Resolve the switch and export it so deep code sees the CLI choice.

    The default (internet, nothing set) leaves the environment untouched.
    """
    env = os.environ if environ is None else environ
    enabled = offline_enabled(env, cli_value=cli_value, network_mode=network_mode)
    if enabled:
        env[OFFLINE_ENV] = "1"
    elif cli_value is False or OFFLINE_ENV in env:
        env[OFFLINE_ENV] = "0"
    return enabled
