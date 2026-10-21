# SPDX-License-Identifier: AGPL-3.0-only
"""ManagedWorker on win32: the process group is the node's job object."""

from __future__ import annotations

from pathlib import Path

import pytest

from mycelium_a13_macos import worker as worker_module
from mycelium_a13_macos.worker import ManagedWorker, WorkerError

from tests.host_platform.fake_win32 import simulate_win32


class _Session:
    def __init__(self, resident=0, owned=True, fail=False):
        self.resident, self.owned, self.fail = resident, owned, fail

    def win32_group_resident_bytes(self):
        if self.fail:
            raise RuntimeError("unobservable")
        return self.resident

    def win32_group_owned(self):
        if self.fail:
            raise RuntimeError("unobservable")
        return self.owned


def _worker(tmp_path):
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    limits = worker_module.WorkerLimits(max_wall_seconds=600, max_commands=10, max_disk_bytes=1 << 30,
                                        max_rss_bytes=1 << 30)
    return ManagedWorker(node_id="node-win", run_id="run-win", deployment_id="dep-win",
                         artifact_root=artifacts, socket_root=tmp_path / "s",
                         sidecar_binary=Path("/unused/mycelium-iroh-sidecar.exe"), resource_policy={},
                         limits=limits, membership_current=lambda: True, authorize_load=None)


def test_job_memory_replaces_ps_and_unobservable_refuses(monkeypatch, tmp_path):
    worker = _worker(tmp_path)
    simulate_win32(monkeypatch)
    monkeypatch.setattr(worker_module.subprocess, "run", lambda *a, **k: pytest.fail("ps must not run"))
    worker._session = _Session(resident=7_000_000)
    assert worker._group_rss() == 7_000_000
    worker._session = _Session(fail=True)
    with pytest.raises(WorkerError, match="worker_memory_observation_failed"):
        worker._group_rss()
    worker._session = None
    with pytest.raises(WorkerError, match="worker_memory_observation_failed"):
        worker._group_rss()


def test_group_presence_is_job_ownership_and_never_killpg(monkeypatch, tmp_path):
    worker = _worker(tmp_path)
    simulate_win32(monkeypatch)
    monkeypatch.setattr(worker_module.os, "killpg", lambda *a: pytest.fail("killpg must not run"), raising=False)
    worker._group_owned, worker.pid = True, 4242
    worker._session = _Session(owned=True)
    assert worker.process_group_present() is True
    worker._session = _Session(owned=False)
    assert worker.process_group_present() is False
    worker._session = _Session(fail=True)
    assert worker.process_group_present() is True  # unobservable counts as present


def test_capacity_root_must_be_an_owner_only_directory_on_win32(monkeypatch, tmp_path):
    simulate_win32(monkeypatch)
    import mycelium_host_platform

    good = tmp_path / "capacity"
    with mycelium_host_platform.private_directory(good, create=True):
        pass
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    limits = worker_module.WorkerLimits(max_wall_seconds=600, max_commands=10, max_disk_bytes=1 << 30,
                                        max_rss_bytes=1 << 30)
    common = dict(node_id="node-win", run_id="run-win", deployment_id="dep-win", artifact_root=artifacts,
                  socket_root=tmp_path / "s", sidecar_binary=Path("/unused/sidecar.exe"), resource_policy={},
                  limits=limits, membership_current=lambda: True, authorize_load=None)
    ManagedWorker(**common, capacity_root=good.resolve())
    with pytest.raises(WorkerError, match="worker_capacity_root_invalid"):
        ManagedWorker(**common, capacity_root=(tmp_path / "missing").resolve())
