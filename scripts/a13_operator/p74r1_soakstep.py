#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""C5 soak against the live route of round p74r1 (extracted from the proven launcher, run standalone
because the launcher's own post-round steps are gone -- the supervisor detaches and the launcher exited).

Ordering is deliberate: wait on the CHAIN'S OWN LOG SENTINEL, then read the app's status exactly once.
Polling the app's private API while the chain drives it is what produced 73r1's 403.
"""
import importlib.util
import json
import os
import sys
import time

sys.path.insert(0, 'scripts/a13_operator')
os.environ.setdefault('A13_OWNER_ROOT', os.path.expanduser('~/mycelium-wp8-02/owner-lhr'))
os.environ.setdefault('A13_REMOTE_ROOT', '/Users/member/mycelium-wp8-02')
os.environ.setdefault('A13_TARGET_PREVIEW', 'preview74')
os.environ.setdefault('A13_RPC_ROUND', 'rpc-round74')
LABEL = os.environ.get('A13_LOG_LABEL', 'p74r1')
EV = os.path.expanduser('~/Documents/mycelium-core-program/runs/wp8-02-useful-two-mac')
LOG = os.path.join(EV, 'round%s-supervisor.log' % LABEL)


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


rpc = load('round14_rpc', 'scripts/a13_operator/round14_rpc.py')

deadline = time.monotonic() + 240
while time.monotonic() < deadline:
    try:
        text = open(LOG).read()
    except OSError:
        text = ''
    if 'chain_finished' in text:
        break
    time.sleep(5)
print('chain_finished seen:', 'chain_finished' in open(LOG).read() if os.path.exists(LOG) else False, flush=True)
print('--- the chain\'s own verdict ---', flush=True)
for line in open(LOG):
    if 'chain_finished' in line or 'supervisor_done' in line or 'STEP_FAILED' in line:
        print('   ', line.strip()[:200], flush=True)

base = ''
deadline = time.monotonic() + 600
serving = {}
while time.monotonic() < deadline:
    try:
        status = rpc.owner('/api/serving/status', {})
    except Exception as exc:
        status = None
        print('   status read failed: %s: %s' % (type(exc).__name__, str(exc)[:100]), flush=True)
    serving = ((status or {}).get('serving') or {})
    if serving.get('state') == 'api_running':
        base = ((serving.get('startup') or {}).get('url') or '').rstrip('/')
        break
    time.sleep(3)
print('serving state:', serving.get('state'), 'base:', base, flush=True)
if not base:
    print(json.dumps({'ok': False, 'error': 'serving_not_running_before_deadline',
                      'serving': serving}), flush=True)
    raise SystemExit(0)

audit = load('ssh_audit', 'scripts/a13_operator/ssh_audit.py')
soak = load('wp8_05_soak', 'scripts/a13_operator/wp8_05_soak.py')


def route_alive(b):
    try:
        st = rpc.owner('/api/serving/status', {})
        return ((st or {}).get('serving') or {}).get('state') == 'api_running'
    except Exception:
        return False


log = os.environ.get('A13_SSH_LOG', '/tmp/asm/criterion3-ssh-calls.log')
before = len(audit.invocations(log))
print('ACCEPTANCE_WINDOW_SSH_BEFORE', before, flush=True)
summary, path = soak.run_soak(
    base,
    requests=int(os.environ.get('SOAK_REQUESTS_N', '110')),
    cancel_every=int(os.environ.get('SOAK_CANCEL_EVERY', '10')),
    max_new_tokens=int(os.environ.get('SOAK_TOKENS', '32')),
    label=LABEL,
    sample_status=lambda: route_alive(base),
    out_dir=EV)
after = len(audit.invocations(log))
print('SOAK_WINDOW_SSH delta=%d total=%d' % (after - before, after), flush=True)
print(json.dumps(summary, indent=1), flush=True)
print('SOAK_WRITTEN', path, flush=True)
