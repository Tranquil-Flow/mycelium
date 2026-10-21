#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Single local A14a software/browser acceptance (never physical qualification)."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
WEB = ROOT / 'ui/web'
LOCK = Path(os.path.expanduser('~/Documents/mycelium-core-program/runs/v2/m1-l2-repair/test-owner.lock'))
CANONICAL = Path(os.path.expanduser('~/Documents/playground/mycelium-core-pilot/ui/web'))
CHECKS = [
    ('python-a14', ROOT, [sys.executable, '-m', 'pytest', '-q', '-p', 'no:cacheprovider', 'tests/a14_acceptance', 'tests/product_spine', 'tests/contracts']),
    ('python-conformance', ROOT, [sys.executable, 'scripts/generate_a14_conformance.py', '--check']),
    ('web-unit', WEB, ['npm', 'test', '--', 'src/features/routeExplorer']),
    ('web-build', WEB, ['npm', 'run', 'build:live']),
    ('browser-standard', WEB, ['node', 'scripts/a14-local-browser.mjs']),
    ('browser-large', WEB, ['node', 'scripts/a14-local-browser.mjs']),
]


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    if len(sys.argv) != 2 or not Path(sys.argv[1]).is_absolute():
        raise SystemExit('usage: python tests/a14_acceptance/accept.py /absolute/private/evidence-directory')
    out = Path(sys.argv[1]).resolve()
    out.mkdir(parents=True, exist_ok=True)
    for key in ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_COMMON_DIR', 'GIT_OBJECT_DIRECTORY',
                'GIT_ALTERNATE_OBJECT_DIRECTORIES', 'PYTHONPATH', 'PYTHONHOME', 'VIRTUAL_ENV'):
        os.environ.pop(key, None)
    os.environ['PYTHONDONTWRITEBYTECODE'] = '1'
    if digest(WEB/'package-lock.json') != digest(CANONICAL/'package-lock.json'):
        raise SystemExit('node dependency lock mismatch; no dependency reuse')
    modules = WEB/'node_modules'
    if not modules.exists() and not modules.is_symlink():
        modules.symlink_to(CANONICAL/'node_modules', target_is_directory=True)
    if not modules.resolve().is_dir():
        raise SystemExit('node dependencies missing; no install attempted')
    receipt = {'protocol': 'mycelium.a14a_local_acceptance.v1', 'source': str(ROOT),
               'claim': 'fixture-backed production composition in local Chromium; no physical evidence',
               'steps': [], 'source_sha256': {}}
    try:
        for name, cwd, argv in CHECKS:
            env = os.environ.copy()
            if name.startswith('browser-'):
                env['A14_EVIDENCE'] = str(out/name)
                if name == 'browser-large':
                    env['A14_CONFORMANCE_MEMBERS'] = '256'
                else:
                    env.pop('A14_CONFORMANCE_MEMBERS', None)
            with LOCK.open('a+') as owned:
                until = time.monotonic() + 120
                while True:
                    try:
                        fcntl.flock(owned, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if time.monotonic() > until:
                            raise TimeoutError('test-owner lock wait exceeded 120s')
                        time.sleep(.25)
                with (out/f'{name}.log').open('w') as log:
                    proc = subprocess.Popen(argv, cwd=cwd, env=env, stdout=log,
                                            stderr=subprocess.STDOUT, start_new_session=True)
                    try:
                        code = proc.wait(timeout=120)
                    except subprocess.TimeoutExpired:
                        os.killpg(proc.pid, signal.SIGTERM)
                        try:
                            proc.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            os.killpg(proc.pid, signal.SIGKILL)
                            proc.wait()
                        code = 124
            entry = {'name': name, 'exit': code, 'log': str(out/f'{name}.log')}
            if name.startswith('browser-') and code == 0:
                report = json.loads((out/name/'browser-report.json').read_text())
                cleanup = json.loads((out/name/'server-cleanup.json').read_text())
                if not (report['source_mode'] == 'fixture' and report['physical_devices'] == 0
                        and report['qualification_claim'] is False and not report['failures']
                        and len(report['checks']) == (9 if name == 'browser-large' else 8)
                        and cleanup['signalCode'] == 'SIGTERM'):
                    code = entry['exit'] = 1
                entry['checks'] = report['checks']
                entry['browser_version'] = report['browser_version']
                entry['server_cleanup'] = cleanup
            receipt['steps'].append(entry)
            print(f'{name}: exit {code} ({entry["log"]})', flush=True)
            if code != 0:
                break
    finally:
        for path in ('mycelium_product_spine/route_explorer.py',
                     'tests/a14_acceptance/test_route_projection_boundaries.py',
                     'ui/web/src/features/routeExplorer/RouteExplorer.tsx',
                     'ui/web/src/features/routeExplorer/RouteExplorer.test.tsx',
                     'ui/web/scripts/a14-local-browser.mjs',
                     'tests/a14_acceptance/serve_browser.py',
                     'tests/a14_acceptance/accept.py'):
            receipt['source_sha256'][path] = digest(ROOT/path)
        receipt['pass'] = len(receipt['steps']) == len(CHECKS) and all(s['exit'] == 0 for s in receipt['steps'])
        (out/'acceptance.json').write_text(json.dumps(receipt, indent=2) + '\n')
    return 0 if receipt['pass'] else 1


if __name__ == '__main__':
    sys.exit(main())
