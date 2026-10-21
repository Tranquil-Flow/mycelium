# SPDX-License-Identifier: AGPL-3.0-only
"""Backend selection for the host platform seam.

The seam reads ``sys.platform`` at call time, dispatches win32 calls to the
win32 backend over the injected API, and refuses (never degrades) when the
host reports win32 but no Win32 API can be bound.
"""

from __future__ import annotations

import ctypes
import importlib
import os
from pathlib import Path
import subprocess
import sys

import pytest

import mycelium_host_platform
from mycelium_host_platform import HostPlatformError, _posix, _win32
from mycelium_host_platform._win32_api import load_real_api

from tests.host_platform.fake_win32 import FakeWin32Api, simulate_win32


def _private_root(tmp_path):
    root = tmp_path / "state"
    root.mkdir(mode=0o700)
    return root


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX backend dispatch: this host is win32")
def test_posix_host_dispatches_to_the_posix_backend(tmp_path) -> None:
    assert mycelium_host_platform.is_win32() is False
    with mycelium_host_platform.private_directory(_private_root(tmp_path), create=False) as lease:
        assert isinstance(lease, _posix.PosixPrivateDirectory)
        assert lease.exists


def test_simulated_win32_dispatches_to_the_win32_backend(tmp_path, monkeypatch) -> None:
    fake = simulate_win32(monkeypatch)
    root = tmp_path / "state"
    with mycelium_host_platform.private_directory(root, create=True) as lease:
        assert isinstance(lease, _win32.Win32PrivateDirectory)
        assert lease.exists
    assert "current_user_sid" in fake.call_names()


def test_selection_is_read_at_call_time(tmp_path, monkeypatch) -> None:
    root = _private_root(tmp_path)
    monkeypatch.setattr(mycelium_host_platform.sys, "platform", "win32")
    assert mycelium_host_platform.is_win32() is True
    monkeypatch.setattr(mycelium_host_platform.sys, "platform", "darwin")
    assert mycelium_host_platform.is_win32() is False
    if os.name == "nt":
        return  # the POSIX backend itself cannot run on a real Windows host
    with mycelium_host_platform.private_directory(root, create=False) as lease:
        assert isinstance(lease, _posix.PosixPrivateDirectory)


def test_win32_without_a_bindable_api_fails_closed(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(mycelium_host_platform.sys, "platform", "win32")
    monkeypatch.setattr(mycelium_host_platform, "_API_OVERRIDE", None)
    monkeypatch.setattr(mycelium_host_platform, "_REAL_API", None)
    # On a real Windows host the binding would succeed: take ctypes.WinDLL away.
    monkeypatch.delattr(ctypes, "WinDLL", raising=False)
    with pytest.raises(HostPlatformError) as caught:
        mycelium_host_platform.private_directory(tmp_path / "state", create=True)
    assert caught.value.code == "host_platform_unavailable"
    # Nothing was created through a POSIX fallback.
    assert not (tmp_path / "state").exists()
    for call in (
        lambda: mycelium_host_platform.open_beneath(tmp_path, ("x",)),
        lambda: mycelium_host_platform.executable_fingerprint(sys.executable),
    ):
        with pytest.raises(HostPlatformError) as caught:
            call()
        assert caught.value.code == "host_platform_unavailable"


@pytest.mark.skipif(sys.platform == "win32", reason="this host is Windows: the real binding binds")
def test_the_real_binding_refuses_off_a_windows_host() -> None:
    with pytest.raises(HostPlatformError) as caught:
        load_real_api()
    assert caught.value.code == "host_platform_unavailable"


@pytest.mark.skipif(sys.platform != "win32", reason="real Win32 binding")
def test_the_real_binding_binds_on_a_windows_host() -> None:
    api = load_real_api()
    assert api.current_user_sid().startswith("S-1-5-")


def test_use_win32_api_restores_the_previous_api() -> None:
    first, second = FakeWin32Api(), FakeWin32Api()
    before = mycelium_host_platform._API_OVERRIDE
    with mycelium_host_platform.use_win32_api(first):
        assert mycelium_host_platform.win32_api() is first
        with mycelium_host_platform.use_win32_api(second):
            assert mycelium_host_platform.win32_api() is second
        assert mycelium_host_platform.win32_api() is first
    assert mycelium_host_platform._API_OVERRIDE is before


def test_unknown_error_codes_are_a_programming_error() -> None:
    with pytest.raises(ValueError):
        HostPlatformError("host_everything_is_fine")
    assert HostPlatformError("host_io_failed", "flush").code == "host_io_failed"


def test_every_backend_module_imports_without_windows_libraries() -> None:
    """The ctypes bindings are lazy: importing the seam never touches WinDLL."""

    for name in (
        "mycelium_host_platform",
        "mycelium_host_platform._common",
        "mycelium_host_platform._posix",
        "mycelium_host_platform._win32",
        "mycelium_host_platform._win32_api",
    ):
        importlib.import_module(name)
    probe = (
        "import sys; sys.modules['msvcrt'] = None; sys.modules['winreg'] = None; "
        "import mycelium_host_platform, mycelium_host_platform._win32, "
        "mycelium_host_platform._win32_api; print('ok')"
    )
    completed = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "ok"
