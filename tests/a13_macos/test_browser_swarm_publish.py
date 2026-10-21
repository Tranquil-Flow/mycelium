# SPDX-License-Identifier: AGPL-3.0-only
"""Real Chromium over the owner UI: publish the run as a swarm, approve an access request.

Real: DemoApp and its page, swarm_publish, the directory server.  The relay
origin is a stub (no tunnel is started) and the run is not serving, so the
listing shows ``offline`` -- the serving path is covered in tests/swarm_directory.
"""
import json
import time
import urllib.request

from playwright.sync_api import expect, sync_playwright

from mycelium_a13_macos.app import DemoApp
from mycelium_a13_macos.swarm_publish import SwarmPublisher
from mycelium_swarm_directory import docs
from mycelium_swarm_directory.crypto import Ed25519Signer, verify
from mycelium_swarm_directory.directory import Directory
from mycelium_swarm_directory.server import DirectoryServer
from mycelium_swarm_directory.store import MemoryStore
from test_app_authority import CONSENT


def post(origin, path, document):
    request = urllib.request.Request(origin + '/swarms/api/v1' + path, data=json.dumps(document).encode(),
                                     headers={'Content-Type': 'application/json', 'X-Mycelium-Swarms': '1'})
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=10) as response:
        return json.load(response)


def test_owner_publishes_run_and_approves_a_request(tmp_path, monkeypatch):
    monkeypatch.delenv('MYCELIUM_A13_RUN_MODEL', raising=False)
    directory = DirectoryServer(Directory(MemoryStore(), verify, allow_loopback_entries=True)).start()
    app = DemoApp(tmp_path / 'owner', check_platform=False).start()
    errors = []
    try:
        app.action('/api/create', {'consent': CONSENT})
        app.swarm_publisher = SwarmPublisher(app, origin=lambda: 'http://127.0.0.1:9999')  # stub relay origin
        with sync_playwright() as p:
            browser = p.chromium.launch()
            try:
                page = browser.new_page()
                page.on('pageerror', lambda e: errors.append(type(e).__name__))
                page.goto(app.launch_file.as_uri())
                page.get_by_role('button', name='Open private demo', exact=True).click()
                expect(page.locator('#swarm-publish')).to_be_visible()
                expect(page.locator('#swarm-key')).to_contain_text("This Mac's swarm key")
                page.fill('#swarm-directory', directory.origin)
                page.fill('#swarm-name', 'Owner Macs')
                page.click('#swarm-publish-btn')
                expect(page.locator('#swarm-state')).to_contain_text('Published "Owner Macs"', timeout=10000)
                swarm_id = app.swarm_publisher.state['swarm_id']
                row, = directory.directory.list_swarms()
                assert row['swarm_id'] == swarm_id and row['status'] == 'offline'  # run not serving

                visitor = Ed25519Signer.generate()
                post(directory.origin, '/access-requests',
                     docs.access_request(visitor, swarm_id, note='please', at=int(time.time())))
                page.click('#swarm-inbox')
                page.locator('[data-approve]').click()
                expect(page.locator('#swarm-requests')).to_contain_text('granted', timeout=10000)
                mine = post(directory.origin, '/my-grants', docs.grantee_query(visitor, at=int(time.time())))
                grant, = mine['grants']
                assert grant['issuer_key'] == app.swarm_publisher.signer.public_key

                page.click('#swarm-unpublish')
                expect(page.locator('#swarm-state')).to_contain_text('Not published', timeout=10000)
            finally:
                browser.close()
        assert errors == []
    finally:
        app.close()
        directory.close()
