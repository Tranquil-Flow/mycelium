# SPDX-License-Identifier: AGPL-3.0-only
"""Hosted-demo UI readiness: what a visitor and an owner see on /swarms, in a real headless Chromium.

Real: the directory server and web page (WebCrypto Ed25519), the A13 relay + swarm-entry hook, SwarmPublisher (link
mode: the browser owns the swarm), EntryGuard/EntryGateway, ChatService with its real cancel path, the live
projection.  FAKE (labelled): the generation backend (``Script``: scripted tokens, honours cancel) and the owner
app's run / adaptive / recovery / observatory documents (shaped like the real ones).  Nothing is mocked in the page.
Screenshots are written when HOSTED_UI_SHOTS names a directory.
"""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import pytest
from playwright.sync_api import expect, sync_playwright

from mycelium_a13_macos import swarm_entry, swarm_live
from mycelium_a13_macos.chat import ChatService
from mycelium_a13_macos.public_relay import SeedRelayProxy
from mycelium_a13_macos.swarm_publish import SwarmPublisher
from mycelium_swarm_directory.crypto import verify
from mycelium_swarm_directory.directory import Directory
from mycelium_swarm_directory.server import DirectoryServer
from mycelium_swarm_directory.store import SqliteStore

from _swarm_fixtures import FakeApp, request

SHOTS = os.environ.get('HOSTED_UI_SHOTS')
DOWNLOAD = {'available': True, 'url': '/swarms/download/mycelium-0.1.0-preview.2-macos-arm64.zip',
            'sha256': 'ab' * 32, 'version': '0.1.0-preview.2', 'notarized': False}


def shot(page, name):
    if SHOTS:
        Path(SHOTS).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(Path(SHOTS) / (name + '.png')), full_page=True)


class Script:
    """FAKE generation backend: scripted by the prompt's first word; honours cancel like the real client."""
    instances: list = []

    def __init__(self, origin):
        self.cancelled = threading.Event()
        Script.instances.append(self)

    def cancel(self):
        self.cancelled.set()
        return True

    def stream(self, prompt, max_new_tokens, on_token):
        if prompt.startswith('fail'):
            on_token('Par')
            return {'terminal': 'failed', 'code': 'route_lost_a_helper'}
        if prompt.startswith('cancel'):
            on_token('Par')
            return {'terminal': 'cancelled', 'code': None}
        if prompt.startswith('slow'):
            for i in range(60):
                if self.cancelled.wait(0.15):
                    return {'terminal': 'cancelled', 'code': None}
                on_token('tok%d ' % i)
            return {'terminal': 'completed', 'code': None}
        for text in ('Ber', 'lin', '.'):
            on_token(text)
            time.sleep(0.05)
        return {'terminal': 'completed', 'code': None}


RUN_DOC = {'state': 'serving', 'step': 'serving', 'failed_step': None, 'error_code': None, 'route_ready': True,
           'run_id': 'run-0123456789abcdef', 'started_at': 1_800_000_000, 'registry_origin': 'http://127.0.0.1:9',
           'participants': [{'node_id': 'a' * 20, 'label': 'This Mac (owner)', 'layers': [0, 14], 'state': 'serving', 'detail': None},
                            {'node_id': 'b' * 20, 'label': 'MacBook helper', 'layers': [14, 28], 'state': 'withdrawn',
                             'detail': 'helper withdrew its capacity'}]}
RECOVERY = {'requests': [{'request_id': 'r1', 'path_id': 'p', 'attempt': 1, 'path_attempt': 0, 'phase': 'done',
                          'committed_token_count': 3, 'recovery_attempt': 1, 'terminal': 'completed',
                          'cleanup_complete': True, 'reconciled': 'resumed'},
                         {'request_id': 'r2', 'path_id': 'p', 'attempt': 1, 'path_attempt': 0, 'phase': 'done',
                          'committed_token_count': 1, 'recovery_attempt': 2, 'terminal': 'failed',
                          'cleanup_complete': True, 'reconciled': 'aborted'}], 'breaker': {'state': 'closed'}}
TRANSPORT = {'segments': [{'edge_kind': 'transport_observation', 'transport': 'relay'},
                          {'edge_kind': 'transport_observation', 'transport': 'direct'}]}
ADAPTIVE = {'enabled': True, 'game_mode': False, 'stopped': False, 'state': 'drain', 'mode': 'drain',
            'reason': 'memory_pressure', 'generation': 3, 'capacity_scale': 0.5, 'new_admissions_allowed': False,
            'expires_in_s': 12.0, 'withdraw_overdue': False, 'reserve_bytes': 1}


def make_stack(tmp_path, *, download=None):
    Script.instances = []
    directory = DirectoryServer(Directory(SqliteStore(tmp_path / 'd.sqlite3'), verify, allow_loopback_entries=True),
                                download=download).start()
    (tmp_path / 'mac').mkdir()
    app = FakeApp(tmp_path / 'mac', 'http://127.0.0.1:9')
    app.owner_run.status = lambda: dict(RUN_DOC)
    app.chat = ChatService(app, client_factory=Script)
    proxy = SeedRelayProxy().start()
    reader = lambda path: RECOVERY if 'recovery' in path else TRANSPORT  # noqa: E731
    publisher = SwarmPublisher(app, origin=lambda: proxy.origin,
                               live=lambda: swarm_live.live_state(app, adaptive=lambda: ADAPTIVE, read_json=reader))
    app.swarm_publisher = publisher
    swarm_entry.bind(app, proxy)
    return {'directory': directory, 'publisher': publisher, 'proxy': proxy, 'app': app}


def close_stack(stack):
    stack['publisher'].close()
    stack['proxy'].close()
    stack['directory'].close()


@pytest.fixture
def stack(tmp_path):
    value = make_stack(tmp_path)
    yield value
    close_stack(value)


def open_page(browser, stack):
    page = browser.new_context().new_page()
    page.goto(stack['directory'].origin + '/swarms')
    page.wait_for_selector('body[data-ready="1"]')
    return page


def create_swarm(owner, stack, name, mode='open', requests=20, tokens=4000):
    owner.fill('#c-name', name)
    owner.select_option('#c-mode', mode)
    owner.fill('#c-req', str(requests))
    owner.fill('#c-tok', str(tokens))
    owner.click('#create-form button[type=submit]')
    link = owner.wait_for_selector('[data-owned] [data-link]').text_content()
    state = stack['publisher'].publish({'link': link})
    owner.click('#refresh')
    owner.click('[data-attach="%s"]' % state['entry_fingerprint'])
    owner.wait_for_selector('[data-attached="%s"]' % state['entry_fingerprint'])
    assert stack['publisher'].tick()['attached'] is True
    owner.click('#refresh')
    return state


def chat(visitor, name, prompt):
    visitor.click('#refresh')
    visitor.locator('.swarm', has_text=name).locator('button', has_text='Chat').click()
    visitor.fill('#prompt', prompt)
    visitor.click('#send')


def terminal(visitor, kind, timeout=15000):
    node = visitor.locator('.msg.terminal[data-terminal="%s"]' % kind).last
    node.wait_for(timeout=timeout)
    return node


def poll_text(locator, predicate, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        text = locator.text_content() or ''
        if predicate(text):
            return text
        time.sleep(0.05)
    raise AssertionError('text never matched; last: %r' % locator.text_content())


def get_open_access(visitor, name):
    card = visitor.locator('.swarm', has_text=name)
    card.locator('button', has_text='Get open access').click()
    expect(card.locator('button', has_text='Chat')).to_be_visible()


# -- 1. the way in: flow, helper download, honest unavailability ----------------------------------

def test_flow_and_helper_download_are_unavailable_until_published(stack):
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = open_page(browser, stack)
            steps = page.locator('#flow li[data-step]')
            assert [steps.nth(i).get_attribute('data-step') for i in range(steps.count())] == ['create', 'helper', 'access', 'run']
            card = page.locator('#helper-card')
            assert card.get_attribute('data-download-state') == 'unavailable'
            expect(card).to_contain_text('not published yet')
            expect(page.locator('#download-link')).to_have_count(0)
            expect(card).to_contain_text('Invite a Mac')  # the app, not this page, makes the join link
            shot(page, '01-home-download-unavailable')
        finally:
            browser.close()


def test_published_download_shows_link_checksum_and_the_unnotarized_warning(tmp_path):
    stack = make_stack(tmp_path, download=DOWNLOAD)
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            try:
                page = open_page(browser, stack)
                card = page.locator('#helper-card')
                assert card.get_attribute('data-download-state') == 'available'
                assert page.locator('#download-link').get_attribute('href') == DOWNLOAD['url']
                expect(page.locator('#download-sha')).to_have_text(DOWNLOAD['sha256'])
                expect(card).to_contain_text('not notarized')
                expect(card).to_contain_text('right-click')
                shot(page, '02-home-download-available')
            finally:
                browser.close()
    finally:
        close_stack(stack)


# -- 2. owner: create, set access, live panel, helpers, grants, usage -----------------------------

def test_owner_changes_access_mode_and_sees_helpers_grants_usage_and_the_kill_switch(stack):
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            owner = open_page(browser, stack)
            create_swarm(owner, stack, 'Hosted demo', 'request')
            panel = owner.locator('[data-owned]')
            expect(panel.locator('[data-step-help="helper"]')).to_contain_text('Invite a Mac')
            expect(panel.locator('[data-owner-helpers]')).to_contain_text('MacBook helper')
            expect(panel.locator('[data-owner-helpers]')).to_contain_text('layers 14-28')
            expect(panel.locator('[data-owner-grants]')).to_contain_text('No grants yet')
            expect(panel.locator('[data-owner-usage]')).to_contain_text('not shown')  # per-key usage stays on the entry Mac
            shot(owner, '03-owner-panel')

            visitor = open_page(browser, stack)
            card = visitor.locator('.swarm', has_text='Hosted demo')
            card.locator('input').fill('please')
            card.locator('button', has_text='Request access').click()
            visitor_fp = visitor.text_content('#identity').split()[-1]
            owner.click('#refresh')
            owner.click('[data-approve="%s"]' % visitor_fp)
            owner.click('#refresh')
            expect(owner.locator('[data-owned] [data-owner-grants]')).to_contain_text('granted')

            # request -> open -> invite, each saved as a new signed record and picked up by the entry
            for mode, expected in (('open', 'Get open access'), ('invite', 'Invite only')):
                owner.select_option('[data-owned] select.access-mode', mode)
                owner.click('[data-owned] button.save-access')
                expect(owner.locator('[data-owned] .access-state')).to_contain_text('Saved')
                stack['publisher'].tick()
                newcomer = open_page(browser, stack)  # a visitor with no grant sees the new mode
                expect(newcomer.locator('.swarm', has_text='Hosted demo')).to_contain_text(expected)
                expect(newcomer.locator('.swarm', has_text='Hosted demo')).to_contain_text('access ' + mode)
            shot(newcomer, '04-visitor-invite-only')
        finally:
            browser.close()


def test_visitor_card_shows_every_live_section_with_unavailable_ones_marked(stack):
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            owner = open_page(browser, stack)
            create_swarm(owner, stack, 'Hosted demo', 'open')
            visitor = open_page(browser, stack)
            live = visitor.locator('.swarm', has_text='Hosted demo').locator('.live')
            expect(live.locator('[data-live="route"]')).to_contain_text('serving')
            expect(live.locator('[data-live="helpers"]')).to_contain_text('MacBook helper')
            expect(live.locator('[data-live="helpers"]')).to_contain_text('withdrawn')
            expect(live.locator('[data-live="helpers"]')).to_contain_text('helper withdrew its capacity')
            expect(live.locator('[data-live="transport"]')).to_contain_text('1 relay')
            expect(live.locator('[data-live="transport"]')).to_contain_text('1 direct')
            expect(live.locator('[data-live="recovery"]')).to_contain_text('1 recovered')
            expect(live.locator('[data-live="recovery"]')).to_contain_text('1 aborted')
            expect(live.locator('[data-live="host"]')).to_contain_text('drain')
            expect(live.locator('[data-live="host"]')).to_contain_text('memory_pressure')
            expect(live.locator('[data-live="sessions"]')).to_contain_text('one prompt at a time')
            shot(visitor, '05-visitor-live-status')
            # a source that is not available says so instead of showing a green or a zero
            stack['app'].owner_run.status = lambda: dict(RUN_DOC, participants=None)
            visitor.click('#refresh')
            helpers = live.locator('[data-live="helpers"]')
            expect(helpers).to_contain_text('Unavailable')
            assert helpers.get_attribute('data-available') == 'false'
        finally:
            browser.close()


# -- 3. visitor: inference with every terminal state ------------------------------------------------

def test_inference_streams_tokens_and_every_terminal_state_is_named(stack):
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            owner = open_page(browser, stack)
            create_swarm(owner, stack, 'Hosted demo', 'open', requests=3, tokens=2000)
            visitor = open_page(browser, stack)
            get_open_access(visitor, 'Hosted demo')

            chat(visitor, 'Hosted demo', 'capital of Germany?')
            expect(visitor.locator('.msg.swarm').last).to_have_text('Berlin.', timeout=10000)
            expect(terminal(visitor, 'completed')).to_contain_text('Completed')
            expect(visitor.locator('#log')).to_contain_text('Served by: This Mac (owner) (layers 0-14), MacBook helper (layers 14-28)')
            shot(visitor, '06-chat-completed')

            visitor.fill('#prompt', 'fail please')
            visitor.click('#send')
            expect(terminal(visitor, 'failed')).to_contain_text('route_lost_a_helper')
            expect(visitor.locator('.msg.swarm').last).to_contain_text('Par')  # what arrived before the failure stays
            shot(visitor, '07-chat-failed')

            visitor.fill('#prompt', 'cancel please')
            visitor.click('#send')
            expect(terminal(visitor, 'cancelled')).to_contain_text('Cancelled')
            shot(visitor, '08-chat-cancelled')

            visitor.fill('#prompt', 'one more')
            visitor.click('#send')
            expect(terminal(visitor, 'quota')).to_contain_text('used up')
            shot(visitor, '09-chat-quota-exhausted')
        finally:
            browser.close()


def test_tokens_arrive_progressively_and_the_owner_kill_switch_ends_it_and_cancels_the_generation(stack):
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            owner = open_page(browser, stack)
            create_swarm(owner, stack, 'Hosted demo', 'open')
            visitor = open_page(browser, stack)
            get_open_access(visitor, 'Hosted demo')
            chat(visitor, 'Hosted demo', 'slow story')
            answer = visitor.locator('.msg.swarm').last
            first = poll_text(answer, lambda text: 'tok1 ' in text)  # CSP forbids eval, so poll from Python
            later = poll_text(answer, lambda text: len(text) > len(first))
            assert len(later) > len(first)  # still growing: live streaming, not one final paint
            assert visitor.locator('.msg.terminal').count() == 0  # no terminal while it runs

            owner.click('#refresh')
            owner.click('[data-owned] button[data-kill]')
            expect(owner.locator('[data-owned] [data-kill-state]')).to_have_text('Kill switch ENGAGED')
            expect(terminal(visitor, 'killed')).to_contain_text('owner')
            shot(visitor, '10-chat-killed-by-owner')
            deadline = time.monotonic() + 5
            while not Script.instances[-1].cancelled.is_set() and time.monotonic() < deadline:
                time.sleep(0.05)
            assert Script.instances[-1].cancelled.is_set()  # the backend generation was stopped, not just hidden
            shot(owner, '11-owner-kill-engaged')

            visitor.fill('#prompt', 'after kill')
            visitor.click('#send')
            expect(visitor.locator('.msg.terminal[data-terminal="killed"]')).to_have_count(2, timeout=10000)

            owner.click('[data-owned] button[data-kill]')
            expect(owner.locator('[data-owned] [data-kill-state]')).to_have_text('Kill switch off')
            visitor.fill('#prompt', 'back again')
            visitor.click('#send')
            expect(terminal(visitor, 'completed')).to_be_visible()
        finally:
            browser.close()


def test_a_visitor_can_stop_their_own_prompt_and_is_told_it_was_cancelled_by_them(stack):
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            owner = open_page(browser, stack)
            create_swarm(owner, stack, 'Hosted demo', 'open', requests=3, tokens=2000)
            visitor = open_page(browser, stack)
            get_open_access(visitor, 'Hosted demo')
            visitor.locator('.swarm', has_text='Hosted demo').locator('button', has_text='Chat').click()
            expect(visitor.locator('#stop')).to_be_hidden()  # nothing to stop yet

            visitor.fill('#prompt', 'slow story')
            visitor.click('#send')
            expect(visitor.locator('#stop')).to_be_visible(timeout=10000)
            poll_text(visitor.locator('.msg.swarm').last, lambda text: 'tok1 ' in text)
            assert visitor.locator('.msg.terminal').count() == 0
            shot(visitor, '14-chat-stop-button-while-generating')

            visitor.click('#stop')
            expect(terminal(visitor, 'cancelled_by_you')).to_contain_text('Cancelled by you')
            assert visitor.locator('.msg.terminal[data-terminal="killed"], .msg.terminal[data-terminal="failed"]').count() == 0
            expect(visitor.locator('#stop')).to_be_hidden()
            expect(visitor.locator('#send')).to_be_enabled()
            assert Script.instances[-1].cancelled.is_set()  # the generation on the route was stopped
            shot(visitor, '15-chat-cancelled-by-you')

            visitor.fill('#prompt', 'capital of Germany?')  # the single-flight slot is free again
            visitor.click('#send')
            expect(terminal(visitor, 'completed')).to_be_visible()
            expect(visitor.locator('#stop')).to_be_hidden()
        finally:
            browser.close()


def test_a_revoked_grant_is_reported_as_not_granted(stack):
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            owner = open_page(browser, stack)
            create_swarm(owner, stack, 'Hosted demo', 'open')
            visitor = open_page(browser, stack)
            get_open_access(visitor, 'Hosted demo')
            chat(visitor, 'Hosted demo', 'capital of Germany?')
            terminal(visitor, 'completed')
            ledger = stack['publisher'].ledger
            for grant_id in [row[0] for row in ledger._db.execute('SELECT grant_id FROM usage')]:  # the app's revoke action
                ledger.revoke(grant_id, int(time.time()))
            visitor.fill('#prompt', 'second try')
            visitor.click('#send')
            expect(terminal(visitor, 'not_granted')).to_contain_text('revoked')
            shot(visitor, '12-chat-not-granted')
        finally:
            browser.close()


def test_a_request_mode_visitor_without_a_grant_is_told_it_is_not_granted(stack):
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            owner = open_page(browser, stack)
            create_swarm(owner, stack, 'Hosted demo', 'request')
            visitor = open_page(browser, stack)
            card = visitor.locator('.swarm', has_text='Hosted demo')
            assert card.locator('button', has_text='Chat').count() == 0
            expect(card.locator('[data-access-note]')).to_contain_text('Not granted')
            shot(visitor, '13-visitor-not-granted')
        finally:
            browser.close()
