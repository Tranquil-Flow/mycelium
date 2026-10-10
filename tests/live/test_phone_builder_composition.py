"""Local synthetic composition only: never physical, APK or source-build evidence."""
from dataclasses import replace
import json

import pytest

from scripts import build_qwen_live_route as builder
from scripts import qwen_phone_builder as phone
from mycelium_qualification.phone import phone_load_proof_digest
from test_qwen_phone_builder import evidence, synthetic_snapshot, admit


def test_graph_refuses_unsupported_phone_digest(evidence, synthetic_snapshot):
    e = admit(evidence)
    from mycelium_router.contracts import Placement, Stage, LayerRange, StageCost
    from types import SimpleNamespace
    placement = Placement(placement_id='p', node_id='node-1', replica_group_id='r',
        assignment_id=e.profile['assignment_id'], stage_signature='s', load_proof_digest='wrong',
        runtime_backend=phone.BACKEND, runtime_endpoint='iroh://node-1/a')
    stage = Stage(stage_id='stage-001', layer_range=LayerRange(0,2,2), component_roles=(),
                  stage_cost=StageCost(1,1,1), placements=(placement,))
    # Use the production graph binder against an actual immutable graph shell.
    from test_router_inprocess_mesh import three_device_graph
    graph = replace(three_device_graph(), stages=(stage,))
    with pytest.raises(ValueError, match="phone_load_provenance_unsupported"):
        phone.bind_phone_graph(graph, {"node-1":e})
    assert graph.stages[0].placements[0].load_proof_digest == "wrong"


def test_external_control_only_emits_safe_existing_secret_path(tmp_path):
    secret = tmp_path/'bootstrap'
    secret.write_bytes(b'x'*32)
    secret.chmod(0o600)
    descriptor = dict(socket_path=str(tmp_path/'control.sock'), bootstrap_secret_file=str(secret),
                      controller_endpoint_id='cd'*32)
    assert phone.external_control_descriptor(descriptor) == descriptor
    assert 'x'*32 not in json.dumps(descriptor)
    secret.chmod(0o644)
    with pytest.raises(Exception, match='external_control_binding_invalid'):
        phone.external_control_descriptor(descriptor)


def test_actual_build_with_real_native_fixture_executor(tmp_path, record_property, monkeypatch):
    import subprocess
    original = subprocess.run
    def fixture_ssh(command, **kw):
        # ONLY remote identity/preflight is simulated. All preparation, desktop
        # loads, Rust execution, authority and plan emission are production code.
        if command[0] == 'ssh':
            return subprocess.CompletedProcess(command, 0, json.dumps(['host-fixture-remote','boot-fixture-remote']), '')
        return original(command, **kw)
    monkeypatch.setattr(subprocess, 'run', fixture_ssh)
    from phone_composition_fixture import fixture_build, build_packaging_fixture
    fixture = fixture_build(tmp_path)
    with pytest.raises(ValueError, match="phone_load_provenance_unsupported"):
        builder.build(fixture.args)
    assert not (fixture.args.output_root/"operator-plan.json").exists()
    # Preserve the refused partial output; use a distinct B2-only artifact root.
    fixture.args.output_root = tmp_path/"packaging-only"
    # Separately retain B2 packaging mechanics; not affirmative phone admission.
    report = build_packaging_fixture(fixture.args, monkeypatch)
    plan = json.loads(__import__('pathlib').Path(report['operator_plan']).read_text())
    phone_node = plan['controller']['run_plan']['nodes'][1]
    assert set(phone_node) == {'node_id','configure'}
    assert plan['controller']['external_control'] == fixture.descriptor
    graph = plan['controller']['run_plan']['nodes'][0]['configure']['graph']
    from test_phone_live_authority import legacy_phone_digest
    expected = legacy_phone_digest(fixture.document['configured']['result']['observation'])
    assert graph['stages'][1]['placements'][0]['load_proof_digest'] == expected
    checkpoint=json.loads((fixture.args.output_root/'local-challenge-checkpoint.json').read_text())
    assert len(checkpoint['phone_challenges']['node-1']['steps']) == 1+len(report['challenge_output_token_ids'])
    assert checkpoint['assignments'][1]['load_proof_digest'] == expected
    assert report['route_ready'] is False
    from mycelium_physical_runner.config import load_operator_plan
    assert load_operator_plan(report['operator_plan']).controller['external_control'] == fixture.descriptor
    from mycelium_live.route import PhysicalLiveRoute
    route=PhysicalLiveRoute.from_operator_plan(report['operator_plan'],seed_state_root=fixture.args.phone_seed_state_root)
    assert route._external_session_factory is not None
    route.close()
    # Execute the actual transferred source entrypoint with archive-only imports.
    import sys
    result=subprocess.run([sys.executable,str(fixture.args.output_root/'transfer-bundle/physical_inference_node.py'),'--help'],
                          cwd=tmp_path,capture_output=True,text=True,timeout=30)
    assert result.returncode == 0, result.stderr

    from phone_composition_fixture import qualify_fixture_plan
    from mycelium_qualification import LiveRouteQualificationError
    with pytest.raises(LiveRouteQualificationError, match="phone_load_provenance_unsupported"):
        qualify_fixture_plan(fixture,plan,checkpoint)
    record_property('fixture_only_report',json.dumps(report))
    record_property('native_steps',json.dumps(checkpoint['phone_challenges']['node-1']['steps']))
    # A previously valid evidence file must not survive a mid-build withdrawal.
    from mycelium_seed import route_assignment as ra
    copy_runtime = builder._copy_runtime_closure
    def withdraw_during_build(*args):
        copy_runtime(*args)
        with ra._connect(fixture.database) as connection:
            connection.execute('UPDATE seed_route_assignments SET withdrawn=1')
    monkeypatch.setattr(builder, '_copy_runtime_closure', withdraw_during_build)
    fixture.args.output_root = tmp_path/'refused-after-withdrawal'
    with pytest.raises(Exception, match='phone_applied_assignment_required'):
        build_packaging_fixture(fixture.args, monkeypatch)
    assert not (fixture.args.output_root/'operator-plan.json').exists()
