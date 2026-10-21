# SPDX-License-Identifier: AGPL-3.0-only
"""Owner app shell, local TLS material and owner journal lock on the win32 backend.

FakeWin32Api stands in for the Windows kernel: owner-only protected DACLs replace
POSIX 0600/0700 + getuid, and LockFileEx replaces flock. Same refusal codes as
the macOS paths in tests/a13_macos.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from mycelium_a13_macos import app as app_module
from mycelium_a13_macos.app import DemoApp, DemoError, preflight
from mycelium_a13_macos.tls import LocalTLSError, ensure_local_tls_material

from tests.host_platform.fake_win32 import OTHER_USER_SID, owner_only, simulate_win32

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "a13_macos"))
from test_app import Authority  # noqa: E402


@pytest.fixture
def fake(monkeypatch):
    return simulate_win32(monkeypatch)


# ------------------------------------------------------------------ preflight


def test_preflight_admits_windows_x86_64_at_the_admission_floor():
    report = preflight(system="Windows", machine="AMD64", os_version="10.0.19045")
    assert report["platform"] == "windows" and report["architecture"] == "x86_64"
    assert preflight(system="Windows", machine="AMD64", os_version="10.0.26100")["os_version"] == "10.0.26100"
    for machine, version in (("ARM64", "10.0.26100"), ("AMD64", "10.0.19044"), ("AMD64", "garbage")):
        with pytest.raises(DemoError, match="unsupported_platform"):
            preflight(system="Windows", machine=machine, os_version=version)


# ------------------------------------------------------------------ tls.py


def test_tls_material_is_created_owner_only_and_reloads(fake, tmp_path):
    material = ensure_local_tls_material(tmp_path / "tls")
    for path in (material.ca_certificate, material.server_certificate, material.server_private_key):
        descriptor = fake.security_of(path)
        assert descriptor.dacl_protected and descriptor.owner_sid == fake.user_sid
    again = ensure_local_tls_material(tmp_path / "tls")
    assert again.server_private_key.read_bytes() == material.server_private_key.read_bytes()


def test_tls_foreign_owned_key_and_partial_material_are_refused(fake, tmp_path):
    material = ensure_local_tls_material(tmp_path / "tls")
    fake.set_security(material.server_private_key, owner_only(OTHER_USER_SID))
    with pytest.raises(LocalTLSError, match="local_tls_material_invalid"):
        ensure_local_tls_material(tmp_path / "tls")
    material.server_private_key.unlink()
    with pytest.raises(LocalTLSError, match="local_tls_material_incomplete"):
        ensure_local_tls_material(tmp_path / "tls")


def test_tls_directory_owned_by_another_user_is_refused(fake, tmp_path):
    ensure_local_tls_material(tmp_path / "tls")
    fake.set_security(tmp_path / "tls", owner_only(OTHER_USER_SID))
    with pytest.raises(LocalTLSError, match="local_tls_path_invalid"):
        ensure_local_tls_material(tmp_path / "tls")


# ------------------------------------------------------------------ app shell


def test_private_dir_write_and_resource_policy_use_owner_only_dacls(fake, tmp_path):
    root = app_module.private_dir(tmp_path / "private")
    assert fake.security_of(root).dacl_protected
    assert app_module.read_resource_policy(root) is None
    app_module.private_write(root / "resource-policy.json", b"{}")
    assert fake.security_of(root / "resource-policy.json").dacl_protected
    with pytest.raises(DemoError, match="resource_policy_invalid"):
        app_module.read_resource_policy(root)
    fake.set_security(root / "resource-policy.json", owner_only(OTHER_USER_SID))
    with pytest.raises(DemoError, match="unsafe_state_permissions"):
        app_module.read_resource_policy(root)
    fake.set_security(root, owner_only(OTHER_USER_SID))
    with pytest.raises(DemoError, match="unsafe_state_permissions"):
        app_module.private_dir(root)


def test_reopen_native_reads_owner_only_control_file(fake, tmp_path):
    root = app_module.private_dir(tmp_path / "private")
    with pytest.raises(DemoError, match="native_control_unavailable"):
        app_module.reopen_native(root)
    app_module.private_write(root / "native-control.json", json.dumps({"bogus": 1}).encode())
    fake.set_security(root / "native-control.json", owner_only(OTHER_USER_SID))
    with pytest.raises(DemoError, match="unsafe_state_permissions"):
        app_module.reopen_native(root)


def test_application_lock_is_exclusive_and_released_on_close(fake, tmp_path, monkeypatch):
    import ssl

    # simulate_win32 flips sys.platform process-wide; keep ssl off the Windows cert store.
    monkeypatch.setattr(ssl.SSLContext, "load_default_certs", lambda self, purpose=None: None)
    root = tmp_path / "private"
    first = DemoApp(root, authority_factory=Authority, check_platform=False).start()
    try:
        with pytest.raises(DemoError, match="already_running"):
            DemoApp(root, authority_factory=Authority, check_platform=False).start()
        assert any(call[0] == "lock_file" for call in fake.calls)
        assert fake.security_of(first.launch_file).dacl_protected
    finally:
        first.close()
    restarted = DemoApp(root, authority_factory=Authority, check_platform=False).start()
    restarted.close()


# ------------------------------------------------------------------ control_session


def test_owner_journal_lock_is_exclusive_on_win32(fake, tmp_path, monkeypatch):
    import ssl

    monkeypatch.setattr(ssl.SSLContext, "load_default_certs", lambda self, purpose=None: None)
    from test_control_pump import _world
    from test_control_session import owner
    from physical_inference_qualification import ControllerError

    with _world(tmp_path / "swarm") as world:
        sender = owner(tmp_path / "sender", world)
        try:
            with pytest.raises(ControllerError, match="owner_already_running"):
                owner(tmp_path / "sender", world)
            assert sender._fd == -1 and sender._win32_lock is not None
            assert fake.security_of(tmp_path / "sender").dacl_protected
        finally:
            sender.close()
        assert sender._win32_lock is None
        owner(tmp_path / "sender", world).close()


# ------------------------------------------------------------------ public relay (cloudflared)


def test_relay_isolates_userprofile_and_closes_without_o_nofollow(fake, tmp_path, monkeypatch):
    import io
    import os

    from mycelium_a13_macos import public_relay as relay

    launched = {}

    class Process:
        def __init__(self, command, **kwargs):
            launched.update(command=command, **kwargs)
            self.stdout = io.BytesIO(b"INF https://one.trycloudflare.com\n")
            self.code = None

        def poll(self):
            return self.code

        def terminate(self):
            self.code = 0

        def wait(self, timeout=None):
            return self.code

    class Proxy:
        origin = "http://127.0.0.1:1"

        def start(self):
            return self

        def close(self):
            pass

    monkeypatch.setenv("USERPROFILE", r"C:\Users\owner")
    monkeypatch.setattr(relay, "installed_relay_binary", lambda: "cloudflared.exe")
    monkeypatch.setattr(relay, "SeedRelayProxy", Proxy)
    monkeypatch.setattr(relay.subprocess, "Popen", Process)
    monkeypatch.delattr(os, "O_NOFOLLOW", raising=False)  # absent on Windows
    tunnel = relay.TemporaryHttpsRelay(tmp_path / "relay")
    tunnel.start(acknowledged=True, max_seconds=30)
    assert tunnel.public_origin == "https://one.trycloudflare.com"
    home = str(tmp_path / "relay" / "isolated-home")
    assert launched["env"]["HOME"] == home and launched["env"]["USERPROFILE"] == home
    tunnel.close()
    assert (tmp_path / "relay" / "relay-tail.log").exists()
    assert fake.security_of(tmp_path / "relay" / "isolated-home").dacl_protected


# ------------------------------------------------------------------ resource observation (gap 2)


def _windows_reading(monkeypatch, document):
    import types

    from mycelium_a13_windows.managed_host import resources as windows_resources

    calls = []

    def run(command, **_kwargs):
        calls.append(command)
        return types.SimpleNamespace(stdout=document, returncode=0)

    monkeypatch.setattr(windows_resources.subprocess, "run", run)
    return calls


def test_owner_observation_uses_the_windows_observer(fake, tmp_path, monkeypatch):
    from mycelium_a13_macos.resources import observe_policy

    calls = _windows_reading(
        monkeypatch, json.dumps({"power": "external", "network": "unmetered", "kelvin": [318.15]})
    )
    observation = observe_policy()
    assert observation == {"power": "external", "network": "unmetered", "thermal": "nominal"}
    assert calls and calls[0][0] == "powershell"


def test_owner_app_resource_check_reads_windows_and_fails_closed(fake, tmp_path, monkeypatch):
    import ssl

    monkeypatch.setattr(ssl.SSLContext, "load_default_certs", lambda self, purpose=None: None)
    app = DemoApp(tmp_path / "private", authority_factory=Authority, check_platform=False).start()
    try:
        _windows_reading(monkeypatch, json.dumps({"power": "external", "network": "unmetered", "kelvin": 318.15}))
        app._resource_check()
        assert app.resource_observation == {"power": "external", "network": "unmetered", "thermal": "nominal"}
        _windows_reading(monkeypatch, "not json")
        app._resource_check()
        assert app.resource_observation == {"power": "unknown", "network": "unknown", "thermal": "unknown"}
    finally:
        app.close()
