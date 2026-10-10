# SPDX-License-Identifier: AGPL-3.0-or-later
"""Real native-phone/host loopback witness; NEVER physical or model qualification.

Desktop session/attestation records and the pre-applied seed membership are
explicit test doubles. Phone commands, signatures, stage execution and telemetry
are native. The runtime is the repository's tiny synthetic CPU stage fixture.
"""
import copy
import json
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import tempfile
import time
import traceback
from types import SimpleNamespace
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests/qualification')]
from mycelium_iroh_sidecar import SidecarClient
from mycelium_live.phone_stage_session import PhoneStageSessionFactory, require_cancel_receipt
from mycelium_live.route import PhysicalLiveRoute
from mycelium_qualification.evidence import sha256_document
from mycelium_qualification.phone import phone_load_proof_digest
from mycelium_qualification.signing import generate_ed25519_signer
from physical_inference_qualification import PeerIdentity, QualificationController
from test_phone_live_authority import phone_attestation, qualify

D = 'sha256:' + 'a' * 64
NODES = ['node-mac-entry', 'node-phone', 'node-mac-final']
PROFILE = dict(assignment_id='assignment-1', stage_id='stage-golden', pack_digest=D,
               manifest_digest=D, load_generation=1)
TRANSCRIPT = []


def exchange(action):
    print(json.dumps({'action': action}), flush=True)
    assert select.select([sys.stdin], [], [], 15)[0], 'native witness timeout'
    result = json.loads(sys.stdin.readline())
    TRANSCRIPT.append({'native_witness': action, 'result': result})
    return result


def packet(fixture):
    """Reuse the consumer's synthetic desktop fixture, never synthesize phone envelopes."""
    att, _, _ = phone_attestation()
    old = [s['placements'][0]['node_id'] for s in att['execution_graph']['stages']]
    replacements = dict(zip(old, NODES))
    def rewrite(value):
        if isinstance(value, dict): return {k: rewrite(v) for k, v in value.items()}
        if isinstance(value, list): return [rewrite(v) for v in value]
        return replacements.get(value, value) if isinstance(value, str) else value
    att = rewrite(att)
    graph = att['execution_graph']
    graph['deployment_epoch'] = 1
    graph['hidden_size'] = 64
    graph['topology_version'] = 1
    graph['stages'][1]['stage_id'] = 'stage-golden'
    graph['stages'][1]['placements'][0]['assignment_id'] = 'assignment-1'
    # Phone records must ONLY come from the native process. Discard fixture phone records.
    att['signed_observations'] = [x for x in att['signed_observations']
                                  if x['observation']['node_id'] != 'node-phone']
    endpoints = {n: fixture[k]['id'] for n, k in zip(NODES, ('entry', 'phone', 'final'))}
    signers = {n: generate_ed25519_signer(endpoint_id=endpoints[n]) for n in (NODES[0], NODES[2])}
    for e in att['signed_observations']:
        o = e['observation']; n = o['node_id']; d = o['details']
        o['endpoint_id'] = endpoints[n]
        if o['event'] == 'configured': d['endpoint_addr'] = fixture['entry' if n == NODES[0] else 'final']
        if o['event'] == 'started': d['peer']['endpoint_id'] = endpoints[d['peer']['node_id']]
        e['verification_key'] = signers[n].public_key_record()
        e['signature'] = signers[n].sign(o)
    return att, endpoints


def run(binary, mode):
    secret = b'c' * 32  # synthetic local IPC authentication only
    # macOS UDS path length is small; authenticated runtime owns a private subdir.
    with tempfile.TemporaryDirectory(prefix='phone-compose-', dir='/tmp') as temp:
        socket = Path(temp) / 'run' / 's.sock'
        sidecar = subprocess.Popen([binary, '--uds', str(socket), '--bootstrap-fd', '0', '--local-only'],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        route = session = None
        try:
            sidecar.stdin.write(secret); sidecar.stdin.close()
            assert select.select([sidecar.stdout], [], [], 10)[0], 'sidecar readiness timeout'
            ready = json.loads(sidecar.stdout.readline())
            assert ready['event'] == 'ready', ready
            print(json.dumps({'controller': ready['endpoint_id']}), flush=True)
            assert select.select([sys.stdin], [], [], 15)[0], 'native setup timeout'
            fixture = json.loads(sys.stdin.readline())
            TRANSCRIPT.append({'fixture': fixture, 'mode': mode, 'synthetic_cpu_only': True})
            with SidecarClient(socket, secret, timeout=5) as client:
                client.configure_peer(fixture['phone']['id'], fixture['phone'], generation=1)
            factory = PhoneStageSessionFactory(socket_path=socket, bootstrap_secret=secret,
                                               controller_endpoint_id=ready['endpoint_id'])
            att, endpoints = packet(fixture)
            dep = att['execution_graph']['deployment_id']
            key = fixture['key']['verification_key_digest']
            peers = [PeerIdentity(node_id=n, host_id=n, boot_id='inc-phone' if n == 'node-phone' else 'boot-'+n,
                ssh_target=None, staging_root='/tmp/mycelium-composition/'+n,
                process_transport='external_iroh' if n == 'node-phone' else 'managed_https') for n in NODES]
            session = factory(argv=(), node_id='node-phone', run_id='run', deployment_id=dep,
                timeout_seconds=5, peer=peers[1], endpoint_id=endpoints['node-phone'], member_key_digest=key)
            responses = []
            def call(command, payload=None):
                response = session.send(command_id=f'composition-{len(responses)}-{command}',
                    command=command, payload=payload or {})
                responses.append(response)
                TRANSCRIPT.append({'command':command, 'payload':payload or {}, 'response':response})
                assert response['ok'] is True, response
                return response.get('result')
            if mode == 'startup':
                # Desktop lifecycle alone is doubled. Route.open and its native
                # phone bind->cancel startup proof are not patched or bypassed.
                class DesktopDouble:
                    returncode = None
                    stderr = b''
                    def __init__(self, **kw):
                        self.node = kw['node_id']
                        self.signer = generate_ed25519_signer(endpoint_id=endpoints[self.node])
                    def send(self, *, command_id, command, payload):
                        identity = dict(run_id='run', deployment_id=dep, node_id=self.node, host_id=self.node,
                            process_id=17, endpoint_id=endpoints[self.node], peer_generation=1, state='RUNNING', route_ready=False)
                        if command == 'hello':
                            result = dict(identity, protocol='mycelium.physical_node_control.v1', boot_id='boot-'+self.node,
                                          endpoint_id=None, peer_generation=0, state='NEW')
                        else:
                            event = {'configure':'configured', 'start':'started', 'stop':'stopping'}.get(command,command)
                            o = dict(identity, protocol='mycelium.physical_node_observation.v1', event=event,
                                monotonic_ns=time.monotonic_ns(), details={'endpoint_addr':fixture['entry' if self.node == NODES[0] else 'final']})
                            result = dict(observation=o, signature=self.signer.sign(o), verification_key=self.signer.public_key_record())
                            if command == 'stop': result = {'final_observation':result}
                        return dict(protocol='mycelium.physical_node_control.v1', command_id=command_id,
                                    node_id=self.node, ok=True, route_ready=False, result=result)
                    def close(self): pass
                # Transparent recording wrapper: forwards all bytes through the real session.
                class RecordedPhone:
                    def __init__(self, real): self.real = real
                    def __getattr__(self, name): return getattr(self.real, name)
                    def send(self, **kw):
                        r = self.real.send(**kw); TRANSCRIPT.append({'route_call':kw,'response':r}); return r
                    def send_before(self, **kw):
                        r = self.real.send_before(**kw); TRANSCRIPT.append({'route_call':kw,'response':r}); return r
                member = dict(node_id='node-phone', endpoint_id=endpoints['node-phone'], incarnation='inc-phone',
                    generation=1, lifecycle_state='RUNNING', lease_expires_at=time.time()+300, verification_key_digest=key)
                authority = SimpleNamespace(current_members=lambda:(member,), close=lambda:None)
                controller = object.__new__(QualificationController); controller.peers=tuple(peers)
                route_graph = copy.deepcopy(att['execution_graph'])
                for stage in route_graph['stages']:
                    stage['layer_range'] = stage.pop('range')
                plan = dict(run_id='run', deployment_id=dep, entry_node_id=NODES[0], nodes=[dict(node_id=n,
                    configure=PROFILE if n=='node-phone' else {'graph':route_graph}) for n in NODES])
                route = PhysicalLiveRoute(controller=controller,
                    endpoints={n:dict(endpoint_id=endpoints[n],membership_generation=1) for n in NODES},
                    run_plan=plan, session_factory=DesktopDouble,
                    external_session_factory=lambda **kw:RecordedPhone(factory(**kw)), seed_authority=authority)
                route.open()
                events = [x for x in TRANSCRIPT if 'route_call' in x]
                assert [x['route_call']['command'] for x in events] == ['hello','configure','start','bind_request_control','infer_cancel_wait']
                bind = events[-2]; cancel = events[-1]
                ctl = bind['route_call']['payload']['control']
                assert ctl['cancellation_generation'] == 0
                assert bind['response']['result']['observation']['details']['control_digest'] == sha256_document(ctl)
                subject = {k:v for k,v in cancel['route_call']['payload'].items() if k != 'deadline_budget_ms'}
                assert subject == dict(ctl, request_id=bind['route_call']['payload']['request_id'], cancellation_generation=1)
                require_cancel_receipt(cancel['response']['result']['observation'], subject)
                route._snapshot_all()
                assert 'node-phone' in route._last_snapshots
                return
            call('hello')
            configured = call('configure', dict(PROFILE, request_control_graph=dict(deployment_id=dep,deployment_epoch=1,topology_generation=1)))
            def peer(n, k): return dict(node_id=n, endpoint_id=endpoints[n], endpoint_addr=fixture[k], generation=1)
            started = call('start', dict(peer=peer(NODES[2],'final'), peers=[peer(NODES[0],'entry')], local_generation=1))
            if mode == 'deadline':
                # Same CommandIdentity type and host-monotonic producer convention
                # as PhysicalRouterPort admission; not full router admission.
                from mycelium_live.command_controller import CommandIdentity
                from dataclasses import asdict
                identity = CommandIdentity(deployment_id=dep, deployment_epoch=1, qualification_digest=D,
                    request_id='request-phone-golden', request_attempt=1, path_id='path-phone-golden',path_attempt=2,
                    path_digest=sha256_document(fixture['path_manifest']), topology_generation=1, command_id='request:request-phone-golden:attempt:1',
                    publisher_generation=1, absolute_deadline_ms=int(time.monotonic()*1000)+2500)
                control = asdict(identity); control.pop('request_id'); control['cancellation_generation']=0
                payload = {'request_id':identity.request_id,'control':control}
                original = copy.deepcopy(payload)
                bound = call('bind_request_control', payload)
                assert bound['observation']['details']['control_digest'] == sha256_document(control)
                first = exchange('deadline')
                assert abs(time.monotonic()-first['native_monotonic_s']) > 1, 'clock origins unexpectedly equal'
                exchange('drive')
                time.sleep(0.05)
                call('bind_request_control', payload)
                second = exchange('deadline')
                assert second['remaining_ms'] < first['remaining_ms']
                def native_duplicate_with_larger_budget(command_id):
                    # Adversarial native consumer control: after the normal
                    # PhoneStageSession producer has bound, enlarge ONLY the
                    # wire budget. Neither this nor expiry may renew execution.
                    wire = dict(protocol='mycelium.physical_node_control.v1', command_id=command_id,
                        run_id='run',deployment_id=dep,command='bind_request_control',
                        payload=dict(payload,execution_budget_ms=60_000))
                    with SidecarClient(socket,secret,timeout=5) as client:
                        raw = client.control_call(peer_endpoint_id=endpoints['node-phone'],
                            request_json=json.dumps(wire,sort_keys=True,separators=(',',':')),timeout_seconds=4)
                    response = json.loads(raw['response_json'])
                    TRANSCRIPT.append({'native_duplicate_budget_control':wire,'response':response})
                    assert response['ok'] is True, response
                    verifier = object.__new__(QualificationController)
                    observed = verifier._verified_observation(response,event='request_control_bound',peer=peers[1],
                        process_id='inc-phone',run_id='run',deployment_id=dep,endpoint_id=endpoints['node-phone'],
                        expected_verification_key=fixture['key'])
                    assert observed['details']['control_digest'] == sha256_document(control)
                native_duplicate_with_larger_budget('duplicate-larger-budget')
                exchange('deadline')
                assert payload == original
                exchange('expired')
                native_duplicate_with_larger_budget('expired-larger-budget')
                exchange('expired')
                try: call('bind_request_control', payload)
                except TimeoutError as e: assert 'request_execution_deadline_exceeded' in str(e)
                else: raise AssertionError('host accepted expired bind')
                subject = dict(control, request_id=identity.request_id, cancellation_generation=1)
                cancelled = call('infer_cancel_wait', dict(subject,deadline_budget_ms=1000))
                require_cancel_receipt(cancelled['observation'], subject)
                scoped = call('snapshot', {'cleanup_subject':subject,'receipt_only':True})
                require_cancel_receipt(scoped['observation'], subject)
                return
            exchange('drive')
            snapshot = call('snapshot')
            again = call('snapshot')
            assert again['observation']['monotonic_ns'] > snapshot['observation']['monotonic_ns']
            assert snapshot['observation']['details']['runtime']['applied_operation_count'] == 1
            assert snapshot['observation']['details']['runtime']['replay_result_count'] == 1
            if mode == 'stop':
                call('stop')
                stopped = exchange('stopped')
                assert stopped['control_gone'] and stopped['deadlines'] == stopped['active_work'] == stopped['live_paths'] == 0
                assert stopped['replay_results'] == 0, stopped
                assert stopped['revision'] > 0
                reopened = call('hello')
                assert reopened['peer_generation'] == 0 and reopened['state'] == 'NEW'
                call('configure', dict(PROFILE, request_control_graph=dict(deployment_id=dep,deployment_epoch=1,topology_generation=1)))
                call('start', dict(peer=peer(NODES[2],'final'), peers=[peer(NODES[0],'entry')], local_generation=2))
                fresh = call('snapshot')['observation']
                assert fresh['peer_generation'] == 2 and fresh['monotonic_ns'] > again['observation']['monotonic_ns']
                assert fresh['details']['runtime']['replay_result_count'] == fresh['details']['runtime']['active_state_count'] == 0
                return
            # Whole production qualifier consumes unmodified native signed records.
            att['signed_observations'] += [configured,started,snapshot]
            graph_phone = att['execution_graph']['stages'][1]['placements'][0]
            graph_phone['load_proof_digest'] = phone_load_proof_digest(configured['observation'])
            bindings = {'node-phone':dict(endpoint_id=endpoints['node-phone'],process_id='inc-phone',member_key_digest=key,configure=PROFILE)}
            snapshots = [x['observation']['details'] for x in att['signed_observations'] if x['observation']['event']=='snapshot']
            att['counters'] = dict(frames_sent=sum(x['transport']['remote_frames_sent'] for x in snapshots),
                frames_received=sum(x['transport']['remote_frames_received'] for x in snapshots),
                applied_operation_count=sum(x['runtime']['applied_operation_count'] for x in snapshots),fatal=None)
            record = qualify(att, bindings)
            assert record.route_ready
            # Mutation control proves the consumer really verifies native bytes.
            corrupt = copy.deepcopy(att)
            next(x for x in corrupt['signed_observations'] if x['observation']['node_id']=='node-phone')['observation']['details']['load_generation'] = 99
            from mycelium_qualification import LiveRouteQualificationError
            try: qualify(corrupt,bindings)
            except LiveRouteQualificationError: pass
            else: raise AssertionError('tampered native signature accepted')
            TRANSCRIPT.append({'qualification_consumed_native_records':True,'physical_qualification':False,
                               'desktop_records':'synthetic_test_doubles','native_signature_tamper_rejected':True})
        finally:
            if route is not None: route.close()
            if session is not None: session.close()
            if sidecar.poll() is None:
                sidecar.terminate()
                try: sidecar.wait(timeout=5)
                except subprocess.TimeoutExpired: sidecar.kill(); sidecar.wait(timeout=5)
            TRANSCRIPT.append({'sidecar_exit':sidecar.returncode})
            sidecar.stdout.close(); sidecar.stderr.close()


def main():
    binary, mode = sys.argv[1:]
    def terminate(*_):
        raise SystemExit('native harness terminated; unwinding owned sidecar')
    signal.signal(signal.SIGTERM, terminate)
    began = time.monotonic(); error = None
    try: run(binary,mode)
    except BaseException:
        error = traceback.format_exc()
        print(error,file=sys.stderr,flush=True)
    finally:
        print(json.dumps({'action':'done','ok':error is None,'error':error}),flush=True)
        print(json.dumps({'mode':mode,'transcript':TRANSCRIPT}),file=sys.stderr,flush=True)
        if os.environ.get('PHONE_COMPOSITION_EVIDENCE'):
            out = Path(os.environ['PHONE_COMPOSITION_EVIDENCE']); out.mkdir(parents=True,exist_ok=True)
            (out/(mode+'.json')).write_text(json.dumps({'error':error,'transcript':TRANSCRIPT},indent=2)+'\n')
            suite = ET.Element('testsuite',name='real_phone_composition',tests='1',failures=str(int(error is not None)))
            case=ET.SubElement(suite,'testcase',name=mode,time=str(time.monotonic()-began))
            if error: ET.SubElement(case,'failure',message=error.splitlines()[-1]).text=error
            ET.ElementTree(suite).write(out/(mode+'.xml'),encoding='unicode')
    return int(error is not None)


if __name__ == '__main__':
    raise SystemExit(main())
