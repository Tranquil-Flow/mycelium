# SPDX-License-Identifier: AGPL-3.0-only
"""Single-Mac rehearsal of the coworking demo through the app's own UI routes.

Two packaged app instances on THIS Mac, separate fresh state roots, and only
the routes the buttons call (no MYCELIUM_A13_RUN_MODEL, no manual chain steps):

  owner  : /api/network/start (cloudflared quick tunnel) -> /api/create
           -> /api/run-model {qwen3-0.6b} (model picker) -> /api/model/download
           -> /api/pair/window/open (Invite a Mac) -> QR join page over the relay
  target : /api/join/window (code + standing consent) ; owner /api/pair/window/approve
           -> target /api/join/window/poll -> joined (standing consent bound)
  run    : owner /api/demo/start (one click) -> poll owner_run + target demo_target
  chat   : the shareable chat page over the public relay (GET /chat, state)

Expected stop (by design): both instances share one host_id, so the run stops at
compile with ``two_distinct_hosts_required`` (or at hello collection with
``managed_observation_hosts_not_distinct``).  The receipt records every step.
"""
import json
import os
import shutil
import ssl
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

OPS = Path(__file__).resolve().parent
REPO = OPS.parents[1]


def _required_path(name):
    value = os.environ.get(name)
    if not value or not os.path.isabs(value):
        raise SystemExit('a13_rehearsal_env_required:' + name)
    return Path(value)


EXE = _required_path('A13_DEMO_EXECUTABLE')
RUN = _required_path('A13_REHEARSAL_ROOT') / ('run-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ'))
OWNER = RUN / 'owner'
TARGET = RUN / 'target'
COMMIT = 'c1899de289a04d12100db370d81485cdf75e47ca'
MODEL_DIR = 'models--Qwen--Qwen3-0.6B'
SNAP = Path(os.environ.get('A13_SEED_SNAPSHOT') or
            Path.home() / '.cache/huggingface/hub' / MODEL_DIR / 'snapshots' / COMMIT)
SNAPSHOT_BYTES = 1_519_209_243
STORAGE = 8_589_934_592
CONSENT = {'peer_visibility': True, 'power': 'battery_allowed', 'storage_bytes': STORAGE,
           'network': 'unmetered_only', 'background': False, 'diagnostics': False,
           'thermal': 'stop_on_serious'}
receipt = {'protocol': 'mycelium.a13_single_mac_ui_rehearsal.v1', 'executable': str(EXE),
           'run_root': str(RUN), 'observations': [], 'stopped_at': None, 'stop_reason': None}
T0 = time.monotonic()


def observe(event, **fields):
    entry = {'event': event, **fields, 't': round(time.monotonic() - T0, 3)}
    receipt['observations'].append(entry)
    print(json.dumps(entry), flush=True)


def launch(root, label):
    env = {k: v for k, v in os.environ.items()
           if k not in ('PYTHONPATH', 'PYTHONHOME', 'VIRTUAL_ENV', 'MYCELIUM_A13_RUN_MODEL')}
    log = open(RUN / f'{label}-daemon.log', 'w')
    proc = subprocess.Popen([sys.executable, '-B', str(OPS / 'packaged_app_daemon.py'), '--root', str(root),
                             '--executable', str(EXE), '--max-seconds', '1500'],
                            env=env, stdout=log, stderr=subprocess.STDOUT, cwd=str(RUN))
    deadline = time.monotonic() + 120
    while not (root / 'native-control.json').exists():
        if proc.poll() is not None:
            raise SystemExit(f'{label}_app_exited')
        if time.monotonic() > deadline:
            raise SystemExit(f'{label}_native_control_timeout')
        time.sleep(0.5)
    observe('app_up', label=label)
    return proc


def seed_owner(root):
    if not SNAP.is_dir():
        observe('not_seeded', label='owner'); return
    stage = root / ('model-source-' + COMMIT[:12]) / MODEL_DIR / 'snapshots' / ('.' + COMMIT + '.acquiring')
    stage.mkdir(mode=0o700, parents=True, exist_ok=True)
    p = stage.parent
    while p != root:
        os.chmod(p, 0o700); p = p.parent
    total = 0
    for name in sorted(os.listdir(SNAP)):
        shutil.copyfile(SNAP / name, stage / name)
        os.chmod(stage / name, 0o600)
        total += (stage / name).stat().st_size
    observe('seeded', label='owner', total_bytes=total)


def acquire(client):
    client.call('/api/model/download', {'consent': {'download_acknowledged': True,
        'max_download_bytes': SNAPSHOT_BYTES, 'storage_limit_bytes': STORAGE}})
    started = time.monotonic(); last = None
    while True:
        st = client.call('/api/status')['acquisition']
        cur = (st['state'], st['downloaded_bytes'], st['total_bytes'], st['error_code'])
        if cur != last:
            observe('acquisition', state=st['state'], downloaded=st['downloaded_bytes'],
                    total=st['total_bytes'], error=st['error_code'])
            last = cur
        if st['state'] in ('prepared', 'failed', 'cancelled'):
            if st['state'] != 'prepared':
                raise RuntimeError(f'owner_acquisition_{st["state"]}:{st["error_code"]}')
            return
        if time.monotonic() - started > 600:
            raise RuntimeError('owner_acquisition_timeout')
        time.sleep(1.0)


def https_get(url, *, method='GET', body=None, headers=None):
    request = urllib.request.Request(url, method=method, data=body, headers=headers or {})
    context = ssl.create_default_context()
    try:
        with urllib.request.urlopen(request, timeout=20, context=context) as response:
            return response.status, response.headers.get('Content-Type'), response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.headers.get('Content-Type'), error.read()


def step(name, fn):
    receipt['stopped_at'] = name
    try:
        value = fn()
    except Exception as exc:  # codes only; no secrets in these errors
        receipt['stop_reason'] = f'{type(exc).__name__}: {str(exc)[-400:]}'
        observe('step_failed', step=name, error=receipt['stop_reason'])
        raise
    observe('step_ok', step=name)
    return value


def main():
    sys.path.insert(0, str(OPS))
    from app_rpc import AppClient
    assert EXE.is_file(), EXE
    RUN.mkdir(parents=True, mode=0o700)
    procs = []
    try:
        procs.append(('owner', OWNER, launch(OWNER, 'owner')))
        owner = AppClient(OWNER)
        net = step('owner_network_start', lambda: owner.call(
            '/api/network/start', {'relay_acknowledged': True, 'max_seconds': 1500}))
        origin = net.get('public_origin')
        observe('owner_relay', state=net.get('state'), origin_host=(origin or '').split('/')[-1])
        step('owner_create', lambda: owner.call('/api/create', {'consent': CONSENT}))
        picked = step('owner_pick_model', lambda: owner.call('/api/run-model', {'choice': 'qwen3-0.6b'}))
        observe('model_picker', selected=picked['selected'], source=picked['source'], model_id=picked['model_id'])
        seed_owner(OWNER)
        step('owner_acquire', lambda: acquire(owner))

        window = step('owner_invite', lambda: owner.call('/api/pair/window/open', {'ttl_seconds': 600}))
        window_id, a, b = window['short_code'].split('-')
        page = origin + '/pairing-window/v1/%s/join/%s%s' % (window_id, a, b)
        status, kind, raw = https_get(page)
        observe('qr_join_page_over_relay', status=status, content_type=kind,
                shows_code=window['short_code'].encode() in raw)

        procs.append(('target', TARGET, launch(TARGET, 'target')))
        target = AppClient(TARGET)
        from_target = target.call('/api/status')['join_flow']['standing_disclosure']
        standing = {'acknowledged': True, 'disclosure_digest': from_target['digest'],
                    'caps': {'storage_bytes': STORAGE, 'max_memory_bytes': 8 * 1024 ** 3,
                             'max_wall_seconds': 1800, 'max_commands': 5000}}
        joined = step('target_join_with_code', lambda: target.call('/api/join/window', {
            'consent': CONSENT, 'standing_consent': standing, 'join': window['join_link']}))
        observe('target_waiting', state=joined.get('state'), pinned=joined.get('pinned'))
        pending = None
        until = time.monotonic() + 60
        while pending is None and time.monotonic() < until:
            listed = owner.call('/api/pair/window/list', {})['windows']
            pending = next((w.get('pending') for w in listed if w['window_id'] == window_id and w.get('pending')), None)
            if pending is None: time.sleep(1)
        step('owner_approve', lambda: owner.call('/api/pair/window/approve', {
            'window_id': window_id, 'request_digest': pending['request_digest']}))
        result = None
        until = time.monotonic() + 60
        while time.monotonic() < until:
            result = target.call('/api/join/window/poll', {})
            if result.get('state') == 'joined': break
            time.sleep(2)
        observe('target_joined', state=(result or {}).get('state'))
        status, _, _ = https_get(page)
        observe('qr_join_page_after_use', status=status)

        step('owner_demo_start', lambda: owner.call('/api/demo/start', {}))
        last = None; until = time.monotonic() + 900
        while time.monotonic() < until:
            st = owner.call('/api/status')
            run = st['owner_run']; mine = target.call('/api/status').get('demo_target') or {}
            key = (run['state'], run['step'], run['error_code'], mine.get('state'),
                   json.dumps([(p['node_id'][:8], p['state']) for p in run.get('participants') or []]))
            if key != last:
                observe('owner_run', state=run['state'], step=run['step'], error=run['error_code'],
                        failed_step=run['failed_step'],
                        steps={s['name']: [s['state'], s['detail']] for s in run['steps']},
                        participants=[{k: p.get(k) for k in ('label', 'state', 'detail', 'layers')}
                                      for p in run.get('participants') or []],
                        target_demo_state=mine.get('state'), target_detail=mine.get('detail'))
                last = key
            if run['state'] in ('failed', 'serving', 'stopped'): break
            time.sleep(2)
        receipt['stopped_at'] = 'owner_run:' + str(run['failed_step'] or run['state'])
        receipt['stop_reason'] = run['error_code']

        chat = st.get('chat') or {}
        observe('chat_links', share_url_host=(chat.get('share_url') or '').split('/')[2:3], available=chat.get('available'))
        if chat.get('share_url'):
            status, kind, raw = https_get(chat['share_url'])
            observe('chat_page_over_relay', status=status, content_type=kind, html=b'chat.js' in raw)
            status, _, raw = https_get(origin + '/chat/api/state', method='POST', body=b'{}',
                                       headers={'Content-Type': 'application/json', 'X-Mycelium-Chat': '1'})
            observe('chat_state_over_relay', status=status, state=json.loads(raw) if status == 200 else raw[:80].decode())
    finally:
        for label, root, proc in reversed(procs):
            try:
                AppClient(root).call('/api/quit', {})
            except Exception as exc:
                observe('quit_error', label=label, detail=repr(exc)[:160])
            try:
                proc.wait(timeout=90)
            except subprocess.TimeoutExpired:
                proc.terminate(); proc.wait(timeout=20)
            observe('app_closed', label=label, exit_code=proc.returncode)


if __name__ == '__main__':
    try:
        main()
    except Exception:
        pass
    finally:
        out = RUN / 'receipt.json'
        out.write_text(json.dumps(receipt, indent=2, sort_keys=True) + '\n')
        os.chmod(out, 0o600)
        print(json.dumps({'stopped_at': receipt['stopped_at'], 'stop_reason': receipt['stop_reason']}))
