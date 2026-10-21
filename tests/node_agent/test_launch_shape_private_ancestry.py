# SPDX-License-Identifier: AGPL-3.0-only
"""Launch-shape walks must trust a root-owned sticky directory (``/tmp``).

``/private/tmp`` is mode 1777, and a tree checked out there is a legitimate
deployment location.  The strict walk (``_validate_walk_component``) already
trusts a root-owned sticky component; the ``strict_components=False`` walk used
by ``capture_executable_identity`` - which the node launch shape calls for the
service script and the sidecar - must decide that component the same way.
When it did not, every node launch from such a tree died at
``node_preflight_failed`` with ``ValueError: path component is invalid``.

The tests synthesise the ancestor's metadata (a real 1777 directory owned by
uid 0 cannot be created without root) while every descriptor opened is real.
"""

from __future__ import annotations

import sys
import importlib
import os
import stat
from pathlib import Path
from typing import Any

import pytest

# Root-owned sticky ancestors and uid/mode metadata are POSIX; the win32 walk is
# handle-bound and refuses reparse points instead (test_node_win32_seam.py).
pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX sticky-ancestor walk (uids, mode bits)"
)

process = importlib.import_module("mycelium_node.process")


def _synthetic_directory(real: os.stat_result, *, mode: int, uid: int) -> Any:
    return os.stat_result(
        (
            stat.S_IFDIR | mode,
            real.st_ino,
            real.st_dev,
            real.st_nlink,
            uid,
            real.st_gid,
            real.st_size,
            real.st_atime,
            real.st_mtime,
            real.st_ctime,
        )
    )


def _bind_component_metadata(
    monkeypatch: pytest.MonkeyPatch,
    component: Path,
    *,
    mode: int,
    uid: int,
) -> None:
    real_fstat = os.fstat
    bound = os.stat(component)

    def fstat(descriptor: int) -> os.stat_result:
        metadata = real_fstat(descriptor)
        if (metadata.st_dev, metadata.st_ino) == (bound.st_dev, bound.st_ino):
            return _synthetic_directory(metadata, mode=mode, uid=uid)
        return metadata

    monkeypatch.setattr(process.os, "fstat", fstat)


def _tree(tmp_path: Path) -> Path:
    root = tmp_path / "sticky"
    (root / "bin").mkdir(parents=True)
    executable = root / "bin" / "python"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="ascii")
    os.chmod(executable, 0o755)
    return executable


def test_lenient_walk_trusts_root_owned_sticky_ancestor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = _tree(tmp_path)
    _bind_component_metadata(monkeypatch, executable.parent.parent, mode=0o1777, uid=0)

    descriptor = process.private_directory_parent_fd(
        executable, strict_components=False
    )
    os.close(descriptor)


def test_both_walks_agree_on_root_owned_sticky_ancestor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = _tree(tmp_path)
    _bind_component_metadata(monkeypatch, executable.parent.parent, mode=0o1777, uid=0)

    descriptor = process.private_directory_parent_fd(
        executable, strict_components=True
    )
    os.close(descriptor)


def test_capture_executable_identity_accepts_root_owned_sticky_ancestor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The exact shipped call the node launch shape makes for the service script."""

    executable = _tree(tmp_path)
    _bind_component_metadata(monkeypatch, executable.parent.parent, mode=0o1777, uid=0)

    identity = process.capture_executable_identity(executable)
    assert identity.path == str(executable)


def test_user_owned_sticky_ancestor_is_still_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No new trust is granted: only a *root*-owned sticky directory is accepted."""

    executable = _tree(tmp_path)
    _bind_component_metadata(
        monkeypatch,
        executable.parent.parent,
        mode=0o1777,
        uid=os.getuid(),
    )

    with pytest.raises(ValueError, match="path component is invalid"):
        process.private_directory_parent_fd(executable, strict_components=False)
    with pytest.raises(ValueError, match="path component is invalid"):
        process.capture_executable_identity(executable)


def test_strict_walk_trusts_group_writable_root_owned_sticky_ancestor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The sticky trust is exactly the rule the strict walk already shipped.

    The strict walk masks ``0o022`` (group or other writable) and the lenient
    walk masks ``0o002`` (other writable only); that pre-existing difference is
    untouched here.  The strict walk already trusts a root-owned sticky
    component whatever the group bits say, and it still does.
    """

    executable = _tree(tmp_path)
    _bind_component_metadata(monkeypatch, executable.parent.parent, mode=0o1775, uid=0)

    descriptor = process.private_directory_parent_fd(
        executable, strict_components=True
    )
    os.close(descriptor)
