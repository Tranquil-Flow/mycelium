# SPDX-License-Identifier: AGPL-3.0-only
"""CPU-only package selection: MLX is a shared namespace, not one wheel root."""
from __future__ import annotations

import base64
import hashlib
import importlib.machinery
import importlib.metadata
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import build_a13_macos as build


def _distribution(root, name, files):
    """Real distribution metadata/RECORD, without loading any native code."""
    info = root / (name.replace('-', '_') + '-0.31.1.dist-info')
    info.mkdir(parents=True)
    (info / 'METADATA').write_text(f'Metadata-Version: 2.1\nName: {name}\nVersion: 0.31.1\n')
    (info / 'WHEEL').write_text('Wheel-Version: 1.0\nTag: py3-none-macosx_14_0_arm64\n')
    rows = []
    for relative, data in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).decode().rstrip('=')
        rows.append(f'{relative},sha256={digest},{len(data)}\n')
    (info / 'RECORD').write_text(''.join(rows))
    return importlib.metadata.PathDistribution(info)


@pytest.fixture
def wheels(tmp_path, monkeypatch):
    selected = tmp_path / 'venv'
    other = tmp_path / 'homebrew'
    extension = 'mlx/core' + importlib.machinery.EXTENSION_SUFFIXES[0]
    distributions = {
        'mlx': _distribution(selected, 'mlx', {
            extension: b'pinned extension',
            'mlx/_reprlib_fix.py': b'raise AssertionError("must not execute")',
            'mlx/nn/__init__.py': b'',
        }),
        'mlx-metal': _distribution(selected, 'mlx-metal', {
            'mlx/lib/libmlx.dylib': b'macos14 library',
            'mlx/lib/mlx.metallib': b'macos14 shaders',
        }),
    }
    for relative in ('mlx/lib/libmlx.dylib', 'mlx/lib/mlx.metallib', extension):
        path = other / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'macos26 foreign bytes')
    monkeypatch.setattr(importlib.metadata, 'distribution', distributions.__getitem__)
    original = importlib.util.find_spec
    def find_spec(name, *args, **kwargs):
        if name == 'mlx':
            return SimpleNamespace(submodule_search_locations=[str(selected/'mlx'), str(other/'mlx')])
        return original(name, *args, **kwargs)
    monkeypatch.setattr(importlib.util, 'find_spec', find_spec)
    return selected, other, distributions


def _pairs(args, option):
    return [args[i+1] for i, value in enumerate(args[:-1]) if value == option]


def test_real_build_command_never_collects_the_merged_mlx_namespace(wheels, tmp_path, monkeypatch):
    import sys
    from mycelium_a13_macos import resources
    selected, other, _ = wheels
    root = tmp_path / 'repo'
    (root / '.build-native').mkdir(parents=True)
    (root / '.build-native/cloudflared-LICENSE').write_text('test license')
    monkeypatch.setattr(build, 'ROOT', root)
    monkeypatch.setattr(sys, 'argv', ['build', '--output-root', str(tmp_path/'output')])
    monkeypatch.setattr(sys, 'path', list(sys.path))
    monkeypatch.setattr(build.platform, 'system', lambda: 'Darwin')
    monkeypatch.setattr(build.platform, 'machine', lambda: 'arm64')
    monkeypatch.setattr(build, 'require_app_tree_root', lambda: None)
    monkeypatch.setattr(build, 'stage_compiler_sources', lambda destination: {})
    monkeypatch.setattr(build.shutil, 'which', lambda name: '/fake/cloudflared')
    monkeypatch.setattr(resources, 'build_policy_probe', lambda path: path)
    from mycelium_a13_macos import native_quit
    monkeypatch.setattr(native_quit, 'build_quit_bridge', lambda path: path)
    class Captured(Exception):
        pass
    def run(command, **kwargs):
        if command[1:3] == ['-m', 'PyInstaller']:
            raise Captured(command)
        if command[0] == 'cargo':
            return SimpleNamespace(returncode=0)
        if command[-1] == '--help':
            return SimpleNamespace(stdout='--bootstrap-fd --uds', stderr='')
        raise AssertionError(f'unexpected execution: {command}')
    monkeypatch.setattr(build.subprocess, 'run', run)
    with pytest.raises(Captured) as caught:
        build.main()
    args = caught.value.args[0]
    assert '--collect-all' not in args, 'collect-all merges foreign MLX namespace resources'
    assert f'{selected}/mlx/lib/mlx.metallib:mlx/lib' in _pairs(args, '--add-data')
    assert str(other) not in '\n'.join(args)
    assert any(value.endswith('libmycelium_quit.dylib:mycelium_a13_macos/native')
               for value in _pairs(args, '--add-binary'))


def test_collection_uses_record_owned_files_not_all_namespace_locations(wheels):
    selected, other, _ = wheels
    args = build.mlx_pyinstaller_arguments()
    assert '--collect-all' not in args
    assert '--collect-submodules' not in args  # no imports merely to enumerate files
    assert str(other) not in '\n'.join(args)
    assert f'{selected}/mlx/lib/mlx.metallib:mlx/lib' in _pairs(args, '--add-data')
    assert f'{selected}/mlx/lib/libmlx.dylib:mlx/lib' in _pairs(args, '--add-binary')
    assert set(_pairs(args, '--hidden-import')) == {'mlx', 'mlx.core', 'mlx._reprlib_fix', 'mlx.nn'}
    assert set(_pairs(args, '--copy-metadata')) == {'mlx', 'mlx-metal'}


@pytest.mark.parametrize('mutation', ['content', 'missing', 'unhashed', 'escape'])
def test_collection_refuses_invalid_record_owned_native_input(wheels, mutation):
    selected, _, distributions = wheels
    target = selected / 'mlx/lib/mlx.metallib'
    record = selected / 'mlx_metal-0.31.1.dist-info/RECORD'
    if mutation == 'content':
        target.write_bytes(b'foreign replacement')
    elif mutation == 'missing':
        target.unlink()
    elif mutation == 'unhashed':
        record.write_text('mlx/lib/mlx.metallib,,\n')
    else:
        target.unlink()
        target.symlink_to(selected.parent/'homebrew/mlx/lib/mlx.metallib')
    with pytest.raises(SystemExit, match='mlx_wheel_content_invalid'):
        build.mlx_pyinstaller_arguments()


def test_collection_refuses_a_different_import_root_first(wheels, monkeypatch):
    selected, other, _ = wheels
    monkeypatch.setattr(importlib.util, 'find_spec', lambda name: SimpleNamespace(
        submodule_search_locations=[str(other/'mlx'), str(selected/'mlx')]))
    with pytest.raises(SystemExit, match='mlx_import_root_mismatch'):
        build.mlx_pyinstaller_arguments()
