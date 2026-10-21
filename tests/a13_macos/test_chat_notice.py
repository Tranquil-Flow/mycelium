# SPDX-License-Identifier: AGPL-3.0-only
"""PV14 (1)+(2): the swarm's own chat page carries a first-prompt notice and a forget control.

Server side: ``/chat/api/state`` names who runs the swarm, from the entry's signed listing (never from a
client-supplied or unverified value), and its open quota.  Page side (static pins; the browser behaviour is in
tests/swarm_directory/test_chat_notice_browser.py): the notice and the controls exist, remembered per origin in
localStorage only, no cookie, no IndexedDB, no third-party URL.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from mycelium_a13_macos import chat
from mycelium_swarm_directory import docs
from mycelium_swarm_directory.crypto import Ed25519Signer, verify
from mycelium_swarm_directory.entry import EntryGateway, EntryGuard, EntryPolicy, UsageLedger
from mycelium_swarm_directory.wire import fingerprint

WEB = Path(chat.__file__).parent / 'web'
SWARM = 'sw-aaaaaaaaaaaaaaaaaaaa'
HEADERS = {'X-Mycelium-Chat': '1', 'Content-Type': 'application/json'}


class Run:
    def status(self):
        return {'state': 'serving', 'step': None, 'registry_origin': 'http://127.0.0.1:55555', 'run_id': 'r1',
                'participants': []}


class Publisher:
    def __init__(self, gateway):
        self.gateway = gateway


class App:
    installation_role = 'owner'

    def __init__(self):
        self.owner_run = Run()
        self.chat = None
        self.server = None
        self.swarm_publisher = None

    def run_model_contract(self):
        return type('M', (), {'model_id': 'Qwen/Qwen3-0.6B'})()


def published(tmp_path, *, access_mode='open', attached=True):
    owner, entry = Ed25519Signer.generate(), Ed25519Signer.generate()
    quota = {'max_requests': 50, 'max_tokens': 5000, 'ttl_seconds': 3600} if access_mode == 'open' else None
    record = docs.swarm_record(owner, swarm_id=SWARM, seq=1, name='Demo', model='Qwen/Qwen3-0.6B',
                               access_mode=access_mode, open_quota=quota,
                               entry={'key': entry.public_key, 'origin': 'https://quiet-river.trycloudflare.com'})
    policy = EntryPolicy.from_record(record, verify, entry_key=entry.public_key)
    guard = EntryGuard(lambda: policy if attached else None, entry, verify, UsageLedger(tmp_path / 'e.sqlite3'))
    app = App()
    app.swarm_publisher = Publisher(EntryGateway(guard, lambda: app.chat))
    return app, owner


def state(app):
    status, _kind, raw = chat.handle(app, 'POST', '/chat/api/state', HEADERS, b'{}', via_relay=True)
    assert status == 200
    return json.loads(raw)


def test_state_names_the_owner_from_the_signed_listing_and_the_open_quota(tmp_path):
    app, owner = published(tmp_path)
    notice = state(app)['notice']
    assert notice['published'] is True
    assert notice['owner_fingerprint'] == fingerprint(owner.public_key)
    assert notice['swarm_name'] == 'Demo' and notice['access_mode'] == 'open'
    assert notice['open_quota'] == {'max_requests': 50, 'max_tokens': 5000, 'ttl_seconds': 3600}
    assert notice['kept_answers'] == chat.MAX_KEPT_REQUESTS


def test_a_swarm_that_is_not_open_says_so_and_has_no_quota(tmp_path):
    app, _owner = published(tmp_path, access_mode='invite')
    notice = state(app)['notice']
    assert notice['access_mode'] == 'invite' and notice['open_quota'] is None and notice['published'] is True


def test_an_unpublished_or_unattached_swarm_claims_no_owner(tmp_path):
    for app in (App(), published(tmp_path, attached=False)[0]):
        notice = state(app)['notice']
        assert notice['published'] is False and notice['owner_fingerprint'] is None
        assert notice['access_mode'] is None and notice['open_quota'] is None


def test_the_notice_is_public_facts_only(tmp_path):
    app, owner = published(tmp_path)
    raw = json.dumps(state(app)['notice'])
    assert owner.public_key not in raw  # the grouped fingerprint, not the key
    assert set(state(app)['notice']) == {'published', 'swarm_name', 'owner_fingerprint', 'access_mode',
                                         'open_quota', 'kept_answers', 'directory'}


def test_the_page_has_the_notice_the_acknowledge_button_and_the_forget_control():
    html = (WEB / 'chat.html').read_text()
    for needle in ('id="notice"', 'id="notice-ack"', 'id="forget"', 'id="forget-confirm"', 'id="forget-yes"',
                   'id="forget-no"', 'I understand'):
        assert needle in html, needle
    text = re.sub(r'<[^>]+>', ' ', html)
    for fact in ('volunteer', 'relay', 'stored', 'quota'):
        assert fact in text.lower(), fact


def test_the_acknowledgement_is_localstorage_only_per_origin_no_cookie_no_indexeddb_no_third_party():
    js = (WEB / 'chat.js').read_text()
    assert 'localStorage' in js
    for forbidden in ('document.cookie', 'indexedDB', 'sessionStorage', 'XMLHttpRequest', 'sendBeacon', 'WebSocket',
                      'importScripts'):
        assert forbidden not in js, forbidden
    assert not re.search(r'(?:https?:)?//[a-z0-9.-]+\.[a-z]{2,}', js)  # no absolute URL in the script
    html = (WEB / 'chat.html').read_text()
    css = (WEB / 'chat.css').read_text()
    assert not re.search(r'(?:src|href|action)="(?!/chat/|#)', html)
    assert 'url(' not in css and '@import' not in css


def test_send_is_refused_by_the_page_until_the_notice_is_acknowledged():
    js = (WEB / 'chat.js').read_text()
    assert re.search(r'if\s*\(\s*!acknowledged\(\)\s*\)\s*return', js)
