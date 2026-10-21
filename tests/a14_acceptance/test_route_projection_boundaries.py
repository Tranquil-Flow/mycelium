# SPDX-License-Identifier: AGPL-3.0-only
import json
from pathlib import Path
import runpy
import pytest
from mycelium_product_spine.contracts import validate_product_snapshot
from mycelium_product_spine.route_explorer import digest, project_route_explorer, validate_route_explorer
ROOT=Path(__file__).resolve().parents[2]
fixture=runpy.run_path(str(ROOT/'tests/a14_acceptance/conformance.py'))['conformance_snapshot']
inputs=runpy.run_path(str(ROOT/'tests/a14_acceptance/conformance.py'))['inputs']


def test_public_authorities_keep_namespaces_status_and_transition_facts_separate():
    s=fixture(); p=s['route_explorer']
    assert len(p['nodes'])==3
    assert {n['kind'] for n in p['nodes']}=={'member','endpoint'}
    selected=[v for v in p['segments'] if v['path_status']=='selected']
    assert len(selected)==1 and selected[0]['transport']=='unknown'
    assert all(v is None for k,v in selected[0]['metrics'].items() if k!='measured_zero')
    observed=[v for v in p['segments'] if v['path_status']=='observed']
    assert [v['transport'] for v in observed]==['direct','relay']
    assert [v['connection_generation'] for v in observed]==[1,2]
    assert [v['history'] for v in observed]==[True,False]
    assert all(v['source'] is None and v['phase']=='unknown' for v in observed)
    assert all(v['binding']['request'] is None for v in observed)
    assert all(n['region']=='unknown' for n in p['nodes'])
    assert 'RAW-ENDPOINT-CANARY' not in json.dumps(s)
    assert '192.0.2.23' not in json.dumps(s)


def test_transition_ids_do_not_change_on_refresh_or_new_publication():
    s=fixture(); old=s['route_explorer']
    s['publication']['generation']+=1
    s['publication']['cursor']+=1
    s['publication']['published_at_unix_ms']+=1000
    refreshed=project_route_explorer(s)
    assert refreshed['segments']==old['segments']
    assert refreshed['digest']!=old['digest']


@pytest.mark.parametrize('field', ['latitude','longitude','hostname','endpoint_id','prompt','output','token_ids','kv','private_path'])
@pytest.mark.parametrize('level',['projection','node','segment','metrics','binding'])
def test_nested_closed_shape_rejects_private_extra_fields_even_with_recomputed_digest(field,level):
    p=fixture()['route_explorer']
    target={'projection':p,'node':p['nodes'][0],'segment':p['segments'][0],
            'metrics':p['segments'][0]['metrics'],'binding':p['segments'][0]['binding']}[level]
    target[field]='CANARY'
    p['digest']=digest({k:v for k,v in p.items() if k!='digest'})
    with pytest.raises(ValueError): validate_route_explorer(p)


@pytest.mark.parametrize('case',['node_duplicate','segment_duplicate','unbound_endpoint','region','negative_generation','false_zero','unknown_metric','overlong_nodes','overlong_segments','binding_raw','mode_mismatch'])
def test_semantic_rejection(case):
    p=fixture()['route_explorer'];s=p['segments'][0]
    if case=='node_duplicate':p['nodes'].append(p['nodes'][0])
    if case=='segment_duplicate':p['segments'].append(s)
    if case=='unbound_endpoint':s['source']='sha256:'+'f'*64
    if case=='region':p['nodes'][0]['region']='us-east-1'
    if case=='negative_generation':s['binding']['path_generation']=-1
    if case=='false_zero':p['segments'][1]['metrics']['measured_zero']=False
    if case=='unknown_metric':s['metrics']['rtt_ms']=0
    if case=='overlong_nodes':p['nodes']*=4097
    if case=='overlong_segments':p['segments']*=4097
    if case=='binding_raw':s['binding']['request']='private-request'
    if case=='mode_mismatch':s['provenance']='live'
    p['digest']=digest({k:v for k,v in p.items() if k!='digest'})
    with pytest.raises(ValueError):validate_route_explorer(p)


def test_product_validator_rejects_rehashed_unbound_projection():
    s=fixture();s['route_explorer']['segments'][0]['phase']='decode_forward'
    p=s['route_explorer'];p['digest']=digest({k:v for k,v in p.items() if k!='digest'})
    with pytest.raises(ValueError):validate_product_snapshot(s)


def test_legacy_product_remains_readable_without_an_invented_explorer():
    s=fixture();s.pop('route_explorer')
    assert 'route_explorer' not in validate_product_snapshot(s)


def test_aging_source_freshness_does_not_rewrite_immutable_observation_identity():
    s=fixture(); old=s['route_explorer']
    for a in s['internet_native']['activation_history']: a['freshness']='stale'
    aged=project_route_explorer(s)
    assert [v['id'] for v in aged['segments']]==[v['id'] for v in old['segments']]
    assert aged['segments'][1]['freshness']=='stale'


def test_unbound_a8_observation_cannot_inherit_selected_route_transport():
    """Production projector consumes A8 unknown without borrowing a selected edge's class."""
    from mycelium_product_spine.projector import ProductProjector
    members, route, internet = inputs()
    missing = internet['activation_history'][-1]
    missing['path_class'] = 'unknown'
    missing['path_source'] = 'unknown'
    missing['metrics'] = {k: False if k == 'measured_zero' else None for k in missing['metrics']}
    internet['activation_observation'] = missing
    s = ProductProjector(pseudonym_salt=b'c' * 32).project(
        members=members, route_status=route, qualification=None,
        internet_native=internet, now_unix_ms=1000000, source_mode='fixture')
    p = s['route_explorer']
    assert [v['transport'] for v in p['segments']] == ['unknown', 'direct', 'unknown']
    assert [v['path_status'] for v in p['segments']] == ['selected', 'observed', 'unknown']
    assert p['segments'][-1]['connection_generation'] == 2
    assert p['segments'][-1]['source'] is None
    assert all(v is None for k, v in p['segments'][-1]['metrics'].items() if k != 'measured_zero')
    assert validate_product_snapshot(s) == s


def test_forged_unbound_direct_rejected_by_real_a8_validator():
    from mycelium_internet.contracts import InternetContractError, validate_activation_observation
    _, _, internet = inputs()
    forged = internet['activation_history'][-1]
    forged['path_source'] = 'unknown'
    with pytest.raises(InternetContractError):
        validate_activation_observation(forged)


@pytest.mark.parametrize('field',['source','destination'])
def test_invalid_endpoint_type_is_a_contract_rejection(field):
    p=fixture()['route_explorer'];p['segments'][0][field]=[]
    with pytest.raises(ValueError,match='route_explorer_invalid'):validate_route_explorer(p)
