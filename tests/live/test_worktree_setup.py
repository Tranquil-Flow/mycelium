# SPDX-License-Identifier: AGPL-3.0-only
"""Worktree setup copies only declared offline binaries into the target tree."""
import hashlib
import os
from pathlib import Path
import subprocess

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / 'tools/worktree_test_setup.sh'


def sources(tmp_path):
    source, target = tmp_path / 'integration checkout', tmp_path / 'worktree'
    target.mkdir()
    for name in ('.build-native/release/mycelium-iroh-sidecar',
                 'native/iroh_transport/target/debug/mycelium-iroh-sidecar',
                 'native/mobile_core/target/debug/examples/qwen2_stage_exec'):
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(('fixture:' + name).encode())
        path.chmod(0o755)
    return source, target


def test_setup_copies_identical_regular_files_and_is_idempotent(tmp_path):
    source, target = sources(tmp_path)
    for _ in range(2):
        result = subprocess.run(['/bin/bash', str(SCRIPT), str(source), str(target)],
                                capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
    for path in source.rglob('*'):
        if path.is_file():
            copied = target / path.relative_to(source)
            assert copied.is_file() and not copied.is_symlink()
            assert copied.read_bytes() == path.read_bytes()
            assert os.access(copied, os.X_OK)
            assert hashlib.sha256(path.read_bytes()).hexdigest() in result.stdout


def test_setup_missing_source_does_not_partially_install(tmp_path):
    source, target = sources(tmp_path)
    (source / 'native/mobile_core/target/debug/examples/qwen2_stage_exec').unlink()
    result = subprocess.run(['/bin/bash', str(SCRIPT), str(source), str(target)],
                            capture_output=True, text=True)
    assert result.returncode != 0
    assert 'qwen2_stage_exec' in result.stderr
    assert not list(target.iterdir())


def test_setup_refuses_to_overwrite_different_binary(tmp_path):
    source, target = sources(tmp_path)
    existing = target / '.build-native/release/mycelium-iroh-sidecar'
    existing.parent.mkdir(parents=True)
    existing.write_bytes(b'owned existing build')
    result = subprocess.run(['/bin/bash', str(SCRIPT), str(source), str(target)],
                            capture_output=True, text=True)
    assert result.returncode != 0
    assert existing.read_bytes() == b'owned existing build'


@pytest.mark.parametrize('name', ['.build-native', 'native'])
def test_setup_refuses_target_symlink_escape(tmp_path, name):
    source, target = sources(tmp_path)
    outside = tmp_path / 'outside'
    outside.mkdir()
    (target / name).symlink_to(outside, target_is_directory=True)
    result = subprocess.run(['/bin/bash', str(SCRIPT), str(source), str(target)],
                            capture_output=True, text=True)
    assert result.returncode != 0
    assert not list(outside.iterdir())
