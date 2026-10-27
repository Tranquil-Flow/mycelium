# SPDX-License-Identifier: AGPL-3.0-only
"""Real Chromium: the consent choices shown are the per-model numbers the app enforces."""
from playwright.sync_api import sync_playwright, expect

from mycelium_a13_macos import model_resources as R
from mycelium_a13_macos.app import DemoApp, host_memory_bytes
from mycelium_a13_macos.serving import QWEN3_14B_MODEL


def test_consent_choices_match_the_enforced_per_model_bounds(tmp_path, monkeypatch):
    monkeypatch.delenv('MYCELIUM_A13_RUN_MODEL', raising=False)
    app = DemoApp(tmp_path / 'owner', check_platform=False).start()
    errors = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page()
            page.on('pageerror', lambda e: errors.append(str(e)))
            page.goto(app.launch_file.as_uri())
            page.get_by_role('button', name='Open private demo', exact=True).click()
            need = R.owner_storage_requirement(QWEN3_14B_MODEL)
            storage = page.locator('#storage')
            expect(storage.locator(f'option[value="{need}"]')).to_contain_text('Qwen3-14B on the owner Mac')
            values = [int(v) for v in storage.locator('option').evaluate_all('(os) => os.map((o) => o.value)')]
            assert max(values) <= R.storage_consent_ceiling()
            expect(page.locator('#storage-detail')).to_contain_text('32.6 GB')
            quota = [int(v) for v in page.locator('#pair-storage option').evaluate_all(
                '(os) => os.map((o) => o.value)')]
            assert max(quota) == R.pairing_quota_ceiling()
            memory = [int(v) for v in page.locator('#standing-memory option').evaluate_all(
                '(os) => os.map((o) => o.value)')]
            assert sorted(memory) == R.memory_cap_options(host_memory_bytes())
            assert max(memory) <= R.member_memory_ceiling(host_memory_bytes())
            limit = page.locator('#download-limit')
            # L3's fresh-state starter has its own exact enforced download bound.
            assert int(limit.get_attribute('max')) == 999_604_126
            expect(page.locator('#acquisition-detail')).to_contain_text('999,604,126 bytes')
            # Before enrollment the picker is hidden; use its real app action
            # to retain the original large-model consent assertions as well.
            app.action('/api/run-model', {'choice': 'qwen3-14b'})
            page.reload()
            expect(limit).to_have_attribute('max', '29552613776')
            assert int(limit.get_attribute('max')) == 29_552_613_776
            expect(page.locator('#acquisition-detail')).to_contain_text('29,552,613,776 bytes')
            browser.close()
        assert errors == []
    finally:
        app.close()
