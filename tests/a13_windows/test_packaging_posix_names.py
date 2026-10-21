# SPDX-License-Identifier: AGPL-3.0-only
"""Staged packaging inputs are keyed by POSIX relative names on every host.

The frozen-input record and its digest are built from these keys; on a Windows
build host ``str(relative_to(...))`` produced ``mycelium_a13_macos\\__main__.py``
and a different digest than macOS/Linux for the same tree.
"""

from __future__ import annotations

from pathlib import Path

from mycelium_a13_packaging import build as _build

ROOT = Path(__file__).resolve().parents[2]


def test_the_runtime_closure_and_data_tree_use_posix_names(tmp_path) -> None:
    closure = _build.runtime_source_closure(ROOT, ("mycelium_a13_windows",), label="test")
    assert closure and all("\\" not in name for name in closure)
    assert "mycelium_a13_windows/__init__.py" in closure
    staged = _build.stage_files(ROOT, tmp_path / "source", closure)
    assert set(staged["sha256"]) == set(closure)
    digests: dict[str, str] = {}
    _build.stage_data_tree(ROOT, "mycelium_a13_macos/web", tmp_path / "source", digests)
    assert digests and all(name.startswith("mycelium_a13_macos/web/") for name in digests)
