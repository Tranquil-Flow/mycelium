# SPDX-License-Identifier: AGPL-3.0-only
"""Execute relay consent/status copy in shipped app.js; fake DOM/API, no browser."""
import json
import subprocess

import pytest

from tests.a13_macos.test_swarm_ux_copy import NODE, WEB, needs_node, status

LIFETIME = ('The temporary relay keeps running while your swarm is serving '
            '(renewed every 30 minutes, at most 24 hours); it stops when the swarm stops.')
HARNESS = r"""
const fs=require('fs'), vm=require('vm'), input=JSON.parse(process.argv[2]);
const nodes={}, confirmations=[], calls=[];
function el(id){return {id,textContent:'',value:'',checked:false,dataset:{},children:[],listeners:{},
 addEventListener(t,f){this.listeners[t]=f},append(...c){this.children.push(...c)},
 replaceChildren(...c){this.children=c},setAttribute(){}}}
const document={getElementById(id){return nodes[id] ||= el(id)},createElement:el,
 querySelectorAll(){return []},addEventListener(){}};
const window={addEventListener(){},confirm(text){confirmations.push(text);return input.approve}};
const fetch=async(path,opts)=>{if(opts.method==='POST')calls.push({path,body:JSON.parse(opts.body)});
 return {ok:true,json:async()=>input.status}};
const ctx=vm.createContext({document,window,fetch,setInterval(){},console});
(async()=>{
 vm.runInContext(fs.readFileSync(process.argv[1]+'/app.js','utf8'),ctx);
 await new Promise(setImmediate);
 if(input.action==='create'){
  document.getElementById('create-with-relay').checked=true;
  await nodes['create-form'].listeners.submit({preventDefault(){},submitter:{id:'create'}});
 }else if(input.action==='start'){nodes['relay-start'].listeners.click()}
 await new Promise(setImmediate);
 function text(node){return [node.textContent,...node.children.map(text)].join(' ')}
 console.log(JSON.stringify({confirmations,calls,ladder:text(nodes.ladder)}));
})().catch(e=>{console.error(e);process.exitCode=1});
"""


def run_page(**extra):
    snapshot = status()
    snapshot['network'] = extra.pop('network', snapshot['network'])
    result = subprocess.run([NODE, '-e', HARNESS, str(WEB), json.dumps({'status': snapshot, **extra})],
                            capture_output=True, text=True, timeout=15, check=True)
    return json.loads(result.stdout)


@needs_node
@pytest.mark.parametrize('action', ['create', 'start'])
@pytest.mark.parametrize('approve', [False, True])
def test_quick_relay_consent_explains_renewal_and_preserves_payload(action, approve):
    output = run_page(action=action, approve=approve)
    assert len(output['confirmations']) == 1
    assert LIFETIME in output['confirmations'][0]
    assert 'web chat prompts and replies' in output['confirmations'][0]
    if not approve:
        assert output['calls'] == []
    else:
        assert len(output['calls']) == 1
        call = output['calls'][0]
        assert call['path'] == ('/api/create' if action == 'create' else '/api/network/start')
        assert call['body']['relay_acknowledged'] is True and call['body']['max_seconds'] == 1800
        assert not {'auto_renew', 'long_lived', 'max_renewed_seconds'} & call['body'].keys()


@needs_node
@pytest.mark.parametrize('state,stopped,needle', [
    ('relay_process_running', None, 'renews while your swarm is serving'),
    ('relay_process_running', 'relay_renewal_cap_reached', '24-hour renewal cap reached'),
    ('stopped', 'relay_renewal_cap_reached', 'Temporary relay stopped'),
])
def test_quick_relay_status_explains_lifetime_and_cap(state, stopped, needle):
    output = run_page(network={'provider': 'cloudflare', 'state': state, 'auto_renew': True,
                              'long_lived': False, 'renewals': 3, 'expires_in_seconds': 42,
                              'renewal_cap_seconds': 86400, 'renewal_stopped': stopped})
    assert needle in output['ladder']
    if state != 'stopped':
        assert '42 seconds' in output['ladder']
        if stopped is None:
            assert 'at most 24 hours' in output['ladder']
            assert '3 renewals' in output['ladder']
