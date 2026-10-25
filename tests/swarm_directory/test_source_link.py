# SPDX-License-Identifier: AGPL-3.0-only
"""The network-use source offer is a real link on every served HTML shell."""
import http.client
from html.parser import HTMLParser

import pytest
from playwright.sync_api import expect, sync_playwright

from mycelium_swarm_directory.crypto import verify
from mycelium_swarm_directory.directory import Directory
from mycelium_swarm_directory.server import ASSETS, DirectoryServer, csp
from mycelium_swarm_directory.store import MemoryStore

SOURCE = 'https://github.com/Tranquil-Flow/mycelium'


class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []

    def handle_starttag(self, tag, attrs):
        if tag == 'a':
            self.links.append(dict(attrs))


@pytest.fixture
def directory():
    server = DirectoryServer(Directory(MemoryStore(), verify)).start()
    try:
        yield server
    finally:
        server.close()


@pytest.mark.parametrize('path', [path for path, (_, mime) in ASSETS.items() if mime.startswith('text/html')])
def test_source_offer_in_every_html_response(directory, path):
    connection = http.client.HTTPConnection('127.0.0.1', directory.server_port)
    try:
        connection.request('GET', path)
        response = connection.getresponse()
        body = response.read().decode()
        assert response.status == 200
        assert response.getheader('Set-Cookie') is None
        assert response.getheader('Content-Security-Policy') == csp(False)
        links = Links()
        links.feed(body)
        source_links = [link for link in links.links if link.get('href') == SOURCE]
        assert len(source_links) == 1
        assert 'noopener' in source_links[0].get('rel', '').split()
        assert 'target' not in source_links[0]
        assert 'Source code on GitHub' in body
    finally:
        connection.close()


@pytest.mark.parametrize('theme', ['light', 'dark'])
@pytest.mark.parametrize('viewport', [{'width': 1440, 'height': 900}, {'width': 390, 'height': 844}])
def test_source_offer_visible_and_keyboard_accessible_on_hash_routes(directory, theme, viewport):
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        context = browser.new_context(viewport=viewport, color_scheme=theme)
        page = context.new_page()
        try:
            page.goto(directory.origin + '/swarms/')
            page.wait_for_selector('body[data-ready="1"]')
            for route in ('browse', 'chat', 'contributing', 'owned', 'new', 'notice', 'swarm/missing'):
                page.evaluate('(route) => { location.hash = route; }', route)
                link = page.get_by_role('link', name='Source code on GitHub', exact=True)
                expect(link).to_have_attribute('href', SOURCE)
                expect(link).to_have_attribute('rel', 'noopener')
                link.scroll_into_view_if_needed()
                expect(link).to_be_visible()
                link.focus()
                expect(link).to_be_focused()
                assert link.evaluate('e => { const r=e.getBoundingClientRect(); return r.left >= 0 && r.right <= innerWidth && document.elementFromPoint(r.x+r.width/2, r.y+r.height/2) === e; }')
            assert not context.cookies()
        finally:
            context.close()
            browser.close()
