# SPDX-License-Identifier: AGPL-3.0-only
"""Owner-facing model import on the win32 backend (FakeWin32Api).

A LAN/USB snapshot is copied into the acquisition's staging directory as
owner-only protected-DACL directories, verified by pinned sha256 and size, and
the acquisition then prepares it without fetching a byte.
"""

from __future__ import annotations

import hashlib
import json
import ssl
import sys
from pathlib import Path

import pytest

from mycelium_a13_macos.acquisition import SelectedModelAcquisition
from mycelium_a13_macos.serving import A9_ROUTE_MODEL, CANDIDATE_MODEL
from mycelium_a13_windows import import_model
from mycelium_a13_windows.import_model import ModelImportError, import_snapshot, stage_directory

from tests.host_platform.fake_win32 import simulate_win32

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "a13_macos"))
from test_acquisition import _BombTransport, _consent, _injected_manifest, _repository_files  # noqa: E402


@pytest.fixture
def fake(monkeypatch):
    monkeypatch.setattr(ssl.SSLContext, "load_default_certs", lambda self, purpose=None: None)
    return simulate_win32(monkeypatch)


@pytest.fixture
def snapshot(tmp_path, monkeypatch):
    """A tiny valid repository pinned as the candidate model's import manifest."""

    files = _repository_files()
    source = tmp_path / "usb" / "snapshot"
    source.mkdir(parents=True)
    for name, payload in files.items():
        (source / name).write_bytes(payload)
    pinned = tuple(sorted((name, len(payload), hashlib.sha256(payload).hexdigest())
                          for name, payload in files.items()))
    monkeypatch.setitem(import_model.PINNED_SNAPSHOTS,
                        (CANDIDATE_MODEL.model_id, CANDIDATE_MODEL.resolved_commit), pinned)
    return source, files


def test_import_then_acquisition_prepares_without_network(fake, tmp_path, snapshot):
    source, files = snapshot
    root = tmp_path / "owner" / "model-source"
    result = import_snapshot(source, root, model=CANDIDATE_MODEL)
    assert result["files_imported"] == len(files)
    stage = stage_directory(root, CANDIDATE_MODEL)
    for directory in (root, stage.parent, stage):
        assert fake.security_of(directory).dacl_protected
    total = sum(map(len, files.values()))
    acquisition = SelectedModelAcquisition(
        root, _transport=_BombTransport(), _manifest=_injected_manifest(files)
    )
    try:
        prepared = acquisition._run_blocking(_consent(total, download=1, storage=total))
        assert prepared["state"] == "prepared" and prepared["error_code"] is None
    finally:
        acquisition.close()
    # Re-import after the acquisition moved the stage into the snapshot is refused.
    with pytest.raises(ModelImportError, match="snapshot_already_present"):
        import_snapshot(source, root, model=CANDIDATE_MODEL)


def test_plain_mkdir_tree_is_what_the_acquisition_refuses(fake, tmp_path, snapshot):
    """Why the helper exists: a robocopy-style tree (inherited ACL) is refused."""

    _source, files = snapshot
    root = tmp_path / "owner" / "model-source"
    SelectedModelAcquisition(root, _transport=_BombTransport()).close()  # the app's own root
    stage = stage_directory(root, CANDIDATE_MODEL)
    stage.mkdir(parents=True)  # what robocopy/mkdir produce: inherited, unprotected ACLs
    for name, payload in files.items():
        (stage / name).write_bytes(payload)
    total = sum(map(len, files.values()))
    acquisition = SelectedModelAcquisition(
        root, _transport=_BombTransport(), _manifest=_injected_manifest(files)
    )
    try:
        assert acquisition._run_blocking(_consent(total))["error_code"] == "artifact_storage_unsafe"
    finally:
        acquisition.close()


@pytest.mark.parametrize("mutation,code", [
    ("tamper", "import_digest_mismatch"),
    ("truncate", "import_size_mismatch"),
    ("missing", "import_source_incomplete"),
    ("extra", "import_source_unexpected_file"),
])
def test_any_mismatch_refuses_and_leaves_nothing_unverified(fake, tmp_path, snapshot, mutation, code):
    source, files = snapshot
    victim = source / "config.json"
    if mutation == "tamper":
        raw = bytearray(victim.read_bytes())
        raw[0] ^= 1
        victim.write_bytes(bytes(raw))
    elif mutation == "truncate":
        victim.write_bytes(victim.read_bytes()[:-1])
    elif mutation == "missing":
        victim.unlink()
    else:
        (source / "evil.py").write_bytes(b"print(1)")
    root = tmp_path / "owner" / "model-source"
    with pytest.raises(ModelImportError, match=code):
        import_snapshot(source, root, model=CANDIDATE_MODEL)
    stage = stage_directory(root, CANDIDATE_MODEL)
    if stage.exists():
        names = {path.name for path in stage.iterdir()}
        assert "config.json" not in names and not any(name.endswith(".partial") for name in names)


def test_import_refuses_while_the_acquisition_holds_its_lock(fake, tmp_path, snapshot):
    source, _files = snapshot
    root = tmp_path / "owner" / "model-source"
    acquisition = SelectedModelAcquisition(root, _transport=_BombTransport())
    try:
        with pytest.raises(ModelImportError, match="acquisition_in_progress"):
            import_snapshot(source, root, model=CANDIDATE_MODEL)
    finally:
        acquisition.close()


def test_cli_refuses_unpinned_model_and_reports_json(fake, tmp_path, capsys):
    assert import_model.main([str(tmp_path), "--state-root", str(tmp_path / "r"),
                              "--model", CANDIDATE_MODEL.resolved_commit]) == 2
    assert json.loads(capsys.readouterr().err)["refused"] == "model_import_manifest_unavailable"
    assert import_model.main([str(tmp_path), "--state-root", str(tmp_path / "r"), "--model", "0" * 40]) == 2
    assert json.loads(capsys.readouterr().err)["refused"] == "model_not_registered"
    assert import_model.pinned_files(A9_ROUTE_MODEL)[6][0] == "model.safetensors"
