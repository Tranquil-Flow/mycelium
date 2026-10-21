# SPDX-License-Identifier: AGPL-3.0-only
"""Portable test setup for owner-only directories.

``path.mkdir(mode=0o700)`` is an owner-only directory on POSIX but not on
Windows, where the new directory inherits its parent's DACL.  Fixtures that need
a private directory create it through the host-platform seam instead: mode 0700
on POSIX, a protected owner-only DACL on Windows.
"""

from __future__ import annotations

from pathlib import Path

import mycelium_host_platform


def make_private_directory(path: Path) -> Path:
    with mycelium_host_platform.private_directory(path, create=True):
        pass
    return path


def copy_private_file(source: Path, destination: Path) -> Path:
    """Copy bytes into a new owner-only file (``shutil.copy2`` keeps 0600 on
    POSIX, but on Windows the copy inherits the directory's DACL and owner)."""

    data = Path(source).read_bytes()
    with mycelium_host_platform.private_directory(destination.parent, create=True) as lease:
        lease.create_file_exclusive(destination.name, data)
    return destination
