# SPDX-License-Identifier: AGPL-3.0-only
"""Production route lifecycle with signed scripted peers; not physical proof."""
import time
from types import SimpleNamespace

import pytest

from mycelium_live import route as routes
from mycelium_live.phone_stage_session import PhoneStageSession, validate_phone_configure
from mycelium_qualification.signing import generate_ed25519_signer
from physical_inference_qualification import ControllerError, PeerIdentity, QualificationController
from test_phone_stage_session import Carrier, ENDPOINT, peer

DIGEST = 'sha256:' + 'a' * 64


def world(monkeypatch):
    carrier = Carrier()
    carrier.cleanup = True
    peers = [PeerIdentity(node_id=n, host_id=n, boot_id='boot-'+n, ssh_target=None,
                          staging_root='/tmp/mycelium/'+n, process_transport='managed_https')
             for n in ('entry', 'final')]
    peers.insert(1, peer())
    endpoints = {p.node_id: {'endpoint_id': ENDPOINT if p.node_id == 'phone' else ('01' if p.node_id == 'entry' else '02')*32,
                           'membership_generation': 1} for p in peers}
    graph = SimpleNamespace(deployment_id='dep', deployment_epoch=1, topology_version=1,
                            model_id='model', resolved_commit='commit', manifest_digest=DIGEST,
                            stages=[SimpleNamespace(placements=[SimpleNamespace(node_id=p.node_id)]) for p in peers])
    monkeypatch.setattr(routes, 'execution_graph_from_document', lambda d: graph)
    calls = []
    class Host:
        returncode = None
        stderr = b''
        def __init__(self, **kwargs):
            assert kwargs['argv'] == ()
            self.node_id = kwargs['node_id']
            self.signer = generate_ed25519_signer(endpoint_id=endpoints[self.node_id]['endpoint_id'])
        def send(self, *, command_id, command, payload):
            calls.append((self.node_id, command))
            identity = dict(run_id='run', deployment_id='dep', node_id=self.node_id,
                            host_id=self.node_id, process_id=17, endpoint_id=None,
                            peer_generation=0, state='NEW', route_ready=False)
            if command == 'hello':
                result = dict(identity, protocol='mycelium.physical_node_control.v1', boot_id='boot-'+self.node_id)
            else:
                event = {'configure':'configured', 'start':'started', 'stop':'stopping'}.get(command, command)
                observation = dict(identity, protocol='mycelium.physical_node_observation.v1', event=event,
                                   monotonic_ns=123, details={'endpoint_addr':{'id':self.signer.endpoint_id}})
                observation['endpoint_id'] = self.signer.endpoint_id
                result = dict(observation=observation, signature=self.signer.sign(observation), verification_key=self.signer.public_key_record())
            return dict(protocol='mycelium.physical_node_control.v1', command_id=command_id,
                        node_id=self.node_id, ok=True, route_ready=False, result=result)
        def close(self): pass
    member = dict(node_id='phone', endpoint_id=ENDPOINT, incarnation='inc-123', generation=1,
                  lifecycle_state='RUNNING', lease_expires_at=time.time()+300,
                  verification_key_digest=carrier.signer.verification_key_digest)
    authority = SimpleNamespace(current_members=lambda: (member,), close=lambda: None)
    def external(**kw):
        return PhoneStageSession(**kw, controller_endpoint_id='cd'*32, client_factory=carrier.client)
    controller = object.__new__(QualificationController)
    controller.peers = tuple(peers)
    plan = dict(run_id='run', deployment_id='dep', entry_node_id='entry', nodes=[
        {'node_id':p.node_id, 'configure': (dict(assignment_id='assignment', stage_id='middle', pack_digest=DIGEST,
                                             manifest_digest=DIGEST, load_generation=1) if p.node_id == 'phone' else {'graph':{}})} for p in peers])
    r = routes.PhysicalLiveRoute(controller=controller, endpoints=endpoints, run_plan=plan,
                                 session_factory=Host, external_session_factory=external, seed_authority=authority)
    return r, carrier, member, calls


def test_route_opens_mixed_sessions_proves_cancel_then_health_and_close(monkeypatch):
    r, c, member, calls = world(monkeypatch)
    try:
        assert len(r.open().endpoint_ids) == 3
        assert [d['command'] for d in c.calls] == ['hello','configure','start','bind_request_control','infer_cancel_wait']
        assert all(n != 'phone' for n, _ in calls)
        configure = next(d['payload'] for d in c.calls if d['command']=='configure')
        assert configure['request_control_graph'] == dict(deployment_id='dep',deployment_epoch=1,topology_generation=1)
        cancel = next(d['payload'] for d in c.calls if d['command']=='infer_cancel_wait')
        assert cancel['publisher_generation'] > 0 and cancel['request_attempt'] > 0
        assert cancel['absolute_deadline_ms'] > int(time.monotonic()*1000)
        r._snapshot_all()
        assert 'phone' in r._last_snapshots
        response = r._send_before('phone', command_id='health-now', command='health', payload={},
                                  deadline_monotonic_s=time.monotonic()+0.5)
        r._verify_observation('phone', response, event='health')
        # A seed revocation/generation change defeats even a valid pinned signature.
        member['generation'] = 2
        with pytest.raises(ControllerError, match='external_member_identity_invalid'):
            r._verify_observation('phone', response, event='health')
    finally:
        r.close()


@pytest.mark.parametrize('field,value', [('complete',False), ('cancellation_generation',2),
                                        ('publisher_generation',2), ('request_id','other'),
                                        ('path_attempt',1), ('complete',1)])
def test_open_refuses_inexact_signed_cancellation_receipt(monkeypatch, field, value):
    r, c, _, _ = world(monkeypatch)
    c.receipt_transform = lambda receipt: dict(receipt, **{field:value})
    with pytest.raises(ControllerError, match='external_node_cancel_receipt_unsupported'):
        r.open()
    assert not r._open
    r.close()


@pytest.mark.parametrize('field,value', [('request_id', 'other'), ('control_digest', 'sha256:'+'0'*64)])
def test_open_refuses_inexact_signed_probe_bind(monkeypatch, field, value):
    r, c, _, _ = world(monkeypatch)
    c.bind_transform = lambda details: dict(details, **{field: value})
    try:
        with pytest.raises(ControllerError, match='external_node_cancel_probe_bind_invalid'):
            r.open()
        assert not r._open
        assert not any(d['command'] == 'infer_cancel_wait' for d in c.calls)
    finally:
        r.close()


def test_open_refuses_missing_signed_receipt(monkeypatch):
    r, c, _, _ = world(monkeypatch)
    c.cleanup = False
    with pytest.raises(ControllerError, match='external_node_cancel_receipt_unsupported'):
        r.open()
    r.close()


def test_open_refuses_old_phone_without_cancel_command(monkeypatch):
    r, c, _, _ = world(monkeypatch)
    def old_phone(response):
        result = response.get('result', {})
        if result.get('observation', {}).get('event') == 'inference_cancelled':
            response.pop('result')
            response.update(ok=False, error={'code':'unsupported_command'})
        return response
    c.mutate = old_phone
    with pytest.raises(ControllerError, match='external_node_cancel_receipt_unsupported'):
        r.open()
    assert not r._open
    r.close()


@pytest.mark.parametrize('end', [0, -1])
def test_external_role_guard_precedes_any_process_or_phone_command(monkeypatch, end):
    r, c, _, calls = world(monkeypatch)
    r._plan['nodes'][1], r._plan['nodes'][end] = r._plan['nodes'][end], r._plan['nodes'][1]
    with pytest.raises(ControllerError, match='external_node_stage_only'):
        r.open()
    assert not calls and not c.calls
    r.close()


def test_open_refuses_seed_key_substitution_at_configure(monkeypatch):
    r, c, _, _ = world(monkeypatch)
    c.signer = generate_ed25519_signer(endpoint_id=ENDPOINT)
    with pytest.raises(ControllerError, match='external_member_key_mismatch'):
        r.open()
    r.close()


def test_phone_profile_accepts_no_host_paths():
    profile = dict(assignment_id='a', stage_id='s', pack_digest=DIGEST, manifest_digest=DIGEST, load_generation=1)
    assert validate_phone_configure(profile) == profile
    for bad in (dict(profile, assignment_file='/tmp/a'), dict(profile, load_generation=True), dict(profile, pack_digest='bad')):
        with pytest.raises(ControllerError):
            validate_phone_configure(bad)


@pytest.mark.parametrize("automatic", [False, True])
def test_from_operator_plan_threads_external_factory(tmp_path, monkeypatch, automatic):
    import json
    captured = {}
    authority = SimpleNamespace(signer=object(), seed_node_id='seed', members=(), close=lambda: None)
    monkeypatch.setattr(routes, '_load_live_seed_authority', lambda **kw: authority)
    monkeypatch.setattr(routes, '_refresh_membership_snapshot', lambda snapshot, **kw: snapshot)
    class Controller:
        def __init__(self, **kw): pass
        def _validate_physical_distinctness(self): pass
        def _validate_membership(self): return {}
        def _validate_run_plan(self): return {}
    class Route(routes.PhysicalLiveRoute):
        def __init__(self, **kw): captured.update(kw)
    monkeypatch.setattr(routes, 'QualificationController', Controller)
    path = tmp_path/'plan.json'
    path.write_text(json.dumps({'controller':dict(mode='physical', peers=[], source_root=str(tmp_path),
                           transfer_manifest={}, membership_snapshot={}, run_plan={})}))
    factory = object()
    if automatic:
        from dataclasses import asdict
        from mycelium_live.phone_stage_session import PhoneStageSessionFactory
        doc = json.loads(path.read_text())
        doc['controller']['peers'] = [{k:v for k,v in asdict(peer()).items() if not k.startswith('_')}]
        secret = tmp_path / 'bootstrap'
        secret.write_bytes(b'x'*32)
        secret.chmod(0o600)
        doc['controller']['external_control'] = dict(socket_path=str(tmp_path/'sidecar.sock'),
            bootstrap_secret_file=str(secret), controller_endpoint_id='cd'*32)
        path.write_text(json.dumps(doc))
        Route.from_operator_plan(path, seed_state_root=tmp_path)
        assert isinstance(captured['external_session_factory'], PhoneStageSessionFactory)
    else:
        Route.from_operator_plan(path, seed_state_root=tmp_path, external_session_factory=factory)
        assert captured['external_session_factory'] is factory


def test_external_without_factory_never_builds_process_arguments(monkeypatch):
    r, c, _, _ = world(monkeypatch)
    r._external_session_factory = None
    with pytest.raises(ControllerError, match='external_session_provider_required'):
        r._node_command('phone')
    assert not c.calls
    r.close()


def test_supervisor_threads_external_provider_before_open(tmp_path, monkeypatch):
    import mycelium_live.supervisor as supervisor
    monkeypatch.setattr(supervisor, 'native_deployment_projection', lambda path: object())
    captured = {}
    factory = object()
    def stop(path, **kwargs):
        captured.update(kwargs)
        raise RuntimeError('factory_reached')
    monkeypatch.setattr(supervisor.PhysicalLiveRoute, 'from_operator_plan', stop)
    with pytest.raises(RuntimeError, match='factory_reached'):
        supervisor._qualified_runtime(tmp_path/'plan.json', seed_state_root=tmp_path,
                                      external_session_factory=factory)
    assert captured['external_session_factory'] is factory


def test_piq_refuses_process_staging_and_run_for_phone():
    controller = object.__new__(QualificationController)
    controller.peers = (peer(),)
    with pytest.raises(ControllerError, match='external_assignment_lifecycle_required'):
        controller._prepare_physical((), {})
    with pytest.raises(ControllerError, match='external_assignment_lifecycle_required'):
        controller._cleanup_peer(peer(), archive_digest=DIGEST)
    with pytest.raises(ControllerError, match='external_live_route_provider_required'):
        controller._run_physical({})


def test_piq_validates_mixed_plan_without_phone_process_fields():
    controller = object.__new__(QualificationController)
    controller.peers = tuple([PeerIdentity(node_id=n, host_id=n, boot_id='boot-'+n,
        ssh_target=None, staging_root='/tmp/mycelium/'+n, process_transport='managed_https')
        for n in ('entry','final')] + [peer()])
    graph = {'stages': [{'placements':[{'node_id':n}]} for n in ('entry','phone','final')]}
    profile = dict(assignment_id='a', stage_id='s', pack_digest=DIGEST, manifest_digest=DIGEST, load_generation=1)
    nodes = [dict(node_id=n, python_executable='/usr/bin/python', socket_root='/tmp/mycelium/socket',
                  sidecar_binary='/opt/bin/sidecar', endpoint_secret_file='/tmp/mycelium/n/identity/key',
                  configure={'graph':graph}) for n in ('entry','final')]
    nodes.insert(1, {'node_id':'phone', 'configure':profile})
    controller._membership_snapshot = {'deployment_id':'dep'}
    controller._transfer_manifest = {'files':[{'path':'physical_inference_node.py'}]}
    controller._run_plan = dict(protocol='mycelium.controller_run_plan.v1', run_id='run', deployment_id='dep',
        entry_node_id='entry', nodes=nodes, decode_count=1, expected_token_ids=[1,2], decode_mode='complete_context_replay',
        request=dict(request_id='request', prompt_token_ids=[1], max_new_tokens=2, expected_new_tokens=2,
                     qos_class='interactive', admitted_at=0, target_ttft_ms=100, target_tpot_ms=100,
                     target_tokens_per_second=1, sampling_seed=0, generation_config_digest=DIGEST))
    assert controller._validate_run_plan()['nodes'][1] == {'node_id':'phone','configure':profile}
    nodes[1]['endpoint_secret_file'] = '/tmp/untrusted'
    with pytest.raises(ControllerError, match='run_plan_external_node_invalid'):
        controller._validate_run_plan()
