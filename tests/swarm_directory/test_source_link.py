# SPDX-License-Identifier: AGPL-3.0-only
"""The network-use source offer and the CCAI Discord invite are real links on
every served HTML shell."""
import http.client
import json
from html.parser import HTMLParser

import pytest
from playwright.sync_api import expect, sync_playwright

from mycelium_swarm_directory.crypto import verify
from mycelium_swarm_directory.directory import Directory
from mycelium_swarm_directory.server import ASSETS, DirectoryServer, csp
from mycelium_swarm_directory.store import MemoryStore

SOURCE = 'https://github.com/Tranquil-Flow/mycelium'
DISCORD = 'https://discord.gg/XnagJyDsZ'
DOWNLOAD = {'available': True, 'url': SOURCE + '/releases/tag/v0.1.4',
            'sha256': 'fce4743a7f3a2453f6621753ed3892a6cefaddf52e7a3e98fd79ba51f8930025',
            'version': '0.1.4', 'notarized': False}


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


def test_config_publishes_the_released_mac_app(directory):
    from mycelium_swarm_directory.hosted_defaults import HELPER_DOWNLOAD
    assert HELPER_DOWNLOAD == DOWNLOAD
    connection = http.client.HTTPConnection('127.0.0.1', directory.server_port)
    try:
        connection.request('GET', '/swarms/api/v1/config')
        response = connection.getresponse()
        assert response.status == 200
        assert json.loads(response.read())['download'] == DOWNLOAD
        assert response.getheader('Set-Cookie') is None
    finally:
        connection.close()


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
        assert len(source_links) == {'/': 2, '/about': 3}.get(path, 1)  # /about: header, technical CTA, footer
        for link in source_links:
            assert 'noopener' in link.get('rel', '').split()
            assert 'target' not in link
        if path == '/':
            assert 'The helper download may not yet be published' not in body
        assert 'Source code on GitHub' in body
    finally:
        connection.close()


@pytest.mark.parametrize('path', [path for path, (_, mime) in ASSETS.items() if mime.startswith('text/html')])
def test_ccai_discord_invite_in_every_html_response(directory, path):
    """The owner's CCAI Discord invite is a real footer link on every shell,
    plus once in-page where the owner asked for it (the landing 'This is the
    first step' row, the Contributing card, both About CCAI sections), with
    the same off-origin link hygiene as the source offer."""
    connection = http.client.HTTPConnection('127.0.0.1', directory.server_port)
    try:
        connection.request('GET', path)
        response = connection.getresponse()
        body = response.read().decode()
        assert response.status == 200
        assert response.getheader('Set-Cookie') is None
        links = Links()
        links.feed(body)
        invites = [link for link in links.links if link.get('href') == DISCORD]
        assert len(invites) == {'/': 2, '/about': 3, '/swarms': 2, '/swarms/': 2}.get(path, 1)  # /about: footer + both CCAI sections; others: footer + one in-page spot
        for link in invites:
            assert 'noopener' in link.get('rel', '').split()
            assert 'target' not in link
        assert 'CCAI Discord' in body
        footer = body[body.index('<footer'):body.index('</footer>')]
        assert DISCORD in footer, 'the invite is reachable from every footer'
    finally:
        connection.close()


def test_the_invite_sits_in_every_owner_named_spot():
    from pathlib import Path
    web = Path(__file__).resolve().parents[2] / 'mycelium_swarm_directory/web'
    about = (web / 'about.html').read_text()
    for section_id in ('s-bigger', 't-stack'):
        start = about.index(f'id="{section_id}"')
        assert DISCORD in about[start:about.index('</section>', start)], section_id
    home = (web / 'apex.html').read_text()
    closing = home.split('<h2>This is the first step</h2>', 1)[1].split('</section>', 1)[0]
    assert DISCORD in closing, 'landing closing CTA row'
    app = (web / 'index.html').read_text()
    contributing = app.split('id="panel-contributing"', 1)[1].split('</section>', 1)[0]
    assert DISCORD in contributing, 'Contributing card'


@pytest.mark.parametrize('theme', ['light', 'dark'])
@pytest.mark.parametrize('viewport', [{'width': 1440, 'height': 900}, {'width': 390, 'height': 844}])
def test_source_offer_visible_and_keyboard_accessible_on_hash_routes(directory, theme, viewport):
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        context = browser.new_context(viewport=viewport, color_scheme=theme)
        page = context.new_page()
        try:
            page.goto(directory.origin + '/')
            header_link = page.locator('header').get_by_role('link', name='Source code on GitHub', exact=True)
            expect(header_link).to_be_visible()
            header_link.focus()
            expect(header_link).to_be_focused()
            assert header_link.evaluate('e => { const r=e.getBoundingClientRect(); return r.left >= 0 && r.right <= innerWidth && r.bottom <= innerHeight; }')
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            page.goto(directory.origin + '/swarms/')
            page.wait_for_selector('body[data-ready="1"]')
            for route in ('browse', 'chat', 'contributing', 'owned', 'new', 'notice', 'swarm/missing'):
                page.evaluate('(route) => { location.hash = route; }', route)
                if route == 'contributing':
                    download = page.get_by_role('link', name='Download Mycelium 0.1.4', exact=True)
                    expect(download).to_be_visible()
                    expect(download).to_have_attribute('href', DOWNLOAD['url'])
                    expect(download).to_have_attribute('rel', 'noopener')
                    assert download.get_attribute('download') is None  # navigation to release/install steps
                    expect(page.locator('#download-sha')).to_have_text(DOWNLOAD['sha256'])
                    expect(page.locator('#helper-card')).to_contain_text('not notarized by Apple')
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
