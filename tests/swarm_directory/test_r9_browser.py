"""R9 visual preferences and motion, against a local real directory only."""
import pytest
from playwright.sync_api import sync_playwright, expect
from test_site_v2_browser import local, CLS


@pytest.mark.parametrize('width,height', [(1440,900), (390,844)])
@pytest.mark.parametrize('theme', ['dark','light'])
def test_glow_preference_motion_and_reserved_layout(local, width, height, theme):
    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(viewport={'width':width,'height':height}, color_scheme=theme, reduced_motion='reduce')
        context.add_init_script(CLS)
        page = context.new_page()
        errors = []
        page.on('pageerror', lambda e: errors.append(str(e)))
        page.goto(local['directory'].origin + '/')
        expect(page.locator('html')).to_have_attribute('data-glow', 'green')
        expect(page.locator('[data-glow-choice=green]')).to_have_attribute('aria-pressed', 'true')
        assert page.evaluate('localStorage.length') == 0  # no preference until an explicit choice
        expect(page.locator('#hyphae')).to_have_attribute('data-spore-nodes', '0')
        before = page.locator('#hyphae').evaluate('(c)=>c.toDataURL()')
        page.wait_for_timeout(700)
        assert before == page.locator('#hyphae').evaluate('(c)=>c.toDataURL()')
        assert page.evaluate('window.__pulseMetrics.paused && window.__understoryMetrics.paused')
        assert sum(page.evaluate('window.__cls')) == 0
        page.get_by_role('button',name='Blue',exact=True).click()
        expect(page.locator('html')).to_have_attribute('data-glow', 'blue')
        assert page.evaluate('localStorage.getItem("mycelium-site-glow-v1")') == 'blue'
        page.reload()
        expect(page.locator('html')).to_have_attribute('data-glow', 'blue')
        page.goto(local['directory'].origin + '/swarms/#notice')
        expect(page.locator('body')).to_have_attribute('data-ready','1')
        expect(page.locator('html')).to_have_attribute('data-glow','blue')
        expect(page.locator('#page-mycelium')).to_have_attribute('data-spore-nodes','0')
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        assert not errors
        browser.close()


def test_named_visual_assets_are_served_but_unknown_asset_is_not(local):
    import urllib.request, urllib.error
    for name in ('design.css','theme.js','design.js','mycelium-geometry.js','mycelium-hero.js','mycelium-field.js','spore-print.js'):
        with urllib.request.urlopen(local['directory'].origin+'/swarms/'+name) as r:
            assert r.status == 200
            assert 'unsafe-inline' not in r.headers.get('Content-Security-Policy','')
    with pytest.raises(urllib.error.HTTPError) as error:
        urllib.request.urlopen(local['directory'].origin+'/swarms/not-allowlisted.js')
    assert error.value.code == 404
