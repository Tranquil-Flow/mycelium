# SPDX-License-Identifier: AGPL-3.0-only
"""Real mobile layout gate for populated signed stats (synthetic measurements)."""
import time
from playwright.sync_api import sync_playwright, expect
from mycelium_swarm_directory import docs, stats_docs
from mycelium_swarm_directory.crypto import Ed25519Signer, verify
from mycelium_swarm_directory.directory import Directory
from mycelium_swarm_directory.server import DirectoryServer
from mycelium_swarm_directory.store import SqliteStore
from tests.swarm_directory.test_performance_stats import payload, call, SWARM


def test_mobile_stats_fit_and_are_keyboard_scrollable(tmp_path):
    now=int(time.time())
    owner=Ed25519Signer.generate()
    directory=Directory(SqliteStore(tmp_path/'d.sqlite3'),verify,clock=lambda:now)
    record=docs.swarm_record(owner,swarm_id=SWARM,seq=1,name='Synthetic stats',model='Qwen/Qwen3-0.6B',
        entry={'key':owner.public_key,'origin':'https://entry.example.org'})
    assert call(directory,'/records',record)[0]==200
    assert call(directory,'/heartbeat',docs.heartbeat(owner,SWARM,status='serving',members=2,served_model=record['model'],at=now))[0]==200
    values=payload(); values['serving_since']=now-100
    values['hops']=[{'from_stage':None,'to_stage':1,'kind':'owner_control_rtt_probe','rtt_ms':125,'samples':8,'age_seconds':2}]
    assert call(directory,'/stats',stats_docs.stats(owner,SWARM,at=now,values=values))[0]==200
    server=DirectoryServer(directory).start()
    try:
        with sync_playwright() as p:
            browser=p.chromium.launch()
            try:
                page=browser.new_context(viewport={'width':390,'height':844}).new_page()
                page.route('**/*',lambda r:r.continue_() if r.request.url.startswith(server.origin+'/') else r.abort())
                page.goto(server.origin+'/swarms/#swarm/'+SWARM)
                expect(page.locator('#detail-performance')).to_contain_text('125 ms')
                assert page.locator('.stats-table-wrap').evaluate_all('xs=>xs.every(x=>x.scrollWidth<=x.clientWidth)'), 'All metric columns must fit without hidden horizontal scrolling'
                panel=page.locator('#detail-performance')
                expect(panel).to_have_attribute('tabindex','0')
                expect(panel).to_contain_text('Scroll this panel for all measurements')
                maximum = panel.evaluate('e=>e.scrollHeight-e.clientHeight')
                assert maximum > 0
                panel.focus(); page.keyboard.press('End')
                expect(panel).to_have_js_property('scrollTop', maximum)
            finally: browser.close()
    finally: server.close()
