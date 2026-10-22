# SPDX-License-Identifier: AGPL-3.0-only
"""Two-job site promotion. Dry-run by default; --apply requires owner approval.

Wait for launchd removal, verify loopback HTTP, and recover both original jobs
on failure. --live adds public verification inside the recovery transaction.
"""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import subprocess
import sys
import time
import urllib.request

LABELS = ('now.mycelium.swarm-directory', 'now.mycelium.apex-proxy')


def load_plist(data):
    return plistlib.loads(re.sub(rb'<!--.*?-->', b'', data, flags=re.S))


def candidate_doc(old, release):
    doc = old.copy()
    doc['WorkingDirectory'] = str(release)
    if old['Label'].endswith('.apex-proxy'):
        doc['ProgramArguments'] = [
            '--no-redirect-root-to-swarms' if a == '--redirect-root-to-swarms' else a
            for a in old['ProgramArguments']]
    return doc


def checked(command, run):
    result = run(command, capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise RuntimeError('Command failed: ' + ' '.join(command[:2]) +
                           f' rc={result.returncode}: {result.stderr.strip()}')
    return result.stdout


def unquarantined(path, run):
    result = run(['/usr/bin/xattr', '-p', 'com.apple.quarantine', str(path)], capture_output=True, text=True)
    if result.returncode == 0 or 'No such xattr' not in result.stderr:
        raise RuntimeError('Quarantined or uninspectable executable: ' + str(path))


def preflight(stage, home, run, mode):
    cfg = json.loads((stage / 'deployment.json').read_text())
    labels = tuple(cfg.get('labels', LABELS))
    if labels != LABELS and (not cfg.get('sandbox') or home.resolve() == Path.home().resolve()
            or len(labels) != 2 or not all(re.fullmatch(r'now\.mycelium\.test-[a-z0-9-]+\.' + role, label)
                for label, role in zip(labels, ('swarm-directory', 'apex-proxy')))):
        raise ValueError('Invalid sandbox labels/home')
    # Check every staged file and BOTH jobs before any bootout or write.
    for filename, digest in cfg['staged_sha256'].items():
        if Path(filename).name != filename:
            raise ValueError('Invalid staged filename')
        if hashlib.sha256((stage / filename).read_bytes()).hexdigest() != digest:
            raise ValueError('Staged file drift: ' + filename)
    for executable in ('/bin/launchctl', '/usr/bin/plutil', '/usr/bin/git'):
        unquarantined(executable, run)
    for key in (('candidate', 'rollback') if mode == 'deploy' else ('rollback',)):
        release = cfg[key]
        if checked(['/usr/bin/git', '-C', release['path'], 'rev-parse', 'HEAD'], run).strip() != release['sha']:
            raise ValueError('Checkout HEAD mismatch: ' + key)
        if checked(['/usr/bin/git', '-C', release['path'], 'status', '--porcelain'], run).strip():
            raise ValueError('Dirty checkout: ' + key)
    pairs = {}
    for label in labels:
        old_path = stage / (label + '.rollback.plist')
        new_path = stage / (label + '.plist')
        old, new = load_plist(old_path.read_bytes()), load_plist(new_path.read_bytes())
        if old['Label'] != label or old['WorkingDirectory'] != cfg['rollback']['path']:
            raise ValueError('Unexpected rollback binding')
        if new != candidate_doc(old, cfg['candidate']['path']):
            raise ValueError('Non-site configuration changed')
        for path in (old_path, new_path):
            checked(['/usr/bin/plutil', '-lint', str(path)], run)
        argv = new['ProgramArguments']
        prefix = argv[:argv.index('-m')] if '-m' in argv else argv[:1]
        for arg in prefix:
            if arg.startswith('/'):
                unquarantined(arg, run)
        target = home / 'Library/LaunchAgents' / (label + '.plist')
        if load_plist(target.read_bytes()) not in (old, new):
            raise ValueError('Live plist drift: ' + label)
        pairs[label] = (old_path, new_path)
    return cfg, pairs


def switch(stage, home, mode, *, apply=False, run=subprocess.run, sleep=time.sleep, uid=None,
           verify=None, live=False):
    # One writer across prepared state directories, including emergency rollback.
    if apply:
        with (home/'Library/LaunchAgents/.mycelium-site-switch.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return _switch(stage, home, mode, apply=apply, run=run, sleep=sleep, uid=uid, verify=verify, live=live)
    return _switch(stage, home, mode, apply=apply, run=run, sleep=sleep, uid=uid, verify=verify, live=live)


def _switch(stage, home, mode, *, apply, run, sleep, uid, verify, live):
    if mode not in ('deploy', 'rollback'):
        raise ValueError('Invalid mode')
    cfg, pairs = preflight(stage, home, run, mode)
    plan = {'mode': mode, 'dry_run': not apply, 'candidate_sha': cfg['candidate']['sha'],
            'jobs': [{'label': label, 'working_directory': cfg['candidate' if mode == 'deploy' else 'rollback']['path']}
                     for label in pairs],
            'allowed_changes': ['WorkingDirectory', 'apex explicit root redirect opt-out'],
            'preserved': 'All other args, environment, ports, database and capability paths'}
    if not apply:
        return plan
    if mode == 'deploy' and any((home / 'mycelium-demo-run' / n).exists() for n in ('DEVICE.busy', 'CERT.busy')):
        raise RuntimeError('Device/certification busy; no job changed')
    domain = 'gui/' + str(os.getuid() if uid is None else uid)
    plan['commands'] = []
    journal = stage / (mode+'-operation-'+str(time.time_ns())+'.json')

    def save():
        data = json.dumps(plan, indent=2)+'\n'
        journal.write_text(data)
        (stage/'last-operation.json').write_text(data)

    def command(args):
        cmd = ['/bin/launchctl', *args]
        try:
            r = run(cmd, capture_output=True, text=True, timeout=20)
        except subprocess.TimeoutExpired as error:
            plan['commands'].append({'argv':cmd,'error':'timeout'})
            save()
            raise RuntimeError('launchctl timeout: '+args[0]) from error
        plan['commands'].append({'argv':cmd,'rc':r.returncode,'stdout':r.stdout,'stderr':r.stderr})
        save()
        return r

    def stop(label):
        r = command(['bootout',domain+'/'+label])
        # Successful bootout only schedules removal. ESRCH after removal is fine.
        # Reconcile any other error with observed registration, never its text alone.
        for _ in range(80):
            state = command(['print',domain+'/'+label])
            if state.returncode == 113 and 'Could not find service' in state.stderr:
                return
            if state.returncode:
                raise RuntimeError(f'Cannot inspect removal: {label}: {state.stderr}')
            sleep(.25)
        raise RuntimeError(f'Job did not unload: {label}; bootout rc={r.returncode}: {r.stderr}')

    def ready(doc):
        if run is not subprocess.run:
            return True  # injected launchd unit harness
        argv = doc['ProgramArguments']
        directory = doc['Label'].endswith('.swarm-directory')
        port = argv[argv.index('--port' if directory else '--listen-port')+1]
        path = '/swarms/api/v1/config' if directory else '/swarms/'
        request = urllib.request.Request('http://127.0.0.1:'+port+path,
                                         headers={'Cf-Connecting-Ip':'203.0.113.7'})
        try:
            with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=1) as response:
                return response.status == 200
        except Exception:
            return False

    def start(label, which, recovery=False):
        source = pairs[label][0 if which == 'rollback' else 1]
        target = home/'Library/LaunchAgents'/(label+'.plist')
        doc = load_plist(source.read_bytes())
        try:
            stop(label)
        except Exception as error:
            if not recovery:
                raise
            plan.setdefault('recovery_warnings',[]).append(str(error))
            save()
            # A failed bootout must not prevent restoration/bootstrap of originals.
        temporary = target.with_suffix('.plist.site-stage')
        temporary.write_bytes(source.read_bytes())
        temporary.replace(target)
        r = command(['bootstrap',domain,str(target)])
        # In recovery a failed bootstrap may mean the original remains registered.
        # Only a matching running binding plus HTTP can prove restoration.
        if r.returncode and not recovery:
            raise RuntimeError(f'Command failed: launchctl bootstrap rc={r.returncode}: {r.stderr}')
        for _ in range(80):
            state = command(['print',domain+'/'+label])
            if (not state.returncode and 'state = running' in state.stdout
                    and 'working directory = '+doc['WorkingDirectory']+'\n' in state.stdout+'\n'
                    and ready(doc)):
                if target.read_bytes() != source.read_bytes():
                    raise RuntimeError('Plist readback mismatch: '+label)
                return {'label':label,'working_directory':doc['WorkingDirectory'],'running':True}
            sleep(.25)
        raise RuntimeError(f'Runtime/HTTP readiness failed: {label}; bootstrap rc={r.returncode}: {r.stderr}')

    def verify_all(which):
        if verify is not None:
            return verify(which)
        if run is not subprocess.run:
            return
        unquarantined(sys.executable, run)
        unquarantined(stage/'http_verify.py', run)
        label = list(pairs)[1]
        doc = load_plist(pairs[label][0 if which == 'rollback' else 1].read_bytes())
        argv = doc['ProgramArguments']
        base = 'http://127.0.0.1:'+argv[argv.index('--listen-port')+1]
        cmd = [sys.executable,'-B',str(stage/'http_verify.py'),
               'rollback' if which=='rollback' else 'candidate']
        for flags in ([['--base',base],['--live']] if live else [['--base',base]]):
            r = run(cmd+flags,capture_output=True,text=True,timeout=180)
            plan.setdefault('http',[]).append({'mode':which,'flags':flags,'rc':r.returncode,'stdout':r.stdout,'stderr':r.stderr})
            save()
            if r.returncode:
                raise RuntimeError('HTTP verification failed: '+which)

    def restore():
        receipts, errors = [], []
        for label in pairs:
            for attempt in range(3):
                try:
                    receipts.append(start(label,'rollback',recovery=True))
                    break
                except Exception as error:
                    plan.setdefault('recovery_warnings',[]).append(str(error))
                    save()
                    if attempt == 2:
                        errors.append(str(error))
        plan['recovery_runtime'] = receipts
        # Attempt every job, even if the first cannot be recovered.
        if errors:
            raise RuntimeError('Recovery incomplete: '+'; '.join(errors))
        verify_all('rollback')
        plan['rollback_verified'] = True
        save()

    try:
        if mode == 'rollback':
            restore()
            plan['runtime'] = plan['recovery_runtime']
        else:
            plan['runtime'] = [start(label,mode) for label in pairs]
            verify_all(mode)
    except BaseException as error:
        plan['error'] = str(error)
        save()
        if mode == 'deploy':
            try:
                restore()
            except BaseException as recovery:
                plan['recovery_error'] = str(recovery)
                save()
                raise RuntimeError(f'{error}; rollback failed: {recovery}; see {journal}') from error
        raise
    plan['ok'] = True
    save()
    return plan


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument('mode', choices=('deploy', 'rollback'))
    parser.add_argument('--apply', action='store_true', help='OWNER APPROVAL REQUIRED: rebind the two site jobs')
    parser.add_argument('--live', action='store_true', help='Also verify public HTTP inside the rollback transaction')
    parser.add_argument('--sandbox-home', type=Path, help='Isolated rehearsal home; test labels only')
    args = parser.parse_args(argv)
    stage = Path(__file__).resolve().parent
    if args.sandbox_home:
        cfg = json.loads((stage/'deployment.json').read_text())
        if not cfg.get('sandbox') or tuple(cfg.get('labels',LABELS)) == LABELS:
            parser.error('Sandbox home requires sandbox test labels')
    result = switch(stage, args.sandbox_home or Path.home(), args.mode, apply=args.apply, live=args.live)
    name = args.mode + ('-applied-' if args.apply else '-dry-run-') + str(time.time_ns()) + '.json'
    (stage / name).write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
