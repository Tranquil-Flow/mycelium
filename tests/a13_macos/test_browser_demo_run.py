# SPDX-License-Identifier: AGPL-3.0-only
"""Real Chromium over the owner UI: model picker, one-click Start/Stop, chat page; no model run."""
import pytest
from playwright.sync_api import sync_playwright, expect

from mycelium_a13_macos.app import DemoApp
from test_app_authority import CONSENT


def test_owner_picks_model_presses_start_and_sees_a_clear_failure(tmp_path, monkeypatch):
    monkeypatch.delenv('MYCELIUM_A13_RUN_MODEL', raising=False)
    app = DemoApp(tmp_path / 'owner', check_platform=False).start()
    errors = []
    try:
        app.action('/api/create', {'consent': CONSENT})
        from mycelium_a13_macos import demo_app
        demo_app.owner_run(app).chain.budgets['model'] = 0.5
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page()
            page.on('pageerror', lambda e: errors.append(type(e).__name__))
            page.on('console', lambda m: m.type == 'error' and errors.append(m.text))
            page.goto(app.launch_file.as_uri())
            page.get_by_role('button', name='Open private demo', exact=True).click()
            expect(page.locator('#demo-run')).to_be_visible()
            picker = page.locator('#run-model')
            # L3 added the tested starter while retaining every larger option.
            expect(picker.locator('option')).to_have_count(4)
            expect(picker).to_have_value('a9-route')
            expect(picker.locator('option[value="a9-route"]')).to_contain_text('tested starter')
            expect(picker.locator('option[value="qwen3-14b"]')).not_to_contain_text('too large for the storage limit')
            picker.select_option('qwen3-0.6b')
            expect(page.locator('#run-model-note')).to_contain_text('Qwen/Qwen3-0.6B', timeout=5000)
            assert app.run_model_contract().model_id == 'Qwen/Qwen3-0.6B'
            expect(page.locator('#demo-steps li')).to_have_count(10)
            page.locator('#demo-start').click()
            expect(page.locator('#demo-state')).to_contain_text('Download the selected model', timeout=15000)
            expect(page.locator('#demo-steps li.failed')).to_contain_text('Model ready on this Mac')
            expect(page.locator('#demo-start')).to_be_enabled()
            expect(page.locator('#demo-target')).to_be_hidden()
            chat = browser.new_page()
            chat.on('pageerror', lambda e: errors.append(type(e).__name__))
            chat.goto(app.origin + '/chat')
            expect(chat.locator('#state')).to_contain_text('not serving', timeout=5000)
            expect(chat.locator('#send')).to_be_disabled()
            browser.close()
        assert errors == []
    finally:
        app.close()
