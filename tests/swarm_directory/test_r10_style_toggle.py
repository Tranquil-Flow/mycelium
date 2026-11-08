"""STAGING-ONLY background style toggle: r10 "Mycelium" (default) vs r9 "Electric".

Production (no staging flag) renders no toggle and serves no electric asset;
staging renders it, persists the choice, and runs exactly one renderer.
"""
import hashlib
import urllib.error
import urllib.request

import pytest
from playwright.sync_api import sync_playwright

from mycelium_demo import public_origin_proxy as proxy
from mycelium_swarm_directory import server
from mycelium_swarm_directory.server import DirectoryServer
from test_site_v2_browser import CLS, local  # noqa: F401  (fixture)

ELECTRIC = sorted(server.STAGING_ASSETS)
READY = "document.querySelector('#page-mycelium')?.width > 4"
RAF = """window.__raf=0;const _raf=window.requestAnimationFrame.bind(window);
window.requestAnimationFrame=(cb)=>{window.__raf++;return _raf(cb)};"""


def fetch(url):
    try:
        with urllib.request.urlopen(url) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, b''


def test_production_lists_no_electric_asset_and_proxy_refuses_them(monkeypatch):
    assert not set(ELECTRIC) & set(server.ASSETS)
    assert not set(ELECTRIC) & set(proxy._DIRECTORY_ASSET_PATHS)
    assert not [p for p in server.WEB.iterdir() if 'electric' in p.name]
    assert sorted(p.name for p in server.WEB_STAGING.iterdir()) == sorted(n for n, _ in server.STAGING_ASSETS.values())
    for path in ELECTRIC:
        assert proxy._directory_route_decision('GET', path)[0] is False
        assert proxy._directory_route_decision('GET', path, True)[0] is True
    monkeypatch.delenv('MYCELIUM_STAGING_SAMPLES', raising=False)
    assert not proxy._staging_style_assets('next.mycelium.now')
    monkeypatch.setenv('MYCELIUM_STAGING_SAMPLES', '1')
    assert proxy._staging_style_assets('next.mycelium.now')
    assert not proxy._staging_style_assets('mycelium.now')


@pytest.mark.parametrize('origin,flag,staging', [
    ('https://next.mycelium.now', None, False),
    ('https://mycelium.now', '1', False),
    ('https://next.mycelium.now', '1', True),
])
def test_toggle_and_electric_assets_need_flag_and_staging_origin(local, monkeypatch, origin, flag, staging):  # noqa: F811
    if flag is None:
        monkeypatch.delenv('MYCELIUM_STAGING_SAMPLES', raising=False)
    else:
        monkeypatch.setenv('MYCELIUM_STAGING_SAMPLES', flag)
    srv = DirectoryServer(local['directory'].directory, public_origin=origin).start()
    try:
        for path in ['/', '/swarms/']:
            status, html = fetch(srv.origin + path)
            assert status == 200
            assert (b'data-bg-choice' in html) is staging
            assert (b'electric' in html.lower()) is staging
        for path in ELECTRIC:
            status, body = fetch(srv.origin + path)
            if staging:
                name = server.STAGING_ASSETS[path][0]
                assert status == 200 and body == (server.WEB_STAGING / name).read_bytes()
            else:
                assert status == 404 and body == b''
        # design.js imports the electric modules only when the injected toggle exists.
        status, js = fetch(srv.origin + '/swarms/design.js')
        assert status == 200 and b'[data-bg-choice]' in js
    finally:
        srv.close()


def canvas_digest(page):
    return hashlib.sha256(page.evaluate("document.getElementById('page-mycelium').toDataURL()").encode()).hexdigest()


def raf_rate(page, ms=1500):
    a = page.evaluate('window.__raf')
    page.wait_for_timeout(ms)
    return (page.evaluate('window.__raf') - a) * 1000 / ms


def switch(page, style):
    page.click(f'[data-bg-choice="{style}"]')
    page.wait_for_function(f"document.querySelector('[data-bg-choice=\"{style}\"]').getAttribute('aria-pressed')==='true'")
    page.wait_for_function(READY)
    page.wait_for_timeout(2500)


def test_staging_toggle_switches_one_renderer_persists_and_never_shifts(local):  # noqa: F811
    local['directory'].staging_samples = True
    origin = local['directory'].origin
    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(viewport={'width': 1440, 'height': 900})
        context.add_init_script(CLS)
        context.add_init_script(RAF)
        page = context.new_page()
        errors = []
        page.on('pageerror', lambda e: errors.append(str(e)))
        requests = []
        page.on('request', lambda r: requests.append(r.url))
        page.goto(origin + '/')
        page.wait_for_function(READY)
        page.wait_for_timeout(2500)
        # Default r10; the electric renderer is not even requested.
        assert page.get_attribute('[data-bg-choice="mycelium"]', 'aria-pressed') == 'true'
        assert not [u for u in requests if 'electric' in u]
        base_canvases = page.evaluate("document.querySelectorAll('canvas').length")
        base_list = page.evaluate("[...document.querySelectorAll('canvas')].map(c=>c.id||c.className||c.parentElement.className)")
        base_rate = raf_rate(page)
        assert page.locator('.page-mycelium-live').count() == 1
        switch(page, 'electric')
        assert page.evaluate('document.documentElement.dataset.bgStyle') == 'electric'
        assert [u for u in requests if 'mycelium-hero-electric.js' in u]
        # r10's live layer and breathing silhouettes are gone: one renderer.
        assert page.locator('.page-mycelium-live').count() == 0
        assert page.locator('.mycelium-breath').count() == 0
        electric_rate = raf_rate(page)
        switch(page, 'mycelium')
        assert page.locator('.page-mycelium-live').count() == 1
        canvases = "[...document.querySelectorAll('canvas')].map(c=>c.id||c.className||c.parentElement.className)"
        assert page.evaluate("document.querySelectorAll('canvas').length") == base_canvases, (sorted(base_list), sorted(page.evaluate(canvases)))
        back_rate = raf_rate(page)
        # No leaked animation loops after a round trip.
        assert back_rate <= base_rate * 1.35 + 3, (base_rate, electric_rate, back_rate)
        assert electric_rate <= base_rate * 1.6 + 3, (base_rate, electric_rate)
        # Persisted (staging-only key); reload keeps the choice.
        switch(page, 'electric')
        assert page.evaluate("localStorage.getItem('mycelium-staging-bg-style-v1')") == 'electric'
        page.reload()
        page.wait_for_function(READY)
        assert page.get_attribute('[data-bg-choice="electric"]', 'aria-pressed') == 'true'
        assert page.evaluate('document.documentElement.dataset.bgStyle') == 'electric'
        page.wait_for_timeout(1500)
        assert sum(page.evaluate('window.__cls')) == 0, page.evaluate('window.__clsDetails')
        assert not errors, errors
        context.close()
        browser.close()


@pytest.mark.parametrize('style', ['mycelium', 'electric'])
def test_staging_toggle_respects_reduced_motion_for_both_styles(local, style):  # noqa: F811
    local['directory'].staging_samples = True
    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(viewport={'width': 1440, 'height': 900}, reduced_motion='reduce')
        context.add_init_script(f"localStorage.setItem('mycelium-staging-bg-style-v1','{style}')")
        page = context.new_page()
        errors = []
        page.on('pageerror', lambda e: errors.append(str(e)))
        page.goto(local['directory'].origin + '/')
        page.wait_for_function(READY)
        assert page.get_attribute(f'[data-bg-choice="{style}"]', 'aria-pressed') == 'true'
        page.wait_for_timeout(6000)
        first = canvas_digest(page)
        page.wait_for_timeout(2000)
        assert canvas_digest(page) == first
        assert not errors, errors
        browser.close()
