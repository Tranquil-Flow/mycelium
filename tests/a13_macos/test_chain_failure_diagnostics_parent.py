# SPDX-License-Identifier: AGPL-3.0-only
"""Private failure diagnostics; no model work or physical claim."""
import json,stat
from types import SimpleNamespace as NS
from mycelium_a13_macos.chain_compile import start_serving


def test_serving_exception_retained_privately(tmp_path):
    def fail(**kwargs):raise RuntimeError('diagnostic-canary')
    app=NS(root=tmp_path,installation_role='owner',running=True,
        _chain_compile_state={'state':'compiled','execution_proposal':{'test':'proposal'}},
        _chain_workflow=NS(run=fail),_chain_approval=object(),
        _observation_context=NS(run_id='diagnostic-run'))
    start_serving(app,proposal={'test':'proposal'},acknowledged=True)
    app._chain_serving_thread.join(3);assert not app._chain_serving_thread.is_alive()
    path=tmp_path/'chain-runs/diagnostic-run/serve-error.private.json'
    assert path.is_file()
    assert stat.S_IMODE(path.stat().st_mode)==0o600
    doc=json.loads(path.read_text());assert doc['exception_type']=='RuntimeError'
    assert 'diagnostic-canary' in doc['traceback']
