# SPDX-License-Identifier: AGPL-3.0-only
"""A failed native module init must not be retried inside the same app process."""
import builtins
from types import SimpleNamespace

import pytest

from mycelium_a13_macos import capability


@pytest.fixture(autouse=True)
def reset_import_failure(monkeypatch):
    monkeypatch.setattr(capability, '_MLX_IMPORT_ERROR', None, raising=False)


@pytest.mark.parametrize('error', [ImportError, RuntimeError, OSError, ValueError])
def test_join_probes_do_not_retry_a_partially_initialized_native_extension(monkeypatch, error):
    original = builtins.__import__
    attempts = []
    def import_module(name, *args, **kwargs):
        if name == 'mlx.core':
            attempts.append(name)
            if len(attempts) > 1:
                raise AssertionError('native initializer would register DeviceType twice')
            raise error('initial native setup failure with private details')
        return original(name, *args, **kwargs)
    monkeypatch.setattr(builtins, '__import__', import_module)
    first = capability.runtime_probe_report()
    second = capability.runtime_probe_report()
    assert first == second
    assert first['available'] is False
    assert first['error_type'] == error.__name__
    assert 'private details' not in str(first)
    assert attempts == ['mlx.core']


def test_concurrent_probes_share_the_failed_initialization(monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    original = builtins.__import__
    start = threading.Barrier(3)
    entered = threading.Event()
    release = threading.Event()
    attempts = []
    def import_module(name, *args, **kwargs):
        if name == 'mlx.core':
            attempts.append(name)
            assert len(attempts) == 1, 'duplicate native initialization'
            entered.set()
            assert release.wait(5)
            raise ImportError('partially initialized native extension')
        return original(name, *args, **kwargs)
    monkeypatch.setattr(builtins, '__import__', import_module)
    def probe():
        start.wait(5)
        return capability.runtime_probe_report()
    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = [pool.submit(probe) for _ in range(2)]
        try:
            start.wait(5)
            assert entered.wait(5)
        finally:
            release.set()
        reports = [future.result(timeout=5) for future in pending]
    assert reports[0] == reports[1]
    assert reports[0]['error_type'] == 'ImportError'
    assert attempts == ['mlx.core']


def test_arithmetic_failure_does_not_poison_a_successful_import(monkeypatch):
    original = builtins.__import__
    evaluations = []
    class Array:
        dtype = 'float32'
        def __add__(self, other): return self
        def item(self): return 2.0
    def evaluate(value):
        evaluations.append(value)
        if len(evaluations) == 1:
            raise RuntimeError('transient arithmetic failure')
    core = SimpleNamespace(array=lambda *a, **kw: Array(), float32='float32',
                           eval=evaluate, default_device=lambda: 'Device(gpu, 0)')
    def import_module(name, *args, **kwargs):
        if name == 'mlx.core': return SimpleNamespace(core=core)
        return original(name, *args, **kwargs)
    monkeypatch.setattr(builtins, '__import__', import_module)
    assert capability.runtime_probe_report()['available'] is False
    assert capability.runtime_probe_report()['available'] is True
    assert len(evaluations) == 2
