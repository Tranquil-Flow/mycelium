"""R11.3 (owner: "two points during the grow-in phase that stutter"): every
scheduled slice of the hero build and of the post-ready passes (colony monotone
pass, re-bake and its cross-fade start, dip-glow measurement) stays short.
da4767e2 at 1920 DPR2: fuzz 76-94 ms, monotone 30-89 ms, re-bake swap 40-107 ms,
dip passes 21-90 ms. Real local stack only."""
import pytest
from playwright.sync_api import sync_playwright
from test_site_v2_browser import local, CLS  # noqa: F401  (fixture)

READY = '#page-mycelium[data-ready="1"]'


@pytest.mark.parametrize('width', [1440, 1920])
def test_grow_in_and_post_ready_passes_have_no_long_slice(local, width):  # noqa: F811
    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(viewport={'width': width, 'height': 900}, device_scale_factor=2, color_scheme='dark')
        context.add_init_script(CLS)
        page = context.new_page()
        errors = []
        page.on('pageerror', lambda e: errors.append(str(e)))
        page.goto(local['directory'].origin + '/')
        page.wait_for_selector(READY, state='attached', timeout=60000)
        t = {}
        for _ in range(240):  # CSP: poll, no string wait_for_function
            t = page.evaluate('__myceliumScene.timings')
            if t.get('dipsDone'):
                break
            page.wait_for_timeout(250)
        assert t.get('rebaked') and t.get('dipsDone'), t
        # Every slice of the build, the monotone pass and the re-bake (one
        # scheduler, recorded per task), and every dip-glow slice.
        assert t['maxSlice'] < 50, (t['maxSlice'], t['maxSliceAt'], t.get('slowSlices'))
        assert t['maxDipSlice'] < 50, t['maxDipSlice']
        # Both cross-fades (re-bake, dip glow) started, each inside one short task.
        assert len(t.get('xfadeStarts') or []) >= 1 and max(t['xfadeStarts']) < 50, t.get('xfadeStarts')
        stages = page.evaluate('__understoryMetrics.attachStages')
        assert max(stages) < 30, list(zip(stages, page.evaluate('__understoryMetrics.attachLabels')))
        assert sum(page.evaluate('__cls')) == 0
        assert not errors, errors
        browser.close()
