"""STAGING-ONLY load-in duration comparison (owner, 9 Nov 2026): the r11 intro
is one shared duration (default 6 s) that scales every intro timing; on
next.mycelium.now with staging samples, ``/?intro=12`` or ``/?intro=2x`` plays
it at 12 s (established at 12 s, first hero pulse at 12 + 5 = 17 s). Production
never injects the control, refuses the query, and never reads the parameter.
Real local stack only."""
import time
import urllib.error
import urllib.request

import pytest
from playwright.sync_api import sync_playwright

from mycelium_demo import public_origin_proxy as proxy
from mycelium_swarm_directory import server
from mycelium_swarm_directory.server import DirectoryServer
from test_site_v2_browser import CLS, local  # noqa: F401  (fixture)
from test_r11_hero_browser import _hero_alpha

READY = '#page-mycelium[data-ready="1"]'
TARGETS = ['/?intro=6', '/?intro=12', '/?intro=1x', '/?intro=2x']
# Hero age map: START_AGE * (1 - (1 - t / INTRO_S) ** 1.6).
AGE = lambda u: 110 * (1 - (1 - u) ** 1.6)  # noqa: E731


def fetch(url):
    try:
        with urllib.request.urlopen(url) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, b''


def test_proxy_accepts_the_intro_targets_only_on_staging(monkeypatch):
    assert proxy._STAGING_INTRO_PATHS == server.STAGING_INTRO_PATHS == frozenset(TARGETS)
    for target in TARGETS:
        assert proxy._directory_route_decision('GET', target) == (False, 404, 'public_route_not_found')
        assert proxy._directory_route_decision('GET', target, True)[0] is True
        assert proxy._directory_route_decision('POST', target, True)[0] is False
    for other in ('/?intro=13', '/?intro=12&x=1', '/swarms/?intro=12', '/?t=3', '/?Intro=12'):
        assert proxy._directory_route_decision('GET', other, True)[0] is False, other
    monkeypatch.setenv('MYCELIUM_STAGING_SAMPLES', '1')
    assert not proxy._staging_style_assets('mycelium.now')


@pytest.mark.parametrize('origin,flag,staging', [
    ('https://next.mycelium.now', None, False),
    ('https://mycelium.now', '1', False),
    ('https://next.mycelium.now', '1', True),
])
def test_intro_control_and_targets_need_flag_and_staging_origin(local, monkeypatch, origin, flag, staging):  # noqa: F811
    if flag is None:
        monkeypatch.delenv('MYCELIUM_STAGING_SAMPLES', raising=False)
    else:
        monkeypatch.setenv('MYCELIUM_STAGING_SAMPLES', flag)
    srv = DirectoryServer(local['directory'].directory, public_origin=origin).start()
    try:
        status, home = fetch(srv.origin + '/')
        assert status == 200 and (b'data-intro-choice' in home) is staging
        if staging:
            labels = [part.split(b'>', 1)[1].split(b'<', 1)[0] for part in home.split(b'data-intro-choice=')[1:]]
            assert labels == [b'6s', b'12s'] and b'<span class="lbl">Intro</span>' in home
        for target in TARGETS:
            status, body = fetch(srv.origin + target)
            assert (status, body == home) == ((200, True) if staging else (404, False)), (target, status)
        # The swarms app never carries the intro control or accepts the query.
        status, app = fetch(srv.origin + '/swarms/')
        assert status == 200 and b'data-intro-choice' not in app
        assert fetch(srv.origin + '/swarms/?intro=12')[0] == 404
    finally:
        srv.close()


def _page(p, url, reduced=None, hidden=False, route_from=None):
    browser = p.chromium.launch()
    kw = {'reduced_motion': reduced} if reduced else {}
    context = browser.new_context(viewport={'width': 1440, 'height': 900}, color_scheme='dark', **kw)
    context.add_init_script(CLS)
    if hidden:
        context.add_init_script("Object.defineProperty(document,'hidden',{configurable:true,get:()=>true})")
    page = context.new_page()
    errors = []
    page.on('pageerror', lambda e: errors.append(str(e)))
    if route_from:
        # Production HTML served at a ?intro= URL: the page must ignore the parameter.
        html = urllib.request.urlopen(route_from).read()
        page.route(url, lambda r: r.fulfill(status=200, body=html, headers={'content-type': 'text/html; charset=utf-8'}))
    page.goto(url)
    page.wait_for_selector(READY, state='attached', timeout=30000)
    return browser, page, errors


def _scene_ready(page):
    for _ in range(300):  # CSP forbids string predicates
        if page.evaluate('()=>!!(window.__myceliumScene && __myceliumScene.ready)'):
            return
        page.wait_for_timeout(100)
    raise AssertionError('hero scene never ready')


def _done_at(page, limit_s):
    """performance.now() (s) when the field reports its intro done."""
    t0 = time.monotonic()
    while time.monotonic() - t0 < limit_s:
        if page.get_attribute('#page-mycelium', 'data-intro') == 'done':
            return page.evaluate('performance.now()') / 1000
        page.wait_for_timeout(100)
    raise AssertionError('intro never done')


def test_production_never_reads_the_parameter(local):  # noqa: F811
    local['directory'].staging_samples = False
    origin = local['directory'].origin
    with sync_playwright() as p:
        browser, page, errors = _page(p, origin + '/?intro=12', route_from=origin + '/')
        assert page.locator('[data-intro-choice]').count() == 0
        assert page.evaluate('[MyceliumHero.INTRO_MS, MyceliumField.INTRO_MS, MyceliumField.INTRO_SCALE, MyceliumHero.firstPulseAt]') == [6000, 6000, 1, 8.5]
        assert not errors, errors
        browser.close()


@pytest.mark.parametrize('target,ms', [('/', 6000), ('/?intro=12', 12000), ('/?intro=2x', 12000)])
def test_staging_intro_scales_every_timing(local, target, ms):  # noqa: F811
    local['directory'].staging_samples = True
    scale = ms / 6000
    with sync_playwright() as p:
        browser, page, errors = _page(p, local['directory'].origin + target)
        assert page.evaluate('[MyceliumHero.INTRO_MS, MyceliumField.INTRO_MS, MyceliumField.INTRO_SCALE]') == [ms, ms, scale]
        pressed = page.evaluate("[...document.querySelectorAll('[data-intro-choice]')].map(b=>b.getAttribute('aria-pressed'))")
        assert pressed == (['true', 'false'] if ms == 6000 else ['false', 'true'])
        # Field and margins: still growing past the 6 s mark at 12 s, established by ~INTRO_MS.
        done = _done_at(page, 40)
        assert ms / 1000 - 0.3 <= done <= ms / 1000 + 4, done
        _scene_ready(page)
        # Hero: the same age map, stretched (89% of age at 0.75 INTRO_S, START_AGE at INTRO_S).
        for u in (0.25, 0.5, 0.75, 1.0):
            assert abs(page.evaluate(f'__myceliumScene.ageAt({u * ms / 1000})') - AGE(u)) < 1e-6
        assert abs(page.evaluate(f'__myceliumScene.ageAt({ms / 1000 + 3})') - 113) < 1e-6
        # First hero pulse at INTRO_S + 2.5 s * scale: 8.5 s (6 s) or 17 s (12 s).
        first = 8.5 * scale
        assert page.evaluate('MyceliumHero.firstPulseAt') == first
        assert abs(page.evaluate('__pulseTrips[0].start') - first) < 1e-9
        # Frozen growth: same fraction at the same fraction of the intro.
        frac = []
        for t in (0.25, 0.75, 1.0):
            page.evaluate(f'__setPulseTime({t * ms / 1000})')
            frac.append(_hero_alpha(page))
        page.evaluate('__setPulseTime(40)')
        full = _hero_alpha(page)
        assert frac[0] < 0.8 * full and frac[2] >= 0.95 * full, (frac, full)
        assert sum(page.evaluate('__cls')) == 0, page.evaluate('window.__clsDetails')
        assert not errors, errors
        browser.close()


@pytest.mark.parametrize('mode', ['reduce', 'hidden'])
def test_reduced_motion_and_hidden_tabs_stay_instant_with_the_parameter(local, mode):  # noqa: F811
    local['directory'].staging_samples = True
    with sync_playwright() as p:
        browser, page, errors = _page(p, local['directory'].origin + '/?intro=12',
                                      reduced='reduce' if mode == 'reduce' else None, hidden=mode == 'hidden')
        assert page.evaluate('MyceliumHero.INTRO_MS') == 12000
        assert page.get_attribute('#page-mycelium', 'data-intro') == 'skipped'
        _scene_ready(page)
        assert not page.evaluate('__myceliumScene.intro.on')
        assert page.evaluate('MyceliumHero.introProgress()') == 1
        assert _hero_alpha(page) > 10000
        assert not errors, errors
        browser.close()


def test_intro_control_reloads_with_the_parameter_without_shifting(local):  # noqa: F811
    local['directory'].staging_samples = True
    origin = local['directory'].origin
    with sync_playwright() as p:
        browser, page, errors = _page(p, origin + '/')
        page.wait_for_timeout(500)
        assert sum(page.evaluate('__cls')) == 0
        page.click('[data-intro-choice="12"]')
        page.wait_for_url(origin + '/?intro=12')
        page.wait_for_selector(READY, state='attached', timeout=30000)
        assert page.evaluate('MyceliumHero.INTRO_MS') == 12000
        assert page.get_attribute('[data-intro-choice="12"]', 'aria-pressed') == 'true'
        page.wait_for_timeout(1500)
        assert sum(page.evaluate('__cls')) == 0, page.evaluate('window.__clsDetails')
        page.click('[data-intro-choice="6"]')
        page.wait_for_url(origin + '/')
        page.wait_for_selector(READY, state='attached', timeout=30000)
        assert page.evaluate('MyceliumHero.INTRO_MS') == 6000
        assert not errors, errors
        browser.close()
