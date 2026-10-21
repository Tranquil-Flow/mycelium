# SPDX-License-Identifier: AGPL-3.0-only
"""Selected-model acquisition on the win32 backend (FakeWin32Api): Windows as the owner.

Same state machine and loopback fixtures as tests/a13_macos/test_acquisition.py.
Owner-only protected DACL directories replace euid + mode bits, status.json is
written with an explicit owner-only DACL and read back through the owner-only
check, and acquisition.lock is LockFileEx.
"""

from __future__ import annotations

import ssl
import sys
from pathlib import Path

import pytest

from mycelium_a13_macos.acquisition import AcquisitionError, SelectedModelAcquisition
from mycelium_a13_macos.serving import CANDIDATE_MODEL

from tests.host_platform.fake_win32 import OTHER_USER_SID, owner_only, simulate_win32

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "a13_macos"))
from test_acquisition import (  # noqa: E402
    _BombTransport,
    _consent,
    _file_requests,
    _FixtureServer,
    _injected_manifest,
    _repository_files,
)


@pytest.fixture
def fake(monkeypatch):
    # simulate_win32 flips sys.platform process-wide; keep ssl off the Windows cert store.
    monkeypatch.setattr(ssl.SSLContext, "load_default_certs", lambda self, purpose=None: None)
    return simulate_win32(monkeypatch)


def _snapshots(root: Path) -> Path:
    return root / ("models--" + CANDIDATE_MODEL.model_id.replace("/", "--")) / "snapshots"


def test_download_prepares_with_owner_only_dacls_and_lockfileex(fake, tmp_path):
    files = _repository_files()
    total = sum(map(len, files.values()))
    with _FixtureServer(tmp_path, files) as server:
        acquisition = SelectedModelAcquisition(tmp_path / "state", _transport=server.transport())
        try:
            result = acquisition._run_blocking(_consent(total))
            assert result["state"] == "prepared" and result["error_code"] is None
            prepared = acquisition.prepared_source()
            assert prepared.model == CANDIDATE_MODEL
            assert len(_file_requests(server)) == len(files)
            assert any(call[0] == "lock_file" for call in fake.calls)
            for directory in (tmp_path / "state", _snapshots(tmp_path / "state"), prepared.source_root):
                assert fake.security_of(directory).dacl_protected
            assert fake.security_of(tmp_path / "state" / "status.json").dacl_protected
        finally:
            acquisition.close()


def test_lock_is_exclusive_and_released_on_close(fake, tmp_path):
    first = SelectedModelAcquisition(tmp_path / "state", _transport=_BombTransport())
    with pytest.raises(AcquisitionError, match="acquisition_in_progress"):
        SelectedModelAcquisition(tmp_path / "state", _transport=_BombTransport())
    first.close()
    second = SelectedModelAcquisition(tmp_path / "state", _transport=_BombTransport())
    assert second.status()["state"] == "closed"
    second.close()


def test_foreign_owned_state_root_and_status_are_refused(fake, tmp_path):
    root = tmp_path / "state"
    SelectedModelAcquisition(root, _transport=_BombTransport()).close()
    fake.set_security(root / "status.json", owner_only(OTHER_USER_SID))
    with pytest.raises(AcquisitionError, match="acquisition_state_corrupt"):
        SelectedModelAcquisition(root, _transport=_BombTransport())
    fake.set_security(root, owner_only(OTHER_USER_SID))
    with pytest.raises(AcquisitionError, match="acquisition_state_root_unsafe"):
        SelectedModelAcquisition(root, _transport=_BombTransport())


def test_foreign_owned_snapshot_directory_is_unsafe_without_network(fake, tmp_path):
    files = _repository_files()
    total = sum(map(len, files.values()))
    root = tmp_path / "state"
    acquisition = SelectedModelAcquisition(
        root, _transport=_BombTransport(), _manifest=_injected_manifest(files)
    )
    try:
        assert acquisition._run_blocking(_consent(total, download=1))["error_code"] == "download_limit_exceeded"
        fake.set_security(_snapshots(root), owner_only(OTHER_USER_SID))
        assert acquisition._run_blocking(_consent(total))["error_code"] == "artifact_storage_unsafe"
    finally:
        acquisition.close()


def test_disconnect_resume_and_recovery(fake, tmp_path):
    files = _repository_files()
    total = sum(map(len, files.values()))
    with _FixtureServer(tmp_path, files) as server:
        server.fixture.disconnect_file = "model.safetensors"
        acquisition = SelectedModelAcquisition(tmp_path / "state", _transport=server.transport())
        try:
            failed = acquisition._run_blocking(_consent(total))
            assert failed["error_code"] == "source_disconnected" and 0 < failed["downloaded_bytes"] < total
            assert acquisition._run_blocking(_consent(total))["state"] == "prepared"
            ranges = [v for p, v in _file_requests(server) if p.endswith("/model.safetensors") and v]
            assert ranges and ranges[-1].startswith("bytes=")
        finally:
            acquisition.close()
    root = tmp_path / "state2"
    first = SelectedModelAcquisition(root, _transport=_BombTransport())
    first._set_status(state="downloading", downloaded_bytes=7, total_bytes=11, error_code=None)
    first._release_lock_for_test()
    second = SelectedModelAcquisition(root, _transport=_BombTransport())
    assert second.status()["error_code"] == "interrupted_transfer"
    second.close()


def test_symlink_partial_is_rejected(fake, tmp_path):
    files = _repository_files()
    total = sum(map(len, files.values()))
    root = tmp_path / "state"
    acquisition = SelectedModelAcquisition(
        root, _transport=_BombTransport(), _manifest=_injected_manifest(files)
    )
    try:
        stage = _snapshots(root) / ("." + CANDIDATE_MODEL.resolved_commit + ".acquiring")
        acquisition._paths()
        import mycelium_host_platform

        with mycelium_host_platform.private_directory(stage, create=True):
            pass
        target = tmp_path / "outside"
        target.write_bytes(b"outside")
        (stage / "config.json.partial").symlink_to(target)
        assert acquisition._run_blocking(_consent(total))["error_code"] == "artifact_storage_unsafe"
        assert target.read_bytes() == b"outside"
    finally:
        acquisition.close()
