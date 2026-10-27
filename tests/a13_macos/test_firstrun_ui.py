# SPDX-License-Identifier: AGPL-3.0-only
"""Real run.js/app.js logic in Node vm; no browser, network or inference."""
import json
import subprocess
import pytest
from tests.a13_macos.test_swarm_ux_copy import WEB, NODE, needs_node, run_ui, status

HARNESS = r"""
const vm=require('vm'),fs=require('fs');
const input=JSON.parse(process.argv[2]), els={}, calls=[];
function el(id){return {id,textContent:'',value:'',checked:false,disabled:false,hidden:false,dataset:{},children:[],
 listeners:{},addEventListener(t,f){this.listeners[t]=f},replaceChildren(...c){this.children=c},append(...c){this.children.push(...c)},
 insertRow(){return {insertCell(){return el('cell')},append(){}}}}}
const document={getElementById(id){return els[id] ||= el(id)},createElement:el};
async function fetch(path,opts){
 if(opts.method==='POST') {calls.push({path,body:JSON.parse(opts.body),disabled:els['fast-mode'].disabled});
 return {ok:!input.error,json:async()=>input.error?{error:input.error}: {}};}
 return {ok:true,json:async()=>input.status};
}
const ctx=vm.createContext({document,fetch,setInterval(){},console});
(async()=>{
 vm.runInContext(fs.readFileSync(process.argv[1]+'/run.js','utf8'),ctx);
 await new Promise(setImmediate);
 if(input.change!==undefined){document.getElementById('fast-mode').checked=input.change;
   if(els['fast-mode'].listeners.change) await els['fast-mode'].listeners.change();
   await new Promise(setImmediate);}
 const out={calls,els:{}};
 for(const id of ['fast-mode','fast-mode-note','demo-state','run-model-note']) {
   const e=document.getElementById(id);out.els[id]={text:e.textContent,checked:e.checked,disabled:e.disabled};}
 console.log(JSON.stringify(out));
})().catch(e=>{console.error(e);process.exitCode=1});
"""


def run_page(**extra):
    s = {'installation_role': 'owner', 'authority': {'swarm_created': True},
         'fast_mode': {'enabled': False, 'source': 'default', 'locked': False},
         'owner_run': {'state': 'idle', 'steps': []}, 'join_flow': {},
         'run_model': {'selected': 'a9-route', 'locked': False, 'source': 'demo_default',
                       'model_id': 'Qwen/Qwen2.5-0.5B-Instruct', 'resolved_commit': '7ae557604adf', 'choices': []}}
    s.update(extra.pop('status', {}))
    result = subprocess.run([NODE, '-e', HARNESS, str(WEB), json.dumps({'status': s, **extra})],
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@needs_node
def test_fast_mode_change_posts_bool_and_disables_in_flight():
    result = run_page(change=True)
    assert result['calls'] == [{'path': '/api/fast-mode', 'body': {'enabled': True}, 'disabled': True}]


@needs_node
@pytest.mark.parametrize('state', ['running', 'serving', 'stopping', 'failing'])
def test_fast_mode_disabled_while_active(state):
    result = run_page(status={'owner_run': {'state': state, 'steps': []}})
    assert result['els']['fast-mode']['disabled']
    assert 'Stop' in result['els']['fast-mode-note']['text']


@needs_node
def test_fast_mode_locked_environment_and_saved_rendering():
    result = run_page(status={'fast_mode': {'enabled': True, 'source': 'environment', 'locked': True}})
    assert result['els']['fast-mode']['checked']
    assert result['els']['fast-mode']['disabled']
    assert 'MYCELIUM_TOKEN_PATH' in result['els']['fast-mode-note']['text']


@needs_node
def test_switch_refusal_remains_visible_after_status_refresh():
    result = run_page(change=True, error='fast_mode_run_active')
    assert 'Stop' in result['els']['fast-mode-note']['text']
    assert not result['els']['fast-mode']['checked']


ERRORS = {
 'two_macs_required': 'Invite',
 'member_capability_report_required': 'Report',
 'token_path_policy_expired': 'Start',
 'token_path_process_expired': 'Start',
 'managed_control_expired': 'Start',
}


@needs_node
@pytest.mark.parametrize('code,action', ERRORS.items())
def test_run_failure_is_actionable(code, action):
    result = run_page(status={'owner_run': {'state': 'failed', 'error_code': code, 'steps': []}})
    text = result['els']['demo-state']['text']
    assert action in text
    assert 'Action not completed:' not in text
    assert code in text


@needs_node
@pytest.mark.parametrize('code,action', ERRORS.items())
def test_app_action_error_is_actionable(code, action):
    result = run_ui({'perform': {'path': '/fixture'}, 'responses': {'/fixture': {'ok': False, 'body': {'error': code}}},
                     'read': ['notice']})
    assert action in result['text']['notice']


@needs_node
def test_small_model_download_copy_is_honest():
    result = run_ui({'status': status(label='Qwen2.5-0.5B'), 'read': ['acquisition-detail', 'consent-resource-hint']})
    assert 'whole' in result['text']['acquisition-detail']
    assert 'never download' not in result['text']['acquisition-detail']
    assert 'whole snapshot' in run_page()['els']['run-model-note']['text']


def test_static_first_run_copy_and_quit_paths():
    html = (WEB / 'index.html').read_text()
    assert 'id="fast-mode"' in html and 'Fast mode' in html
    assert 'no mid-answer recovery if a Mac drops' in html
    assert 'Two-Mac serving verified on Qwen2.5-0.5B-Instruct' in html
    assert 'Two-Mac serving not yet verified' not in html
    assert 'this application does not open a tunnel' not in html
    assert 'Quit demo' in html and 'menu Quit' in html
    assert '⌘Q' in html
    assert 'menu Quit is not supported' not in html


def test_ui_does_not_claim_publication_or_later_relay_are_impossible():
    html = (WEB / 'index.html').read_text()
    assert 'No payment or publication is performed by this local demo.' not in html
    assert 'you can add a public relay later' in html
