# SPDX-License-Identifier: AGPL-3.0-only
"""Two-job site promotion. Dry-run by default; --apply requires owner approval.

Copy beside deployment.json and staged plists. No work occurs on import.
The test harness injects a recording runner and a scratch home, never launchd.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import subprocess
import time

LABELS = ('now.mycelium.swarm-directory', 'now.mycelium.apex-proxy')


def load_plist(data):
    return plistlib.loads(re.sub(rb'<!--.*?-->', b'', data, flags=re.S))


def candidate_doc(old, release):
    doc = old.copy()
    doc['WorkingDirectory'] = str(release)
    if old['Label'] == LABELS[1]:
        # The current deployment explicitly opts into the legacy root redirect.
        doc['ProgramArguments'] = [
            '--no-redirect-root-to-swarms' if a == '--redirect-root-to-swarms' else a
            for a in old['ProgramArguments']]
    return doc


def checked(command, run):
    result = run(command, capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError('Command failed: ' + ' '.join(command[:2]))
    return result.stdout


def unquarantined(path, run):
    result = run(['/usr/bin/xattr', '-p', 'com.apple.quarantine', str(path)], capture_output=True, text=True)
    if result.returncode == 0 or 'No such xattr' not in result.stderr:
        raise RuntimeError('Quarantined or uninspectable executable: ' + str(path))


def preflight(stage, home, run, mode):
    cfg = json.loads((stage / 'deployment.json').read_text())
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
    for label in LABELS:
        old_path = stage / (label + '.rollback.plist')
        new_path = stage / (label + '.plist')
        old, new = load_plist(old_path.read_bytes()), load_plist(new_path.read_bytes())
        if old['Label'] != label or old['WorkingDirectory'] != cfg['rollback']['path']:
            raise ValueError('Unexpected rollback binding')
        if new != candidate_doc(old, cfg['candidate']['path']):
            raise ValueError('Non-site configuration changed')
        for path in (old_path, new_path):
            checked(['/usr/bin/plutil', '-lint', str(path)], run)
        # launchd uses /usr/bin/env -u ... /absolute/python -B -m ... here.
        # Check the interpreter too, not only the harmless env wrapper.
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


def switch(stage, home, mode, *, apply=False, run=subprocess.run, sleep=time.sleep, uid=None):
    if mode not in ('deploy', 'rollback'):
        raise ValueError('Invalid mode')
    cfg, pairs = preflight(stage, home, run, mode)
    plan = {'mode': mode, 'dry_run': not apply, 'candidate_sha': cfg['candidate']['sha'],
            'jobs': [{'label': label, 'working_directory': cfg['candidate' if mode == 'deploy' else 'rollback']['path']}
                     for label in LABELS],
            'allowed_changes': ['WorkingDirectory', 'apex explicit root redirect opt-out'],
            'preserved': 'All other args, environment, ports, database and capability paths'}
    if not apply:
        return plan
    if any((home / 'mycelium-demo-run' / n).exists() for n in ('DEVICE.busy', 'CERT.busy')):
        raise RuntimeError('Device/certification busy; no job changed')
    domain = 'gui/' + str(os.getuid() if uid is None else uid)

    def rebind(which):
        receipts = []
        for label in LABELS:
            source = pairs[label][0 if which == 'rollback' else 1]
            target = home / 'Library/LaunchAgents' / (label + '.plist')
            doc = load_plist(source.read_bytes())
            result = run(['/bin/launchctl', 'bootout', domain + '/' + label], capture_output=True, text=True)
            if result.returncode and 'Could not find specified service' not in result.stderr:
                raise RuntimeError('bootout failed: ' + label)
            temporary = target.with_suffix('.plist.site-stage')
            temporary.write_bytes(source.read_bytes())
            temporary.replace(target)
            checked(['/bin/launchctl', 'bootstrap', domain, str(target)], run)
            sleep(2)
            runtime = checked(['/bin/launchctl', 'print', domain + '/' + label], run)
            if 'state = running' not in runtime or 'working directory = ' + doc['WorkingDirectory'] not in runtime:
                raise RuntimeError('Runtime binding mismatch: ' + label)
            if load_plist(target.read_bytes()) != doc:
                raise RuntimeError('Plist readback mismatch: ' + label)
            receipts.append({'label': label, 'working_directory': doc['WorkingDirectory'], 'running': True})
        return receipts

    try:
        plan['runtime'] = rebind(mode)
    except BaseException:
        if mode == 'deploy':
            rebind('rollback')
        raise
    return plan


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument('mode', choices=('deploy', 'rollback'))
    parser.add_argument('--apply', action='store_true', help='OWNER APPROVAL REQUIRED: rebind the two site jobs')
    args = parser.parse_args(argv)
    stage = Path(__file__).resolve().parent
    result = switch(stage, Path.home(), args.mode, apply=args.apply)
    name = args.mode + ('-applied-' if args.apply else '-dry-run-') + str(time.time_ns()) + '.json'
    (stage / name).write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
