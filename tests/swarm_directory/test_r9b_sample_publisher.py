"""Real signature validation in an isolated directory; no external effects."""
import json
from scripts.staging_samples import publish, NAMES, MEMBERS, STATUSES
from mycelium_swarm_directory.crypto import Ed25519Signer, verify
from mycelium_swarm_directory import docs
from mycelium_swarm_directory.directory import Directory
from mycelium_swarm_directory.store import MemoryStore

def test_signed_samples_refresh_and_remove_only_their_own_records(tmp_path):
    clock=[1900000000]
    d=Directory(MemoryStore(),verify,clock=lambda:clock[0])
    def get(path):
        status,value=d.handle('GET',path,b'','test'); assert status==200,value; return value
    def post(path,value):
        status,body=d.handle('POST',path,json.dumps(value).encode(),'test'); assert status==200,body; return body
    real=Ed25519Signer.generate();sid=docs.new_swarm_id()
    post('/records',docs.swarm_record(real,swarm_id=sid,seq=1,name='Retained fixture',model='Qwen/Qwen3-0.6B'))
    clock[0]+=65
    rows=publish(tmp_path,get,post,at=clock[0])
    assert len(rows)==len(NAMES)==10
    swarms=get('/swarms')['swarms']; samples=[s for s in swarms if s['swarm_id']!=sid]
    assert len(samples)==10
    assert {s['access_mode'] for s in samples}=={'open','request','invite'}
    from mycelium_a13_macos.run_model import CHOICES
    assert {s['model'] for s in samples}=={c.model.model_id for c in CHOICES}
    for s in samples:
        if '0.5B' in s['model'] or '0.6B' in s['model']: assert s['members']==2 or s['status']=='offline'
        elif '14B' in s['model']: assert 3<=s['members']<=4 or s['status']=='offline'
        else: assert 2<=s['members']<=3 or s['status']=='offline'
    assert all(s['entry_origin'].endswith('.invalid') and s['name'].startswith('Sample: ') for s in samples)
    assert {s['status'] for s in samples}==set(STATUSES)
    assert {s['members'] for s in samples if s['status']!='offline'}<=set(MEMBERS)
    clock[0]+=65
    assert publish(tmp_path,get,post,at=clock[0])==rows
    clock[0]+=65
    publish(tmp_path,get,post,remove=True)
    assert [s['swarm_id'] for s in get('/swarms')['swarms']]==[sid]
