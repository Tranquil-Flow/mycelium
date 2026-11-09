"""R11.2b mobile background mycelium (owner, 9 Nov 2026): "for the mobile,
doesn't seem to show much background mycelium when you scroll down".

The side margins (their colonies, runners, pulses and cascades) were disabled
below 1100 px, so phones and tablets showed only the sparse column field:
below-fold coverage 2.9% (390x844), 3.7% (360x800) and 1.2% (768x1024)
against 4.1% at 1440. Compact viewports now carry small edge colonies scaled
to the width (partly off-screen), their runners and long hyphae across the
column, and a denser sparse field. Real local stack only.

Coverage: share of viewport pixels (below the fixed header) that change by
>= 6/255 when the mycelium layers are hidden, text transparent, established
field (t = 24 s), at six scroll positions spread over the below-fold page.
"""
import io
import json
import os

import numpy as np
import pytest
from PIL import Image
from playwright.sync_api import sync_playwright

from test_site_v2_browser import CLS, local  # noqa: F401  (fixture)

READY = '#page-mycelium[data-ready="1"]'
LAYERS = '.page-mycelium, .page-mycelium-field, #page-mycelium'
VIEWPORTS = [(390, 844, 3), (360, 800, 3), (768, 1024, 2)]


def _shot(page):
    return np.asarray(Image.open(io.BytesIO(page.screenshot())).convert('RGB')).astype(np.int16)


def _coverage(page, header):
    page.evaluate(f"document.querySelectorAll('{LAYERS}').forEach(e=>{{e.style.transition='none';e.style.opacity=''}})")
    page.wait_for_timeout(60)
    a = _shot(page)
    page.evaluate(f"document.querySelectorAll('{LAYERS}').forEach(e=>e.style.opacity='0')")
    page.wait_for_timeout(60)
    b = _shot(page)
    page.evaluate(f"document.querySelectorAll('{LAYERS}').forEach(e=>e.style.opacity='')")
    d = np.abs(a - b).max(axis=2)[int(header * a.shape[0] / page.viewport_size['height']):]
    if os.environ.get('MOBILE_COVERAGE_OUT'):
        tag = f"{os.environ['MOBILE_COVERAGE_OUT']}-{page.viewport_size['width']}-{page.evaluate('scrollY')}"
        Image.fromarray(a.astype(np.uint8)).save(tag + '-a.png')
        Image.fromarray(((d >= 6) * 255).astype(np.uint8)).save(tag + '-m.png')
    return float((d >= 6).mean())


def measure(page):
    """Coverage at six below-fold scroll positions (first full screen below the
    hero to the page bottom)."""
    page.evaluate('__setUnderstoryTime(24)')
    page.evaluate("""()=>{for(const e of document.querySelectorAll('main *, footer *')){e.style.color='transparent';e.style.textShadow='none'}}""")
    header = page.evaluate("Math.round((document.querySelector('header')||{getBoundingClientRect:()=>({bottom:0})}).getBoundingClientRect().bottom)")
    H = page.evaluate('document.documentElement.scrollHeight - innerHeight')
    vh = page.viewport_size['height']
    out = []
    for k in range(6):
        y = round(vh + (H - vh) * k / 5)
        page.evaluate(f'scrollTo(0,{y})')
        for _ in range(40):  # bands near the view are resident (CSP: no string wait_for_function)
            page.wait_for_timeout(100)
            if page.evaluate('''()=>[...document.querySelectorAll('.page-mycelium-field canvas')].every(c=>c.style.visibility!=='visible'||c.width>1)'''):
                break
        page.wait_for_timeout(250)
        out.append({'y': y, 'coverage': round(_coverage(page, header), 4)})
    return out


@pytest.mark.parametrize('width,height,dpr', VIEWPORTS)
def test_mobile_below_fold_carries_background_mycelium(local, width, height, dpr):  # noqa: F811
    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(viewport={'width': width, 'height': height}, device_scale_factor=dpr, color_scheme='dark',
                                      is_mobile=width < 700, has_touch=width < 700)
        context.add_init_script(CLS)
        page = context.new_page()
        errors = []
        page.on('pageerror', lambda e: errors.append(str(e)))
        page.goto(local['directory'].origin + '/')
        page.wait_for_selector(READY, state='attached', timeout=60000)
        for _ in range(300):
            if page.evaluate('!!(window.__understoryAudit && __understoryAudit.bands)'):
                break
            page.wait_for_timeout(100)
        page.wait_for_selector('#page-mycelium[data-intro="done"]', state='attached', timeout=60000)
        audit = page.evaluate('({compact: __myceliumScene.field.layout.compact, knots: __understoryAudit.marginKnots, dpr: __understoryAudit.dpr})')
        rows = measure(page)
        if os.environ.get('MOBILE_COVERAGE_OUT'):
            with open(os.environ['MOBILE_COVERAGE_OUT'], 'a') as f:
                f.write(json.dumps({'viewport': [width, height, dpr], 'audit': audit, 'rows': rows}) + '\n')
        cov = [r['coverage'] for r in rows]
        # Field rasters never exceed DPR 2 (phones report 3).
        assert audit['dpr'] == min(dpr, 2), audit
        # Edge colonies on both sides of compact viewports.
        assert audit['compact'] and audit['knots'] >= 4, audit
        # Desktop-equivalent below-fold coverage, visibly present at every scroll
        # position. Same metric at 1440x900: 7.5% mean (content column alone
        # 1.1-6.0%). 43dd59f4: 2.5% / 2.4% / 2.0% mean, 0.3-0.9% minimum.
        assert sum(cov) / len(cov) >= 0.035, rows
        assert min(cov) >= 0.015, rows
        assert sum(page.evaluate('__cls')) == 0
        assert not errors, errors
        browser.close()
