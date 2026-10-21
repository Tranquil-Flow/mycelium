# SPDX-License-Identifier: AGPL-3.0-only
"""C1: byte-source order follows the joiner's network (owner LAN only on the same /24).

Digests are checked per file by the acquisition whatever the order, so the order
is integrity-neutral; it only decides cost (owner uplink + Cloudflare vs Hugging Face).
"""
from __future__ import annotations

import importlib
import types

import pytest

pms = importlib.import_module("mycelium_a13_macos.peer_model_source")

PEM = "-----BEGIN CERTIFICATE-----\nAAAA\n-----END CERTIFICATE-----\n"
OWNER_LAN = "https://192.168.1.5:45000"


class _Origin:
    timeout_seconds = 5.0

    def resolve(self, model):  # pragma: no cover - not called
        raise AssertionError("resolve is not part of the order test")

    def read_range(self, *args):  # pragma: no cover - not called
        raise AssertionError("no bytes are read in the order test")


def _announcement(*, lan: bool = True, relay: bool = True) -> dict:
    return {"model_source": {
        "protocol": pms.HINT, "relay": relay,
        "lan_origin": OWNER_LAN if lan else None,
        "tls_certificate_pem": PEM if lan else None,
    }}


def _names(monkeypatch, joiner_host, *, offline=False, lan=True, relay=True,
           fallback=True) -> list[str]:
    monkeypatch.setattr(pms, "choose_lan_host", lambda: joiner_host)
    monkeypatch.setattr(pms, "offline_enabled", lambda *a, **k: offline)
    monkeypatch.delenv(pms.ENVIRONMENT, raising=False)
    app = types.SimpleNamespace(authority=types.SimpleNamespace(peer=object()))
    transport = pms.joiner_model_source(
        app, _announcement(lan=lan, relay=relay), fallback=_Origin() if fallback else None)
    return [row["name"] for row in transport.status()]


@pytest.mark.parametrize("joiner_host,offline,fallback,expected", [
    # same /24: unchanged order (owner LAN, relay, Hugging Face)
    ("192.168.1.9", False, True, ["owner_lan", "owner_relay", "huggingface"]),
    ("192.168.1.250", False, True, ["owner_lan", "owner_relay", "huggingface"]),
    # same LAN, offline / no Hugging Face: LAN then relay only
    ("192.168.1.9", True, False, ["owner_lan", "owner_relay"]),
    # another network: no owner_lan, Hugging Face before the owner relay
    ("10.0.0.7", False, True, ["huggingface", "owner_relay"]),
    ("192.168.2.9", False, True, ["huggingface", "owner_relay"]),   # same /16, not /24
    ("172.20.1.4", False, True, ["huggingface", "owner_relay"]),
    # joiner has no private LAN address at all
    (None, False, True, ["huggingface", "owner_relay"]),
    # another network and offline / no Hugging Face: the relay only, never HF
    ("10.0.0.7", True, False, ["owner_relay"]),
    (None, True, False, ["owner_relay"]),
])
def test_source_order_follows_the_joiner_network(monkeypatch, joiner_host, offline, fallback,
                                                 expected) -> None:
    assert _names(monkeypatch, joiner_host, offline=offline, fallback=fallback) == expected


def test_no_lan_hint_keeps_relay_then_huggingface(monkeypatch) -> None:
    # The owner offered no LAN listener: nothing to skip, today's order is kept.
    assert _names(monkeypatch, "10.0.0.7", lan=False) == ["owner_relay", "huggingface"]


def test_offline_never_adds_huggingface_even_off_lan(monkeypatch) -> None:
    names = _names(monkeypatch, "10.0.0.7", offline=True, fallback=False)
    assert "huggingface" not in names and names == ["owner_relay"]


def test_off_lan_without_relay_and_lan_hint_is_hf_only_none(monkeypatch) -> None:
    # Only a LAN offer, joiner elsewhere, no relay: nothing peer-side is usable.
    monkeypatch.setattr(pms, "choose_lan_host", lambda: "10.0.0.7")
    monkeypatch.setattr(pms, "offline_enabled", lambda *a, **k: False)
    monkeypatch.delenv(pms.ENVIRONMENT, raising=False)
    app = types.SimpleNamespace(authority=types.SimpleNamespace(peer=object()))
    assert pms.joiner_model_source(app, _announcement(relay=False), fallback=_Origin()) is None


@pytest.mark.parametrize("owner,joiner,same", [
    ("https://192.168.1.5:1", "192.168.1.200", True),
    ("https://192.168.1.5:1", "192.168.2.5", False),
    ("https://10.0.0.5:1", "10.0.1.5", False),
    ("https://10.0.0.5:1", None, False),
    ("https://10.0.0.5:1", "not-an-ip", False),
    ("https://[fd00::1]:1", "192.168.1.5", False),
])
def test_same_lan_24_helper(owner, joiner, same) -> None:
    assert pms.lan_reachable(owner, joiner) is same


def test_prefer_fallback_places_huggingface_before_the_peer_sources() -> None:
    relay = pms.peer_source("owner_relay", types.SimpleNamespace(read=lambda *a: b""), chunk_bytes=8)
    transport = pms.MultiSourceTransport(
        [relay], fallback=_Origin(), prefer_fallback=True)
    assert [row["name"] for row in transport.status()] == ["huggingface", "owner_relay"]
    default = pms.MultiSourceTransport(
        [relay], fallback=_Origin())
    assert [row["name"] for row in default.status()] == ["owner_relay", "huggingface"]
