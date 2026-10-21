# SPDX-License-Identifier: AGPL-3.0-only
"""Source-aware fixture controls; Cargo mocked, not native build proof."""
import importlib.util
from pathlib import Path
import subprocess
import pytest

ROOT = Path(__file__).resolve().parents[2]


def load_fixture():
    spec = importlib.util.spec_from_file_location(
        'native_fixture_target_probe', ROOT/'tests/e2e_request_iroh/conftest.py')
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize('selection', ['default', 'absolute', 'relative'])
def test_explicit_target_is_built_and_returned(tmp_path, monkeypatch, selection):
    crate = ROOT/'native/iroh_transport'
    if selection == 'default':
        monkeypatch.delenv('CARGO_TARGET_DIR', raising=False)
        expected_target = crate/'target'
    elif selection == 'absolute':
        expected_target = tmp_path/'selected-native-cache'
        monkeypatch.setenv('CARGO_TARGET_DIR', str(expected_target))
    else:
        monkeypatch.setenv('CARGO_TARGET_DIR', 'relative-cache')
        expected_target = crate/'relative-cache'
    monkeypatch.setenv('CARGO_INCREMENTAL', '0')
    module = load_fixture()
    expected = expected_target/'debug/mycelium-iroh-sidecar'
    monkeypatch.setattr(Path, 'is_file', lambda self: self == expected)
    calls = []
    def run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0)
    monkeypatch.setattr(subprocess, 'run', run)
    assert module.native_iroh_sidecar_binary.__wrapped__() == expected
    assert len(calls) == 1
    command, kw = calls[0]
    assert command == ['cargo', 'build', '--locked', '--bin', 'mycelium-iroh-sidecar']
    assert kw['cwd'] == crate and kw['check'] is True
    assert kw['env']['CARGO_NET_OFFLINE'] == 'true'
    assert kw['env']['CARGO_TARGET_DIR'] == str(expected_target)
    assert kw['env']['CARGO_INCREMENTAL'] == '0'


def test_build_error_cannot_return_existing_binary(monkeypatch):
    module = load_fixture()
    monkeypatch.setattr(Path, 'is_file', lambda self: True)
    def fail(*args, **kwargs):
        raise subprocess.CalledProcessError(101, args[0])
    monkeypatch.setattr(subprocess, 'run', fail)
    with pytest.raises(subprocess.CalledProcessError):
        module.native_iroh_sidecar_binary.__wrapped__()


def test_build_target_matches_returned_binary_if_environment_changes(tmp_path, monkeypatch):
    chosen = tmp_path/'selected'
    monkeypatch.setenv('CARGO_TARGET_DIR', str(chosen))
    module = load_fixture()
    module.IROH_BINARY.parent.mkdir(parents=True)
    module.IROH_BINARY.write_bytes(b'old build; not proof of a new build')
    monkeypatch.setenv('CARGO_TARGET_DIR', str(tmp_path/'changed-later'))
    built = []
    def fake_build(command, **kwargs):
        built.append(Path(kwargs['env']['CARGO_TARGET_DIR'])/'debug/mycelium-iroh-sidecar')
        return subprocess.CompletedProcess(command, 0)
    monkeypatch.setattr(subprocess, 'run', fake_build)
    returned = module.native_iroh_sidecar_binary.__wrapped__()
    assert built == [returned]
