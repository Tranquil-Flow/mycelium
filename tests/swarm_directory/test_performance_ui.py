# SPDX-License-Identifier: AGPL-3.0-only
"""Execute the production renderer under Node-vm; no external resources."""
import json
from pathlib import Path
import shutil
import subprocess

import pytest
from tests.swarm_directory.test_performance_stats import payload

WEB = Path(__file__).resolve().parents[2] / 'mycelium_swarm_directory/web'
HARNESS = r"""
const fs=require('fs'), vm=require('vm');
function node(tag) { return {tag, children:[], textContent:'', attrs:{},
  append(...c){this.children.push(...c);},setAttribute(k,v){this.attrs[k]=v;},addEventListener(){}}; }
function text(n){return typeof n==='string'?n:[n.textContent,...n.children.map(text)].join(' ');}
const ctx=vm.createContext({document:{createElement:node},TextEncoder,console,Date});
vm.runInContext(fs.readFileSync(process.argv[1],'utf8').replace(/main\(\);\s*$/, ''),ctx);
ctx.s=JSON.parse(process.argv[2]);
const result=vm.runInContext('({speed:stat(s,"reported_speed"),ttft:stat(s,"ttft"),hop:stat(s,"hop"),detail:renderPerformance(s)})',ctx);
result.detail=text(result.detail);console.log(JSON.stringify(result));
"""


def render(stats):
    node = shutil.which('node')
    assert node, 'Node required for renderer qualification'
    result = subprocess.run([node, '-e', HARNESS, str(WEB/'swarms.js'), json.dumps({
        'swarm_id':'test','status':'serving','stats':stats})], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_reported_stats_and_missing_hops_have_honest_labels():
    out = render({'values':payload(),'stale':False,'age_seconds':0,'loaded_at':4102444800,'updated_at':1800000000})
    assert out['speed'] == '12.5 tok/s'
    assert out['ttft'] == '1.20 s'
    assert out['hop'] == 'Not measured'
    for copy in ('p50', 'p90', 'Samples', 'self-reported', 'Mac 1', '0–23', 'fast', '24 h', '2', 'not measured'):
        assert copy in out['detail']


def test_missing_stale_and_zero_samples_are_not_live_zeroes():
    assert render(None)['speed'] == '—'
    stale = render({'values':payload(),'stale':True,'age_seconds':200,'updated_at':1800000000})
    assert stale['speed'] == 'Stale' and 'Stale' in stale['detail']
    empty = payload(); empty['decode_millitokens_s'] = {'p50':None,'p90':None,'n':0}
    assert render({'values':empty,'stale':False,'age_seconds':0,'loaded_at':4102444800})['speed'] == '—'


def test_measured_hop_rtts_name_transport_vs_control_probe():
    for kind, origin, expected in [('activation_rtt',0,'Activation transport RTT'),
                                    ('owner_control_rtt_probe',None,'Network round trip (probe)')]:
        values = payload()
        values['hops'] = [{'from_stage':origin,'to_stage':1,'kind':kind,
                           'rtt_ms':125,'samples':3,'age_seconds':2}]
        out = render({'values':values,'stale':False,'age_seconds':0,'loaded_at':4102444800})
        assert out['hop'] == '125 ms'
        assert expected in out['detail'] and '125 ms' in out['detail']
        if origin is None:
            assert 'Owner control' in out['detail'] and 'polling' in out['detail']


def test_landing_aggregates_only_fresh_serving_reports():
    harness = r"""
    const vm=require('vm'),fs=require('fs');const els={};
    const document={getElementById(id){return els[id] ||= {textContent:'—',href:'https://demo.example.org'};}};
    const stats={stale:false,values:{decode_millitokens_s:{p50:12500,n:2},requests_24h:7,coverage_seconds:100}};
    const swarms=[{status:'serving',members:2,stats},{status:'offline',members:3,stats},
      {status:'serving',members:1,stats:{...stats,stale:true}}];
    const ctx=vm.createContext({document,URL,AbortSignal,fetch:async()=>({ok:true,json:async()=>({swarms})})});
    (async()=>{await vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),ctx);console.log(JSON.stringify(els));})();
    """
    done=subprocess.run([shutil.which('node'),'-e',harness,str(WEB/'apex.js')],capture_output=True,text=True,timeout=15)
    assert done.returncode == 0, done.stderr
    out=json.loads(done.stdout)
    assert out['fastest-speed']['textContent'] == '12.5'
    assert out['served-count']['textContent'] == '7'
    assert '1 reporting swarm' in out['stats-coverage']['textContent']
