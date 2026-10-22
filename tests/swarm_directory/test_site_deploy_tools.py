# SPDX-License-Identifier: AGPL-3.0-only
"""Deploy tooling qualification: scratch plists + recording runner, real loopback HTTP.

Never execute launchctl or touch the actual user's LaunchAgents/state/database.
"""
import hashlib
import importlib.util
import json
from pathlib import Path
import plistlib
import shutil
import subprocess
import threading

import pytest

ROOT = Path(__file__).resolve().parents[2]
TOOLS = ROOT/'docs/design/site-v2-p3/deploy-tools'


def module(name):
    spec = importlib.util.spec_from_file_location('site_deploy_'+name, TOOLS/(name+'.py'))
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def sandbox(tmp_path):
    switch = module('switch')
    home, stage = tmp_path/'home', tmp_path/'stage'
    stage.mkdir(); (home/'Library/LaunchAgents').mkdir(parents=True)
    config = {'candidate':{'path':str(tmp_path/'candidate'),'sha':'c'*40},
              'rollback':{'path':str(tmp_path/'rollback'),'sha':'b'*40}, 'staged_sha256':{}}
    for label in switch.LABELS:
        old = {'Label':label, 'WorkingDirectory':config['rollback']['path'],
               'ProgramArguments':['/sandbox/python','-m','example','--db','/sandbox/unchanged.sqlite'] +
                                  (['--redirect-root-to-swarms'] if label == switch.LABELS[1] else []),
               'EnvironmentVariables':{'EXAMPLE':'preserved'}, 'KeepAlive':True}
        for suffix,doc in [('.rollback.plist',old),('.plist',switch.candidate_doc(old,config['candidate']['path']))]:
            path=stage/(label+suffix); path.write_bytes(plistlib.dumps(doc))
            config['staged_sha256'][path.name]=hashlib.sha256(path.read_bytes()).hexdigest()
        (home/'Library/LaunchAgents'/(label+'.plist')).write_bytes(plistlib.dumps(old))
    (stage/'deployment.json').write_text(json.dumps(config))
    calls=[]
    loaded=set(switch.LABELS)
    def runner(command, **kwargs):
        calls.append(command)
        if command[0]=='/usr/bin/xattr':
            return subprocess.CompletedProcess(command,1,'','No such xattr: com.apple.quarantine')
        if command[0]=='/usr/bin/git':
            stdout = next(v['sha'] for k,v in config.items() if k in ('candidate','rollback') and v['path']==command[2]) if 'rev-parse' in command else ''
            return subprocess.CompletedProcess(command,0,stdout,'')
        if command[:2]==['/bin/launchctl','print']:
            label=command[2].split('/')[-1]
            if label not in loaded:
                return subprocess.CompletedProcess(command,113,'','Could not find service "'+label+'"')
            doc=plistlib.loads((home/'Library/LaunchAgents'/(label+'.plist')).read_bytes())
            return subprocess.CompletedProcess(command,0,'state = running\nworking directory = '+doc['WorkingDirectory'],'')
        assert command[0] == '/usr/bin/plutil' or command[:2] in (['/bin/launchctl','bootout'],['/bin/launchctl','bootstrap'])
        if command[:2]==['/bin/launchctl','bootout']: loaded.discard(command[-1].split('/')[-1])
        if command[:2]==['/bin/launchctl','bootstrap']: loaded.add(Path(command[-1]).stem)
        return subprocess.CompletedProcess(command,0,'','')
    return switch,home,stage,calls,runner


def test_switch_dry_run_has_no_launchd_or_plist_effect(sandbox):
    s,home,stage,calls,runner=sandbox
    before={p.name:p.read_bytes() for p in (home/'Library/LaunchAgents').iterdir()}
    for mode in ('deploy','rollback'):
        assert s.switch(stage,home,mode,run=runner)['dry_run'] is True
    assert not any(c[0]=='/bin/launchctl' for c in calls)
    assert before=={p.name:p.read_bytes() for p in (home/'Library/LaunchAgents').iterdir()}


def test_switch_promote_rollback_readbacks_and_only_allowed_changes(sandbox):
    s,home,stage,calls,runner=sandbox
    for mode in ('deploy','rollback'):
        result=s.switch(stage,home,mode,apply=True,run=runner,sleep=lambda _:None,uid=123)
        assert all(row['running'] for row in result['runtime'])
        suffix='.plist' if mode=='deploy' else '.rollback.plist'
        for label in s.LABELS:
            assert (home/'Library/LaunchAgents'/(label+'.plist')).read_bytes()==(stage/(label+suffix)).read_bytes()
    effects=[c for c in calls if c[0]=='/bin/launchctl']
    assert len(effects)==16
    assert {c[-1].split('/')[-1].removesuffix('.plist') for c in effects}==set(s.LABELS)


def test_failed_promotion_automatically_restores_both_jobs(sandbox):
    s,home,stage,calls,runner=sandbox
    failed=False
    def fail_once(command,**kwargs):
        nonlocal failed
        if command[:2]==['/bin/launchctl','bootstrap'] and command[-1].endswith(s.LABELS[1]+'.plist') and not failed:
            failed=True
            return subprocess.CompletedProcess(command,1,'','injected failure')
        return runner(command,**kwargs)
    with pytest.raises(RuntimeError,match='Command failed'):
        s.switch(stage,home,'deploy',apply=True,run=fail_once,sleep=lambda _:None)
    assert failed
    for label in s.LABELS:
        assert (home/'Library/LaunchAgents'/(label+'.plist')).read_bytes()==(stage/(label+'.rollback.plist')).read_bytes()


@pytest.mark.parametrize('fault',['staged','live','busy','quarantine','dirty'])
def test_preflight_refuses_without_stopping_either_job(sandbox,fault):
    s,home,stage,calls,runner=sandbox
    if fault=='staged': (stage/(s.LABELS[1]+'.plist')).write_text('corrupted')
    if fault=='live': (home/'Library/LaunchAgents'/(s.LABELS[1]+'.plist')).write_bytes(plistlib.dumps({'unexpected':'drift'}))
    if fault=='busy':
        (home/'mycelium-demo-run').mkdir(); (home/'mycelium-demo-run/CERT.busy').touch()
    def guarded(command,**kwargs):
        if fault=='quarantine' and command[0]=='/usr/bin/xattr': return subprocess.CompletedProcess(command,0,'quarantine','')
        if fault=='dirty' and command[0]=='/usr/bin/git' and 'status' in command: return subprocess.CompletedProcess(command,0,' M source.py','')
        return runner(command,**kwargs)
    with pytest.raises((RuntimeError,ValueError)):
        s.switch(stage,home,'deploy',apply=True,run=guarded,sleep=lambda _:None)
    assert not any(c[0]=='/bin/launchctl' for c in calls)


def test_disallowed_argument_change_is_refused_even_with_new_stage_hash(sandbox):
    s,home,stage,calls,runner=sandbox
    path=stage/(s.LABELS[1]+'.plist')
    doc=plistlib.loads(path.read_bytes());doc['ProgramArguments'].append('--enable-product-routes')
    path.write_bytes(plistlib.dumps(doc))
    config=json.loads((stage/'deployment.json').read_text())
    config['staged_sha256'][path.name]=hashlib.sha256(path.read_bytes()).hexdigest()
    (stage/'deployment.json').write_text(json.dumps(config))
    with pytest.raises(ValueError,match='Non-site configuration changed'):
        s.switch(stage,home,'deploy',apply=True,run=runner,sleep=lambda _:None)
    assert not any(c[0]=='/bin/launchctl' for c in calls)


def test_rollback_does_not_require_healthy_candidate_checkout(sandbox):
    s,home,stage,calls,runner=sandbox
    def damaged_candidate(command,**kwargs):
        if command[0]=='/usr/bin/git' and command[2].endswith('/candidate'):
            raise AssertionError('Rollback must not depend on the damaged candidate')
        return runner(command,**kwargs)
    assert not s.switch(stage,home,'rollback',apply=True,run=damaged_candidate,sleep=lambda _:None)['dry_run']


@pytest.fixture
def apex():
    from mycelium_swarm_directory.crypto import verify
    from mycelium_swarm_directory.directory import Directory
    from mycelium_swarm_directory.server import DirectoryServer
    from mycelium_swarm_directory.store import MemoryStore
    from mycelium_demo.public_origin_proxy import ProxyConfig, PublicOriginProxyServer
    directory=DirectoryServer(Directory(MemoryStore(),verify)).start()
    cfg=ProxyConfig(listen_host='127.0.0.1',listen_port=4350,upstream_host='127.0.0.1',upstream_port=1,
                    public_host='mycelium.now',capability='a'*64,directory_host='127.0.0.1',directory_port=directory.server_port)
    proxy=PublicOriginProxyServer(('127.0.0.1',0),cfg)
    thread=threading.Thread(target=proxy.serve_forever,daemon=True);thread.start()
    try: yield 'http://127.0.0.1:'+str(proxy.server_port)
    finally:
        proxy.shutdown();proxy.server_close();thread.join(5);directory.close()


def test_http_verifier_exercises_real_candidate_proxy_and_offline_directory(apex):
    result=module('http_verify').verify(ROOT,'candidate',apex,client_ip='203.0.113.7')
    assert result['ok'], result
    assert result['demo_listings']==[]  # no false prerequisite that a demo must be online
    assert len(result['guards'])==12


def test_http_verifier_rejects_wrong_root_and_asset_bytes(apex,tmp_path):
    verifier=module('http_verify')
    result=verifier.verify(ROOT,'rollback',apex,client_ip='203.0.113.7')
    assert not result['ok'] and 'legacy redirect' in result['error']
    shutil.copytree(ROOT/'mycelium_swarm_directory/web',tmp_path/'mycelium_swarm_directory/web')
    (tmp_path/'mycelium_swarm_directory/web/site.css').write_text('wrong bytes')
    result=verifier.verify(tmp_path,'candidate',apex,client_ip='203.0.113.7')
    assert not result['ok'] and 'Asset byte mismatch' in result['error']


def launchd_runner(sandbox, *, boot_error=3, bootstrap_fail=False):
    """Model asynchronous removal, unlike the original always-running fake."""
    s,home,stage,calls,base=sandbox
    jobs={label:'old' for label in s.LABELS}
    events=[]
    failed=False
    def run(command,**kwargs):
        nonlocal failed
        if command[0]!='/bin/launchctl': return base(command,**kwargs)
        action=command[1]; label=Path(command[-1]).name.removesuffix('.plist')
        events.append((action,label))
        if action=='bootout':
            if label not in jobs:
                return subprocess.CompletedProcess(command,boot_error,'',f'Boot-out failed: {boot_error}: No such process\n')
            jobs[label]='removing'
            return subprocess.CompletedProcess(command,0,'','')
        if action=='print':
            if jobs.get(label)=='removing':
                del jobs[label]
                return subprocess.CompletedProcess(command,0,'state = SIGTERMed','')
            if label not in jobs:
                return subprocess.CompletedProcess(command,113,'',f'Could not find service "{label}" in domain for user gui: 501')
            return base(command,**kwargs)
        assert action=='bootstrap'
        if label in jobs:
            return subprocess.CompletedProcess(command,5,'','Bootstrap failed: 5: Input/output error')
        if bootstrap_fail and not failed:
            failed=True
            return subprocess.CompletedProcess(command,5,'','injected bootstrap failure')
        jobs[label]='running'
        return subprocess.CompletedProcess(command,0,'','')
    return run,jobs,events


def test_async_bootout_waits_for_removal_before_bootstrap(sandbox):
    s,home,stage,_,_=sandbox
    run,jobs,events=launchd_runner(sandbox)
    assert s.switch(stage,home,'deploy',apply=True,run=run,sleep=lambda _:None)['runtime']
    assert jobs=={label:'running' for label in s.LABELS}


@pytest.mark.parametrize('boot_error',[3,5])
def test_absent_job_and_unexpected_bootout_error_still_restore_both(sandbox,boot_error):
    s,home,stage,_,_=sandbox
    run,jobs,events=launchd_runner(sandbox,boot_error=boot_error)
    jobs.clear()
    s.switch(stage,home,'rollback',apply=True,run=run,sleep=lambda _:None)
    assert jobs=={label:'running' for label in s.LABELS}
    for label in s.LABELS:
        assert (home/'Library/LaunchAgents'/(label+'.plist')).read_bytes()==(stage/(label+'.rollback.plist')).read_bytes()
    assert (stage/'last-operation.json').exists()


def test_http_failure_triggers_verified_rollback(sandbox):
    s,home,stage,_,_=sandbox
    run,jobs,events=launchd_runner(sandbox)
    checked=[]
    def verify(which):
        checked.append(which)
        if which=='deploy': raise RuntimeError('HTTP 502')
    with pytest.raises(RuntimeError,match='HTTP 502'):
        s.switch(stage,home,'deploy',apply=True,run=run,sleep=lambda _:None,verify=verify)
    assert checked==['deploy','rollback']
    assert set(jobs)==set(s.LABELS)


def test_rollback_is_allowed_during_certification(sandbox):
    s,home,stage,_,_=sandbox
    (home/'mycelium-demo-run').mkdir(); (home/'mycelium-demo-run/CERT.busy').touch()
    run,jobs,_=launchd_runner(sandbox)
    assert s.switch(stage,home,'rollback',apply=True,run=run,sleep=lambda _:None)['runtime']


def test_http_transport_failure_is_not_misreported_as_privacy_failure(monkeypatch):
    from mycelium_swarm_directory import surface_probe
    monkeypatch.setattr(surface_probe,'probe',lambda *a,**kw:{'verdict':'FAIL','checks':[{'name':'no Set-Cookie on /swarms/','status':502,'ok':False}]})
    result=module('http_verify').verify(ROOT,'candidate','http://127.0.0.1:1')
    assert not result['ok']
    assert result['failure_kind']=='availability'
    assert '502' in result['error']
