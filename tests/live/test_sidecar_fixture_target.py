# SPDX-License-Identifier: AGPL-3.0-only
"""The cross-language fixture must spawn Cargo's actual output path."""
import runpy
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize('override', [None, 'local-target', '/owned/native-target'])
def test_cross_language_fixture_uses_the_effective_cargo_target(monkeypatch, override):
    if override is None:
        monkeypatch.delenv('CARGO_TARGET_DIR', raising=False)
    else:
        monkeypatch.setenv('CARGO_TARGET_DIR', override)
    module = runpy.run_path(str(ROOT / 'test_iroh_sidecar_cross_language.py'))
    crate = ROOT / 'native' / 'iroh_transport'
    target = crate / 'target' if override is None else Path(override)
    if not target.is_absolute():
        target = crate / target
    assert module['BINARY'] == target / 'debug' / 'mycelium-iroh-sidecar'


def test_cross_language_fixture_keeps_a_private_built_binary(tmp_path, monkeypatch):
    from types import SimpleNamespace
    source = tmp_path / 'shared-target' / 'debug' / 'mycelium-iroh-sidecar'
    monkeypatch.setenv('CARGO_TARGET_DIR', str(source.parent.parent))
    module = runpy.run_path(str(ROOT / 'test_iroh_sidecar_cross_language.py'))
    calls = []
    def fake_build(command, **options):
        calls.append((command, options))
        source.parent.mkdir(parents=True)
        source.write_bytes(b'inert compiled-artifact canary')
        source.chmod(0o700)
    monkeypatch.setattr(module['subprocess'], 'run', fake_build)
    private = tmp_path / 'worker-artifact'
    def mktemp(name):
        private.mkdir()
        return private
    fixture = module['_build_sidecar'].__wrapped__
    # Use the existing supported signature for RED; no production compiler runs.
    import inspect
    fixture(**({'tmp_path_factory': SimpleNamespace(mktemp=mktemp)}
               if 'tmp_path_factory' in inspect.signature(fixture).parameters else {}))
    chosen = fixture.__globals__['BINARY']
    source.unlink()  # A competing target writer cannot remove our specimen.
    assert chosen.is_relative_to(private)
    assert chosen.read_bytes() == b'inert compiled-artifact canary'
    assert calls[0][0] == ['cargo', 'build', '--locked', '--bin', 'mycelium-iroh-sidecar']
    assert calls[0][1]['env']['CARGO_TARGET_DIR'] == str(source.parent.parent)
