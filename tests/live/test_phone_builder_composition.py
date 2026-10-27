# SPDX-License-Identifier: AGPL-3.0-only
"""Local synthetic composition only: never physical, APK or source-build evidence."""
from dataclasses import replace
import json

import pytest

from scripts import build_qwen_live_route as builder
from scripts import qwen_phone_builder as phone
from mycelium_qualification.phone import phone_load_proof_digest
from test_qwen_phone_builder import evidence, synthetic_snapshot, admit


def test_graph_binder_replaces_the_placement_digest_with_the_v2_identity(evidence, synthetic_snapshot):
    e = admit(evidence)
    from mycelium_router.contracts import Placement, Stage, LayerRange, StageCost
    placement = Placement(placement_id='p', node_id='node-1', replica_group_id='r',
        assignment_id=e.profile['assignment_id'], stage_signature='s', load_proof_digest='wrong',
        runtime_backend=phone.BACKEND, runtime_endpoint='iroh://node-1/a')
    stage = Stage(stage_id='stage-001', layer_range=LayerRange(0,2,2), component_roles=(),
                  stage_cost=StageCost(1,1,1), placements=(placement,))
    # Use the production graph binder against an actual immutable graph shell.
    from test_router_inprocess_mesh import three_device_graph
    graph = replace(three_device_graph(), stages=(stage,))
    bound = phone.bind_phone_graph(graph, {"node-1":e})
    assert bound.stages[0].placements[0].load_proof_digest == phone_load_proof_digest(e.observations[0])
    assert graph.stages[0].placements[0].load_proof_digest == "wrong"
    assert phone.bind_phone_graph(graph, {}) is graph


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


def _route(plan_path, fixture):
    from mycelium_live.route import PhysicalLiveRoute
    from tests.live.token_path_test_support import ObserverTokenOwner
    return PhysicalLiveRoute.from_operator_plan(plan_path,
        seed_state_root=fixture.args.phone_seed_state_root,
        token_path_owner=ObserverTokenOwner())


def _refused(action, code):
    from physical_inference_qualification import ControllerError
    with pytest.raises(ControllerError) as refused:
        action()
    assert refused.value.code == code, refused.value.code


SEED_ATTACKS = [
    ('withdrawn', "UPDATE seed_route_assignments SET withdrawn=1", 'phone_applied_assignment_required'),
    ('expired', "UPDATE seed_route_assignments SET expires_at=issued_at", 'phone_applied_assignment_required'),
    ('unapplied', "UPDATE seed_route_assignments SET applied_at=NULL, applied_generation=NULL", 'phone_applied_assignment_required'),
    ('stale_apply', "UPDATE seed_route_assignments SET applied_at=applied_at-601", 'phone_applied_assignment_required'),
    ('applied_under_old_generation', "UPDATE seed_route_assignments SET applied_generation=applied_generation+1", 'phone_applied_assignment_required'),
    ('reissued_same_assignment', "UPDATE seed_route_assignments SET issued_at={after_authorization}", 'phone_applied_assignment_reissued'),
    ('replaced_by_new_assignment_id', "UPDATE seed_route_assignments SET assignment_id='replacement-B'", 'phone_applied_assignment_id_mismatch'),
    ('preparation_dropped', "DELETE FROM seed_route_preparations", 'phone_preparation_binding_required'),
    ('route_generation_changed', "UPDATE seed_route_assignments SET route_generation=route_generation+1", 'phone_preparation_binding_mismatch'),
    ('pack_changed', "UPDATE seed_route_assignments SET stage_pack_digest='sha256:" + "0"*64 + "'", 'phone_applied_manifest_mismatch'),
    ('controller_withdrawn', "UPDATE seed_route_assignments SET controller_endpoint_ids_json='[]'", 'phone_controller_not_authorized'),
    ('member_revoked', "UPDATE seed_members SET lifecycle_state='STOPPED' WHERE node_id='node-1'", 'seed_member_revoked'),
    ('member_lease_expired', "UPDATE seed_members SET lease_expires_at=0 WHERE node_id='node-1'", 'seed_member_lease_expired'),
    ('member_generation', "UPDATE seed_members SET generation=generation+1 WHERE node_id='node-1'", 'phone_authority_identity_mismatch'),
    ('member_incarnation', "UPDATE seed_members SET incarnation='inc-restarted' WHERE node_id='node-1'", 'phone_authority_identity_mismatch'),
    ('member_key', "UPDATE seed_members SET verification_key_digest='sha256:" + "0"*64 + "' WHERE node_id='node-1'", 'phone_applied_assignment_required'),
    ('member_and_row_rekeyed', "UPDATE seed_members SET verification_key_digest='sha256:" + "0"*64 + "' WHERE node_id='node-1';"
        "UPDATE seed_route_assignments SET verification_key_digest='sha256:" + "0"*64 + "'", 'membership_key_pin_mismatch'),
]


def test_actual_build_with_real_native_fixture_executor(native_phone_executor_required, tmp_path, record_property, monkeypatch):
    """Builder proof -> graph/checkpoint digest -> production route authority -> final qualifier.

    The phone records are signed by the fixture's SYNTHETIC member key in the
    native v2 shape; the real native process is exercised by the real
    composition, not here. Everything else is unpatched production code.
    """
    import copy
    import subprocess
    original = subprocess.run
    def fixture_ssh(command, **kw):
        # ONLY remote identity/preflight is simulated. All preparation, desktop
        # loads, Rust execution, authority and plan emission are production code.
        if command[0] == 'ssh':
            return subprocess.CompletedProcess(command, 0, json.dumps(['host-fixture-remote','boot-fixture-remote']), '')
        return original(command, **kw)
    monkeypatch.setattr(subprocess, 'run', fixture_ssh)
    from pathlib import Path
    from phone_composition_fixture import (
        SYNTHETIC_LOAD, elapse_keepalive_interval, fixture_build, members_heartbeat_during_builds,
        qualify_fixture_plan, restoring, synthetic_route_open)
    from mycelium_qualification import LiveRouteQualificationError
    from mycelium_seed import route_assignment as ra
    fixture = fixture_build(tmp_path)
    preparation = copy.deepcopy(fixture.document)
    # The outcome must not depend on host speed: each build below runs as if
    # one seed keepalive interval had passed since the members last heartbeated
    # (a loaded host took ~34 s here and the final build refused with
    # seed_member_liveness_stale instead of the withdrawal's code). Live
    # members heartbeat during the build, as they do in a real swarm.
    members_heartbeat_during_builds(fixture, monkeypatch)
    elapse_keepalive_interval(fixture)
    report = builder.build(fixture.args)
    assert report['route_ready'] is False
    plan_path = Path(report['operator_plan'])
    plan = json.loads(plan_path.read_text())
    phone_node = plan['controller']['run_plan']['nodes'][1]
    assert set(phone_node) == {'node_id','configure'}
    assert phone_node['configure']['load_generation'] == SYNTHETIC_LOAD
    assert plan['controller']['external_control'] == fixture.descriptor
    graph = plan['controller']['run_plan']['nodes'][0]['configure']['graph']
    expected = phone_load_proof_digest(fixture.document['configured']['result']['observation'])
    assert graph['stages'][1]['placements'][0]['load_proof_digest'] == expected
    checkpoint=json.loads((fixture.args.output_root/'local-challenge-checkpoint.json').read_text())
    assert len(checkpoint['phone_challenges']['node-1']['steps']) == 1+len(report['challenge_output_token_ids'])
    assert checkpoint['assignments'][1]['load_proof_digest'] == expected
    control = fixture.args.output_root/'transfer-bundle/control'
    proof = json.loads((control/'node-1-load-proof.json').read_text())
    assert proof['protocol'] == phone.PROOF_PROTOCOL
    # Original signed envelopes survive packaging byte-for-byte.
    for name in ('configured','snapshot','authorization','manifest'):
        assert proof[name] == preparation[name] == fixture.document[name]
    binding = proof['preparation_binding']
    assert binding['route_generation'] == 1 != SYNTHETIC_LOAD and binding['provenance'] == phone.PREPARATION_PROVENANCE
    offers = {o['message']['recipient_node_id']: o['message'] for o in plan['controller']['membership_snapshot']['assignment_offers']}
    assert offers['node-1']['load_generation'] == SYNTHETIC_LOAD
    from mycelium_physical_runner.config import load_operator_plan
    assert load_operator_plan(report['operator_plan']).controller['external_control'] == fixture.descriptor
    # Execute the actual transferred source entrypoint with archive-only imports.
    import sys
    result=subprocess.run([sys.executable,str(fixture.args.output_root/'transfer-bundle/physical_inference_node.py'),'--help'],
                          cwd=tmp_path,capture_output=True,text=True,timeout=30)
    assert result.returncode == 0, result.stderr

    # Positive: current seed authority + packaged proof + records of this route open.
    route = _route(plan_path, fixture)
    try:
        assert route._external_session_factory is not None
        bindings = route.phone_qualification_bindings()
        authority = bindings['node-1']['load_authority']
        assert (authority.load_proof_digest, authority.load_generation, authority.assignment_id) == (
            expected, SYNTHETIC_LOAD, phone_node['configure']['assignment_id'])
        records = synthetic_route_open(fixture, plan)
        untouched = copy.deepcopy(records)
        record = qualify_fixture_plan(fixture, plan, checkpoint, bindings=bindings, phone_records=records)
        assert record.route_ready is True and records == untouched
        assert [b.load_proof_digest for b in record.stage_bindings if b.node_id == 'node-1'] == [expected]
        # Long after the 600 s preparation window the same identity still matches,
        # because freshness is the current seed rows' and not the old statement's.
        restore = restoring(fixture.database)
        later = fixture.document['authorization']['message']['expires_at'] + 1200
        with ra._connect(fixture.database) as c:
            c.execute('UPDATE seed_route_assignments SET applied_at=?', (later-10,))
            c.execute('UPDATE seed_members SET lease_expires_at=?', (later+600,))
        with monkeypatch.context() as m:
            import time as clock
            m.setattr(clock, 'time', lambda: later)
            assert route.phone_qualification_bindings()['node-1']['load_authority'].load_proof_digest == expected
            # ...and at that later time a stale application is still refused.
            with ra._connect(fixture.database) as c:
                c.execute('UPDATE seed_route_assignments SET applied_at=?', (later-601,))
            _refused(route.phone_qualification_bindings, 'phone_applied_assignment_required')
        restore()

        def refuse_final(mutate, code):
            with pytest.raises(LiveRouteQualificationError) as refused:
                qualify_fixture_plan(fixture, plan, checkpoint, bindings=bindings,
                                     phone_records=synthetic_route_open(fixture, plan, mutate=mutate))
            assert str(refused.value) == code, str(refused.value)
        def configured(field, value):
            def mutate(obs):
                if obs['event'] == 'configured': obs['details'][field] = value
            return mutate
        def snapshot(field, value):
            def mutate(obs):
                if obs['event'] == 'snapshot': obs['details']['load_identity'][field] = value
            return mutate
        # Native record of this open vs. real current authority (member-signed, so not a signature refusal).
        for field, value in [('phone_stage_pack_digest','sha256:'+'0'*64), ('golden_output_digest','sha256:'+'0'*64),
                             ('runtime_build_digest','sha256:'+'0'*64), ('load_generation',SYNTHETIC_LOAD+1),
                             ('manifest_digest','sha256:'+'0'*64), ('assignment_id','replacement-B')]:
            refuse_final(configured(field, value), 'live_phone_load_authority_mismatch')
        refuse_final(snapshot('load_generation', SYNTHETIC_LOAD+1), 'live_phone_load_stale')
        refuse_final(snapshot('assignment_id', 'replacement-B'), 'live_phone_load_stale')
        def v1(obs):
            if obs['event'] == 'configured':
                from mycelium_qualification.phone import LOAD_IDENTITY_PROVENANCE_V1
                obs['details']['load_identity_provenance'] = copy.deepcopy(LOAD_IDENTITY_PROVENANCE_V1)
        refuse_final(v1, 'phone_load_provenance_unsupported')
        tampered = synthetic_route_open(fixture, plan)
        tampered[0]['observation']['details']['load_generation'] += 1
        with pytest.raises(LiveRouteQualificationError) as refused:
            qualify_fixture_plan(fixture, plan, checkpoint, bindings=bindings, phone_records=tampered)
        assert str(refused.value) == 'live_signature_invalid'
        # The plan's configure copy alone (the retired binding) is not authority.
        with pytest.raises(LiveRouteQualificationError) as refused:
            qualify_fixture_plan(fixture, plan, checkpoint, phone_records=synthetic_route_open(fixture, plan),
                bindings={'node-1': {k: v for k, v in bindings['node-1'].items() if k != 'load_authority'}})
        assert str(refused.value) == 'live_phone_load_authority_required'

        # Renewal on the SAME open route: every seed change since preparation refuses,
        # and restoring the rows restores exactly the same authority.
        seen = {}
        for name, sql, code in SEED_ATTACKS:
            with ra._connect(fixture.database) as c:
                for statement_sql in sql.format(after_authorization=repr(
                        fixture.document['authorization']['message']['issued_at'] + 0.001)).split(';'):
                    c.execute(statement_sql)
            try:
                _refused(route.phone_qualification_bindings, code)
                # A freshly loaded plan refreshes its membership offers from the
                # current seed; that refresh must not launder the same change.
                try: reloaded = _route(plan_path, fixture)
                except Exception as exc: seen[name] = 'route_load_refused:' + str(exc)
                else:
                    # The refreshed route holds the seed's NEW member facts, so its
                    # refusal may name a later check; it must still be a refusal.
                    from physical_inference_qualification import ControllerError
                    try:
                        with pytest.raises(ControllerError) as refreshed:
                            reloaded.phone_qualification_bindings()
                    finally: reloaded.close()
                    seen[name] = refreshed.value.code
            finally:
                restore()
            assert route.phone_qualification_bindings()['node-1']['load_authority'].load_proof_digest == expected
        record_property('seed_controls', json.dumps(seen, sort_keys=True))
        assert set(seen) == {name for name, _sql, _code in SEED_ATTACKS}
    finally:
        route.close()

    # Persisted and reloaded plans: an identical copy is accepted; changed claims are not.
    def reloaded_plan(name, edit):
        document = json.loads(plan_path.read_text())
        edit(document)
        path = tmp_path/name
        path.write_text(json.dumps(document))
        return path
    same = _route(reloaded_plan('reloaded.json', lambda d: None), fixture)
    try: assert same.phone_qualification_bindings()['node-1']['load_authority'].load_proof_digest == expected
    finally: same.close()
    def claim(field, value):
        return lambda d: d['controller']['run_plan']['nodes'][1]['configure'].__setitem__(field, value)
    for field, value in [('load_generation', SYNTHETIC_LOAD+1), ('manifest_digest', 'sha256:'+'0'*64),
                         ('pack_digest', 'sha256:'+'0'*64)]:
        changed = _route(reloaded_plan('changed-'+field+'.json', claim(field, value)), fixture)
        try: _refused(changed.phone_qualification_bindings, 'phone_configure_mismatch')
        finally: changed.close()
    changed = _route(reloaded_plan('changed-assignment.json', claim('assignment_id', 'replacement-B')), fixture)
    try: _refused(changed.phone_qualification_bindings, 'phone_applied_assignment_id_mismatch')
    finally: changed.close()
    # Unexpected extra plan fields never reach the authority: the closed plan refuses.
    extra = reloaded_plan('extra.json', lambda d: d['controller']['run_plan']['nodes'][1].update(
        load_authority=dict(verified=True)))
    with pytest.raises(Exception, match='run_plan_external_node_invalid'):
        _route(extra, fixture)
    with pytest.raises(Exception, match='plan_field_invalid'):
        load_operator_plan(extra)
    # Missing or corrupt packaged proof: no fallback to the plan's configure.
    def drop(document):
        files = document['controller']['transfer_manifest']['files']
        files[:] = [f for f in files if f['path'] != 'control/node-1-load-proof.json']
    unlisted = None
    try: unlisted = _route(reloaded_plan('unlisted.json', drop), fixture)
    except Exception as exc: record_property('unlisted_proof_route_load', type(exc).__name__ + ':' + str(exc))
    if unlisted is not None:
        try: _refused(unlisted.phone_qualification_bindings, 'phone_packaged_proof_missing')
        finally: unlisted.close()
    packaged = control/'node-1-load-proof.json'
    original_bytes = packaged.read_bytes()
    for name, damage in [('corrupt', lambda: packaged.write_bytes(original_bytes.replace(b'"route_ready":false', b'"route_ready":true ', 1))),
                         ('missing', packaged.unlink)]:
        current = _route(plan_path, fixture)
        try:
            damage()
            _refused(current.phone_qualification_bindings, 'phone_packaged_proof_corrupt')
        finally:
            packaged.write_bytes(original_bytes)
            current.close()
    for other in ('node-1-assignment.json', 'model-manifest.json'):
        target = control/other
        kept = target.read_bytes()
        current = _route(plan_path, fixture)
        try:
            target.write_bytes(kept + b' ')
            _refused(current.phone_qualification_bindings, 'phone_packaged_proof_corrupt')
        finally:
            target.write_bytes(kept)
            current.close()

    record_property('fixture_only_report',json.dumps(report))
    record_property('native_steps',json.dumps(checkpoint['phone_challenges']['node-1']['steps']))
    # A previously valid evidence file must not survive a mid-build withdrawal.
    copy_runtime = builder._copy_runtime_closure
    def withdraw_during_build(*args):
        copy_runtime(*args)
        with ra._connect(fixture.database) as connection:
            connection.execute('UPDATE seed_route_assignments SET withdrawn=1')
    monkeypatch.setattr(builder, '_copy_runtime_closure', withdraw_during_build)
    fixture.args.output_root = tmp_path/'refused-after-withdrawal'
    elapse_keepalive_interval(fixture)
    with pytest.raises(Exception, match='phone_applied_assignment_required'):
        builder.build(fixture.args)
    assert not (fixture.args.output_root/'operator-plan.json').exists()
