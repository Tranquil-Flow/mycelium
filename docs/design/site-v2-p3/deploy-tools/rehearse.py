# SPDX-License-Identifier: AGPL-3.0-only
"""Real launchd rehearsal, exclusively test labels and copied state on loopback.

--stage is a prepared deployment; --output must be a NEW private directory.
Copies DB bytes without opening the live database in SQLite. No public HTTP,
model/relay/tunnel/watchdog processes or live launchd labels are contacted.
"""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import plistlib
import shutil
import socket
import sqlite3
import subprocess
import sys
import time


def load(path):
    spec = importlib.util.spec_from_file_location('site_rehearsal_switch', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def copy_database(source, target):
    """Copy a stable DB+WAL byte snapshot; SQLite only ever opens the copy."""
    sources = [Path(str(source)+suffix) for suffix in ('','-wal')]
    def fingerprint():
        return [(p.exists(), p.stat().st_size if p.exists() else None,
                 p.stat().st_mtime_ns if p.exists() else None) for p in sources]
    for _ in range(20):
        before = fingerprint()
        for p in sources:
            out = Path(str(target)+str(p)[len(str(source)):])
            if p.exists():
                shutil.copyfile(p, out)
                out.chmod(0o600)
            elif out.exists():
                out.unlink()
        if before == fingerprint():
            with sqlite3.connect(target) as db:
                assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
                tables = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
                counts = {name:db.execute('SELECT COUNT(*) FROM "'+name.replace('"','""')+'"').fetchone()[0] for name in tables}
            return {'source':str(source),'copy':str(target),'integrity':'ok','row_counts':counts,
                    'source_metadata':before,'copy_sha256':hashlib.sha256(target.read_bytes()).hexdigest()}
        time.sleep(.1)
    raise RuntimeError('Could not obtain a stable copied DB snapshot')


def main():
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument('--stage',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args = parser.parse_args()
    source, root = args.stage.resolve(), args.output.resolve()
    sw = load(source/'switch.py')
    for path in (sys.executable, '/bin/launchctl', '/usr/bin/env', source/'switch.py', source/'http_verify.py'):
        sw.unquarantined(path, subprocess.run)
    config = json.loads((source/'deployment.json').read_text())
    root.mkdir(mode=0o700,parents=True,exist_ok=False)
    home, stage, state = root/'home', root/'stage', root/'state'
    (home/'Library/LaunchAgents').mkdir(parents=True)
    stage.mkdir(); state.mkdir()
    labels = tuple('now.mycelium.test-sitefix-'+str(os.getpid())+'.'+x.split('.')[-1] for x in sw.LABELS)
    domain = 'gui/'+str(os.getuid())
    # Reserve unique ports together while constructing the sandbox configuration.
    sockets = [socket.socket() for _ in range(3)]
    for sock in sockets: sock.bind(('127.0.0.1',0))
    directory_port, apex_port, unused_port = [sock.getsockname()[1] for sock in sockets]
    cap = state/'proxy.cap'; cap.write_text('a'*64+'\n'); cap.chmod(0o600)
    report = {'ok':False,'labels':labels,'ports':[directory_port,apex_port,unused_port],
              'candidate':config['candidate'],'rollback':config['rollback'],'cycles':[]}
    sandbox = {'candidate':config['candidate'],'rollback':config['rollback'],
               'labels':labels,'sandbox':True,'staged_sha256':{}}
    for label, original in zip(labels,sw.LABELS):
        doc = sw.load_plist((source/(original+'.rollback.plist')).read_bytes())
        doc['Label'] = label
        doc['EnvironmentVariables'] = dict(doc.get('EnvironmentVariables',{}),HOME=str(home))
        if 'MYCELIUM_SWARM_DIR_STATE_DIR' in doc['EnvironmentVariables']:
            doc['EnvironmentVariables']['MYCELIUM_SWARM_DIR_STATE_DIR'] = str(state)
        doc['StandardOutPath'] = str(root/(label+'.log'))
        doc['StandardErrorPath'] = str(root/(label+'.err.log'))
        argv = list(doc['ProgramArguments'])
        def replace(flag, value):
            argv[argv.index(flag)+1] = str(value)
        if label.endswith('.swarm-directory'):
            live_db = Path(argv[argv.index('--db')+1])
            report['database'] = copy_database(live_db, state/'directory.sqlite3')
            replace('--db',state/'directory.sqlite3'); replace('--port',directory_port)
        else:
            replace('--listen-port',apex_port)
            replace('--directory-upstream','http://127.0.0.1:'+str(directory_port))
            replace('--upstream','http://127.0.0.1:'+str(unused_port))
            replace('--trusted-proxy-capability-file',cap)
        doc['ProgramArguments'] = argv
        for suffix, document in [('.rollback.plist',doc),('.plist',sw.candidate_doc(doc,config['candidate']['path']))]:
            path = stage/(label+suffix); path.write_bytes(plistlib.dumps(document))
            sandbox['staged_sha256'][path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
        (home/'Library/LaunchAgents'/(label+'.plist')).write_bytes(plistlib.dumps(doc))
    for name in ('switch.py','http_verify.py'):
        shutil.copyfile(source/name,stage/name)
        sandbox['staged_sha256'][name] = hashlib.sha256((stage/name).read_bytes()).hexdigest()
    (stage/'deployment.json').write_text(json.dumps(sandbox,indent=2)+'\n')
    for sock in sockets: sock.close()
    def control(action,label):
        assert label in labels and label.startswith('now.mycelium.test-')
        return subprocess.run(['/bin/launchctl',action,domain+'/'+label],capture_output=True,text=True,timeout=20)
    # Refuse to take over any already-existing test label.
    for label in labels:
        r = control('print',label)
        assert r.returncode == 113 and 'Could not find service' in r.stderr
    def run(mode):
        receipt = sw.switch(stage,home,mode,apply=True)
        report['cycles'].append({'mode':mode,'ok':receipt.get('ok'),
                                 'rollback_verified':receipt.get('rollback_verified',False)})
        return receipt
    try:
        run('rollback')
        run('deploy')
        run('rollback')
        # Exercise the same real jobs and HTTP verification after a failed deploy.
        def fail_candidate(which):
            if which == 'deploy':
                raise RuntimeError('injected post-start candidate verification failure')
            r = subprocess.run([sys.executable,'-B',str(stage/'http_verify.py'),'rollback','--base',
                                'http://127.0.0.1:'+str(apex_port)],capture_output=True,text=True,timeout=180)
            (root/'injected-failure-rollback-http.json').write_text(r.stdout)
            assert r.returncode == 0, r.stderr+r.stdout
        try:
            sw.switch(stage,home,'deploy',apply=True,verify=fail_candidate)
        except RuntimeError as error:
            assert 'injected post-start' in str(error), str(error)
            receipt = json.loads((stage/'last-operation.json').read_text())
            assert receipt['rollback_verified'] is True
            report['cycles'].append({'mode':'injected-failure','ok':True,'rollback_verified':True})
        else:
            raise AssertionError('Failure injection was bypassed')
        # Independently prove both final bindings and byte-exact rollback plists.
        for label in labels:
            r = control('print',label)
            assert r.returncode == 0 and 'state = running' in r.stdout
            assert 'working directory = '+config['rollback']['path']+'\n' in r.stdout
            assert (home/'Library/LaunchAgents'/(label+'.plist')).read_bytes() == (stage/(label+'.rollback.plist')).read_bytes()
        report['ok'] = True
    finally:
        report['cleanup'] = []
        for label in labels:
            r = control('bootout',label)
            for _ in range(100):
                q = control('print',label)
                if q.returncode == 113 and 'Could not find service' in q.stderr: break
                time.sleep(.1)
            report['cleanup'].append({'label':label,'bootout_rc':r.returncode,'absent':q.returncode==113})
        report['ok'] = report['ok'] and all(row['absent'] for row in report['cleanup'])
        (root/'rehearsal.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report,indent=2))
    return 0 if report['ok'] else 1


if __name__ == '__main__':
    sys.exit(main())
