"""R11.1 polish: margin cords dissolve into their colony mat with no bright
convergence point. Real local stack only."""
import pytest
from playwright.sync_api import sync_playwright
from test_site_v2_browser import local, CLS  # noqa: F401  (fixture)

READY = '#page-mycelium[data-ready="1"]'


def _open(p, local, width, dpr=2, hue='green'):  # noqa: F811
    browser = p.chromium.launch()
    context = browser.new_context(viewport={'width': width, 'height': 900}, device_scale_factor=dpr, color_scheme='dark')
    context.add_init_script(CLS)
    if hue == 'blue':
        context.add_init_script("localStorage.setItem('mycelium-site-glow-v1','blue')")
    page = context.new_page()
    errors = []
    page.on('pageerror', lambda e: errors.append(str(e)))
    page.goto(local['directory'].origin + '/')
    page.wait_for_selector(READY, state='attached', timeout=60000)
    return browser, page, errors


@pytest.mark.parametrize('width,hue', [(1440, 'green'), (1920, 'green'), (1920, 'blue')])
def test_margin_cords_dissolve_into_the_colony_mat(local, width, hue):  # noqa: F811
    with sync_playwright() as p:
        browser, page, errors = _open(p, local, width, hue=hue)
        page.evaluate('__setUnderstoryTime(24)')  # the established field
        rows = []
        for _ in range(30):  # wait for every band to be resident (CSP: no string wait_for_function)
            rows = page.evaluate('__marginJunctionProbe()')
            if rows and all(r['resident'] for r in rows):
                break
            page.wait_for_timeout(100)
        measured = [r for r in rows if r['resident']]
        assert len(measured) >= 4, rows  # both margins carry several colonies joined by cords
        # Where the two margin cords meet the colony vertex the displayed field is
        # never brighter than the mat's own peak (no bright convergence point).
        # 8b9c7977: junction 1.4-2.4x the mat peak. The 2% allowance is below one
        # 8-bit code level at these luminances: the band and the mat-only raster
        # round their additive ('lighter') strokes in a different order.
        bright = [r for r in measured if r['junction'] > r['matPeak'] * 1.02]
        assert not bright, [(round(r['x']), round(r['y']), round(r['junction'], 4), round(r['matPeak'], 4)) for r in bright]
        assert not errors, errors
        browser.close()


@pytest.mark.parametrize('width', [1920, 2560])
def test_page_build_has_no_long_task(local, width):  # noqa: F811
    """8b9c7977 at 1920: one 51-92 ms task when the scene completed and the
    compositor attached in the same task as the last build slice, and 30-50 ms
    build slices (a full scan of every path per cord; cotton yields only every
    60 hairs). Now the completion, the ready dispatch and each attach stage run
    as separate short tasks and every build slice stays well under 50 ms."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(viewport={'width': width, 'height': 900}, device_scale_factor=2, color_scheme='dark')
        context.add_init_script(CLS)
        page = context.new_page()
        page.goto(local['directory'].origin + '/')
        page.wait_for_selector(READY, state='attached', timeout=60000)
        t = page.evaluate('__myceliumScene.timings')
        stages = page.evaluate('__understoryMetrics.attachStages')
        assert len(stages) >= 4 and max(stages) < 30, stages
        assert t['completeTask'] < 30 and t['readyTask'] < 30, t
        assert t['maxSlice'] < 50, t
        # The 6 s intro contract is unchanged.
        assert page.evaluate('MyceliumField.INTRO_MS') == 6000 and page.evaluate('MyceliumHero.INTRO_MS') == 6000
        assert page.get_attribute('#page-mycelium', 'data-intro') in ('running', 'done')
        assert sum(page.evaluate('__cls')) == 0
        browser.close()
