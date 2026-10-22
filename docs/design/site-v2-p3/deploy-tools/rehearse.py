# SPDX-License-Identifier: AGPL-3.0-only
"""Real launchd rehearsal, exclusively test labels and copied state on loopback.

--stage is a prepared deployment; --output must be a NEW private directory.
Copies DB bytes without opening the live database in SQLite. No public HTTP,
model/relay/tunnel/watchdog processes or live launchd labels are contacted.
"""
import argparse
from contextlib import closing
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
    if not source.is_file() or source.is_symlink():
        raise ValueError('Source database must be an existing regular file')
    if target.exists() or target.resolve() == source.resolve():
        raise ValueError('Copy target must be new and separate from the source')
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
            snapshot_hashes = {p.name:hashlib.sha256(Path(str(target)+str(p)[len(str(source)):]).read_bytes()).hexdigest()
                               for p in sources if p.exists()}
            summary = database_summary(target)
            return dict(summary, source=str(source), copy=str(target),
                        source_metadata=before, snapshot_sha256=snapshot_hashes)
        time.sleep(.1)
    raise RuntimeError('Could not obtain a stable copied DB snapshot')


def database_summary(path):
    # Only the sandbox copy is opened in SQLite; keep source bytes private.
    with closing(sqlite3.connect('file:'+str(path)+'?mode=ro', uri=True)) as db:
        if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise ValueError('Copied database integrity failed')
        columns = [row[1] for row in db.execute('PRAGMA table_info(kv)')]
        if columns != ['tbl', 'k', 'v']:
            raise ValueError('Missing directory kv schema')
        rows = db.execute('SELECT tbl,k,v FROM kv ORDER BY tbl,k').fetchall()
        return {'integrity':'ok', 'row_counts':{'kv':len(rows)},
                'logical_sha256':hashlib.sha256(json.dumps(rows,separators=(',',':')).encode()).hexdigest()}


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def cleanup_jobs(labels, control, sleep=time.sleep):
    results = []
    for label in labels:
        row = {'label':label, 'absent':False}
        try:
            r = control('bootout',label)
            row.update(bootout_rc=r.returncode, bootout_stderr=r.stderr)
            for _ in range(100):
                q = control('print',label)
                if q.returncode == 113 and 'Could not find service' in q.stderr:
                    row['absent'] = True
                    break
                sleep(.1)
        except Exception as error:
            row['error'] = str(error)
        results.append(row)
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument('--stage',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args = parser.parse_args()
    source, root = args.stage.resolve(), args.output.resolve()
    sw = load(source/'switch.py')
    for path in (sys.executable, '/bin/launchctl', '/usr/bin/env', source/'switch.py', source/'http_verify.py'):
        sw.unquarantined(path, subprocess.run)
    config, _ = sw.preflight(source, Path.home(), subprocess.run, 'deploy')
    for name in ('switch.py','http_verify.py','rehearse.py'):
        require(name in config['staged_sha256'], 'Missing rehearsal source pin: '+name)
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
        require(label in labels and label.startswith('now.mycelium.test-'), 'Non-sandbox control refused')
        return subprocess.run(['/bin/launchctl',action,domain+'/'+label],capture_output=True,text=True,timeout=20)
    # Refuse to take over any already-existing test label.
    for label in labels:
        r = control('print',label)
        require(r.returncode == 113 and 'Could not find service' in r.stderr, 'Test label already exists or cannot be inspected')
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
            require(r.returncode == 0, 'Injected rollback HTTP failed: '+r.stderr+r.stdout)
        try:
            sw.switch(stage,home,'deploy',apply=True,verify=fail_candidate)
        except RuntimeError as error:
            require('injected post-start' in str(error), str(error))
            receipt = json.loads((stage/'last-operation.json').read_text())
            require(receipt.get('rollback_verified') is True, 'Injected recovery not verified')
            report['cycles'].append({'mode':'injected-failure','ok':True,'rollback_verified':True})
        else:
            raise AssertionError('Failure injection was bypassed')
        # Independently prove both final bindings and byte-exact rollback plists.
        for label in labels:
            r = control('print',label)
            require(r.returncode == 0 and 'state = running' in r.stdout, 'Final job not running: '+label)
            require('working directory = '+config['rollback']['path']+'\n' in r.stdout, 'Final binding mismatch: '+label)
            require((home/'Library/LaunchAgents'/(label+'.plist')).read_bytes() == (stage/(label+'.rollback.plist')).read_bytes(), 'Final plist mismatch: '+label)
        report['database_after'] = database_summary(state/'directory.sqlite3')
        require(report['database_after']['logical_sha256'] == report['database']['logical_sha256'], 'Copied database changed during read-only cycles')
        report['ok'] = True
    except BaseException as error:
        report['error'] = str(error)
        raise
    finally:
        report['cleanup'] = cleanup_jobs(labels, control)
        report['ok'] = report['ok'] and all(row['absent'] for row in report['cleanup'])
        (root/'rehearsal.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report,indent=2))
    return 0 if report['ok'] else 1


if __name__ == '__main__':
    sys.exit(main())
