# SPDX-License-Identifier: AGPL-3.0-only
"""Discriminating B1/B3 controls using disposable authority, never live seed state."""
import copy
import json
import sqlite3
import threading
import time

import pytest
from mycelium_seed import phone_preparation as pp
from mycelium_seed import route_assignment as ra
from test_phone_preparation import preparation
from test_qwen_phone_builder import evidence, synthetic_snapshot


def authority_rows(database):
    with ra._connect(database) as c:
        return {r[0]: list(c.execute('SELECT * FROM '+r[0]+' ORDER BY 1'))
                for r in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'seed_%'")}


def test_replaced_assignment_cannot_authorize_old_signed_load(preparation):
    swarm, kw = preparation
    configured = kw['document']['configured']['result']['observation']['details']['assignment_id']
    # Positive same-ID control, then B differs ONLY in current assignment ID.
    with ra._connect(swarm.database) as c:
        assert c.execute('SELECT assignment_id FROM seed_route_assignments').fetchone()[0] == configured
    pp.issue_builder_authorization(swarm.database.parent, clock=lambda:swarm.clock[0], **kw)
    with ra._connect(swarm.database) as c:
        c.execute("UPDATE seed_route_assignments SET assignment_id='replacement-B'")
    before = authority_rows(swarm.database)
    with pytest.raises(Exception, match='phone_applied_assignment_id_mismatch'):
        pp.issue_builder_authorization(swarm.database.parent, clock=lambda:swarm.clock[0], **kw)
    assert authority_rows(swarm.database) == before


@pytest.mark.parametrize('boundary,code', [('heartbeat','seed_member_liveness_stale'),
    ('lease','seed_member_lease_expired'), ('applied','phone_applied_assignment_required')])
@pytest.mark.parametrize('hold', ['lock', 'verifier'])
def test_freshness_at_acquisition_and_release(preparation, monkeypatch, boundary, code, hold):
    swarm, kw = preparation
    now = swarm.clock[0]
    clock = [now]
    monkeypatch.setattr(pp.time, 'time', lambda: clock[0])
    configured = kw['document']['configured']['result']['observation']['details']['assignment_id']
    with ra._connect(swarm.database) as c:
        c.execute('UPDATE seed_route_assignments SET assignment_id=?, applied_at=?, expires_at=?', (configured,now,now+2000))
        c.execute('UPDATE seed_members SET lease_expires_at=?, next_heartbeat_due_at=?', (now+2000,now+2000))
        if boundary == 'heartbeat': c.execute('UPDATE seed_members SET next_heartbeat_due_at=?',(now+1,))
        elif boundary == 'lease': c.execute('UPDATE seed_members SET lease_expires_at=?',(now+1,))
        else: c.execute('UPDATE seed_route_assignments SET applied_at=?',(now-599,))
    pp.issue_builder_authorization(swarm.database.parent, **kw)  # on-time control
    entered, release = threading.Event(), threading.Event()
    if hold == 'lock':
        loaded = pp._load_authority(swarm.database.parent, incarnation='lock-fixture')
        monkeypatch.setattr(pp, '_load_authority', lambda *a,**k: loaded)
        original_connect = ra._connect
        def traced(*a,**k):
            c=original_connect(*a,**k)
            c.set_trace_callback(lambda sql: entered.set() if sql == 'BEGIN IMMEDIATE' else None)
            return c
        monkeypatch.setattr(ra, '_connect', traced)
        lock = sqlite3.connect(swarm.database, isolation_level=None)
        lock.execute('BEGIN IMMEDIATE')
    else:
        from scripts import qwen_phone_builder as phone
        original = phone.PhoneEvidence
        def verifying(*a, **k):
            value = original(*a, **k)
            entered.set()
            assert release.wait(5)
            return value
        monkeypatch.setattr(phone, 'PhoneEvidence', verifying)
    result = []
    def work():
        try: result.append(pp.issue_builder_authorization(swarm.database.parent, **kw))
        except Exception as exc: result.append(exc)
    before = authority_rows(swarm.database)
    worker = threading.Thread(target=work)
    worker.start()
    try:
        assert entered.wait(5)
        clock[0] = now+2
    finally:
        if hold == 'lock': lock.execute('ROLLBACK'); lock.close()
        release.set()
        worker.join(8)
    assert not worker.is_alive()
    assert len(result)==1 and isinstance(result[0],Exception) and code in str(result[0]), result
    assert authority_rows(swarm.database) == before


@pytest.mark.parametrize('field,value,code',[
    # The manifest digest is a controller claim: only the seed-compiled assignment binds it.
    ('manifest_digest','sha256:'+'b'*64,'phone_preparation_binding_mismatch'),
    # A configured record re-signed for another count disagrees with the same
    # session's signed snapshot of the applied load.
    ('load_generation',18,'phone_load_stale')])
def test_controller_claims_require_independent_issuance_binding(preparation,field,value,code):
    swarm,kw=preparation
    result=kw['document']['configured']['result']
    result['observation']['details'][field]=value
    result['signature']=swarm.nodes['node-1'].signer.sign(result['observation'])
    before=authority_rows(swarm.database)
    with pytest.raises(Exception,match=code):
        pp.issue_builder_authorization(swarm.database.parent,clock=lambda:swarm.clock[0],**kw)
    assert authority_rows(swarm.database)==before


def test_seed_route_generation_is_not_the_load_generation(preparation):
    """Generation-domain confusion: route 17, membership generation and load 3 all differ."""
    swarm,kw=preparation
    details=kw['document']['configured']['result']['observation']['details']
    with ra._connect(swarm.database) as c:
        route_generation,applied_generation=c.execute('SELECT route_generation,applied_generation FROM seed_route_assignments').fetchone()
    assert route_generation == 17 and details['load_generation'] == 3 != applied_generation
    envelope=pp.issue_builder_authorization(swarm.database.parent,clock=lambda:swarm.clock[0],**kw)
    statement=envelope['message']
    assert statement['configure']['load_generation'] == 3
    assert statement['preparation_binding']['route_generation'] == 17
    assert 'not_native_observation' not in statement['preparation_binding']['provenance']
    # The retired echoing producer cannot be authorized whatever number it carries,
    # including one made equal to the seed's route generation.
    from mycelium_qualification.phone import LOAD_IDENTITY_PROVENANCE_V1
    for name in ('configured','snapshot'):
        result=kw['document'][name]['result']; d=result['observation']['details']
        if name == 'configured': d.update(load_generation=17,load_identity_provenance=dict(LOAD_IDENTITY_PROVENANCE_V1))
        else: d['load_identity']['load_generation']=17; d['configured_load_generation']=17
        result['signature']=swarm.nodes['node-1'].signer.sign(result['observation'])
    before=authority_rows(swarm.database)
    with pytest.raises(Exception,match='phone_load_provenance_unsupported'):
        pp.issue_builder_authorization(swarm.database.parent,clock=lambda:swarm.clock[0],**kw)
    assert authority_rows(swarm.database)==before


@pytest.mark.parametrize('attack',['id','node','range','runtime','tensor','missing-binding'])
def test_preparation_issuance_validates_before_authority_write(preparation,attack):
    swarm,kw=preparation
    with ra._connect(swarm.database) as c:
        assignment=json.loads(c.execute('SELECT assignment_json FROM seed_route_preparations').fetchone()[0])
    if attack=='missing-binding':
        with ra._connect(swarm.database) as c:c.execute('DELETE FROM seed_route_preparations')
        before=authority_rows(swarm.database)
        with pytest.raises(Exception,match='phone_preparation_binding_required'):
            pp.issue_builder_authorization(swarm.database.parent,clock=lambda:swarm.clock[0],**kw)
    else:
        if attack=='id':assignment['assignment_id']='not-deterministic'
        elif attack=='node':assignment['node_id']='another-node'
        elif attack=='range':assignment['range']['end_layer_exclusive']=1
        elif attack=='runtime':assignment['runtime']['backend']='numpy'
        else:assignment['expected_tensor_keys']=[]
        if attack!='id':
            from layer_assignment import assignment_id_for
            assignment['assignment_id']=assignment_id_for(assignment)
        before=authority_rows(swarm.database)
        with pytest.raises(Exception,match='phone_preparation_assignment_invalid'):
            ra.issue_route_assignment(swarm.database,node_id='node-1',stage_pack=kw['document']['manifest'],
                peer_endpoint_id='ef'*32,route_generation=17,preparation_assignment=assignment,
                seed_key_record=swarm.coordinator.signer.public_key_record(),controller_endpoint_ids=['cd'*32],
                artifact_sources=[dict(endpoint_id='ef'*32,verification_key_digest=swarm.nodes['node-1'].signer.verification_key_digest)])
    assert authority_rows(swarm.database)==before
