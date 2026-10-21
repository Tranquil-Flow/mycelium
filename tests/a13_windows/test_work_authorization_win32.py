# SPDX-License-Identifier: AGPL-3.0-only
"""The Windows managed host reads staged work documents by handle.

``WindowsTargetApplication`` reuses the macOS work-authorization workflow, whose
document reader opened files with ``O_NOFOLLOW``; that flag does not exist on
Windows, so every Windows consent failed with ``work_graph_invalid``.  On win32
the reader opens through ``mycelium_host_platform.open_beneath`` (every reparse
point refused).  These cases run on the fake Win32 API; the real-host path is
exercised by test_route_model_and_node_runtime on Windows.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mycelium_a13_macos.work_authorization import _staged_document
from tests.host_platform.fake_win32 import simulate_win32


def _staged(tmp_path: Path, raw: bytes) -> Path:
    root = tmp_path / "staged"
    (root / "control").mkdir(parents=True)
    (root / "control" / "execution-graph.json").write_bytes(raw)
    return root


def test_a_staged_document_is_read_through_the_handle_seam(tmp_path, monkeypatch) -> None:
    fake = simulate_win32(monkeypatch)
    root = _staged(tmp_path, json.dumps({"stages": []}).encode())
    assert _staged_document(root, "control/execution-graph.json") == {"stages": []}
    opened = [call[1] for call in fake.calls if call[0] == "open"]
    assert any(path.endswith("execution-graph.json") for path in opened)


def test_a_symlinked_staged_document_is_refused_on_win32(tmp_path, monkeypatch) -> None:
    simulate_win32(monkeypatch)
    root = _staged(tmp_path, b"{}")
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps({"stages": []}))
    (root / "control" / "linked.json").symlink_to(outside)
    with pytest.raises(ValueError):
        _staged_document(root, "control/linked.json")


def test_duplicate_keys_are_still_refused_on_win32(tmp_path, monkeypatch) -> None:
    simulate_win32(monkeypatch)
    root = _staged(tmp_path, b'{"stages": [], "stages": []}')
    with pytest.raises(ValueError, match="work_document_invalid"):
        _staged_document(root, "control/execution-graph.json")
