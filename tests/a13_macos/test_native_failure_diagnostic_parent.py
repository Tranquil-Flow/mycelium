# SPDX-License-Identifier: AGPL-3.0-only
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


def test_diagnostic_records_the_bounded_native_error_code(tmp_path):
    """The native told us WHY; the recorder must keep it.

    `_validate_response` admits an error response only when its error dict is exactly
    {'code': <token matching the closed operation grammar>}. That token is the native's
    own name for the cause, and p74r1's `infer_start` failure was recorded without it --
    the third occurrence of a diagnostic that says THAT something failed and WHERE, never
    WHY. Reaching this recorder without a code must therefore be impossible for a failed
    command.
    """
    worker=NS(capacity_root=tmp_path,run_id='fixture-run',node_id='fixture-node')
    ManagedWorker._record_native_failure(worker,NS(stderr=b''),command_id='fixture-command',
        command='infer_start',response={'ok':False,'error':{'code':'prefill_completion_timeout'}})
    data=json.loads((tmp_path/'native-failure.private.json').read_text())
    assert data['error_code']=='prefill_completion_timeout'


def test_diagnostic_refuses_an_out_of_grammar_error_code(tmp_path):
    """Bounded means bounded: free text must never reach the diagnostic."""
    worker=NS(capacity_root=tmp_path,run_id='fixture-run',node_id='fixture-node')
    ManagedWorker._record_native_failure(worker,NS(stderr=b''),command_id='fixture-command',
        command='infer_start',
        response={'ok':False,'error':{'code':'SECRET /private/path with spaces'}})
    p=tmp_path/'native-failure.private.json';text=p.read_text()
    assert 'SECRET' not in text and '/private/path' not in text
    assert json.loads(text).get('error_code') is None


def test_diagnostic_survives_a_malformed_response(tmp_path):
    """A diagnostic must never raise; the caller swallows, but the record should still land."""
    worker=NS(capacity_root=tmp_path,run_id='fixture-run',node_id='fixture-node')
    for bad in ({}, {'error':None}, {'error':'node_command_failed'}, {'error':{'code':123}},
                {'error':{'code':'ok','extra':'x'}}, 'not-a-mapping', None):
        ManagedWorker._record_native_failure(worker,NS(stderr=b''),command_id='fixture-command',
            command='infer_start',response=bad)
        data=json.loads((tmp_path/'native-failure.private.json').read_text())
        assert data['protocol']=='mycelium.a13.native_failure_diagnostic.v1'
        assert data.get('error_code') is None
        assert data['command']=='infer_start'
