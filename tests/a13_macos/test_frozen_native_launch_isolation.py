# SPDX-License-Identifier: AGPL-3.0-only
"""Characterize the packaged worker path without executing an app or MLX."""
import sys
from types import SimpleNamespace

import pytest

from mycelium_a13_macos import worker
from mycelium_node import process


def test_frozen_worker_ignores_incompatible_homebrew_and_preserves_import_path(tmp_path, monkeypatch):
    exe = tmp_path / 'Demo.app/Contents/MacOS/Demo'
    exe.parent.mkdir(parents=True)
    exe.write_text('not an executable test payload')
    exe.chmod(0o700)
    legacy = tmp_path / 'Python.framework/Versions/3.15/bin/python3.15'
    legacy.parent.mkdir(parents=True)
    legacy.write_text('not an executable test payload')
    legacy.chmod(0o700)
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    monkeypatch.setattr(sys, 'executable', str(exe))
    monkeypatch.setattr(process, '_MACOS_LEGACY_PYTHON', str(legacy))
    monkeypatch.delenv('MYCELIUM_PHYSICAL_SERVICE_PYTHON', raising=False)
    identity = process.capture_executable_identity(exe)
    monkeypatch.setattr(process, '_inventory_process', lambda pid: SimpleNamespace(executable=identity))
    def forbidden(*args, **kwargs):
        raise AssertionError('packaged worker must not select a Homebrew interpreter')
    monkeypatch.setattr(process, 'physical_service_interpreter_identity', forbidden)
    launched = []
    def capture(argv, **kwargs):
        launched.append((argv, kwargs))
        raise OSError('sandboxed before subprocess creation')
    monkeypatch.setattr(process.subprocess, 'Popen', capture)
    subject = SimpleNamespace(limits=worker.WorkerLimits(1024, 1024, 60, 20),
        node_id='node-1', run_id='run-1', deployment_id='deployment-1',
        artifact_root=tmp_path/'artifacts', socket_root=tmp_path/'sockets',
        sidecar_binary=tmp_path/'sidecar', capacity_root=None, endpoint_secret_file=None)
    before = list(sys.path)
    argv = worker.ManagedWorker._native_argv(subject)
    assert argv[:2] == (str(exe), '--managed-node')
    with pytest.raises(process.NodeProcessError, match='node_process_start_failed'):
        worker._ConcurrentNativeSession(argv=argv, node_id=subject.node_id,
            run_id=subject.run_id, deployment_id=subject.deployment_id, timeout_seconds=10)
    assert len(launched) == 1
    command, options = launched[0]
    assert command[:2] == (str(exe), '--node-process-launcher')
    assert command[-len(argv):] == argv
    assert options['env'] is None
    assert str(legacy) not in command
    assert sys.path == before
