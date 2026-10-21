# SPDX-License-Identifier: AGPL-3.0-only
"""'Publish this run as a swarm' (swarm_publish) and the swarm-entry ingress hook (swarm_entry)."""
import json
import os

import pytest

from mycelium_a13_macos import swarm_entry
from mycelium_a13_macos.swarm_publish import SwarmPublishError, SwarmPublisher, run_state
from mycelium_swarm_directory import docs
from mycelium_swarm_directory.crypto import Ed25519Signer, verify
from mycelium_swarm_directory.directory import Directory
from mycelium_swarm_directory.store import MemoryStore

ORIGIN = 'https://quiet-river.trycloudflare.com'
DIRECTORY = 'https://swarms.example.org'


class Run:
    def __init__(self, state='serving', route_ready=True):
        self.state, self.route_ready = state, route_ready

    def status(self):
        return {'state': self.state, 'route_ready': self.route_ready, 'registry_origin': 'http://127.0.0.1:5',
                'participants': [{'node_id': 'a'}, {'node_id': 'b'}, {'node_id': 'c'}]}


class App:
    installation_role = 'owner'

    def __init__(self, root, run=None):
        self.root = root
        self.owner_run = run or Run()

    def run_model_contract(self):
        return type('M', (), {'model_id': 'Qwen/Qwen3-0.6B'})()


class InProcessClient:
    """The real directory core in-process (no sockets)."""
    directory = None

    def __init__(self, origin):
        assert origin == DIRECTORY

    def get(self, path):
        status, value = self.directory.handle('GET', path, b'', 'ip:1')
        if status != 200:
            raise SwarmPublishError('swarm_directory_' + value['error'])
        return value

    def post(self, path, document):
        status, value = self.directory.handle('POST', path, json.dumps(document).encode(), 'ip:1')
        if status != 200:
            raise SwarmPublishError('swarm_directory_' + value['error'])
        return value


@pytest.fixture(autouse=True)
def directory():
    InProcessClient.directory = Directory(MemoryStore(), verify)
    return InProcessClient.directory


def publisher(tmp_path, origin=ORIGIN, run=None):
    app = App(tmp_path, run)
    value = SwarmPublisher(app, origin=lambda: origin, client_factory=InProcessClient, chat=lambda: None)
    app.swarm_publisher = value
    return app, value


def test_serving_is_reported_only_when_the_route_is_qualified(tmp_path):
    assert run_state(App(tmp_path, Run('serving', True))) == ('serving', 3, 'Qwen/Qwen3-0.6B')
    assert run_state(App(tmp_path, Run('serving', False)))[0] == 'offline'
    assert run_state(App(tmp_path, Run('running', False)))[0] == 'warming'
    assert run_state(App(tmp_path, Run('failed', False))) == ('offline', 0, None)


def test_publish_signs_record_heartbeats_and_keeps_a_private_key(tmp_path, directory):
    _app, pub = publisher(tmp_path)
    state = pub.publish({'directory': DIRECTORY, 'name': 'Two Macs', 'access_mode': 'request'})
    assert state['enabled'] and state['attached'] and state['heartbeat'] == 'serving'
    row, = directory.list_swarms()
    assert row['status'] == 'serving' and row['members'] == 3 and row['entry_origin'] == ORIGIN
    assert row['owner_fingerprint'] == state['owner_fingerprint'] == state['entry_fingerprint']
    assert os.stat(tmp_path / 'swarm-key').st_mode & 0o777 == 0o600
    assert state['link'].startswith('mycelium-swarm:')


def test_publish_requires_the_public_relay(tmp_path):
    _app, pub = publisher(tmp_path, origin=None)
    with pytest.raises(SwarmPublishError) as caught:
        pub.publish({'directory': DIRECTORY, 'name': 'x', 'access_mode': 'open'})
    assert caught.value.code == 'swarm_relay_required'


def test_new_relay_origin_is_re_signed_into_the_record(tmp_path, directory):
    origins = [ORIGIN]
    app = App(tmp_path)
    pub = SwarmPublisher(app, origin=lambda: origins[0], client_factory=InProcessClient, chat=lambda: None)
    pub.publish({'directory': DIRECTORY, 'name': 'x', 'access_mode': 'request'})
    origins[0] = 'https://new-name.trycloudflare.com'
    pub.tick()
    assert directory.list_swarms()[0]['entry_origin'] == 'https://new-name.trycloudflare.com'
    assert pub.state['record']['seq'] == 2


def test_link_mode_refuses_a_directory_that_swaps_the_owner(tmp_path, directory):
    owner, impostor = Ed25519Signer.generate(), Ed25519Signer.generate()
    swarm_id = docs.new_swarm_id()
    directory.handle('POST', '/records', json.dumps(docs.swarm_record(
        impostor, swarm_id=swarm_id, seq=1, name='x', model='m', access_mode='open')).encode(), 'ip:1')
    _app, pub = publisher(tmp_path)
    with pytest.raises(SwarmPublishError) as caught:
        pub.publish({'link': docs.swarm_link(DIRECTORY, swarm_id, owner.public_key)})
    assert caught.value.code == 'swarm_record_owner_mismatch'
    assert pub.policy() is None


def test_owner_in_browser_cannot_be_approved_from_the_app(tmp_path, directory):
    owner = Ed25519Signer.generate()
    swarm_id = docs.new_swarm_id()
    directory.handle('POST', '/records', json.dumps(docs.swarm_record(
        owner, swarm_id=swarm_id, seq=1, name='x', model='m', access_mode='request')).encode(), 'ip:1')
    _app, pub = publisher(tmp_path)
    pub.publish({'link': docs.swarm_link(DIRECTORY, swarm_id, owner.public_key)})
    with pytest.raises(SwarmPublishError) as caught:
        pub.inbox({})
    assert caught.value.code == 'swarm_owner_in_browser'


def test_tampered_state_on_disk_is_not_trusted_after_restart(tmp_path):
    _app, pub = publisher(tmp_path)
    pub.publish({'directory': DIRECTORY, 'name': 'x', 'access_mode': 'request'})
    pub.ledger.close()
    state = json.loads((tmp_path / 'swarm-publish.json').read_text())
    state['record']['access_mode'] = 'open'
    (tmp_path / 'swarm-publish.json').write_text(json.dumps(state))
    _app, again = publisher(tmp_path)
    assert again.policy() is None


def test_unpublish_reports_offline_and_closes_the_entry(tmp_path, directory):
    _app, pub = publisher(tmp_path)
    pub.publish({'directory': DIRECTORY, 'name': 'x', 'access_mode': 'request'})
    pub.unpublish({})
    assert directory.list_swarms()[0]['status'] == 'offline' and pub.policy() is None


def test_entry_hook_refuses_wrong_role_method_type_and_unpublished(tmp_path):
    app = App(tmp_path)
    assert swarm_entry.handle(app, 'POST', '/swarm-entry/v1/state', {'Content-Type': 'application/json'}, b'{}',
                              via_relay=True)[0] == 503
    assert swarm_entry.handle(app, 'GET', '/swarm-entry/v1/state', {}, b'', via_relay=True)[0] == 405
    assert swarm_entry.handle(app, 'POST', '/swarm-entry/v1/state', {'Content-Type': 'text/plain'}, b'{}',
                              via_relay=True)[0] == 415
    app.installation_role = 'target'
    assert swarm_entry.handle(app, 'POST', '/swarm-entry/v1/state', {'Content-Type': 'application/json'}, b'{}',
                              via_relay=True)[0] == 404
