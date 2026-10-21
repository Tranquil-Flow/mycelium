# SPDX-License-Identifier: AGPL-3.0-only
"""Readiness guards for A13 HTTP tests' recorded 3 s client read bound.

Batch-13 candidate c7a039f77a1231481d1b18fc845b4f86240328f7 did not contain
``tests/a13_macos/test_http.py``; the normalized receipts name the three A13
HTTP timeouts as two prerequisite ``/api/create`` calls in
``test_app_acquisition.py`` plus one authenticated ``/api/work/preview`` call in
``test_work_http_parent.py``.  The shared failure mode is the loopback HTTP
helper's fixed 3 s client read bound expiring while unrelated one-time product
work is carried inside the timed request.

These tests keep the bound and assertions unchanged.  They guard the repair
shape instead: prerequisite work is arranged through the same product action
before the timed HTTP contract, while product TLS generation and staging
verification remain live.
"""

import hashlib
import inspect
import io
import json
import tarfile
import threading
import time
from dataclasses import asdict

import pytest

from mycelium_a13_macos import tls
from mycelium_a13_macos.app import DemoApp
from mycelium_a13_macos.managed_staging import TargetStaging
from test_app import login, request
from test_app_authority import CONSENT
from test_work_acceptance_parent import arrange
from test_worker import SAFE
import test_app_acquisition
import test_work_http_parent


def test_product_create_still_generates_tls_material_once_in_process(tmp_path, monkeypatch):
    """The acquisition setup still exercises the real product create body.

    Moving this prerequisite outside the timed HTTP helper must not pre-forge or
    bypass TLS, membership, SQLite, or authority state.
    """
    app = DemoApp(tmp_path / 'owner', check_platform=False).start()
    calls = []
    real = tls._generate_material
    monkeypatch.setattr(tls, '_generate_material',
                        lambda: (calls.append('generate'), real())[1])
    try:
        status = app.action('/api/create', {'consent': CONSENT})
        assert status['membership_state'] == 'current'
        assert calls == ['generate']
        assert (app.root / 'authority' / 'tls' / 'ca.pem').is_file()
    finally:
        app.close()


def test_http_create_bound_reproduces_when_tls_generation_is_blocked(tmp_path, monkeypatch):
    """Blocked generation deterministically yields the recorded timeout shape."""
    app = DemoApp(tmp_path / 'owner', check_platform=False).start()
    entered = threading.Event()
    release = threading.Event()
    real = tls._generate_material

    def blocked():
        entered.set()
        release.wait(10)
        return real()

    monkeypatch.setattr(tls, '_generate_material', blocked)
    try:
        headers = login(app)
        with pytest.raises(TimeoutError):
            request(app, '/api/create', 'POST', {'consent': CONSENT}, headers)
        assert entered.is_set()
        release.set()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if (app.root / 'authority' / 'tls' / 'ca.pem').exists():
                break
            time.sleep(0.01)
        assert (app.root / 'authority' / 'tls' / 'ca.pem').is_file()
    finally:
        release.set()
        app.close()


def test_acquisition_enroll_setup_is_not_inside_http_read_bound():
    src = inspect.getsource(test_app_acquisition.enroll)
    assert ".action('/api/create'" in src
    assert "request(" not in src


def test_work_preview_http_bound_reproduces_when_stage_prepare_is_blocked(tmp_path, monkeypatch):
    """Blocked stage prepare deterministically yields the recorded preview timeout."""
    app = DemoApp(tmp_path / 'owner', check_platform=False).start()
    entered = threading.Event()
    release = threading.Event()
    real_run = TargetStaging._run

    def blocked_run(self, operation, args, data=b''):
        if operation == 'prepare':
            entered.set()
            release.wait(10)
        return real_run(self, operation, args, data)

    monkeypatch.setattr(TargetStaging, '_run', blocked_run)
    try:
        app.action('/api/create', {'consent': CONSENT})
        app.action('/api/local-contributor/start', {})
        actor = app.local_contributor
        assert actor is not None
        authority = app.authority
        member = actor.member
        assert authority is not None and member is not None
        _, offer, options = arrange({'target': actor.peer, 'authority': authority,
            'member': authority.coordinator.member(member.node_id)})
        assert offer['offer_id']
        app._policy_observer = lambda: dict(SAFE)
        body = {'limits': asdict(options['limits']), 'stage_acknowledged': True}
        assert request(app, '/api/work/preview', 'POST', body)[0] == 401
        headers = login(app)
        with pytest.raises(TimeoutError):
            request(app, '/api/work/preview', 'POST', body, headers)
        assert entered.is_set()
        release.set()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                code, _, raw = request(app, '/api/work/preview', 'POST', body, headers)
                if code == 200 and json.loads(raw).get('consent_digest'):
                    break
            except TimeoutError:
                pass
            time.sleep(0.01)
        else:
            pytest.fail('released preview did not complete')
    finally:
        release.set()
        app.close()


def test_work_preview_arranges_staging_before_timed_authenticated_preview():
    src = inspect.getsource(test_work_http_parent.test_work_http_exact_consent_rejections_and_native_start)
    arrange = src.index('preview_work(actor,offer')
    timed_preview = src.index("code,_,raw=request(app,'/api/work/preview'")
    assert arrange < timed_preview
    assert "assert request(app,'/api/work/preview','POST',body)[0]==401" in src


def test_prepared_stage_is_reused_without_respawning_the_child(tmp_path, monkeypatch):
    payload = b'{"protocol":"readiness-probe"}\n'
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode='w') as tar:
        info = tarfile.TarInfo('control/readiness.json')
        info.size = len(payload)
        tar.addfile(info, io.BytesIO(payload))
    archive = buffer.getvalue()
    digest = 'sha256:' + hashlib.sha256(archive).hexdigest()
    staging = TargetStaging(tmp_path / 'stage', node_id='readiness-node',
                            max_archive_bytes=1_000_000, max_disk_bytes=1_000_000,
                            current_member=lambda: True, authorize=lambda intent: True)
    first = staging.prepare(archive=archive, archive_digest=digest)
    calls = []
    real_run = TargetStaging._run
    monkeypatch.setattr(TargetStaging, '_run',
                        lambda self, *a, **k: (calls.append(a[0]), real_run(self, *a, **k))[1])
    before = {p.relative_to(staging.staging_root).as_posix(): (p.lstat().st_ino, p.lstat().st_mtime_ns)
              for p in sorted(staging.staging_root.rglob('*'))}
    second = staging.prepare(archive=archive, archive_digest=digest)
    assert second == first
    assert calls == []
    after = {p.relative_to(staging.staging_root).as_posix(): (p.lstat().st_ino, p.lstat().st_mtime_ns)
             for p in sorted(staging.staging_root.rglob('*'))}
    assert after == before
