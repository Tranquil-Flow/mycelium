# SPDX-License-Identifier: AGPL-3.0-only
"""Deploy a packaged A13 preview bundle to the laptop as operator input.

SSH/SCP carries installation files only; the core control/data transport stays
managed HTTPS. ditto on both ends preserves macOS bundle metadata so the
executable identity stays verifiable. No execution downloads.

Usage: deploy_preview_to_laptop.py --version 34
"""
import argparse
import hashlib
import json
import os
import shlex
import subprocess
from pathlib import Path


def _required(name):
    value = os.environ.get(name)
    if not value or not value.startswith('/'):
        raise SystemExit('a13_operator_env_required:' + name)
    return value


root = Path(__file__).resolve().parents[2]
evidence = Path(os.environ.get('A13_EVIDENCE_DIR') or (root / 'outputs/a13-macos-evidence'))
REMOTE = _required('A13_REMOTE_ROOT')
HOST = os.environ.get('A13_REMOTE_HOST', 'member-mac')
APP = 'Mycelium A13 Demo.app'
EXE_REL = APP + '/Contents/MacOS/Mycelium A13 Demo'

parser = argparse.ArgumentParser()
parser.add_argument('--version', required=True)
args = parser.parse_args()
tag = 'preview' + args.version
src = Path(os.environ.get('A13_DIST_ROOT') or (root / 'dist')) / ('0.1.0-preview.' + args.version)
zip_path = evidence / ('%s-package.zip' % tag)


def run(argv):
    print('RUN ' + ' '.join(shlex.quote(a) for a in argv), flush=True)
    result = subprocess.run(argv, capture_output=True, text=True)
    if result.returncode:
        print(result.stdout[-2000:], flush=True)
        print(result.stderr[-2000:], flush=True)
        raise SystemExit(result.returncode)
    return result


def remote(command):
    return run(['ssh', '-T', '-o', 'BatchMode=yes', HOST, command])


def scp_dest(path):
    """scp re-parses the remote path through the remote shell: escape spaces."""
    return HOST + ':' + path.replace('\\', '\\\\').replace(' ', '\\ ')


local_digest = hashlib.sha256((src / EXE_REL).read_bytes()).hexdigest()
run(['ditto', '-c', '-k', '--keepParent', str(src / APP), str(zip_path)])
print('zip_bytes %d' % zip_path.stat().st_size, flush=True)

remote('mkdir -p ' + shlex.quote(REMOTE + '/' + tag))
run(['scp', '-q', '-O', str(zip_path), scp_dest(REMOTE + '/%s-package.zip' % tag)])
remote('ditto -x -k ' + shlex.quote(REMOTE + '/%s-package.zip' % tag) + ' ' + shlex.quote(REMOTE + '/' + tag))
for name in ('app_rpc.py', 'packaged_app_daemon.py'):
    run(['scp', '-q', '-O', str(Path(__file__).resolve().parent / name), scp_dest(REMOTE + '/' + tag + '/' + name)])

remote_digest = remote('shasum -a 256 ' + shlex.quote(REMOTE + '/' + tag + '/' + EXE_REL)).stdout.split()[0]
print('LOCAL  ' + local_digest, flush=True)
print('REMOTE ' + remote_digest, flush=True)
print('REMOTE_DISK ' + remote('df -h ~ | tail -1').stdout.strip(), flush=True)
if remote_digest != local_digest:
    raise SystemExit('%s deployment digest mismatch' % tag)
(evidence / ('%s-laptop-deployment.json' % tag)).write_text(
    json.dumps({'executable': REMOTE + '/' + tag + '/' + EXE_REL, 'sha256': remote_digest}) + '\n')
print('DEPLOY_OK ' + tag, flush=True)
