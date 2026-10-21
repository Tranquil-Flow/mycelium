# SPDX-License-Identifier: AGPL-3.0-only
"""Actual Chromium -> app HTTP -> acquisition -> tiny local origin; no model run."""
import json
import os
from pathlib import Path
from playwright.sync_api import sync_playwright, expect
from mycelium_a13_macos.app import DemoApp
from mycelium_a13_macos.acquisition import SelectedModelAcquisition
from test_acquisition import _FixtureServer, _repository_files
from test_app_acquisition import GOOD


def test_browser_source_download_progress_and_recovery(tmp_path):
    with _FixtureServer(tmp_path, _repository_files(), https=True) as origin:
        fixture=origin.fixture
        app=DemoApp(tmp_path/'application',check_platform=False).start()
        app.acquisition.close()
        app.acquisition=SelectedModelAcquisition(app.root/'model-source',_transport=origin.transport())
        app._policy_observer=lambda:dict(GOOD)
        try:
            with sync_playwright() as p:
                browser=p.chromium.launch()
                context=browser.new_context(viewport={'width':1280,'height':900})
                page=context.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
                page.goto(app.launch_file.as_uri());page.get_by_role('button',name='Open private demo',exact=True).click()
                expect(page.locator('#private')).to_be_visible()
                assert fixture.requests==[]
                assert page.locator('#download-weights').is_disabled()
                page.locator('#visibility').check();page.locator('#create').click()
                expect(page.locator('#ladder')).to_contain_text('current')
                assert page.locator('#download-weights').is_disabled()
                page.locator('#download-consent').check()
                expect(page.locator('#download-weights')).to_be_enabled()
                page.locator('#download-weights').click()
                expect(page.locator('#acquisition-state')).to_contain_text('prepared',timeout=20000)
                assert fixture.requests
                prepared=app.acquisition.prepared_source()
                assert prepared.manifest['runtime_model']['architecture']=='qwen2'
                assert page.locator('#submit').is_disabled()
                page.reload()
                expect(page.locator('#acquisition-state')).to_contain_text('prepared')
                expect(page.locator('#acquisition-progress')).to_have_attribute('value',str(sum(map(len,fixture.files.values()))))
                assert page.locator('#verifier-connect').count()==0
                clean=browser.new_context().new_page();clean.goto(app.origin)
                expect(clean.locator('#private')).to_be_hidden()
                assert clean.locator('#acquisition-state').inner_text()==''
                screenshots=Path(os.environ.get('A13_BROWSER_EVIDENCE',str(tmp_path/'screens')));screenshots.mkdir(parents=True,exist_ok=True)
                page.screenshot(path=str(screenshots/'acquisition-desktop.png'),full_page=True)
                page.set_viewport_size({'width':390,'height':844})
                assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
                page.screenshot(path=str(screenshots/'acquisition-mobile.png'),full_page=True)
                assert errors==[]
                browser.close()
        finally:app.close()
