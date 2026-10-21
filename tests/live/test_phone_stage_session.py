# SPDX-License-Identifier: AGPL-3.0-only
"""Host control composition; signed scripted phone, no device/model claims."""
import json
import threading
import time
from types import SimpleNamespace

import pytest

from physical_inference_qualification import PeerIdentity, QualificationController, ControllerError, _peer_process_argv
from mycelium_qualification.signing import generate_ed25519_signer
from mycelium_qualification.evidence import canonical_json_bytes

ENDPOINT = 'ab' * 32


def peer():
    return PeerIdentity(node_id='phone', host_id='phone', boot_id='inc-123',
                        ssh_target=None, staging_root='/tmp/mycelium/phone',
                        process_transport='external_iroh')


def test_external_peer_no_argv_or_ssh_authority():
    assert _peer_process_argv(peer(), ('never-run',)) == ()
    with pytest.raises(ControllerError):
        PeerIdentity(node_id='phone', host_id='other', boot_id='inc-123',
                     ssh_target=None, staging_root='/tmp/mycelium/phone',
                     process_transport='external_iroh')


def test_phone_hello_uses_enrolled_incarnation_and_endpoint():
    p = peer()
    identity = dict(protocol='mycelium.physical_node_control.v1', run_id='run', deployment_id='dep',
                    node_id='phone', host_id='phone', process_id='inc-123', boot_id='inc-123',
                    endpoint_id=ENDPOINT, peer_generation=0, state='NEW', route_ready=False)
    controller = object.__new__(QualificationController)
    assert controller._hello_identity({'ok': True, 'result': identity}, peer=p,
                                      run_id='run', deployment_id='dep')['process_id'] == 'inc-123'
    identity['process_id'] = 'inc-old'
    with pytest.raises(ControllerError):
        controller._hello_identity({'ok': True, 'result': identity}, peer=p, run_id='run', deployment_id='dep')


class Carrier:
    def __init__(self):
        self.signer = generate_ed25519_signer(endpoint_id=ENDPOINT)
        self.calls = []
        self.mutate = lambda x: x
        self.block = threading.Event()
        self.entered = threading.Event()
        self.clients = []
        self.cleanup = False
        self.bound_controls = {}
        self.bind_transform = lambda details: details
        self.receipt_transform = lambda receipt: receipt

    def client(self, *, timeout):
        owner = self
        class Client:
            endpoint_id = 'cd' * 32
            def connect(self, *, deadline):
                assert deadline > time.monotonic()
            def control_call(self, *, peer_endpoint_id, request_json, timeout_seconds):
                from inspect import signature
                from mycelium_iroh_sidecar.client import SidecarClient
                signature(SidecarClient.control_call).bind(self, peer_endpoint_id=peer_endpoint_id,
                    request_json=request_json, timeout_seconds=timeout_seconds)
                assert peer_endpoint_id == ENDPOINT
                assert isinstance(request_json, str)
                assert 0 < timeout_seconds <= 60
                d = json.loads(request_json)
                assert canonical_json_bytes(d).decode('utf-8') == request_json
                owner.calls.append(d)
                op = d['command']
                if op == 'snapshot' and d['payload'].get('block'):
                    owner.entered.set()
                    owner.block.wait(1)
                identity = dict(run_id=d['run_id'], deployment_id=d['deployment_id'], node_id='phone',
                                host_id='phone', process_id='inc-123', endpoint_id=ENDPOINT,
                                peer_generation=0, state='NEW', route_ready=False)
                if op == 'hello':
                    result = dict(identity, protocol=d['protocol'], boot_id='inc-123')
                else:
                    event = {'configure': 'configured', 'start': 'started', 'bind_request_control': 'request_control_bound',
                             'update_request_control': 'request_control_updated', 'infer_cancel_wait': 'inference_cancelled'}.get(op, op)
                    details = d['payload']
                    if op == 'configure':
                        details = dict(details, endpoint_addr={'id': ENDPOINT})
                    if op == 'bind_request_control' and 'control' in d['payload']:
                        from mycelium_qualification.evidence import sha256_document
                        request_id = d['payload']['request_id']
                        control = dict(d['payload']['control'])
                        assert control['cancellation_generation'] == 0
                        owner.bound_controls[request_id] = control
                        details = owner.bind_transform(dict(request_id=request_id, control_digest=sha256_document(control)))
                    if op == 'infer_cancel_wait' and owner.cleanup:
                        subject = {k: v for k, v in d['payload'].items() if k != 'deadline_budget_ms'}
                        bound = owner.bound_controls.get(subject['request_id'])
                        complete = (bound is not None and all(subject[k] == v for k, v in bound.items()
                            if k != 'cancellation_generation')
                            and subject['cancellation_generation'] == bound['cancellation_generation'] + 1)
                        receipt = dict(subject, runtime_clean=True, transport_clean=True,
                                       cancellation_worker_complete=complete, complete=complete)
                        details = {'request_cleanup': owner.receipt_transform(receipt)}
                    observation = dict(identity, protocol='mycelium.physical_node_observation.v1',
                                       event=event, monotonic_ns=123, details=details)
                    result = dict(observation=observation, signature=owner.signer.sign(observation),
                                  verification_key=owner.signer.public_key_record())
                return {'peer_endpoint_id': peer_endpoint_id, 'response_json': canonical_json_bytes(
                    owner.mutate(dict(protocol=d['protocol'], command_id=d['command_id'],
                    node_id='phone', ok=True, route_ready=False, result=result))).decode('utf-8')}
            def close(self):
                pass
            def interrupt(self):
                owner.block.set()
        client = Client()
        self.clients.append(client)
        return client


def session(carrier):
    from mycelium_live.phone_stage_session import PhoneStageSession
    return PhoneStageSession(argv=(), node_id='phone', run_id='run', deployment_id='dep',
                             timeout_seconds=900, peer=peer(), endpoint_id=ENDPOINT,
                             member_key_digest=carrier.signer.verification_key_digest,
                             controller_endpoint_id='cd' * 32, client_factory=carrier.client)


def test_session_wire_lifecycle_and_signed_cancellation_not_rewritten():
    c = Carrier()
    s = session(c)
    s.send(command_id='h', command='hello', payload={})
    # Bind has a clock-domain wire projection and native-shaped digest response;
    # its complete contract is covered by test_phone_deadline_wire/composition.
    for op in ('configure', 'start', 'update_request_control', 'health', 'snapshot', 'infer_cancel_wait'):
        payload = {'request_cleanup': {'complete': False, 'cancellation_generation': 3}} if op == 'infer_cancel_wait' else {}
        response = s.send(command_id=op, command=op, payload=payload)
        assert response['result']['observation']['details'] == (dict(payload, endpoint_addr={'id': ENDPOINT}) if op == 'configure' else payload)
    assert len(c.clients) == len(c.calls)
    assert s.returncode is None and s.stderr == b''
    s.close()
    with pytest.raises(RuntimeError, match='phone_session_closed'):
        s.send(command_id='late', command='health', payload={})


@pytest.mark.parametrize('attack', ['key', 'signature', 'command', 'endpoint', 'node', 'run'])
def test_session_rejects_untrusted_or_misbound_response(attack):
    c = Carrier()
    s = session(c)
    s.send(command_id='h', command='hello', payload={})
    if attack == 'key':
        c.signer = generate_ed25519_signer(endpoint_id=ENDPOINT)
    else:
        def mutate(r):
            if attack == 'command': r['command_id'] = 'other'
            elif attack == 'node': r['node_id'] = 'other'
            else:
                obs = r['result']['observation']
                if attack == 'signature': obs['details'] = {'tampered': True}
                if attack == 'endpoint': obs['endpoint_id'] = 'ef' * 32
                if attack == 'run': obs['run_id'] = 'other'
            return r
        c.mutate = mutate
    with pytest.raises((ControllerError, RuntimeError)):
        s.send(command_id='health', command='health', payload={})


def test_health_does_not_queue_behind_other_control_call():
    c = Carrier()
    s = session(c)
    s.send(command_id='h', command='hello', payload={})
    t = threading.Thread(target=lambda: s.send(command_id='slow', command='snapshot', payload={'block': True}))
    t.start()
    assert c.entered.wait(1)
    try:
        s.send_before(command_id='health', command='health', payload={}, deadline_monotonic_s=time.monotonic()+0.1)
        assert not c.block.is_set()
    finally:
        c.block.set()
        t.join(2)
    count = len(c.calls)
    with pytest.raises(TimeoutError):
        s.send_before(command_id='late', command='health', payload={}, deadline_monotonic_s=time.monotonic()-1)
    assert len(c.calls) == count


def test_phone_forces_replay_without_changing_numpy_qwen_kv():
    from physical_inference_node import _route_decode_mode
    def graph(backends):
        return SimpleNamespace(stages=[SimpleNamespace(placements=[SimpleNamespace(runtime_backend=b, lifecycle_state='ACTIVE')]) for b in backends])
    assert _route_decode_mode(graph(['mlx', 'mobile-rust-qwen2', 'numpy']), 'qwen2') == 'complete_context_replay'
    assert _route_decode_mode(graph(['mlx', 'numpy']), 'qwen2') == 'stage_local_kv'


@pytest.mark.parametrize('operation', ['interrupt', 'close'])
def test_control_wait_can_be_retired_without_stopping_shared_endpoint(operation):
    c = Carrier()
    s = session(c)
    s.send(command_id='h', command='hello', payload={})
    errors = []
    def blocked():
        try:
            s.send(command_id='blocked', command='snapshot', payload={'block': True})
        except RuntimeError as exc:
            errors.append(str(exc))
    worker = threading.Thread(target=blocked)
    worker.start()
    assert c.entered.wait(1)
    if operation == 'close':
        s.close()
    else:
        assert s.interrupt_command('blocked', code='request_cancelled')
    worker.join(0.5)
    assert not worker.is_alive()
    assert errors == ['phone_session_closed' if operation == 'close' else 'request_cancelled']
    assert s.returncode is None
    if operation == 'interrupt':
        s.send(command_id='health', command='health', payload={})


def test_wrong_local_controller_identity_never_sends_phone_command():
    c = Carrier()
    s = session(c)
    s._controller_endpoint_id = 'ff' * 32
    with pytest.raises(ControllerError, match='external_controller_endpoint_mismatch'):
        s.send(command_id='h', command='hello', payload={})
    assert not c.calls


def test_session_composes_with_real_sidecar_client_authenticated_records():
    """Real Python carrier codec; socketpair replaces IPC accept/handshake only."""
    import socket
    from mycelium_iroh_sidecar import client as wire
    c = Carrier()
    workers = []
    errors = []
    class ConnectedClient(wire.SidecarClient):
        def connect(self, *, deadline):
            local, remote = socket.socketpair()
            local.settimeout(1)
            remote.settimeout(1)
            self._socket = local
            self._send_key = bytearray(b'a'*32)
            self._receive_key = bytearray(b'b'*32)
            self.endpoint_id = 'cd'*32
            def serve():
                try:
                    raw = wire._read_prefixed(remote, wire._MAX_RECORD_BYTES)
                    kind, mid, payload = wire._decode_record(raw, b'a'*32, 0)
                    assert kind == 18
                    assert payload[:32].hex() == ENDPOINT
                    result = c.client(timeout=1).control_call(peer_endpoint_id=payload[:32].hex(),
                        request_json=payload[40:].decode('utf-8'), timeout_seconds=int.from_bytes(payload[32:40],'big')/1000)
                    wire._write_prefixed(remote, wire._encode_record(19,0,mid,
                        result['response_json'].encode('utf-8'),b'b'*32))
                except BaseException as exc:
                    errors.append(exc)
                finally:
                    remote.close()
            worker = threading.Thread(target=serve)
            workers.append(worker)
            worker.start()
    s = session(c)
    s._client_factory = lambda timeout: ConnectedClient('/unused', b'x'*32, timeout=timeout)
    try:
        s.send(command_id='hello-real-codec', command='hello', payload={})
        reply = s.send(command_id='health-real-codec', command='health', payload={})
        assert reply['result']['observation']['event'] == 'health'
    finally:
        s.close()
        for worker in workers:
            worker.join(2)
            assert not worker.is_alive()
    assert not errors


@pytest.mark.parametrize('hint,accepted', [
    ({'protocol': 'mycelium.physical_node_control.v1', 'stage_load_generation': 3}, True),
    ({'protocol': 'mycelium.physical_node_control.v1', 'stage_load_generation': None}, True),
    ({'protocol': 'mycelium.physical_node_control.v1', 'stage_load_generation': 0}, False),
    ({'protocol': 'mycelium.physical_node_control.v1', 'stage_load_generation': True}, False),
    ({'protocol': 'mycelium.physical_node_control.v1', 'stage_load_generation': 3, 'verified': True}, False),
    ({'protocol': 'other', 'stage_load_generation': 3}, False),
    ({'stage_load_generation': 3}, False),
])
def test_stage_load_hint_is_a_closed_unsigned_reply_after_hello(hint, accepted):
    """The hint only tells a controller which number to name; it proves nothing."""
    carrier = Carrier()
    s = session(carrier)
    with pytest.raises(RuntimeError, match='phone_hello_required'):
        s.send(command_id='early', command='stage_load', payload={})
    s.send(command_id='h', command='hello', payload={})
    carrier.mutate = lambda response: dict(response, result=hint)
    if accepted:
        assert s.send(command_id='l', command='stage_load', payload={})['result'] == hint
    else:
        with pytest.raises(ControllerError, match='phone_stage_load_hint_invalid'):
            s.send(command_id='l', command='stage_load', payload={})
    # A native refusal is passed through untouched, as for every command.
    carrier.mutate = lambda response: {k: v for k, v in dict(response, ok=False, error={'code': 'run_id_mismatch'}).items() if k != 'result'}
    assert s.send(command_id='r', command='stage_load', payload={})['error'] == {'code': 'run_id_mismatch'}
