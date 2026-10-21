# SPDX-License-Identifier: AGPL-3.0-only
"""The helper download card and the '/' redirect as a visitor meets them through the apex proxy.

Real: the directory server and its page, the shipped public-origin proxy, a real headless Chromium (CSP intact, no
page.evaluate strings).  FAKE (labelled): the product upstream behind '/' (a one-line page) and the helper zip (a
fixture file, not a package).  Screenshots are written when HOSTED_UI_SHOTS names a directory.
"""
from __future__ import annotations

import hashlib
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from playwright.sync_api import expect, sync_playwright

from mycelium_demo import public_origin_proxy as proxy
from mycelium_swarm_directory.crypto import verify
from mycelium_swarm_directory.directory import Directory
from mycelium_swarm_directory.server import DirectoryServer
from mycelium_swarm_directory.store import SqliteStore

from test_hosted_ui_browser import shot

NAME = 'Mycelium-helper-mac.zip'
PAYLOAD = b'PK\x03\x04 fixture, not a real package ' * 400
SHA = hashlib.sha256(PAYLOAD).hexdigest()


class _Product(ThreadingHTTPServer):
    """FAKE consumer demo behind '/'."""

    def __init__(self):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                body = b'<html><body><h1 id="consumer">consumer demo</h1></body></html>'
                self.send_response(200)
                self.send_header('Content-Type', 'text/html')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        super().__init__(('127.0.0.1', 0), Handler)


@contextmanager
def apex(tmp_path, *, zip_present, redirect=True, product_routes=False):
    zip_path = tmp_path / NAME
    if zip_present:
        zip_path.write_bytes(PAYLOAD)
        zip_path.chmod(0o600)
    directory = DirectoryServer(Directory(SqliteStore(tmp_path / 'd.sqlite3'), verify, allow_loopback_entries=True)).start()
    product = _Product()
    threading.Thread(target=product.serve_forever, daemon=True).start()
    config = proxy.ProxyConfig(
        listen_host='127.0.0.1', listen_port=4350, upstream_host='127.0.0.1', upstream_port=product.server_port,
        public_host='mycelium.now', capability='a' * 64, directory_host='127.0.0.1',
        directory_port=directory.server_port, directory_require_client_ip=False, helper_zip_path=str(zip_path),
        helper_version='0.1.0-preview.2', redirect_root_to_swarms=redirect,
        enable_product_routes=product_routes)
    server = proxy.PublicOriginProxyServer(('127.0.0.1', 0), config)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f'http://127.0.0.1:{server.server_port}'
    finally:
        for item in (server, product):
            item.shutdown()
            item.server_close()
        directory.close()


def test_the_download_card_links_the_served_file_with_its_checksum_and_the_unnotarized_warning(tmp_path):
    with apex(tmp_path, zip_present=True) as origin, sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_context(accept_downloads=True).new_page()
            page.goto(origin + '/swarms')
            page.wait_for_selector('body[data-ready="1"]')
            expect(page.locator('#helper-card')).to_have_attribute('data-download-state', 'available')
            expect(page.locator('#download-sha')).to_have_text(SHA)
            expect(page.locator('#helper-card')).to_contain_text('not notarized')
            expect(page.locator('#helper-card')).not_to_contain_text('not published yet')
            shot(page, '16-home-download-served-by-proxy')
            with page.expect_download() as download:
                page.click('#download-link')
            saved = download.value
            assert saved.suggested_filename == NAME
            assert hashlib.sha256(open(saved.path(), 'rb').read()).hexdigest() == SHA  # the page's checksum is the file's
        finally:
            browser.close()


def test_a_missing_file_leaves_the_card_on_not_published_yet(tmp_path):
    with apex(tmp_path, zip_present=False) as origin, sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_context().new_page()
            page.goto(origin + '/swarms')
            page.wait_for_selector('body[data-ready="1"]')
            expect(page.locator('#helper-card')).to_have_attribute('data-download-state', 'unavailable')
            expect(page.locator('#helper-card')).to_contain_text('not published yet')
            assert page.locator('#download-link').count() == 0
            shot(page, '17-home-download-file-missing')
        finally:
            browser.close()


def test_the_root_lands_a_visitor_on_the_swarms_page(tmp_path):
    with apex(tmp_path, zip_present=True) as origin, sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_context().new_page()
            page.goto(origin + '/')
            page.wait_for_selector('body[data-ready="1"]')
            assert page.url == origin + '/swarms/'
            expect(page.locator('#helper-card')).to_be_visible()
            expect(page.locator('#consumer')).to_have_count(0)
            shot(page, '18-root-redirects-to-swarms')
        finally:
            browser.close()


def test_with_the_redirect_off_the_root_is_the_consumer_demo_only_when_product_routes_are_enabled(tmp_path):
    with apex(tmp_path, zip_present=False, redirect=False, product_routes=True) as origin, sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_context().new_page()
            page.goto(origin + '/')
            expect(page.locator('#consumer')).to_be_visible()
            assert page.url == origin + '/'
        finally:
            browser.close()
