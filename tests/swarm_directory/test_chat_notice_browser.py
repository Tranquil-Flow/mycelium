# SPDX-License-Identifier: AGPL-3.0-only
"""PV14 (1)+(2) in a real headless Chromium: the swarm's own chat page.

Real: the A13 relay (``SeedRelayProxy``) serving ``chat.handle`` with its CSP, the shipped ``ChatService`` and
registry client, the entry guard built from an owner-signed record.  FAKE (labelled): the generation backend
(canned tokens over the registry API contract).  Two relays on two ports stand for two swarm origins.
Screenshots are written when HOSTED_UI_SHOTS names a directory.
"""
from __future__ import annotations

import http.client
import json
import os
from pathlib import Path

import pytest
from playwright.sync_api import expect, sync_playwright

from mycelium_a13_macos import chat
from mycelium_a13_macos.chat import ChatService, handle
from mycelium_a13_macos.public_relay import SeedRelayProxy
from mycelium_swarm_directory import docs
from mycelium_swarm_directory.crypto import Ed25519Signer, verify
from mycelium_swarm_directory.entry import EntryGateway, EntryGuard, EntryPolicy, UsageLedger
from mycelium_swarm_directory.wire import fingerprint

from _swarm_fixtures import FakeApp, fake_gateway

SHOTS = os.environ.get('HOSTED_UI_SHOTS')
SWARM = 'sw-aaaaaaaaaaaaaaaaaaaa'
STORAGE_KEY = 'mycelium.chat.notice.v1'


def shot(page, name):
    if SHOTS:
        Path(SHOTS).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(Path(SHOTS) / (name + '.png')), full_page=True)


class Publisher:
    def __init__(self, gateway, directory):
        self.gateway = gateway
        self.state = {'directory': directory}


def serve(tmp_path, name, *, access_mode='open', owner=None):
    """One swarm origin: a relay whose /chat is the owner's chat page for a published swarm."""
    owner = owner or Ed25519Signer.generate()
    entry = Ed25519Signer.generate()
    quota = {'max_requests': 50, 'max_tokens': 5000, 'ttl_seconds': 3600} if access_mode == 'open' else None
    record = docs.swarm_record(owner, swarm_id=SWARM, seq=1, name='Berlin coworking', model='Qwen/Qwen3-0.6B',
                               access_mode=access_mode, open_quota=quota,
                               entry={'key': entry.public_key, 'origin': 'https://quiet-river.trycloudflare.com'})
    policy = EntryPolicy.from_record(record, verify, entry_key=entry.public_key)
    gateway, _backend = fake_gateway(['Ber', 'lin', '.'])
    (tmp_path / name).mkdir()
    app = FakeApp(tmp_path / name, 'http://127.0.0.1:%d' % gateway.server_port)
    app.chat = ChatService(app)
    guard = EntryGuard(lambda: policy, entry, verify, UsageLedger(tmp_path / (name + '.sqlite3')))
    app.swarm_publisher = Publisher(EntryGateway(guard, lambda: app.chat), 'https://mycelium.now')
    proxy = SeedRelayProxy().start()
    proxy.bind_chat(lambda m, p, h, b: handle(app, m, p, h, b, via_relay=True))
    return proxy, fingerprint(owner.public_key), gateway


@pytest.fixture
def world(tmp_path):
    proxy, fp, gateway = serve(tmp_path, 'a')
    other, _fp, gateway2 = serve(tmp_path, 'b')
    yield {'proxy': proxy, 'fp': fp, 'other': other}
    proxy.close()
    other.close()
    gateway.shutdown()
    gateway2.shutdown()


def watch(page, origin):
    """Record every request URL and every CSP/console complaint a page produces."""
    seen, complaints = [], []
    page.on('request', lambda r: seen.append(r.url))
    page.on('console', lambda m: complaints.append(m.text) if 'Content Security Policy' in m.text else None)
    page.on('pageerror', lambda e: complaints.append(str(e)))
    return seen, complaints


def test_the_first_visit_shows_the_notice_and_holds_the_prompt_box_until_i_understand(world):
    origin = world['proxy'].origin
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            context = browser.new_context(viewport={'width': 900, 'height': 1000})
            page = context.new_page()
            seen, complaints = watch(page, origin)
            page.goto(origin + '/chat')
            expect(page.locator('#notice')).to_be_visible()
            expect(page.locator('#notice')).to_contain_text(world['fp'])  # who runs this swarm
            expect(page.locator('#notice')).to_contain_text('volunteer Macs')
            expect(page.locator('#notice')).to_contain_text('relay')
            expect(page.locator('#notice')).to_contain_text('50 requests')  # the open quota
            expect(page.locator('#notice')).to_contain_text('5000 tokens')
            expect(page.locator('#notice')).to_contain_text('not stored')  # what is / is not stored
            expect(page.locator('#form')).to_be_hidden()
            expect(page.locator('#send')).to_be_disabled()
            shot(page, '20-chat-first-prompt-notice')
            assert page.evaluate('localStorage.getItem(%s)' % json.dumps(STORAGE_KEY)) is None  # nothing before consent

            page.click('#notice-ack')
            expect(page.locator('#notice')).to_be_hidden()
            expect(page.locator('#form')).to_be_visible()
            stored = json.loads(page.evaluate('localStorage.getItem(%s)' % json.dumps(STORAGE_KEY)))
            assert stored == {'v': 1, 'owner': world['fp']}
            page.fill('#prompt', 'Capital of Germany?')
            expect(page.locator('#send')).to_be_enabled()
            page.click('#send')
            expect(page.locator('.msg.swarm')).to_have_text('Berlin.')
            shot(page, '21-chat-after-ack')

            page.reload()  # remembered on this origin: no notice the second time
            expect(page.locator('#form')).to_be_visible()
            expect(page.locator('#notice')).to_be_hidden()

            assert context.cookies() == []  # no cookies, ever
            assert all(url.startswith(origin + '/') for url in seen), seen  # nothing third-party
            assert complaints == [], complaints
        finally:
            browser.close()


def test_the_acknowledgement_is_per_origin_and_per_owner(world):
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            context = browser.new_context()
            page = context.new_page()
            page.goto(world['proxy'].origin + '/chat')
            page.click('#notice-ack')
            second = context.new_page()  # same browser, another swarm origin: asked again
            second.goto(world['other'].origin + '/chat')
            expect(second.locator('#notice')).to_be_visible()
            expect(second.locator('#form')).to_be_hidden()
            page.evaluate('localStorage.setItem(%s, JSON.stringify({v: 1, owner: "0000-0000-0000-0000"}))'
                          % json.dumps(STORAGE_KEY))  # the origin now speaks for a different owner
            page.reload()
            expect(page.locator('#notice')).to_be_visible()
            page.evaluate('localStorage.setItem(%s, "not json")' % json.dumps(STORAGE_KEY))
            page.reload()
            expect(page.locator('#notice')).to_be_visible()  # a damaged note is no consent
        finally:
            browser.close()


def test_send_stays_refused_before_the_acknowledgement_even_if_the_form_is_forced(world):
    origin = world['proxy'].origin
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_context().new_page()
            posts = []
            page.on('request', lambda r: posts.append(r.url) if r.url.endswith('/chat/api/send') else None)
            page.goto(origin + '/chat')
            page.evaluate("""() => { document.getElementById('form').hidden = false;
                document.getElementById('send').disabled = false;
                document.getElementById('prompt').value = 'sneaky';
                document.getElementById('form').requestSubmit(); }""")
            page.wait_for_timeout(300)
            assert posts == []
        finally:
            browser.close()


def test_forget_asks_first_says_what_it_resets_and_then_asks_for_the_notice_again(world):
    origin = world['proxy'].origin
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_context(viewport={'width': 900, 'height': 1000}).new_page()
            page.goto(origin + '/chat')
            page.click('#notice-ack')
            expect(page.locator('#forget-confirm')).to_be_hidden()
            page.click('#forget')
            expect(page.locator('#forget-confirm')).to_be_visible()
            expect(page.locator('#forget-confirm')).to_contain_text('network address')  # what the quota is keyed on
            expect(page.locator('#forget-confirm')).to_contain_text('quota')
            expect(page.locator('#forget-confirm')).to_contain_text('does not reset')  # said plainly
            shot(page, '22-chat-forget-confirm')
            page.click('#forget-no')
            expect(page.locator('#forget-confirm')).to_be_hidden()
            assert page.evaluate('localStorage.getItem(%s)' % json.dumps(STORAGE_KEY)) is not None
            page.click('#forget')
            page.click('#forget-yes')
            assert page.evaluate('localStorage.length') == 0  # nothing of ours is left in this browser
            expect(page.locator('#notice')).to_be_visible()
            expect(page.locator('#form')).to_be_hidden()
        finally:
            browser.close()


def test_a_swarm_that_is_not_open_says_chat_needs_a_grant_from_the_directory(tmp_path):
    proxy, fp, gateway = serve(tmp_path, 'c', access_mode='invite')
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            try:
                page = browser.new_context().new_page()
                page.goto(proxy.origin + '/chat')
                expect(page.locator('#notice')).to_contain_text(fp)
                expect(page.locator('#notice')).to_contain_text('not open')
                expect(page.locator('#notice')).to_contain_text('directory')
                expect(page.locator('#notice')).not_to_contain_text('50 requests')
            finally:
                browser.close()
    finally:
        proxy.close()
        gateway.shutdown()


def test_an_unlisted_swarm_claims_no_owner(tmp_path):
    gateway, _b = fake_gateway(['x'])
    (tmp_path / 'u').mkdir()
    app = FakeApp(tmp_path / 'u', 'http://127.0.0.1:%d' % gateway.server_port)
    app.chat = ChatService(app)
    proxy = SeedRelayProxy().start()
    proxy.bind_chat(lambda m, p, h, b: handle(app, m, p, h, b, via_relay=True))
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            try:
                page = browser.new_context().new_page()
                page.goto(proxy.origin + '/chat')
                expect(page.locator('#notice')).to_contain_text('not listed')
                expect(page.locator('#notice')).to_contain_text('whoever shared this link')
                expect(page.locator('#notice-ack')).to_be_visible()
            finally:
                browser.close()
    finally:
        proxy.close()
        gateway.shutdown()


def test_the_chat_assets_set_no_cookie_and_keep_the_csp(world):
    origin = world['proxy'].origin
    for path in ('/chat', '/chat/chat.js', '/chat/chat.css'):
        connection = http.client.HTTPConnection('127.0.0.1', int(origin.rsplit(':', 1)[1]), timeout=3)
        connection.request('GET', path)
        response = connection.getresponse()
        response.read()
        headers = {name.lower(): value for name, value in response.getheaders()}
        connection.close()
        assert response.status == 200, path
        assert 'set-cookie' not in headers, path
        assert headers['content-security-policy'] == chat.CSP, path
        assert headers['referrer-policy'] == 'no-referrer', path
