# SPDX-License-Identifier: AGPL-3.0-only
"""Execute shipped UI JavaScript with fixture API/DOM, not a browser/hardware claim."""
import json
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[2] / 'mycelium_a13_macos/web'
HARNESS = r"""
const vm = require('vm'), fs = require('fs');
const nodes = {};
const el = () => ({addEventListener(){}, append(){}, replaceChildren(){}, scrollIntoView(){},
  textContent:'', value:'', disabled:false, dataset:{}, classList:{add(){}}});
const ctx = {document:{getElementById(id){return nodes[id] ||= el()}, createElement:el},
  setInterval(){}, setTimeout, console,
  fetch:async () => ({ok:true,json:async () => JSON.parse(process.argv[2])})};
vm.createContext(ctx); vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), ctx);
setImmediate(() => process.stdout.write(JSON.stringify(nodes)));
"""


def render(name, status):
    node = shutil.which('node')
    assert node, 'node is required for shipped UI execution'
    result = subprocess.run([node, '-e', HARNESS, str(ROOT / name), json.dumps(status)],
                            capture_output=True, text=True, timeout=10, check=True)
    return json.loads(result.stdout)


@pytest.mark.parametrize('state,rollover,phrase', [
    ('stopping', 'switching', 'continuing automatically'),
    ('failing', 'switching', 'continuing automatically'),
    ('running', 'backoff', 'retrying after a failed restart'),
    ('serving', 'idle', 'runs continue automatically'),
])
def test_owner_displays_switch_and_stop_stays_enabled(state, rollover, phrase):
    output = render('run.js', {'installation_role':'owner', 'authority':{'swarm_created':True},
        'owner_run':{'state':state, 'rollover':{'state':rollover}, 'run_id':'r', 'steps':[]}})
    assert phrase in output['demo-state']['textContent']
    assert output['demo-start']['disabled']
    assert not output['demo-stop']['disabled']


def test_chat_displays_switch_instead_of_manual_start():
    output = render('chat.js', {'available':False, 'switching_runs':True})
    assert 'Chat will return automatically' in output['state']['textContent']
    assert output['send']['disabled']


def test_failed_rollover_is_clear_and_manual_start_is_available():
    output = render('run.js', {'installation_role':'owner', 'authority':{'swarm_created':True},
        'owner_run':{'state':'failed','error_code':'rollover_failed','failed_step':'compile',
                     'steps':[{'name':'compile','label':'Compile'}], 'rollover':{'state':'failed'}}})
    assert 'Automatic continuation stopped after repeated failures' in output['demo-state']['textContent']
    assert not output['demo-start']['disabled']
