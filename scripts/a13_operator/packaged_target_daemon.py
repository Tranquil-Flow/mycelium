# SPDX-License-Identifier: AGPL-3.0-only
"""Launch a packaged A13 preview app on the laptop over SSH, tracked locally.

SSH carries operator input only; the core control/data transport stays managed
HTTPS. Quoting is built with shlex.join so nothing is interpolated by an outer
shell.

Usage: packaged_target_daemon.py --version 34 [--max-seconds 1800]
                                 [--cancel-diagnostic-log /abs/path.jsonl]

`--cancel-diagnostic-log` sets MYCELIUM_CANCEL_PHASE_DIAGNOSTIC_LOG for the
REMOTE node only.  `ssh -T` does not forward the local environment (and a remote
sshd would need AcceptEnv to do it), so the variable is prefixed onto the remote
command with `env` instead of being exported locally.  The path must be absolute
and writable by the app on the laptop; it is created 0600 by the node itself.
"""
import argparse
import os
import shlex
import subprocess


def _required(name):
    value = os.environ.get(name)
    if not value or not value.startswith('/'):
        raise SystemExit('a13_operator_env_required:' + name)
    return value


REMOTE = _required('A13_REMOTE_ROOT')
HOST = os.environ.get('A13_REMOTE_HOST', 'member-mac')

parser = argparse.ArgumentParser()
parser.add_argument('--version', required=True)
parser.add_argument('--max-seconds', type=int, default=1800)
parser.add_argument('--cancel-diagnostic-log', default=None)
args = parser.parse_args()
tag = 'preview' + args.version

remote_args = [
    '/usr/bin/python3', '-u', '%s/%s/packaged_app_daemon.py' % (REMOTE, tag),
    '--resume',
    '--root', REMOTE + '/target',
    '--executable', '%s/%s/Mycelium A13 Demo.app/Contents/MacOS/Mycelium A13 Demo' % (REMOTE, tag),
    '--max-seconds', str(args.max_seconds),
]
if args.cancel_diagnostic_log:
    if not args.cancel_diagnostic_log.startswith('/'):
        raise SystemExit('cancel_diagnostic_log_must_be_absolute')
    remote_args = [
        '/usr/bin/env',
        'MYCELIUM_CANCEL_PHASE_DIAGNOSTIC_LOG=' + args.cancel_diagnostic_log,
    ] + remote_args
cmd = ['ssh', '-T', '-o', 'BatchMode=yes', HOST, shlex.join(remote_args)]
print('LAUNCH ' + shlex.join(cmd), flush=True)
raise SystemExit(subprocess.call(cmd))
