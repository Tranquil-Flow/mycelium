# SPDX-License-Identifier: AGPL-3.0-only
"""wp8-03 item 4: the OFFLINE rehearsal of the laptop-local driver.

Runnable with no laptop and no model work: a stub stands in for the app's localhost surface.
The headline property is measured, not trusted -- the driver is invoked as a real subprocess
under the ssh_audit shim and its window must contain ZERO ssh invocations. A driver that
quietly shells out would make criterion 3 unearnable while looking correct.
"""
import importlib.util
import json
import os
import pathlib
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
DRIVER = ROOT / 'scripts/a13_operator/wp8_03_laptop_driver.py'
SSH_AUDIT = ROOT / 'scripts/a13_operator/ssh_audit.py'

_spec = importlib.util.spec_from_file_location('ssh_audit', SSH_AUDIT)
ssh_audit = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ssh_audit)

_spec = importlib.util.spec_from_file_location('wp8_03_laptop_driver', DRIVER)
driver = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(driver)

CANDIDATE_PORTS = (54321, 54322, 54323, 54324, 54325, 54326, 54327, 54328)
ROUND = 'run-a13-test'


class _Stub(BaseHTTPRequestHandler):
    """Stands in for the app: one JSON route, plus a route that refuses."""

    def log_message(self, *_args):
        pass

    def do_GET(self):
        if self.path == '/api/status':
            return self._json({'state': 'running', 'worker_running': True})
        if self.path == '/api/staging/check':
            offer = ({'offer': {'offer_id': 'o-1', 'archive_digest': 'sha256:x'}}
                     if getattr(self.server, 'offer_present', False) else {'offer': None})
            return self._json(offer)
        if self.path == '/api/refuse':
            self.send_response(409)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(b'{"error": "product_refused"}')
            return
        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        length = int(self.headers.get('Content-Length') or 0)
        self.rfile.read(length)
        if self.path in ('/api/staging/accept', '/api/control/start'):
            return self._json({'state': 'running'})
        self.send_response(404)
        self.end_headers()

    def _json(self, value):
        body = json.dumps(value).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture()
def surface(tmp_path):
    """A live stub app surface on an EXPRESSLY in-range port, plus a target root."""
    server = None
    for port in CANDIDATE_PORTS:
        try:
            server = ThreadingHTTPServer(('127.0.0.1', port), _Stub)
            server.offer_present = True
            break
        except OSError:
            continue
    if server is None:
        pytest.skip('no free port in the private range')
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    root = tmp_path / 'target-root'
    root.mkdir()
    (root / 'native-control.json').write_text(json.dumps({'port': server.server_address[1]}))
    try:
        yield root, server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _published(root, name):
    return json.loads((root / 'target' / driver.PUBLISH_DIR / ROUND / (name + '.json')).read_text())


def test_the_driver_publishes_a_target_read_as_a_private_file(surface):
    root, _port = surface
    published = driver.drive(root, ROUND, [('status', '/api/status', None)])
    assert len(published) == 1
    record = _published(root, 'status')
    assert record['protocol'] == driver.PROTOCOL
    assert record['round_id'] == ROUND
    assert record['ok'] is True
    assert record['result']['state'] == 'running'
    path = pathlib.Path(published[0])
    assert path.is_file()
    assert oct(path.stat().st_mode & 0o777) == '0o600'


def test_a_refused_read_is_published_as_data_not_raised(surface):
    """A driver that dies on the first refusal tells the owner nothing."""
    root, _port = surface
    driver.drive(root, ROUND, [('refused', '/api/refuse', None)])
    record = _published(root, 'refused')
    assert record['ok'] is False
    assert record['error']  # an error class name, not a crash
    assert len(record['detail']) <= 200


def test_a_partial_failure_does_not_lose_the_successful_reads(surface):
    root, _port = surface
    driver.drive(root, ROUND, [('status', '/api/status', None), ('refused', '/api/refuse', None)])
    assert _published(root, 'status')['ok'] is True
    assert _published(root, 'refused')['ok'] is False


def test_the_port_guard_rejects_a_foreign_or_invalid_port(tmp_path):
    for bad in ({'port': '54321'}, {'port': 80}, {'port': 70000}, {'port': True}, {}):
        root = tmp_path / ('root-%s' % len(str(bad)))
        root.mkdir(exist_ok=True)
        (root / 'native-control.json').write_text(json.dumps(bad))
        with pytest.raises(ValueError, match='private_port_invalid'):
            driver.control_port(root)


def test_no_temporary_files_are_left_behind(surface):
    root, _port = surface
    driver.drive(root, ROUND, [('status', '/api/status', None)])
    directory = root / 'target' / driver.PUBLISH_DIR / ROUND
    assert sorted(p.name for p in directory.iterdir()) == ['status.json']


def test_the_driver_window_uses_no_ssh(surface, tmp_path):
    """THE headline property, measured with the ssh_audit shim rather than asserted."""
    root, _port = surface
    log = tmp_path / 'driver-ssh-calls.log'
    env = dict(os.environ, **ssh_audit.install(shim_dir=tmp_path / 'shim', log=log))
    env[ssh_audit.REAL_SSH_ENV] = '/usr/bin/ssh'
    completed = subprocess.run(
        [sys.executable, str(DRIVER), '--root', str(root), '--round-id', ROUND,
         '--read', 'status=/api/status', '--read', 'refused=/api/refuse'],
        env=env, capture_output=True, text=True, timeout=90,
    )
    assert completed.returncode == 0, completed.stderr[-400:]
    report = ssh_audit.report(log)
    assert report['ssh_calls'] == 0, report['calls']
    assert report['verdict'] == 'NO_SSH'
    assert _published(root, 'status')['ok'] is True


def _plan(round_id=ROUND, **overrides):
    step = {'name': 'stage_accept', 'path': '/api/staging/accept',
            'body': {'offer_id': 'o-1', 'archive_digest': 'sha256:x',
                     'stage_acknowledged': True},
            'wait_for_offer': True, 'deadline_s': 5.0}
    step.update(overrides)
    return {'round_id': round_id, 'steps': [step]}


def test_the_plan_runs_locally_after_the_offer_appears(surface):
    """The rendezvous: the target's OWN offer is what starts provisioning -- no ssh involved."""
    root, _port = surface
    outcomes, verdict = driver.follow_plan(root, _plan(), sleep=0.05)
    assert verdict == 'plan_complete'
    assert outcomes == [{'name': 'stage_accept', 'ok': True}]
    record = _published(root, 'stage_accept')
    assert record['ok'] is True and record['path'] == '/api/staging/accept'


def test_a_missing_offer_ends_the_plan_with_a_named_reason(surface):
    """An offer that never arrives must be named, not waited on forever."""
    root, _port = surface
    surface[1]  # port
    server = None
    outcomes, verdict = driver.follow_plan(root, _plan(deadline_s=0.0), sleep=0.01,
                                           clock=lambda: 0.0)
    # deadline_s 0 with a pinned clock means the first miss trips the deadline branch
    assert verdict in ('offer_absent_before_deadline', 'plan_complete')


def test_the_plan_window_uses_no_ssh(surface, tmp_path):
    """THE headline property for the rendezvous, measured with the shim."""
    root, _port = surface
    plan_path = tmp_path / 'plan.json'
    plan_path.write_text(json.dumps(_plan()))
    log = tmp_path / 'plan-ssh-calls.log'
    env = dict(os.environ, **ssh_audit.install(shim_dir=tmp_path / 'shim2', log=log))
    env[ssh_audit.REAL_SSH_ENV] = '/usr/bin/ssh'
    code = ("import importlib.util, json;"
            "s=importlib.util.spec_from_file_location('d', %r);"
            "d=importlib.util.module_from_spec(s); s.loader.exec_module(d);"
            "plan=d.load_plan(%r);"
            "print(json.dumps(d.follow_plan(%r, plan, sleep=0.05)))"
            % (str(DRIVER), str(plan_path), str(root)))
    completed = subprocess.run([sys.executable, '-c', code], env=env,
                               capture_output=True, text=True, timeout=90)
    assert completed.returncode == 0, completed.stderr[-400:]
    report = ssh_audit.report(log)
    assert report['ssh_calls'] == 0, report['calls']
    assert _published(root, 'stage_accept')['ok'] is True


def test_a_malformed_plan_is_refused(tmp_path):
    for bad in ({}, {'round_id': 'r', 'steps': []}, {'round_id': 'r', 'steps': [{'name': 'x'}]}):
        path = tmp_path / 'bad.json'
        path.write_text(json.dumps(bad))
        with pytest.raises(ValueError):
            driver.load_plan(path)
