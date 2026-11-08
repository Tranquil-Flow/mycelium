# SPDX-License-Identifier: AGPL-3.0-only
"""Fenced staging sample publisher. Real signatures/formats, fictional metrics.

Run only in the next project's namespace. The CLI admits only loopback port 4390,
requires the flag and exact staging origin from /config, and verifies the banner
before creating any keys or sending writes. Never opens a database directly.
"""
import argparse
import json
import os
from pathlib import Path
import time
import urllib.request
from mycelium_swarm_directory import docs, stats_docs
from mycelium_swarm_directory.crypto import Ed25519Signer
from mycelium_a13_macos.run_model import CHOICES
from mycelium_a13_macos.model_resources import model_shape

NAMES = ('Violet Lantern', 'Copper Orchard', 'Frost Spiral', 'Teal Constellation',
         'Ember Halo', 'Golden Spores', 'Aurora Grove', 'Twin Moons', 'Crystal Cap', 'Ink Garden')
MEMBERS = (2, 3, 5, 7, 4, 9, 6, 8, 3, 2)
STATUSES = ('serving',)*7 + ('warming', 'offline', 'offline')


def metrics(index, members, at):
    # Deliberately synthetic display fixtures, not measurements or benchmarks.
    metric = lambda a, b: {'p50': a, 'p90': b, 'n': 12 + index}
    layers = model_shape(CHOICES[index % len(CHOICES)].model).num_layers
    return {'window_seconds':300,'window_limit':50,'observed_seconds':300,
        'requests_24h':87+index*31,'coverage_seconds':21600,'serving_since':at-21600,
        'uptime_seconds':21600,'mode':'normal','ttft_ms':metric(900+index*170,1500+index*200),
        'decode_millitokens_s':metric(4200+index*1350,6000+index*1400),
        'request_ms':metric(15000+index*1200,24000+index*1500),'stage_window_seconds':300,
        'stages':[{'index':j,'layer_start':j*layers//members,'layer_end':(j+1)*layers//members,
                   'compute_ms':metric(20+index*4+j,35+index*6+j)} for j in range(members)],
        'hops':[{'from_stage':j,'to_stage':j+1,'kind':'activation_rtt','rtt_ms':8+index*3+j,
                 'samples':24,'age_seconds':0} for j in range(members-1)]}


def publish(state, get, post, at=None, remove=False):
    at = int(time.time()) if at is None else at
    state = Path(state)
    manifest = state/'manifest.json'
    if remove and not manifest.exists():
        return []
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    if manifest.exists():
        rows = json.loads(manifest.read_text())
    else:
        rows = [{'id':docs.new_swarm_id(),'index':i} for i in range(len(NAMES))]
        with manifest.open('x') as f: json.dump(rows,f)
    existing = {s['swarm_id']:s for s in get('/swarms')['swarms']}
    for row in rows:
        i,sid = row['index'],row['id']
        assert 0 <= i < len(NAMES) and docs.SWARM_ID.fullmatch(sid)
        signer = Ed25519Signer.load_or_create(state/(sid+'.key'))
        current = existing.get(sid)
        if current:
            assert current['record']['owner_key'] == signer.public_key
            assert current['record']['name'] == 'Sample: '+NAMES[i]
        if remove:
            if current: post('/delete', docs.delete(signer,sid,current['record']['seq']+1))
            continue
        model = CHOICES[i % len(CHOICES)].model.model_id
        if not current:
            post('/records',docs.swarm_record(signer,swarm_id=sid,seq=1,name='Sample: '+NAMES[i],model=model,
                description='Fictional staging swarm. Simulated performance, not a benchmark. No model is running; sample chat is unavailable.',
                access_mode='open',entry={'origin':f'https://sample-{i+1}.invalid','key':signer.public_key}))
        post('/heartbeat',docs.heartbeat(signer,sid,status=STATUSES[i],members=MEMBERS[i],served_model=model,at=at))
        post('/stats',stats_docs.stats(signer,sid,at=at,values=metrics(i,MEMBERS[i],at)))
    after = {s['swarm_id'] for s in get('/swarms')['swarms']}
    expected = {r['id'] for r in rows}
    assert not (after & expected) if remove else expected <= after
    return rows


def main():
    p=argparse.ArgumentParser();p.add_argument('--state',default='/samples');p.add_argument('--remove',action='store_true');p.add_argument('--once',action='store_true');a=p.parse_args()
    if os.environ.get('MYCELIUM_STAGING_SAMPLES') != '1' or os.environ.get('PUBLIC_HOST') != 'next.mycelium.now':
        raise SystemExit('Refusing: staging flag and exact next host are required')
    base='http://127.0.0.1:4390'
    def get(path):
        with urllib.request.urlopen(base+'/swarms/api/v1'+path,timeout=10) as r:return json.load(r)
    def post(path,doc):
        req=urllib.request.Request(base+'/swarms/api/v1'+path,data=json.dumps(doc).encode(),headers={'Content-Type':'application/json','X-Mycelium-Swarms':'1'})
        with urllib.request.urlopen(req,timeout=10) as r:return json.load(r)
    assert get('/config')['directory'] == 'https://next.mycelium.now'
    with urllib.request.urlopen(base+'/',timeout=10) as r:assert 'Staging · sample data'.encode() in r.read()
    while True:
        rows=publish(a.state,get,post,remove=a.remove)
        print(json.dumps({'samples':len(rows),'removed':a.remove,'fictional':True}),flush=True)
        if a.once or a.remove:return
        time.sleep(65)  # <90s freshness, no overlapping 30-write/minute budget

if __name__=='__main__':main()
