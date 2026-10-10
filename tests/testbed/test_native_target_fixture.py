"""Source-aware test fixture target selection, no Cargo execution in controls."""
import importlib.util
import os
from pathlib import Path
import subprocess
import pytest

ROOT = Path(__file__).resolve().parents[2]

def fixture_module():
    spec=importlib.util.spec_from_file_location('iroh_fixture_target_probe',ROOT/'tests/e2e_request_iroh/conftest.py')
    assert spec is not None and spec.loader is not None
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

@pytest.mark.parametrize('override',[False,True])
def test_build_and_return_share_selected_target(tmp_path,monkeypatch,override):
    monkeypatch.delenv('CARGO_TARGET_DIR',raising=False)
    if override: monkeypatch.setenv('CARGO_TARGET_DIR',str(tmp_path/'warm-target'))
    module=fixture_module()
    monkeypatch.setattr(module, 'IROH_CRATE', tmp_path/'crate')
    # Default is derived at import time; keep its current value as the oracle.
    expected=(tmp_path/'warm-target/debug/mycelium-iroh-sidecar') if override else module.IROH_BINARY
    calls=[]
    def build(cmd,**kw):
        calls.append((cmd,kw))
    monkeypatch.setattr(module.subprocess,'run',build)
    monkeypatch.setattr(Path,'is_file',lambda p:p==expected)
    result=module.native_iroh_sidecar_binary.__wrapped__()
    assert result==expected
    cmd,kw=calls[0]
    assert '--locked' in cmd and kw['check'] is True
    assert kw['env']['CARGO_NET_OFFLINE']=='true'
    if override: assert kw['env']['CARGO_TARGET_DIR']==str(tmp_path/'warm-target')


def test_build_failure_never_accepts_existing_binary(monkeypatch):
    module=fixture_module()
    monkeypatch.setattr(Path,'is_file',lambda p:True)
    def fail(*a,**kw):raise subprocess.CalledProcessError(17,a[0])
    monkeypatch.setattr(module.subprocess,'run',fail)
    with pytest.raises(subprocess.CalledProcessError):module.native_iroh_sidecar_binary.__wrapped__()
