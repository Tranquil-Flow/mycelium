# SPDX-License-Identifier: AGPL-3.0-only
"""Real native-phone/host loopback witness; NEVER physical or model qualification.

Desktop session/attestation records and the pre-applied seed membership are
explicit test doubles. Phone commands, signatures, stage execution and telemetry
are native. The runtime is the repository's tiny synthetic CPU stage fixture.

The qualification case additionally runs the production seed issuance, builder
and current-authority validation against a disposable seed, and feeds the final
qualifier the unmodified records the native process signed. Its build receipt
names the checked-out source commit the native test binary was compiled from;
the "APK" is a labelled placeholder file. None of it is device evidence.
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
sys.path[:0] = [str(ROOT), str(ROOT / 'tests/qualification'), str(ROOT / 'tests/live'), str(ROOT / 'tests')]
from mycelium_iroh_sidecar import SidecarClient
from mycelium_live.phone_stage_session import PhoneStageSessionFactory, require_cancel_receipt
from mycelium_live.route import PhysicalLiveRoute
from mycelium_qualification.evidence import sha256_document
from mycelium_qualification.phone import (LOAD_IDENTITY_PROVENANCE_V2,
    phone_load_proof_digest, validate_phone_load_provenance)
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


def exchange_with(action, **fields):
    print(json.dumps(dict(fields, action=action)), flush=True)
    assert select.select([sys.stdin], [], [], 60)[0], 'native witness timeout'
    result = json.loads(sys.stdin.readline())
    TRANSCRIPT.append({'native_witness': action, 'result': result})
    return result


def qualification(fixture, ready, socket, secret, factory):
    """Seed issuance -> native load -> builder -> current authority -> final qualifier.

    Every phone envelope below is the native process's own, unmodified. The
    disposable seed's member rows for the phone are signed with the native
    test key the harness also holds (the membership supervisor is not run).
    """
    import tempfile as _tempfile
    from phone_composition_fixture import fixture_build, qualify_fixture_plan
    from scripts import build_qwen_live_route as builder
    from mycelium_qualification import LiveRouteQualificationError
    from physical_inference_qualification import ControllerError
    native = dict(source=os.environ['PHONE_COMPOSITION_SOURCE'], endpoint_id=fixture['phone']['id'],
        controller=ready['endpoint_id'], key=fixture['key'],
        desktop_endpoints={0: fixture['entry']['id'], 2: fixture['final']['id']})
    state = dict(session=None, sessions=0)
    def open_session(setup):
        peer = PeerIdentity(node_id='node-1', host_id='node-1', boot_id=setup['member']['incarnation'], ssh_target=None,
            staging_root='/tmp/mycelium/phone-stage', process_transport='external_iroh')
        state['sessions'] += 1
        state['session'] = factory(argv=(), node_id='node-1', run_id='synthetic-run', deployment_id=setup['deployment_id'],
            timeout_seconds=10, peer=peer, endpoint_id=native['endpoint_id'],
            member_key_digest=fixture['key']['verification_key_digest'])
    def call(command, payload=None, *, ok=True):
        response = state['session'].send(command_id=f"q{state['sessions']}-{len(TRANSCRIPT)}-{command}",
            command=command, payload=payload or {})
        TRANSCRIPT.append({'command': command, 'payload': payload or {}, 'response': response})
        assert response['ok'] is ok, response
        return response
    def close_session():
        call('stop'); state['session'].close(); state['session'] = None
    graph_control = lambda setup: dict(deployment_id=setup['deployment_id'], deployment_epoch=1, topology_generation=1)
    kept = {}
    def observe(setup):
        kept['setup'] = setup
        loaded = exchange_with('apply', model=setup['model'], manifest=setup['manifest'], record=setup['record'],
            member=dict(incarnation=setup['member']['incarnation'], generation=setup['member']['generation']),
            node_id='node-1', entry_node_id='node-0', final_node_id='node-2', deployment_id=setup['deployment_id'])
        assert loaded['load_generation'] == 1, loaded
        assert setup['record']['route_generation'] == 1
        open_session(setup)
        call('hello')
        hint = call('stage_load')['result']
        assert hint == {'protocol': 'mycelium.physical_node_control.v1', 'stage_load_generation': 1}, hint
        profile = dict(setup['profile'], load_generation=hint['stage_load_generation'])
        # Coherent generation laundering: the controller picks another number.
        # The phone signs nothing for it, so there is nothing to re-sign around.
        for chosen in (profile['load_generation'] + 1, 17):
            refused = call('configure', dict(profile, load_generation=chosen), ok=False)
            assert refused['error'] == {'code': 'load_generation_mismatch'} and 'result' not in refused, refused
        configured = call('configure', profile)
        snapshot = call('snapshot')
        details = configured['result']['observation']['details']
        assert details['load_identity_provenance'] == LOAD_IDENTITY_PROVENANCE_V2
        assert details['runtime_build_digest'] == fixture['runtime_build_digest']
        assert details['assignment_id'] == setup['record']['assignment_id']
        close_session()
        return configured, snapshot
    original_run = subprocess.run
    def no_network(cmd, **kw):
        if cmd[0] == 'ssh': return subprocess.CompletedProcess(cmd, 0, json.dumps(['fixture-remote', 'fixture-boot']), '')
        return original_run(cmd, **kw)
    subprocess.run = no_network
    with _tempfile.TemporaryDirectory(prefix='phone-qualify-') as temp:
        tmp = Path(temp).resolve()
        built = fixture_build(tmp, native=native, observe=observe)
        native_preparation = copy.deepcopy(built.document)
        assert built.document['configured'] == next(x['response'] for x in TRANSCRIPT
            if x.get('command') == 'configure' and x['response']['ok'])
        report = builder.build(built.args)  # unpatched production build
        assert report['route_ready'] is False
        plan_path = Path(report['operator_plan']); plan = json.loads(plan_path.read_text())
        checkpoint = json.loads((built.args.output_root / 'local-challenge-checkpoint.json').read_text())
        graph = plan['controller']['run_plan']['nodes'][0]['configure']['graph']
        expected = phone_load_proof_digest(built.document['configured']['result']['observation'])
        assert graph['stages'][1]['placements'][0]['load_proof_digest'] == expected
        proof = json.loads((built.args.output_root / 'transfer-bundle/control/node-1-load-proof.json').read_text())
        assert all(proof[k] == native_preparation[k] for k in ('configured', 'snapshot')), 'packaging rewrote a native envelope'
        setup = kept['setup']
        exchange_with('frames', topology_version=graph['topology_version'])
        route = PhysicalLiveRoute.from_operator_plan(plan_path, seed_state_root=built.args.phone_seed_state_root,
            external_session_factory=factory)
        try:
            # THIS route open: a new control session against the same applied load.
            profile = plan['controller']['run_plan']['nodes'][1]['configure']
            control = dict(deployment_id=graph['deployment_id'], deployment_epoch=graph['deployment_epoch'],
                           topology_generation=graph['topology_version'])
            open_session(setup)
            call('hello')
            configured = call('configure', dict(profile, request_control_graph=control))['result']
            peer = lambda n, k: dict(node_id=n, endpoint_id=fixture[k]['id'], endpoint_addr=fixture[k], generation=1)
            started = call('start', dict(peer=peer('node-2', 'final'), peers=[peer('node-0', 'entry')], local_generation=1))['result']
            exchange('drive')
            snapshot = call('snapshot')['result']
            assert snapshot['observation']['details']['runtime']['applied_operation_count'] == 1
            records = [configured, started, snapshot]
            native_records = copy.deepcopy(records)
            assert configured['observation']['monotonic_ns'] > native_preparation['configured']['result']['observation']['monotonic_ns']
            assert phone_load_proof_digest(configured['observation']) == expected, 'same applied load, same identity'
            bindings = route.phone_qualification_bindings()  # production: pinned seed rows + packaged proof
            authority = bindings['node-1']['load_authority']
            assert (authority.load_generation, authority.load_proof_digest) == (1, expected)
            record = qualify_fixture_plan(built, plan, checkpoint, bindings=bindings, phone_records=records)
            assert record.route_ready is True
            assert [b.load_proof_digest for b in record.stage_bindings if b.node_id == 'node-1'] == [expected]
            assert records == native_records, 'qualifier mutated native envelopes'
            def refused(candidate, code, using=None):
                try: result = qualify_fixture_plan(built, plan, checkpoint, bindings=using or bindings, phone_records=candidate)
                except LiveRouteQualificationError as e: assert str(e) == code, (code, str(e))
                else: raise AssertionError(f'{code} expected; qualifier returned route_ready={result.route_ready}')
            # Tampered native record: a signature refusal, before any provenance decision.
            corrupt = copy.deepcopy(records)
            corrupt[0]['observation']['details']['load_generation'] = 99
            refused(corrupt, 'live_signature_invalid')
            corrupt = copy.deepcopy(records)
            corrupt[2]['observation']['details']['load_identity']['load_generation'] = 99
            refused(corrupt, 'live_signature_invalid')
            # The plan's configure copy without validated authority is refused.
            refused(records, 'live_phone_load_authority_required',
                    using={'node-1': {k: v for k, v in bindings['node-1'].items() if k != 'load_authority'}})
            # Stale generation: the node installs its assignment again while this
            # session is RUNNING. The phone ends the session with its load: the
            # next hop is not executed and nothing more is signed for it.
            replaced = exchange('replace')
            assert replaced == dict(load_generation=2, was_running=True, control_gone=True,
                                    hop_refused=True, executed_after=0), replaced
            orphan = call('snapshot', ok=False)
            assert orphan['error'] == {'code': 'run_id_mismatch'} and 'result' not in orphan, orphan
            state['session'].close(); state['session'] = None
            open_session(setup)
            call('hello')
            stale = call('configure', dict(profile, request_control_graph=control), ok=False)
            assert stale['error'] == {'code': 'load_generation_mismatch'}, stale
            assert call('stage_load')['result']['stage_load_generation'] == 2
            reconfigured = call('configure', dict(profile, load_generation=2, request_control_graph=control))['result']
            newer = call('snapshot')['result']
            assert newer['observation']['details']['load_identity']['load_generation'] == 2
            # The old signed configured record against the newer signed snapshot.
            refused([configured, started, newer], 'live_phone_load_stale')
            # The new load is not the one the seed authorization, graph and proof name.
            refused([reconfigured, started, newer], 'live_phone_load_authority_mismatch')
            assert records == native_records and configured == native_records[0]
            # The seed's rows did not change, so its authority still names load 1:
            # only the phone's own signed count exposes the reload.
            assert route.phone_qualification_bindings()['node-1']['load_authority'].load_generation == 1
            # Current-authority control on the real chain: the applied row is withdrawn.
            from mycelium_seed import route_assignment as ra
            with ra._connect(built.database) as c:
                c.execute('UPDATE seed_route_assignments SET withdrawn=1')
            try: route.phone_qualification_bindings()
            except ControllerError as e: assert e.code == 'phone_applied_assignment_required', e.code
            else: raise AssertionError('withdrawn assignment still produced load authority')
            close_session()
            TRANSCRIPT.append({'qualification_consumed_native_records': True, 'physical_qualification': False,
                'desktop_records': 'synthetic_test_doubles', 'route_ready_published_for_native_load': True,
                'load_proof_digest': expected, 'native_generation_laundering_refused': 'load_generation_mismatch',
                'native_stale_profile_refused': 'load_generation_mismatch',
                'running_session_ended_by_stage_replacement': replaced, 'host_stale_record_refused': 'live_phone_load_stale',
                'native_signature_tamper_rejected': 'live_signature_invalid',
                'build_receipt': 'checked-out source commit; placeholder APK file, not an APK'})
        finally:
            if state['session'] is not None: state['session'].close()
            route.close()


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
            if mode == 'qualification':
                return qualification(fixture, ready, socket, secret, factory)
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
            raise AssertionError('unknown composition mode: ' + mode)
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
