# SPDX-License-Identifier: AGPL-3.0-only
"""The offline model-source switch: no Hugging Face contact in lan/local mode.

Pure tests: every network opener is replaced by one that fails the test.
"""
from __future__ import annotations

import argparse
import importlib
import sys
import types

import pytest

from mycelium_platform_neutral import offline as offline_module
from mycelium_platform_neutral.offline import (
    OFFLINE_ENV,
    OfflineModelSourceError,
    OfflineSettingError,
    add_offline_argument,
    apply_offline,
    offline_enabled,
    offline_message,
    require_online_model_source,
)

acquisition = importlib.import_module("mycelium_a13_macos.acquisition")
pms = importlib.import_module("mycelium_a13_macos.peer_model_source")
weight_provisioning = importlib.import_module("weight_provisioning")
model_manifest = importlib.import_module("model_manifest")


# -- the switch ----------------------------------------------------------------

def test_default_internet_behaviour_is_online() -> None:
    assert offline_enabled({}) is False
    assert offline_enabled({"MYCELIUM_NETWORK_MODE": "internet"}) is False


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
def test_env_switch_turns_offline_on(value: str) -> None:
    assert offline_enabled({OFFLINE_ENV: value}) is True


@pytest.mark.parametrize("value", ["0", "false", "no", "off"])
def test_env_switch_turns_offline_off(value: str) -> None:
    assert offline_enabled({OFFLINE_ENV: value}) is False


@pytest.mark.parametrize("value", ["", "2", "maybe", "offline"])
def test_env_switch_rejects_unknown_values(value: str) -> None:
    with pytest.raises(OfflineSettingError):
        offline_enabled({OFFLINE_ENV: value})


@pytest.mark.parametrize("mode", ["lan", "local"])
def test_lan_and_local_modes_imply_offline(mode: str) -> None:
    assert offline_enabled({"MYCELIUM_NETWORK_MODE": mode}) is True
    assert offline_enabled({}, network_mode=mode) is True


def test_explicit_env_override_beats_the_implied_default() -> None:
    env = {"MYCELIUM_NETWORK_MODE": "lan", OFFLINE_ENV: "0"}
    assert offline_enabled(env) is False


def test_cli_value_beats_env_and_mode() -> None:
    env = {"MYCELIUM_NETWORK_MODE": "lan", OFFLINE_ENV: "0"}
    assert offline_enabled(env, cli_value=True) is True
    assert offline_enabled({OFFLINE_ENV: "1"}, cli_value=False) is False
    assert offline_enabled({}, cli_value=None, network_mode="lan") is True


def test_unknown_network_mode_is_never_coerced() -> None:
    with pytest.raises(ValueError):
        offline_enabled({"MYCELIUM_NETWORK_MODE": "wan"})


def test_argument_has_on_off_and_unset_forms() -> None:
    parser = argparse.ArgumentParser()
    add_offline_argument(parser)
    assert parser.parse_args([]).offline is None
    assert parser.parse_args(["--offline"]).offline is True
    assert parser.parse_args(["--no-offline"]).offline is False


def test_apply_offline_exports_the_resolved_switch_for_deep_code() -> None:
    env: dict[str, str] = {}
    assert apply_offline(True, "internet", env) is True
    assert env[OFFLINE_ENV] == "1"
    env = {}
    assert apply_offline(None, "lan", env) is True
    assert env[OFFLINE_ENV] == "1"
    env = {}
    assert apply_offline(None, "internet", env) is False
    assert OFFLINE_ENV not in env  # default behaviour leaves the environment alone
    env = {OFFLINE_ENV: "0"}
    assert apply_offline(None, "lan", env) is False
    assert env[OFFLINE_ENV] == "0"
    env = {OFFLINE_ENV: "1"}
    assert apply_offline(False, "lan", env) is False
    assert env[OFFLINE_ENV] == "0"


def test_message_names_the_local_sources_and_the_override() -> None:
    text = offline_message("the model files")
    assert "Hugging Face" in text and "not contacted" in text
    assert "owner" in text and "LAN" in text
    assert "cache" in text
    assert f"{OFFLINE_ENV}=0" in text and "--no-offline" in text


def test_require_online_model_source_fails_closed_only_when_offline() -> None:
    require_online_model_source("x", {})
    with pytest.raises(OfflineModelSourceError) as caught:
        require_online_model_source("the model files", {OFFLINE_ENV: "1"})
    assert caught.value.code == "offline_model_source_required"
    assert "the model files" in str(caught.value)


# -- every Hugging Face site ---------------------------------------------------

class _FailingOpener:
    def open(self, *_args, **_kwargs):  # pragma: no cover - must not run
        raise AssertionError("a network request was attempted")


def _model():
    return acquisition.CANDIDATE_MODEL


def test_a13_http_transport_refuses_huggingface_when_offline(monkeypatch) -> None:
    monkeypatch.setenv(OFFLINE_ENV, "1")
    transport = acquisition._HTTPRepositoryTransport()
    transport._opener = _FailingOpener()
    record = {"path": "config.json", "size_bytes": 1}
    with pytest.raises(OfflineModelSourceError) as resolved:
        transport.resolve(_model())
    with pytest.raises(OfflineModelSourceError):
        transport.open_file(_model(), record, offset=0)
    with pytest.raises(OfflineModelSourceError):
        transport.read_range(_model().model_id, _model().resolved_commit, "config.json", 0, 4)
    assert isinstance(resolved.value, acquisition.AcquisitionError)
    assert resolved.value.code == "offline_model_source_required"


def test_a13_http_transport_refuses_huggingface_in_lan_mode(monkeypatch) -> None:
    monkeypatch.delenv(OFFLINE_ENV, raising=False)
    monkeypatch.setenv("MYCELIUM_NETWORK_MODE", "lan")
    transport = acquisition._HTTPRepositoryTransport()
    transport._opener = _FailingOpener()
    with pytest.raises(OfflineModelSourceError):
        transport.resolve(_model())


def test_a13_http_transport_default_still_reaches_the_opener(monkeypatch) -> None:
    monkeypatch.delenv(OFFLINE_ENV, raising=False)
    monkeypatch.delenv("MYCELIUM_NETWORK_MODE", raising=False)
    transport = acquisition._HTTPRepositoryTransport()
    transport._opener = _FailingOpener()
    with pytest.raises(AssertionError, match="network request"):
        transport.open_file(_model(), {"path": "config.json", "size_bytes": 1}, offset=0)


def test_a13_http_transport_allows_a_private_mirror_when_offline(monkeypatch) -> None:
    monkeypatch.setenv(OFFLINE_ENV, "1")
    transport = acquisition._HTTPRepositoryTransport(
        api_origin="http://127.0.0.1:9", download_origin="http://127.0.0.1:9",
        allow_test_http=True,
    )
    transport._opener = _FailingOpener()
    with pytest.raises(AssertionError, match="network request"):
        transport.open_file(_model(), {"path": "config.json", "size_bytes": 1}, offset=0)


def test_multi_source_has_no_huggingface_fallback_when_offline(monkeypatch) -> None:
    monkeypatch.setenv(OFFLINE_ENV, "1")
    created: list[object] = []
    monkeypatch.setattr(
        acquisition, "_HTTPRepositoryTransport", lambda *a, **k: created.append(1) or object()
    )
    peer = pms.peer_source("owner_lan", types.SimpleNamespace(read=lambda *a: b""), chunk_bytes=1)
    transport = pms.MultiSourceTransport([peer], fallback=None, offline=True)
    assert [row["name"] for row in transport.status()] == ["owner_lan"]
    with pytest.raises(OfflineModelSourceError):
        transport.resolve(_model())
    assert created == []


def test_joiner_model_source_builds_no_huggingface_source_when_offline(monkeypatch) -> None:
    monkeypatch.setenv(OFFLINE_ENV, "1")
    monkeypatch.delenv(pms.ENVIRONMENT, raising=False)
    hint = {"lan_origin": None, "relay": True, "tls_certificate_pem": None}
    monkeypatch.setattr(pms, "validate_hint", lambda value: hint)
    monkeypatch.setattr(pms, "PeerRangeClient", lambda *a, **k: types.SimpleNamespace(read=None))
    app = types.SimpleNamespace(authority=types.SimpleNamespace(peer=object()))
    transport = pms.joiner_model_source(app, {"model_source": {}})
    assert [row["name"] for row in transport.status()] == ["owner_relay"]


def test_joiner_model_source_keeps_huggingface_last_by_default(monkeypatch) -> None:
    monkeypatch.delenv(OFFLINE_ENV, raising=False)
    monkeypatch.delenv("MYCELIUM_NETWORK_MODE", raising=False)
    monkeypatch.delenv(pms.ENVIRONMENT, raising=False)
    hint = {"lan_origin": None, "relay": True, "tls_certificate_pem": None}
    monkeypatch.setattr(pms, "validate_hint", lambda value: hint)
    monkeypatch.setattr(pms, "PeerRangeClient", lambda *a, **k: types.SimpleNamespace(read=None))
    app = types.SimpleNamespace(authority=types.SimpleNamespace(peer=object()))
    transport = pms.joiner_model_source(app, {"model_source": {}})
    assert [row["name"] for row in transport.status()] == ["owner_relay", "huggingface"]


def _fake_hub(monkeypatch, *, cached: str | None, error: Exception | None = None) -> list[dict]:
    calls: list[dict] = []

    def hf_hub_download(**kwargs):
        calls.append(kwargs)
        if error is not None:
            raise error
        return cached

    module = types.ModuleType("huggingface_hub")
    module.hf_hub_download = hf_hub_download
    module.try_to_load_from_cache = lambda **kwargs: cached
    monkeypatch.setitem(sys.modules, "huggingface_hub", module)
    return calls


def test_weight_provisioning_downloads_by_default(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv(OFFLINE_ENV, raising=False)
    monkeypatch.delenv("MYCELIUM_NETWORK_MODE", raising=False)
    calls = _fake_hub(monkeypatch, cached=str(tmp_path / "f"))
    weight_provisioning.fetch_huggingface_file("m/x", "a" * 40, "f", tmp_path)
    assert calls[0]["local_files_only"] is False


def test_weight_provisioning_uses_only_the_local_cache_when_offline(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv(OFFLINE_ENV, "1")
    calls = _fake_hub(monkeypatch, cached=str(tmp_path / "f"))
    weight_provisioning.fetch_huggingface_file("m/x", "a" * 40, "f", tmp_path)
    assert calls[0]["local_files_only"] is True


def test_weight_provisioning_missing_model_fails_closed_with_the_message(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("MYCELIUM_NETWORK_MODE", "lan")
    monkeypatch.delenv(OFFLINE_ENV, raising=False)
    _fake_hub(monkeypatch, cached=None, error=FileNotFoundError("not in cache"))
    with pytest.raises(OfflineModelSourceError) as caught:
        weight_provisioning.fetch_huggingface_file("m/x", "a" * 40, "f/g.safetensors", tmp_path)
    assert "f/g.safetensors" in str(caught.value)
    assert "LAN" in str(caught.value)


def test_manifest_resolution_refuses_the_hub_when_offline(monkeypatch) -> None:
    monkeypatch.setenv(OFFLINE_ENV, "1")

    def boom(*_args, **_kwargs):  # pragma: no cover - must not run
        raise AssertionError("hub imported")

    monkeypatch.setitem(sys.modules, "huggingface_hub", None)  # import would fail
    with pytest.raises(OfflineModelSourceError):
        model_manifest.resolve_huggingface_manifest("m/x")


def test_module_exposes_a_closed_public_surface() -> None:
    assert {
        "OFFLINE_ENV", "OfflineModelSourceError", "OfflineSettingError",
        "add_offline_argument", "apply_offline", "offline_enabled",
        "offline_message", "require_online_model_source",
    } <= set(dir(offline_module))
