"""R11.4 (owner, after r11.3: "stutter is better but not completely smooth"):
no dropped-frame hitch during the 9 s grow-in and the seconds after it. Every
requestAnimationFrame interval over 0-12 s of the load-in stays within one
missed 60 Hz vsync (<= 34 ms), at 1440 and 1920 CSS px, DPR 2, and the build
keeps its timeline (the field attaches as early as before).

86a5e62e (before; same host, GPU, load 7-8): 51-75 intervals > 34 ms per load,
max 75-125 ms; causes: 9-16 ms build slices posted back to back from five
MessageChannel loops, landing next to 10-14 ms intro frames, and ~30k active
paths that were waiting for their mat walked, snapshotted and committed every
frame. Now one frame pacer runs every background slice (mycelium-geometry.js).

Timing test on the real local stack, real GPU on macOS. The host load is
measured: above load 8 the numbers are recorded and the test is skipped (a
loaded host preempts the renderer for tens of ms; never judge lag from it).
"""
import os
import sys

import pytest
from playwright.sync_api import sync_playwright
from test_site_v2_browser import local, CLS  # noqa: F401  (fixture)

READY = '#page-mycelium[data-ready="1"]'
RAF = """window.__raf = []; (function f(t) { window.__raf.push(t); requestAnimationFrame(f); })(performance.now());"""
LOAD_MAX = float(os.environ.get('R114_LOAD_MAX', '8'))


@pytest.mark.parametrize('width', [1440, 1920])
def test_grow_in_frame_intervals_stay_within_one_missed_vsync(local, width):  # noqa: F811
    load0 = os.getloadavg()[0]
    with sync_playwright() as p:
        browser = p.chromium.launch(args=['--enable-gpu', '--use-angle=metal'] if sys.platform == 'darwin' else [])
        context = browser.new_context(viewport={'width': width, 'height': 900}, device_scale_factor=2, color_scheme='dark')
        context.add_init_script(CLS)
        context.add_init_script(RAF)
        page = context.new_page()
        errors = []
        page.on('pageerror', lambda e: errors.append(str(e)))
        page.goto(local['directory'].origin + '/')
        page.wait_for_timeout(12500)
        raf = [t for t in page.evaluate('__raf') if t <= 12000]
        timings = page.evaluate('__myceliumScene.timings')
        pacer = page.evaluate('window.__myceliumPacer')
        page.wait_for_selector(READY, state='attached', timeout=30000)
        assert sum(page.evaluate('__cls')) == 0
        browser.close()
    load1 = os.getloadavg()[0]
    iv = sorted(raf[i] - raf[i - 1] for i in range(1, len(raf)))
    gaps = [(round(raf[i - 1]), round(raf[i] - raf[i - 1], 1)) for i in range(1, len(raf)) if raf[i] - raf[i - 1] > 34]
    summary = {'load': [round(load0, 1), round(load1, 1)], 'frames': len(raf), 'p50': round(iv[len(iv) // 2], 1),
               'p99': round(iv[int(len(iv) * 0.99)], 1), 'max': round(iv[-1], 1), 'gaps>34': gaps[:12],
               'ready': round(timings['start'] + timings['complete']), 'pacer': {k: pacer[k] for k in ('slices', 'maxSlice', 'maxAt', 'floor')}}
    print('r11.4 frame intervals', width, summary)
    assert not errors, errors
    if max(load0, load1) > LOAD_MAX:
        pytest.skip(f'host load {load0:.1f}/{load1:.1f} > {LOAD_MAX}: recorded, not judged {summary}')
    # The build's timeline: the field attaches within the first ~6.5 s of 9.
    assert summary['ready'] < 6500, summary
    assert not gaps, summary
