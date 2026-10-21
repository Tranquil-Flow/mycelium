# SPDX-License-Identifier: AGPL-3.0-only
"""Loopback app boundary tests. Fake authority proves HTTP isolation only."""
import http.client
import json
from pathlib import Path
import threading
import urllib.parse

import pytest

from mycelium_a13_macos.app import DemoApp, DemoError, preflight


class Authority:
    def __init__(self, root):
        self.created = False
        self.stopped = False
    def start(self): return self
    def create_swarm(self, consent):
        assert set(consent)=={'cooperative_peer_visibility_acknowledged','resource_use_acknowledged','contribution_enabled'}
        assert consent['contribution_enabled'] is False
        self.created = True
        return self.status()
    def pair_request(self): return {'public':'fixture-only'}
    def approve_pairing(self, request, *, ttl_seconds, quota):
        assert quota['background_allowed'] is False
        return {'ciphertext':'fixture-only'}
    def accept_pairing(self, handoff): return self.status()
    def status(self):
        return {'membership': 'current' if self.created else 'not_joined', 'route_ready': False}
    def stop(self): self.stopped = True
    def renew(self): return self.status()


@pytest.fixture
def app(tmp_path):
    root = tmp_path / 'private'
    a = DemoApp(root, authority_factory=Authority, check_platform=False).start()
    yield a
    a.close()


def request(app, path, method='GET', body=None, headers=None):
    h = dict(headers or {})
    if body is not None:
        body = json.dumps(body)
        h.setdefault('Content-Type', 'application/json')
    c = http.client.HTTPConnection('127.0.0.1', app.port, timeout=3)
    c.request(method, path, body=body, headers=h)
    r = c.getresponse()
    result = (r.status, dict(r.getheaders()), r.read())
    c.close()
    return result


def login(app):
    # Test reads private launch file, never prints its one-use credential.
    import re
    text = app.launch_file.read_text()
    code = re.search(r'name="launch" value="([^"]+)"', text).group(1)
    conn = http.client.HTTPConnection('127.0.0.1', app.port, timeout=3)
    conn.request('POST', '/session', urllib.parse.urlencode({'launch':code}),
                 {'Content-Type':'application/x-www-form-urlencoded','Origin':'null'})
    response=conn.getresponse()
    assert response.status == 303
    cookie=response.getheader('Set-Cookie').split(';')[0]
    response.read(); conn.close()
    return {'Cookie':cookie,'Origin':app.origin,'X-Mycelium-Action':'1'}


def test_preflight_rejects_platform_before_state(tmp_path):
    for version in ['26.5.1','15.6.1']:
        assert preflight(system='Darwin',machine='arm64',os_version=version)['os_version']==version
    with pytest.raises(DemoError,match='unsupported_platform'):
        preflight(system='Darwin',machine='arm64',os_version='14.0')
    with pytest.raises(DemoError, match='unsupported_platform'):
        preflight(system='Linux', machine='arm64', os_version='26.5.1')
    with pytest.raises(DemoError, match='unsupported_platform'):
        preflight(system='Darwin', machine='x86_64', os_version='26.5.1')
    assert not list(tmp_path.iterdir())


def test_preflight_admits_any_macos_26_release_on_arm64(tmp_path):
    # A coworking Mac is not pinned to the owner's exact point release; the
    # bundled runtime's own floor is macOS 26.0 (Homebrew Python/OpenSSL).
    for version in ['26.0','26.0.1','26.1','26.5.1','26.6','27.0']:
        assert preflight(system='Darwin',machine='arm64',os_version=version)['os_version']==version
    for version in ['25.9','15.7','15.6','14.6.1','','26','abc','26.x']:
        with pytest.raises(DemoError,match='unsupported_platform'):
            preflight(system='Darwin',machine='arm64',os_version=version)
    with pytest.raises(DemoError,match='unsupported_platform'):
        preflight(system='Darwin',machine='x86_64',os_version='26.1')
    assert not list(tmp_path.iterdir())


def test_clean_second_browser_cannot_read_or_mutate(app):
    assert request(app, '/api/status')[0] == 401
    assert request(app, '/api/create', 'POST', {'consent':{}})[0] == 401
    h=login(app)
    assert request(app, '/api/status', headers=h)[0] == 200
    assert request(app, '/api/status')[0] == 401
    assert not app.launch_file.exists()


def test_csrf_host_queries_and_unknown_routes_denied(app):
    h=login(app)
    assert request(app, '/api/create', 'POST', {}, {**h,'Origin':'https://evil.invalid'})[0] == 403
    assert request(app, '/api/create', 'POST', {}, {'Cookie':h['Cookie']})[0] == 403
    assert request(app, '/api/status', headers={**h,'Host':'evil.invalid'})[0] == 403
    assert request(app, '/api/status?invite=CANARY', headers=h)[0] == 400
    assert request(app, '/identity/node.key', headers=h)[0] == 404


def test_consent_and_start_stop_are_explicit(app):
    h=login(app)
    assert not app.authority.created
    assert request(app, '/api/create','POST',{'consent':{}},h)[0] == 400
    consent={'peer_visibility':True,'power':'ac_only','storage_bytes':2_000_000_000,
             'network':'unmetered_only','background':False,'diagnostics':False,'thermal':'stop_on_serious'}
    assert request(app,'/api/create','POST',{'consent':consent},h)[0] == 200
    assert app.authority.created
    assert request(app,'/api/stop','POST',{},h)[0] == 200
    assert app.authority.stopped
    assert app.root.exists()


def test_parallel_launch_refused_and_restart_retains_data(app):
    with pytest.raises(DemoError, match='already_running'):
        DemoApp(app.root, authority_factory=Authority, check_platform=False).start()
    evidence=app.root/'retained-evidence.txt'
    evidence.write_text('KEEP')
    app.close()
    restarted=DemoApp(app.root,authority_factory=Authority,check_platform=False).start()
    try: assert evidence.read_text() == 'KEEP'
    finally: restarted.close()


def test_cleanup_failure_still_releases_http_and_lock(tmp_path):
    class BadStop(Authority):
        def stop(self): raise RuntimeError('fixture_stop_failure')
    root=tmp_path/'private'
    a=DemoApp(root,authority_factory=BadStop,check_platform=False).start()
    try:
        a.close()
        assert a.server is None and a.lock_fd is None
        assert not a.launch_file.exists()
        assert a.cleanup_error == 'authority_stop_failed'
    finally:
        a.authority.stop=lambda:None
        a.close()


def test_closed_app_cannot_restart(app):
    app.close()
    with pytest.raises(DemoError,match='application_closed'):
        app.action('/api/start',{})
    assert not app.running


def test_native_reopen_refreshes_one_use_file_after_expiry(app):
    from mycelium_a13_macos.app import reopen_native
    h=login(app)
    app.session_expires=0
    assert request(app,'/api/status',headers=h)[0]==401
    assert not app.launch_file.exists()
    assert reopen_native(app.root) == app.launch_file
    assert app.launch_file.exists()
    fresh=login(app)
    assert request(app,'/api/status',headers=fresh)[0]==200
    assert request(app,'/api/status',headers=h)[0]==401
    assert request(app,'/native-session','POST',{})[0]==403


def test_unsafe_lock_is_rejected_without_state(tmp_path):
    root=tmp_path/'state';root.mkdir(mode=0o700)
    lock=root/'application.lock';lock.write_text('untrusted');lock.chmod(0o644)
    a=DemoApp(root,authority_factory=Authority,check_platform=False)
    with pytest.raises(DemoError,match='unsafe_lock_file'): a.start()
    assert not (root/'authority').exists()


def test_startup_failure_locus_is_root_relative_and_bounded(tmp_path):
    """A bare FileNotFoundError during launch was unattributable (rounds 51)."""
    from mycelium_a13_macos.app import bounded_failure_locus
    root=tmp_path/'state';root.mkdir()
    inside=root/'serving'/'status.json'
    assert bounded_failure_locus(FileNotFoundError(2,'missing',str(inside)),root)=={'locus':'/serving/status.json'}
    outside=tmp_path/'elsewhere'/'x.json'
    assert bounded_failure_locus(FileNotFoundError(2,'missing',str(outside)),root)=={'locus':'outside_state_root'}
    assert bounded_failure_locus(RuntimeError('no filename'),root)=={}
    locus=bounded_failure_locus(FileNotFoundError(2,'missing',str(root/('a'*300))),root)['locus']
    assert len(locus)<=160 and locus.endswith('...')


def test_private_action_failure_diagnostic_is_bounded(tmp_path):
    class BadCreate(Authority):
        def create_swarm(self, consent):
            raise RuntimeError('fixture secret should not appear')
    root=tmp_path/'diagnostic-state'
    a=DemoApp(root,authority_factory=BadCreate,check_platform=False).start()
    try:
        h=login(a)
        consent={'peer_visibility':True,'power':'ac_only','storage_bytes':2_000_000_000,
                 'network':'unmetered_only','background':False,'diagnostics':False,'thermal':'stop_on_serious'}
        status, _headers, raw=request(a,'/api/create','POST',{'consent':consent},h)
        assert status==409
        assert json.loads(raw)=={'error':'action_not_completed'}
        record=json.loads((root/'last-action-error.private.json').read_text())
        assert record['protocol']=='mycelium.a13.action_failure.v1'
        assert record['path']=='/api/create'
        assert record['public_code']=='action_not_completed'
        assert record['exception_type']=='RuntimeError'
        blob=json.dumps(record)
        assert 'secret' not in blob and 'fixture secret should not appear' not in blob
    finally:
        a.close()


def test_body_bound_and_no_credentials_in_public_shell(app):
    status, headers, raw=request(app,'/')
    assert status==200
    assert b'name="launch"' not in raw
    assert 'no-store' in headers['Cache-Control']
    assert "frame-ancestors 'none'" in headers['Content-Security-Policy']
    h=login(app)
    assert request(app,'/api/create','POST',{'x':'a'*70000},h)[0]==413
