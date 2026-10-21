# SPDX-License-Identifier: AGPL-3.0-only
"""Win32Error keeps its GetLastError value on a real Windows CPython.

On Windows, ``OSError.__init__`` resets the C-level ``winerror`` slot, so a
subclass that assigns ``self.winerror`` before calling ``super().__init__``
ends up with ``winerror is None``. Every "missing -> create" branch in
``mycelium_host_platform._win32`` then misreads ERROR_FILE_NOT_FOUND /
ERROR_PATH_NOT_FOUND as ``host_path_invalid`` (seen on the real A9 host,
2026-10-12). POSIX CPython has no such slot, so this test loads the module
against a stand-in ``OSError`` that resets ``winerror`` the way Windows does.
"""
from __future__ import annotations

import builtins
import sys
import types
from pathlib import Path

import pytest

_MODULE = Path(__file__).resolve().parents[2] / "mycelium_host_platform" / "_win32_api.py"


class _WindowsLikeOSError(OSError):
    """OSError whose __init__ clears ``winerror`` like CPython's oserror_init on Windows."""

    def __init__(self, *args: object) -> None:
        super().__init__(*args)
        self.winerror = None


def _load_with_windows_like_oserror() -> types.ModuleType:
    # Only this module's namespace sees the stand-in; importlib and the rest of
    # the process keep the real OSError.
    module = types.ModuleType("mycelium_host_platform._win32_api_under_windows_oserror")
    module.__file__ = str(_MODULE)
    module.__package__ = "mycelium_host_platform"  # relative imports
    module.__dict__["__builtins__"] = {**vars(builtins), "OSError": _WindowsLikeOSError}
    sys.modules[module.__name__] = module  # dataclasses resolves the defining module
    try:
        exec(compile(_MODULE.read_text(encoding="utf-8"), str(_MODULE), "exec"), module.__dict__)
    finally:
        sys.modules.pop(module.__name__, None)
    return module


def test_win32_error_keeps_winerror_when_oserror_init_resets_it() -> None:
    module = _load_with_windows_like_oserror()
    assert issubclass(module.Win32Error, _WindowsLikeOSError)
    error = module.Win32Error(2, "CreateFileW")
    assert error.winerror == 2
    assert error.operation == "CreateFileW"
    assert "Win32 error 2" in str(error)


@pytest.mark.skipif(sys.platform != "win32", reason="real Windows OSError slot")
def test_win32_error_keeps_winerror_on_real_windows() -> None:
    from mycelium_host_platform._win32_api import Win32Error

    assert Win32Error(3, "CreateDirectoryW").winerror == 3
