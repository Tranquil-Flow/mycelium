# SPDX-License-Identifier: AGPL-3.0-only
"""Actual Chromium UI + canonical TLS/SQLite; not model-serving evidence."""
import json
import os
from pathlib import Path
import pytest
from playwright.sync_api import sync_playwright, expect
from mycelium_a13_macos.app import DemoApp, reopen_native


def test_real_browser_enrollment_lifecycle_and_privacy(tmp_path):
    app=DemoApp(tmp_path/'browser-state',check_platform=False).start()
    errors=[]
    try:
        with sync_playwright() as p:
            browser=p.chromium.launch()
            owner=browser.new_context(viewport={'width':1280,'height':900},reduced_motion='reduce')
            page=owner.new_page()
            page.on('pageerror',lambda e: errors.append(type(e).__name__))
            page.goto(app.launch_file.as_uri())
            page.get_by_role('button',name='Open private demo',exact=True).click()
            expect(page.locator('#private')).to_be_visible()
            expect(page.locator('#model-contract')).to_contain_text('Qwen/Qwen3-14B')  # demo default (run_model.py + per-model resources), was the 7B candidate
            expect(page.locator('#model-contract')).to_contain_text('40c069824f4251a91eefaf281ebe4c544efd3e18')
            expect(page.locator('#model-contract')).to_contain_text('int8-weight-only')
            # Core A13 has no hackathon verifier configuration or placeholder.
            assert page.locator('#verifier-connect').count()==0
            assert page.locator('#verifier-consent').count()==0
            assert 'verifier.mycelium.now' not in page.content()
            assert page.locator('#download-weights').is_disabled()
            page.locator('#visibility').check()
            page.get_by_role('button',name='Create authority & enroll this Mac',exact=True).click()
            expect(page.locator('#notice')).to_contain_text('enrolled',timeout=20000)
            expect(page.locator('#ladder')).to_contain_text('current')
            identity=app.authority.member.signer.verification_key_digest
            generation=app.authority.member.generation
            assert page.locator('#submit').is_disabled()
            page.get_by_role('button',name='Report this Mac capability',exact=True).click()
            expect(page.locator('#ladder')).to_contain_text('Self-report accepted')
            assert page.locator('#submit').is_disabled()
            for fragment in ['onboarding','devices','inference','lifecycle']:
                page.goto(app.origin+'/#'+fragment)
                page.reload()
                expect(page.locator('#ladder')).to_contain_text('current')
            page.go_back(); page.go_forward()
            with page.expect_response(lambda r: r.url.endswith('/api/stop') and r.request.method == 'POST') as stop_response:
                page.get_by_role('button',name='Stop & retain private state',exact=True).click()
            assert stop_response.value.ok, stop_response.value.text()
            expect(page.locator('#ladder')).to_contain_text('Stopped')
            with page.expect_response(lambda r: r.url.endswith('/api/start') and r.request.method == 'POST') as start_response:
                page.get_by_role('button',name='Start / reconnect',exact=True).click()
            assert start_response.value.ok, start_response.value.text()
            expect(page.locator('#ladder')).to_contain_text('current')
            assert app.authority.member.signer.verification_key_digest==identity
            assert app.authority.member.generation>generation
            # Separate cookie jar has only the public shell; no decoded private state.
            other=browser.new_context()
            clean=other.new_page();clean.goto(app.origin)
            expect(clean.locator('#private')).to_be_hidden()
            expect(clean.locator('#notice')).to_contain_text('not authorized')
            assert clean.locator('#pair-output').inner_text()==''
            assert clean.locator('#output').inner_text()==''
            assert clean.evaluate('Object.keys(localStorage).length')==0
            # Native-owner reopen invalidates previous browser after redemption.
            app.session_expires=0
            page.reload();expect(page.locator('#private')).to_be_hidden()
            reopen_native(app.root)
            page.goto(app.launch_file.as_uri())
            page.get_by_role('button',name='Open private demo',exact=True).click()
            expect(page.locator('#private')).to_be_visible()
            page.set_viewport_size({'width':390,'height':844})
            assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
            page.keyboard.press('Tab')
            assert page.evaluate('document.activeElement.tagName') in {'A','BUTTON','INPUT','SELECT','SUMMARY'}
            screenshots=Path(os.environ.get('A13_BROWSER_EVIDENCE',str(tmp_path/'screens')))
            screenshots.mkdir(parents=True,exist_ok=True)
            page.screenshot(path=str(screenshots/'macos-a13-mobile.png'),full_page=True)
            page.set_viewport_size({'width':1280,'height':900})
            page.screenshot(path=str(screenshots/'macos-a13-desktop.png'),full_page=True)
            page.locator('#revoke-confirm').check()
            page.get_by_role('button',name='Revoke this Mac',exact=True).click()
            expect(page.locator('#ladder')).to_contain_text('revoked')
            assert page.locator('#revoke').is_disabled()
            assert page.locator('#capability-report').is_disabled()
            assert errors==[]
            other.close();owner.close();browser.close()
    finally: app.close()
