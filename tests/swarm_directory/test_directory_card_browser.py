# SPDX-License-Identifier: AGPL-3.0-only
"""PV14 (2)+(3) on the directory page, in a real headless Chromium.

The swarm card shows the model, how many Macs, the status and how fresh it is, plus a link to the same short
notice the swarm's own chat page shows.  Forgetting this browser's key asks first and says what it resets.  The page
sets no cookie, loads nothing third-party and trips no CSP complaint.

Real: the directory server and page (WebCrypto Ed25519), signed records and heartbeats.  FAKE (labelled): the
clock (so a stale heartbeat does not need a real wait).  Screenshots are written when HOSTED_UI_SHOTS names a directory.
"""
from __future__ import annotations

import time

import pytest
from playwright.sync_api import expect, sync_playwright

from mycelium_a13_macos.public_relay import SeedRelayProxy
from mycelium_swarm_directory import docs
from mycelium_swarm_directory.crypto import Ed25519Signer, verify
from mycelium_swarm_directory.directory import Directory
from mycelium_swarm_directory.server import DirectoryServer
from mycelium_swarm_directory.store import SqliteStore
from mycelium_swarm_directory.wire import fingerprint

from _swarm_fixtures import request
from test_hosted_ui_browser import shot

POST = {'X-Mycelium-Swarms': '1'}
FRESH, STALE, SILENT = 'sw-aaaaaaaaaaaaaaaaaaaa', 'sw-bbbbbbbbbbbbbbbbbbbb', 'sw-cccccccccccccccccccc'


class Clock:
    def __init__(self):
        self.offset = 0

    def __call__(self):
        return int(time.time()) + self.offset


@pytest.fixture
def world(tmp_path):
    clock = Clock()
    server = DirectoryServer(Directory(SqliteStore(tmp_path / 'd.sqlite3'), verify, clock=clock,
                                       allow_loopback_entries=True)).start()
    entry_relay = SeedRelayProxy().start()  # the swarms' own origin (a relay with nothing bound)
    owners = {}

    def post(path, document):
        status, value, _headers = request(server.origin, '/swarms/api/v1' + path, document, headers=POST)
        assert status == 200, (path, value)

    def publish(swarm_id, name, model, *, members=None, status='serving', mode='open', ago=0):
        owner, entry = Ed25519Signer.generate(), Ed25519Signer.generate()
        owners[swarm_id] = fingerprint(owner.public_key)
        quota = {'max_requests': 20, 'max_tokens': 4000, 'ttl_seconds': 86400} if mode == 'open' else None
        clock.offset = -ago
        post('/records', docs.swarm_record(owner, swarm_id=swarm_id, seq=1, name=name, model=model, access_mode=mode,
                                           open_quota=quota, entry={'key': entry.public_key, 'origin': entry_relay.origin}))
        if members is not None:
            post('/heartbeat', docs.heartbeat(entry, swarm_id, status=status, members=members,
                                              served_model=model if status == 'serving' else None, at=clock()))
        clock.offset = 0

    publish(FRESH, 'Berlin coworking', 'Qwen/Qwen3-0.6B', members=2, ago=7)
    publish(STALE, 'Sleepy swarm', 'Qwen/Qwen2.5-0.5B-Instruct', members=3, ago=300)
    publish(SILENT, 'Brand new', 'Qwen/Qwen3-0.6B', mode='invite')
    yield {'server': server, 'owners': owners, 'entry': entry_relay}
    entry_relay.close()
    server.close()


def card(page, name):
    return page.locator('.swarm', has_text=name)


def test_a_card_shows_model_members_status_and_how_fresh_that_status_is(world):
    origin = world['server'].origin
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_context(viewport={'width': 1000, 'height': 1300}).new_page()
            page.goto(origin + '/swarms')
            page.wait_for_selector('body[data-ready="1"]')
            fresh = card(page, 'Berlin coworking')
            expect(fresh.locator('.status')).to_have_text('serving')
            expect(fresh.locator('[data-card-model]')).to_have_text('Qwen/Qwen3-0.6B')
            expect(fresh.locator('[data-card-members]')).to_have_text('2 devices')
            expect(fresh.locator('[data-card-owner]')).to_have_text(world['owners'][FRESH])
            expect(fresh.locator('[data-card-fresh]')).to_contain_text('updated')
            expect(fresh.locator('[data-card-fresh]')).to_contain_text('s ago')
            assert 5 <= int(fresh.locator('[data-card-fresh]').get_attribute('data-seconds')) <= 60
            expect(fresh.locator('[data-card-quota]')).to_contain_text('20 requests')

            stale = card(page, 'Sleepy swarm')
            page.locator('#offline-fold summary').click()
            expect(stale.locator('.status')).to_have_text('offline')
            expect(stale.locator('[data-card-fresh]')).to_have_text('last seen 5 min ago')
            expect(stale.locator('[data-card-fresh]')).to_have_attribute('data-stale', 'true')
            expect(stale.locator('[data-card-model]')).to_have_text('Qwen/Qwen2.5-0.5B-Instruct')

            silent = card(page, 'Brand new')
            expect(silent.locator('[data-card-fresh]')).to_have_text('no status report yet')
            expect(silent.locator('[data-card-members]')).to_have_text('—')  # no stale numbers presented as live
            shot(page, '23-directory-cards')
        finally:
            browser.close()


def test_every_card_links_the_same_short_notice(world):
    origin = world['server'].origin
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_context(viewport={'width': 1000, 'height': 1300}).new_page()
            page.goto(origin + '/swarms')
            page.wait_for_selector('body[data-ready="1"]')
            links = page.locator('.swarm a.notice-link')
            assert links.count() == 3
            assert {links.nth(i).get_attribute('href') for i in range(3)} == {'#notice'}
            expect(page.locator('#privacy-notice')).to_contain_text('participating devices')
            expect(page.locator('#privacy-notice')).to_contain_text('entry service')
            expect(page.locator('#privacy-notice')).to_contain_text('do not store prompts or replies')
            expect(page.locator('#privacy-notice')).to_contain_text('quota')
            links.first.click()
            assert page.url.endswith('#notice')
            expect(page.locator('#privacy-notice')).to_be_in_viewport()
            shot(page, '24-directory-notice')
        finally:
            browser.close()


def test_forget_this_key_asks_first_and_says_what_it_resets(world):
    origin = world['server'].origin
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_context(viewport={'width': 1000, 'height': 1300}).new_page()
            page.goto(origin + '/swarms')
            page.wait_for_selector('body[data-ready="1"]')
            before = page.text_content('#identity')
            expect(page.locator('#forget-confirm')).to_be_hidden()
            if not page.locator('#identity-card').is_visible(): page.click('#key-open')
            page.click('#forget')
            expect(page.locator('#forget-confirm')).to_be_visible()
            for fact in ('quota', 'network address', 'swarms you own', 'new key'):
                expect(page.locator('#forget-confirm')).to_contain_text(fact)
            shot(page, '25-directory-forget-confirm')
            page.click('#forget-no')
            expect(page.locator('#forget-confirm')).to_be_hidden()
            assert page.text_content('#identity') == before  # cancelled: same key
            if not page.locator('#identity-card').is_visible(): page.click('#key-open')
            page.click('#forget')
            with page.expect_navigation():
                page.click('#forget-yes')
            page.wait_for_selector('body[data-ready="1"]')
            page.wait_for_function('document.getElementById("identity").textContent.startsWith("Fingerprint")')
            assert page.text_content('#identity') != before  # a brand-new key
        finally:
            browser.close()


def test_the_page_sets_no_cookie_loads_nothing_third_party_and_keeps_its_csp(world):
    origin = world['server'].origin
    status, _value, headers = request(origin, '/swarms/api/v1/config')
    assert status == 200 and 'Set-Cookie' not in headers
    assert "default-src 'none'" in headers['Content-Security-Policy'] and "script-src 'self'" in headers['Content-Security-Policy']
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            context = browser.new_context()
            page = context.new_page()
            seen, complaints = [], []
            page.on('request', lambda r: seen.append(r.url))
            page.on('console', lambda m: complaints.append(m.text) if 'Content Security Policy' in m.text else None)
            page.on('pageerror', lambda e: complaints.append(str(e)))
            page.goto(origin + '/swarms')
            page.wait_for_selector('body[data-ready="1"]')
            page.click('.swarm a.notice-link')
            if not page.locator('#identity-card').is_visible(): page.click('#key-open')
            page.click('#forget')
            assert context.cookies() == []
            allowed = (origin + '/', world['entry'].origin + '/')  # the directory itself and the listed swarms' entries
            assert all(url.startswith(allowed) for url in seen), [u for u in seen if not u.startswith(allowed)]
            assert complaints == [], complaints
        finally:
            browser.close()
