# SPDX-License-Identifier: AGPL-3.0-only
"""Phone cancellation bytes from the real Python evidence signer (no runtime/model).

Regenerate explicitly with this script; pytest checks fixture determinism.
This is a wire/signature oracle, not a physical-node teardown qualification.
"""
import hashlib
import json
from pathlib import Path
import sys

import os
ROOT = Path(os.environ.get('MYCELIUM_PYTHON_ORACLE_ROOT', Path(__file__).resolve().parents[3])).resolve()
sys.path.insert(0, str(ROOT))
from scripts.generate_phone_node_control_golden import _command, _ok, _signed, _signer
from mycelium_qualification.evidence import canonical_json_bytes

OUTPUT = Path(__file__).with_name('cancel_control_golden.json')


def readiness_golden():
    golden = json.loads((ROOT / 'contracts/phone-node-control-golden/index.json').read_text())
    stage = golden['stage']
    # The node's own stage-load count: the golden controller names exactly it.
    stage.update(runtime_build_digest='sha256:'+'b'*64, golden_output_digest='sha256:'+'a'*64, load_generation=1)
    for row in golden['steps']:
        if row['command']['command'] == 'bind_request_control':
            row['command']['payload']['execution_budget_ms'] = 60_000
    step = golden['steps'][1]
    response = json.loads(step['response_canonical'])
    obs = response['result']['observation']
    obs['details'].update(runtime_backend=stage['backend'], runtime_build_digest=stage['runtime_build_digest'],
                          golden_output_digest=stage['golden_output_digest'])
    obs['details']['load_identity_provenance'] = {
        'applied_fields':['assignment_id','stage_id','phone_stage_pack_digest','runtime_backend','runtime_build_digest','golden_output_digest','load_generation'],
        'controller_claim_fields':['manifest_digest'],
        'limitation':'native_full_model_manifest_digest_not_independently_observed',
        'load_generation_basis':'native_stage_load_sequence_within_process_incarnation'}
    assert obs['details']['load_generation'] == stage['load_generation'] == step['command']['payload']['load_generation']
    response['result'] = _signed(_signer(), obs['event'], obs['state'], obs['peer_generation'], obs['monotonic_ns'], obs['details'])
    step['response_canonical'] = canonical_json_bytes(response).decode()
    return golden


def generate():
    golden = readiness_golden()
    golden['steps'] = golden['steps'][:3]
    golden['steps'][1]['command']['payload']['request_control_graph'] = {
        'deployment_id':'dep-1', 'deployment_epoch':1, 'topology_generation':1}

    control = {
        'deployment_id': 'dep-1', 'deployment_epoch': 1, 'qualification_digest': 'sha256:'+'b'*64,
        'command_id': 'cmd', 'publisher_generation': 1, 'absolute_deadline_ms': 10,
        'request_attempt': 1, 'path_id': 'p', 'path_attempt': 0, 'path_digest': 'sha256:'+'a'*64,
        'topology_generation': 1, 'cancellation_generation': 0,
    }
    # Exercise the actual current Python validator, without constructing a
    # runtime or loading weights. Its graph identity is authoritative.
    from types import SimpleNamespace
    from physical_inference_node import PhysicalNodeService, NodeCommandError
    service = object.__new__(PhysicalNodeService)
    service.graph = SimpleNamespace(deployment_id='dep-1', deployment_epoch=1, topology_version=1)
    assert service._validated_request_control(control, code='invalid', initial=True) == control
    for field, value in [
        ('deployment_epoch', 0), ('deployment_epoch', 2), ('request_attempt', 0),
        ('topology_generation', 2), ('qualification_digest', 'sha256:q'),
        ('path_digest', 'sha256:'+'G'*64), ('command_id', 'é'*129),
        ('publisher_generation', 0), ('absolute_deadline_ms', 0),
        ('path_attempt', -1), ('cancellation_generation', 1),
    ]:
        try:
            service._validated_request_control({**control, field:value}, code='invalid', initial=True)
        except NodeCommandError:
            pass
        else:
            raise AssertionError(f'Python accepted invalid {field}')
    signer = _signer()
    def add(command, payload, event, details):
        response = _ok('c-'+command, _signed(signer, event, 'RUNNING', 5, 7, details))
        golden['steps'].append({'command': _command(command, payload), 'response_canonical': canonical_json_bytes(response).decode()})
    add('bind_request_control', {'request_id':'r', 'control':control, 'execution_budget_ms':60_000}, 'request_control_bound', {
        'request_id':'r', 'control_digest':'sha256:'+hashlib.sha256(canonical_json_bytes(control)).hexdigest(),
    })
    subject = {'request_id':'r', **control, 'cancellation_generation':1}
    payload = {**subject, 'deadline_budget_ms':4000}
    details = {'request_id':'r', 'cancelled':True, 'status':'CANCELLED', 'cleanup_pending':False,
               'request_cleanup': {**subject, 'runtime_clean':True, 'transport_clean':True,
                   'cancellation_worker_complete':True, 'complete':True,
                   'residue':{'live_paths':[], 'pending_cancellations':0, 'inflight':0, 'replay_results':0, 'path_retained':False}}}
    add('infer_cancel_wait', payload, 'inference_cancelled', details)
    add('infer_cancel_wait', payload, 'inference_cancelled', details)
    return json.dumps(golden, sort_keys=True, indent=2)+'\n'


def test_cancellation_golden_matches_python_signer():
    assert OUTPUT.read_text() == generate()
    assert OUTPUT.with_name('readiness_control_golden.json').read_text() == json.dumps(readiness_golden(), sort_keys=True, indent=2)+'\n'


if __name__ == '__main__':
    OUTPUT.write_text(generate())
    OUTPUT.with_name('readiness_control_golden.json').write_text(json.dumps(readiness_golden(), sort_keys=True, indent=2)+'\n')
    print(OUTPUT)
