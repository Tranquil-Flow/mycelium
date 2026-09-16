"""Privacy-reduced native-error diagnostics; no model execution."""
from types import SimpleNamespace as NS
import json,stat
from mycelium_a13_macos.worker import ManagedWorker


def test_diagnostic_keeps_frames_not_raw_error_or_source_lines(tmp_path):
    stderr=b'  File "/private/location/physical_inference_node.py", line 3901, in execute\n    SECRET_CANARY_SOURCE\n  File "/private/location/iroh.py", line 999, in send\nIrohTransportError: SECRET_CANARY_MESSAGE\n'
    worker=NS(capacity_root=tmp_path,run_id='fixture-run',node_id='fixture-node')
    ManagedWorker._record_native_failure(worker,NS(stderr=stderr),command_id='fixture-command',command='infer_start',response={'error':{'code':'node_command_failed'}})
    p=tmp_path/'native-failure.private.json';data=json.loads(p.read_text())
    assert data['frames']==[{'file':'physical_inference_node.py','line':3901,'function':'execute'},{'file':'iroh.py','line':999,'function':'send'}]
    assert data['exception_types']==['IrohTransportError']
    assert 'SECRET_CANARY' not in p.read_text() and '/private/location' not in p.read_text()
    assert stat.S_IMODE(p.stat().st_mode)==0o600
