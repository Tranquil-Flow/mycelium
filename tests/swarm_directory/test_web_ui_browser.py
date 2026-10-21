# SPDX-License-Identifier: AGPL-3.0-only
"""Browser end-to-end (one headless Chromium, two contexts): create, attach, request, approve, chat.

Real: the directory server and web page (WebCrypto Ed25519 in the browser), the
A13 relay + swarm-entry hook + publisher (link mode) + chat service +
registry client.  FAKE (labelled): the product gateway behind the swarm
(canned tokens over the registry API contract) and the owner-run stub.
"""
from __future__ import annotations

import pytest
from playwright.sync_api import expect, sync_playwright

from mycelium_a13_macos import swarm_entry
from mycelium_a13_macos.public_relay import SeedRelayProxy
from mycelium_a13_macos.swarm_publish import SwarmPublisher
from mycelium_swarm_directory.crypto import verify
from mycelium_swarm_directory.directory import Directory
from mycelium_swarm_directory.server import DirectoryServer
from mycelium_swarm_directory.store import SqliteStore

from _swarm_fixtures import FakeApp, fake_gateway, request


@pytest.fixture
def stack(tmp_path):
    directory = DirectoryServer(Directory(SqliteStore(tmp_path / 'd.sqlite3'), verify,
                                          allow_loopback_entries=True)).start()
    gateway, backend = fake_gateway(['Ber', 'lin', '.'])
    (tmp_path / 'mac').mkdir()
    app = FakeApp(tmp_path / 'mac', 'http://127.0.0.1:%d' % gateway.server_port)
    proxy = SeedRelayProxy().start()
    publisher = SwarmPublisher(app, origin=lambda: proxy.origin)
    app.swarm_publisher = publisher
    swarm_entry.bind(app, proxy)
    yield {'directory': directory, 'publisher': publisher, 'proxy': proxy, 'backend': backend}
    publisher.close()
    proxy.close()
    gateway.shutdown()
    directory.close()


def test_browser_create_attach_request_approve_and_chat(stack):
    url = stack['directory'].origin + '/swarms'
    status, _value, headers = request(stack['directory'].origin, '/swarms/api/v1/config')
    assert status == 200 and "frame-ancestors 'none'" in headers['Content-Security-Policy']
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            owner = browser.new_context().new_page()
            owner.goto(url)
            owner.wait_for_selector('body[data-ready="1"]')
            # the private key is non-extractable
            extractable = owner.evaluate("""async () => {
              const db = await new Promise((r) => { const o = indexedDB.open('mycelium-swarms', 1); o.onsuccess = () => r(o.result); });
              const id = await new Promise((r) => { const g = db.transaction('kv').objectStore('kv').get('identity'); g.onsuccess = () => r(g.result); });
              try { await crypto.subtle.exportKey('pkcs8', id.privateKey); return 'exported'; }
              catch (e) { return id.privateKey.extractable; } }""")
            assert extractable is False
            owner.fill('#c-name', 'Browser swarm')
            owner.click('#create-form button[type=submit]')
            link = owner.wait_for_selector('[data-link]').text_content()
            assert link.startswith('mycelium-swarm:')
            expect(owner.locator('img.qr')).to_be_visible()

            # the Mac pastes the link; the owner attaches it by fingerprint
            state = stack['publisher'].publish({'link': link})
            assert state['attached'] is False
            owner.click('#refresh')
            owner.click('[data-attach="%s"]' % state['entry_fingerprint'])
            owner.wait_for_selector('[data-attached="%s"]' % state['entry_fingerprint'])
            assert stack['publisher'].tick()['attached'] is True

            visitor = browser.new_context().new_page()
            visitor.goto(url)
            visitor.wait_for_selector('body[data-ready="1"]')
            card = visitor.locator('.swarm', has_text='Browser swarm')
            expect(card.locator('.status')).to_have_text('serving')
            expect(card).to_contain_text('seen by this swarm')
            visitor_fp = visitor.text_content('#identity').split()[-1]
            card.locator('input').fill('hello from the test')
            card.locator('button', has_text='Request access').click()
            expect(card).to_contain_text('Waiting for the owner')

            owner.click('#refresh')
            owner.click('[data-approve="%s"]' % visitor_fp)
            owner.wait_for_selector('text=granted')

            visitor.click('#refresh')
            card.locator('button', has_text='Chat').click()
            visitor.fill('#prompt', 'Capital of Germany?')
            visitor.click('#send')
            expect(visitor.locator('.msg.swarm').last).to_have_text('Berlin.', timeout=10000)
            assert stack['backend'].inference_posts[-1]['prompt'] == 'Capital of Germany?'
        finally:
            browser.close()
