# SPDX-License-Identifier: AGPL-3.0-only
"""Single-Mac rehearsal of the A13 join path with the packaged demo app.

Two packaged app instances on THIS Mac, separate fresh state roots:
  owner  : /api/network/start (managed public relay: cloudflared quick tunnel)
           -> /api/create -> seeded 0.5B acquisition
  target : /api/join/request -> owner pair preview/approve -> /api/pair/accept
           -> /api/staging/check (the peer->owner read goes over the PUBLIC relay origin,
              never LAN-direct) -> seeded 0.5B acquisition
  chain  : /api/local-contributor/start -> installation descriptors -> observe/prepare
           -> staging accept + native control start (real native hello) -> observe/collect
           -> /api/serving/compile

Model: MYCELIUM_A13_RUN_MODEL=a9-route (Qwen2.5-0.5B-Instruct @ 7ae5576, int8, mlx).
Seeding copies the already-verified local snapshot into each app's resume stage dir;
the app's own acquisition then verifies every digest (same path as the wp8-01 dry runs).

Expected stop (by design): both nodes report the same host_id, so compile refuses
`two_distinct_hosts_required` (mycelium_a13_macos/chain_compile.py:17-18). Every step's
outcome is written to receipt.json; no credentials or invitation plaintext are printed.
"""
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

OPS = Path(__file__).resolve().parent


def _required_path(name):
    value = os.environ.get(name)
    if not value or not os.path.isabs(value):
        raise SystemExit('a13_rehearsal_env_required:' + name)
    return Path(value)


# Env contract: A13_DEMO_EXECUTABLE = the built app's Contents/MacOS executable;
# A13_REHEARSAL_ROOT = a parent directory for fresh run roots (never the frozen
# ~/Library/Application Support tree); A13_SEED_SNAPSHOT = an already-verified local
# Qwen2.5-0.5B-Instruct snapshot directory at revision 7ae5576 (optional: without it the
# app downloads the ~1 GB repository itself, which is the recipient's real path).
EXE = _required_path('A13_DEMO_EXECUTABLE')
RUN = _required_path('A13_REHEARSAL_ROOT') / ('run-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ'))
OWNER = RUN / 'owner-lhr'
TARGET = RUN / 'target'
SNAP = Path(os.environ['A13_SEED_SNAPSHOT']) if os.environ.get('A13_SEED_SNAPSHOT') else None
MODEL_DIR = 'models--Qwen--Qwen2.5-0.5B-Instruct'
COMMIT = '7ae557604adf67be50417f59c2c2f167def9a775'
CONSENT = {'peer_visibility': True, 'power': 'battery_allowed', 'storage_bytes': 8_589_934_592,
           'network': 'unmetered_only', 'background': False, 'diagnostics': False,
           'thermal': 'stop_on_serious'}
DOWNLOAD_CONSENT = {'download_acknowledged': True, 'max_download_bytes': 999_604_126,
                    'storage_limit_bytes': 8_589_934_592}
QUOTA = {'storage_bytes': 8_589_934_592, 'network_bytes_per_day': 8_589_934_592, 'background_allowed': False}
receipt = {'protocol': 'mycelium.a13_single_mac_rehearsal.v1', 'executable': str(EXE),
           'run_root': str(RUN), 'observations': [], 'stopped_at': None, 'stop_reason': None}
T0 = time.monotonic()


def observe(event, **fields):
    entry = {'event': event, **fields, 't': round(time.monotonic() - T0, 3)}
    receipt['observations'].append(entry)
    print(json.dumps(entry), flush=True)


def launch(root, label):
    env = {k: v for k, v in os.environ.items() if k not in ('PYTHONPATH', 'PYTHONHOME', 'VIRTUAL_ENV')}
    env['MYCELIUM_A13_RUN_MODEL'] = 'a9-route'
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


def seed(root, label):
    if SNAP is None:
        observe('not_seeded', label=label)
        return
    base = (root / 'target/serving') if label == 'target' else (root / 'model-source')
    stage = base / MODEL_DIR / 'snapshots' / ('.' + COMMIT + '.acquiring')
    stage.mkdir(mode=0o700, parents=True, exist_ok=True)
    p = stage.parent
    while p != root:
        os.chmod(p, 0o700)
        p = p.parent
    total = 0
    for name in sorted(os.listdir(SNAP)):
        shutil.copyfile(SNAP / name, stage / name)
        os.chmod(stage / name, 0o600)
        total += (stage / name).stat().st_size
    observe('seeded', label=label, total_bytes=total)


def acquire(client, label):
    client.call('/api/model/download', {'consent': DOWNLOAD_CONSENT})
    started = time.monotonic(); last = None
    while True:
        st = client.call('/api/status')['acquisition']
        cur = (st['state'], st['downloaded_bytes'], st['total_bytes'], st['error_code'])
        if cur != last:
            observe('acquisition', label=label, state=st['state'], downloaded=st['downloaded_bytes'],
                    total=st['total_bytes'], error=st['error_code'], seconds=round(time.monotonic() - started, 1))
            last = cur
        if st['state'] in ('prepared', 'failed', 'cancelled'):
            if st['state'] != 'prepared':
                raise RuntimeError(f'{label}_acquisition_{st["state"]}:{st["error_code"]}')
            return
        if time.monotonic() - started > 600:
            raise RuntimeError(f'{label}_acquisition_timeout')
        time.sleep(1.0)


def step(name, fn):
    receipt['stopped_at'] = name
    try:
        value = fn()
    except Exception as exc:  # recorded verbatim (codes only; no secrets in these errors)
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
    receipt['executable'] = str(EXE); receipt['run_root'] = str(RUN)
    procs = []
    try:
        procs.append(('owner', OWNER, launch(OWNER, 'owner')))
        owner = AppClient(OWNER)
        net = step('owner_network_start', lambda: owner.call(
            '/api/network/start', {'relay_acknowledged': True, 'max_seconds': 1500}))
        origin = net.get('public_origin')
        observe('owner_relay', state=net.get('state'), origin_host=(origin or '').split('/')[-1])
        step('owner_create', lambda: owner.call('/api/create', {'consent': CONSENT}))
        seed(OWNER, 'owner')
        step('owner_acquire', lambda: acquire(owner, 'owner'))

        procs.append(('target', TARGET, launch(TARGET, 'target')))
        target = AppClient(TARGET)
        req = step('target_join_request', lambda: target.call('/api/join/request', {'consent': CONSENT}))
        prev = step('owner_pair_preview', lambda: owner.call('/api/pair/preview', {'request': req}))
        observe('pair_preview', peer_class=prev.get('peer_class'))
        hand = step('owner_pair_approve', lambda: owner.call(
            '/api/pair/approve', {'request': req, 'ttl_seconds': 120, 'quota': QUOTA}))
        step('target_pair_accept', lambda: target.call('/api/pair/accept', {'handoff': hand}))
        st = target.call('/api/status')
        observe('target_membership', membership_state=(st.get('authority') or {}).get('membership_state'))
        t = time.monotonic()
        step('target_reads_owner_over_public_relay', lambda: target.call('/api/staging/check', {}))
        observe('peer_owner_read_seconds', seconds=round(time.monotonic() - t, 3))
        seed(TARGET, 'target')
        step('target_acquire', lambda: acquire(target, 'target'))

        step('owner_local_contributor_start', lambda: owner.call('/api/local-contributor/start', {}))
        scope = {'run_id': 'a13-qwen-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ'),
                 'deployment_id': str(uuid.uuid4())}
        cards = step('installation_descriptors', lambda: [
            rpc.call('/api/installation/descriptor', {**scope, 'disclosure_acknowledged': True})
            for rpc in (owner, target)])
        cards = sorted(cards, key=lambda c: c['statement']['target_node_id'])
        step('observe_prepare', lambda: owner.call('/api/serving/observe/prepare',
                                                   {**scope, 'descriptors': cards, 'bootstrap_acknowledged': True}))
        for label, rpc in (('owner', owner), ('target', target)):
            offer = None
            until = time.monotonic() + 120
            while offer is None and time.monotonic() < until:
                offer = rpc.call('/api/staging/check', {}).get('offer')
                if offer is None:
                    time.sleep(2)
            intent = {'offer_id': offer['offer_id'], 'archive_digest': offer['archive_digest']}
            step(f'{label}_staging_accept', lambda: rpc.call('/api/staging/accept', {**intent, 'stage_acknowledged': True}))
            step(f'{label}_native_start', lambda: rpc.call('/api/control/start', {**intent, 'control_acknowledged': True}))
        obs = step('observe_collect', lambda: owner.call('/api/serving/observe/collect', {'observe_acknowledged': True}))
        hosts = [o.get('host_id') for o in obs.get('observations', [])]
        observe('observed_hosts', count=len(hosts), distinct=len(set(hosts)))
        step('serving_compile', lambda: owner.call('/api/serving/compile', {'model_preparation_acknowledged': True}))
        receipt['stopped_at'] = 'compile_accepted'
    finally:
        for label, root, proc in reversed(procs):
            try:
                AppClient(root).call('/api/quit', {})
            except Exception as exc:
                observe('quit_error', label=label, detail=repr(exc)[:160])
            try:
                proc.wait(timeout=60)
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
