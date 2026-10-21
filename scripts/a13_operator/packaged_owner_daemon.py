# SPDX-License-Identifier: AGPL-3.0-only
"""Launch the packaged owner app for a named preview version, bounded lifetime.

Usage: packaged_owner_daemon.py --version 34 [--max-seconds 1800]

The owner app state root is the required `A13_OWNER_ROOT` env input; the dist root
is `A13_DIST_ROOT` (default `<repo>/dist`, repo = this file's grandparent). The
generic helper `packaged_app_daemon.py` is resolved next to this file and run with
the current interpreter (`sys.executable`), so nothing here depends on the
historical lane tree.  The executable is asserted to exist before delegating: a
missing preview fails by name instead of as a raw FileNotFoundError.
"""
import argparse
import os
import shlex
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent


def _required(name):
    value = os.environ.get(name)
    if not value or not value.startswith('/'):
        raise SystemExit('a13_operator_env_required:' + name)
    return value


parser = argparse.ArgumentParser()
parser.add_argument('--version', required=True)
parser.add_argument('--max-seconds', type=int, default=1800)
args = parser.parse_args()
dist = Path(os.environ.get('A13_DIST_ROOT') or (REPO / 'dist'))
owner = Path(_required('A13_OWNER_ROOT'))
exe = dist / ('0.1.0-preview.' + args.version) / 'Mycelium A13 Demo.app/Contents/MacOS/Mycelium A13 Demo'
if not exe.exists():
    raise SystemExit('a13_owner_executable_missing:' + str(exe))
argv = [
    sys.executable, '-B', str(HERE / 'packaged_app_daemon.py'),
    '--resume', '--root', str(owner), '--executable', str(exe),
    '--max-seconds', str(args.max_seconds),
]
print('LAUNCH ' + shlex.join(argv), flush=True)
raise SystemExit(subprocess.call(argv, env={
    k: v for k, v in os.environ.items()
    if k not in ('PYTHONPATH', 'PYTHONHOME', 'VIRTUAL_ENV')}))
