# SPDX-License-Identifier: AGPL-3.0-only
"""Packaged AppKit quit pump; no PyObjC, downloads or runtime compilation."""
from pathlib import Path
import ctypes
import subprocess
import sys


LIBRARY_NAME = 'libmycelium_quit.dylib'


def build_quit_bridge(output: Path) -> Path:
    """Explicit offline packaging step, using only the installed Apple SDK."""
    source = Path(__file__).parent / 'native' / 'quit_bridge.m'
    output.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(['/usr/bin/xcrun', 'clang', '-dynamiclib', '-fobjc-arc',
                    '-O2', '-mmacosx-version-min=15.0', '-framework', 'AppKit',
                    str(source), '-o', str(output)],
                   check=True, capture_output=True, timeout=60)
    return output


class NativeQuit:
    """Hold AppKit ownership until the launcher's normal cleanup has returned."""
    def __init__(self, request_stop, *, headless=False):
        root = Path(getattr(sys, '_MEIPASS', Path(__file__).parent.parent))
        library = root / 'mycelium_a13_macos' / 'native' / LIBRARY_NAME
        self._native = ctypes.CDLL(str(library))
        self._native.mycelium_quit_install.argtypes = [ctypes.c_int]
        self._native.mycelium_quit_install.restype = ctypes.c_int
        self._native.mycelium_quit_poll.argtypes = []
        self._native.mycelium_quit_poll.restype = ctypes.c_int
        self._native.mycelium_quit_uninstall.argtypes = []
        self._native.mycelium_quit_uninstall.restype = None
        self._request_stop = request_stop
        if self._native.mycelium_quit_install(int(headless)) != 1:
            raise RuntimeError('native_quit_install_failed')

    def poll(self):
        if self._native.mycelium_quit_poll():
            self._request_stop()

    def close(self):
        self._native.mycelium_quit_uninstall()
