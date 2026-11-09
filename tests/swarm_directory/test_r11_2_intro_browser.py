"""R11.2 load-in fixes (owner, 9 Nov 2026), real local stack only.

1. "the dense colony mats appear almost instantly during the intro": every hero
   and margin colony mat grows into density over the grow-in (a few hyphae at
   the centre first, then filling outward, complete around the end of the
   hero's growth), with no 100 ms step adding more than 10% of the final mat.
2. "a fast paced burst of sparks erupts from the bottom two nodes" about 2/3
   into the grow-in (6 / 9 / 12 s): 40-60 hero-edge continuation families
   (3-5k field strands rooted on the bottom colonies) all set off together
   when the hero reached 75%, crossing ~550 px in the remaining 1.5-2.5 s with
   glowing tips (~650 px/s). Each family now sets off when the hero has grown
   its seed vertex and cruises at <= 200 px/s (6 s base), and no pulse, bloom
   or margin trip starts before the first regular pulse.
3. Production plays the owner's pick: 9 s.
"""
import re
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

from mycelium_swarm_directory import server
from test_site_v2_browser import CLS, local  # noqa: F401  (fixture)

READY = '#page-mycelium[data-ready="1"]'


def _page(p, url, width=1440):
    browser = p.chromium.launch()
    context = browser.new_context(viewport={'width': width, 'height': 900}, color_scheme='dark')
    context.add_init_script(CLS)
    page = context.new_page()
    errors = []
    page.on('pageerror', lambda e: errors.append(str(e)))
    page.goto(url)
    page.wait_for_selector(READY, state='attached', timeout=60000)
    for _ in range(300):  # CSP forbids string predicates
        if page.evaluate('()=>!!(window.__myceliumScene && __myceliumScene.ready && window.__colonyMatProbe && window.__marginMatProbe)'):
            break
        page.wait_for_timeout(100)
    return browser, page, errors


def test_production_intro_is_the_owners_pick():
    source = (Path(server.__file__).resolve().parent / 'web' / 'mycelium-geometry.js').read_text()
    assert int(re.search(r'PRODUCTION_INTRO_MS = (\d+)', source).group(1)) == 9000


SWEEP = """(T)=>{const out=[];for(let ms=0;ms<=T*1000+500;ms+=100){__setFieldIntro(ms);
  out.push({ms,hero:__colonyMatProbe(),margin:__marginMatProbe()});}return out}"""


@pytest.mark.parametrize('width', [1440, 1920])
def test_colony_mats_grow_in_over_the_intro(local, width):  # noqa: F811
    with sync_playwright() as p:
        browser, page, errors = _page(p, local['directory'].origin + '/', width)
        T = page.evaluate('MyceliumField.INTRO_MS') / 1000
        rows = page.evaluate(SWEEP, T)
        for kind, keys in (('hero', ('ink', 'cores')), ('margin', ('ink', 'glow'))):
            n = len(rows[0][kind])
            assert n >= 4, kind
            for i in range(n):
                for key in keys:
                    series = [r[kind][i][key] for r in rows]
                    assert series[-1] >= 0.999, (kind, i, key, series[-1])
                    steps = [b - a for a, b in zip(series, series[1:])]
                    # grows (never drops) and no 100 ms step adds over 10% of the final mat
                    assert min(steps) >= -1e-9 and max(steps) <= 0.10, (kind, i, key, round(max(steps), 3), [round(v, 2) for v in series[::5]])
                    # not 'almost instantly': still building halfway into the grow-in
                    half = series[int(T * 10 / 2)]
                    assert 0.1 <= half <= 0.92, (kind, i, key, half)
        assert not errors, errors
        browser.close()


@pytest.mark.parametrize('seconds', [6, 9, 12])
def test_no_spark_burst_during_or_after_the_intro(local, seconds):  # noqa: F811
    local['directory'].staging_samples = True
    with sync_playwright() as p:
        browser, page, errors = _page(p, local['directory'].origin + f'/?intro={seconds}')
        T = page.evaluate('MyceliumField.INTRO_MS') / 1000
        assert T == seconds
        scale = T / 6
        first = page.evaluate('MyceliumHero.firstPulseAt')
        assert abs(first - (T + 2.5 * scale)) < 1e-9
        trips = page.evaluate('__pulseTrips')
        rows = page.evaluate("""(T)=>{const out=[];for(let ms=0;ms<=T*1000+5000;ms+=250){__setFieldIntro(ms);
          const a=__introActivity(),m=__marginPulseProbe(ms/1000);out.push({ms,a,margin:m.active.length});}return out}""", T)
        prev = None
        for r in rows:
            t, a = r['ms'] / 1000, r['a']
            # every 250 ms window over the whole intro and the 5 s after it
            active = [x for x in trips if x['start'] <= t < x['end']]
            started = [x for x in trips if t - 0.25 < x['start'] <= t]
            assert len(active) <= 1 and len(started) <= 1, (t, active)
            if t < first:
                assert not active and not r['margin'], (t, active, r['margin'])
            # sparks: no hero-edge family tip faster than 210 px/s (6 s base), ever
            assert a['fastH'] == 0 and a['rateH'] <= 205 / scale, (t, a['fastH'], a['rateH'])
            if prev is not None:
                # hero-edge families set off a few at a time, never together
                assert a['rootsStarted'] - prev['rootsStarted'] <= 4, (t, a['rootsStarted'], prev['rootsStarted'])
                # The field's own fronts start at t=0 everywhere and only slow
                # down (ease-out; 223 px/s at 9 s falling to 0): the grow-in
                # proper, not a burst. A burst is a late onset: never faster
                # than 0.25 s earlier.
                assert a['rateA'] <= prev['rateA'] + 1e-6 or t < 0.5, (t, a['rateA'], prev['rateA'])
            prev = a
        assert not errors, errors
        browser.close()


HIDE_TEXT = """(()=>{for(const e of document.querySelectorAll('body *')){if(e.tagName==='CANVAS'||e.closest('.page-mycelium-field'))continue;
 e.style.setProperty('color','transparent','important');e.style.setProperty('text-shadow','none','important');
 e.style.setProperty('border-color','transparent','important');e.style.setProperty('background','transparent','important');
 e.style.setProperty('box-shadow','none','important');if(/^(IMG|SVG|INPUT|BUTTON)$/i.test(e.tagName))e.style.setProperty('visibility','hidden','important')}})()"""


@pytest.mark.parametrize('seconds', [6, 9, 12])
def test_no_visible_flash_at_the_bottom_hero_colonies(local, seconds):  # noqa: F811
    """What the owner saw, measured on pixels (so fusion/arrival blooms, the
    knot or primordia reveal and tips arriving together are all covered, not
    only the field fronts): frozen clock every 100 ms over the whole intro and
    the 5 s after it, around the two lowest hero colonies, the pixels that
    newly light up (linear luminance >= 0.06, rising by > 0.04 since the
    previous frame). No frame may exceed twice the median of the 2 s around it
    plus 60 px. b218e4f0 at 9 s: +885 px at 2.7 s and +295 px at 5.5 s (61%,
    the burst); now <= -50 at 6 / 9 / 12 s."""
    import io
    import statistics

    import numpy as np
    from PIL import Image
    local['directory'].staging_samples = True
    with sync_playwright() as p:
        browser, page, errors = _page(p, local['directory'].origin + f'/?intro={seconds}')
        page.evaluate(HIDE_TEXT)
        T = page.evaluate('MyceliumField.INTRO_MS') / 1000
        cols = page.evaluate("""(()=>{const s=__myceliumScene,top=s.hero.top;
          return s.geometry.colonies.map(c=>({x:c.node.x,y:c.node.y+top})).sort((a,b)=>b.y-a.y).slice(0,2)})()""")
        mask = np.zeros((900, 1440), bool)
        for c in cols:
            mask[max(0, int(c['y'] - 160)):int(c['y'] + 240), max(0, int(c['x'] - 200)):int(c['x'] + 200)] = True
        lit, prev = [], None
        for ms in range(0, int(T * 1000) + 5001, 100):
            page.evaluate(f'__setFieldIntro({ms})')
            a = np.asarray(Image.open(io.BytesIO(page.screenshot())).convert('RGB')).astype(np.float32) / 255
            a = np.where(a <= 0.04045, a / 12.92, ((a + 0.055) / 1.055) ** 2.4)
            L = 0.2126 * a[..., 0] + 0.7152 * a[..., 1] + 0.0722 * a[..., 2]
            lit.append(int((mask & (L >= 0.06) & ((L - prev) > 0.04)).sum()) if prev is not None else 0)
            prev = L
        for i, n in enumerate(lit):
            around = [lit[j] for j in range(max(0, i - 10), min(len(lit), i + 11)) if j != i]
            assert n <= 2 * statistics.median(around) + 60, (i / 10, n, lit[max(0, i - 10):i + 11])
        assert not errors, errors
        browser.close()


@pytest.mark.parametrize('seconds', [6, 9, 12])
def test_real_time_load_in_never_pops(local, seconds):  # noqa: F811
    """Owner (r11.2 live): "on loading it seems to abruptly load in some
    mycelium strands in the growth phase ... as well as some of the mycelium in
    circles around the nodes". Causes: the compositor attached only after the
    whole hero build (the colony monotone pass and the re-bake held it to
    ~8.5 s of a 9 s intro, so the field appeared nearly whole), the re-bake
    swapped the raster in one blit, and after the intro the hero was presented
    only every 4 age units. Real-time screencast (DPR1 dark green), newly lit
    pixels per 100 ms bin from 30% of the intro to 3 s after it: no bin above
    2 x the median of the surrounding 2 s + 2000 px (0.15% of the viewport).
    b218e4f0 at 9 s: +5636 px at 31%; 43dd59f4: +8050 px at 8.2 s; now <= +1600."""
    import base64
    import io
    import statistics

    import numpy as np
    from PIL import Image
    local['directory'].staging_samples = True
    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(viewport={'width': 1440, 'height': 900}, color_scheme='dark')
        context.add_init_script(CLS)
        page = context.new_page()
        cdp = context.new_cdp_session(page)
        frames, errors = [], []
        page.on('pageerror', lambda e: errors.append(str(e)))

        def on_frame(ev):
            frames.append((ev['metadata']['timestamp'], ev['data']))
            cdp.send('Page.screencastFrameAck', {'sessionId': ev['sessionId']})
        cdp.on('Page.screencastFrame', on_frame)
        cdp.send('Page.startScreencast', {'format': 'png', 'everyNthFrame': 1})
        page.goto(local['directory'].origin + f'/?intro={seconds}')
        nav = page.evaluate('performance.timeOrigin') / 1000
        page.wait_for_timeout(int(seconds * 1000 + 4000))
        cdp.send('Page.stopScreencast')
        T = page.evaluate('MyceliumField.INTRO_MS') / 1000
        assert T == seconds
        bins, prev = {}, None
        for ts, data in frames:
            t = ts - nav
            a = np.asarray(Image.open(io.BytesIO(base64.b64decode(data))).convert('RGB')).astype(np.float32) / 255
            a = np.where(a <= 0.04045, a / 12.92, ((a + 0.055) / 1.055) ** 2.4)
            L = 0.2126 * a[..., 0] + 0.7152 * a[..., 1] + 0.0722 * a[..., 2]
            if prev is not None:
                k = int(t * 10)
                bins[k] = bins.get(k, 0) + int(((L >= 0.02) & ((L - prev) > 0.015)).sum())
            prev = L
        ks = sorted(bins)
        s = [bins.get(k, 0) for k in range(ks[0], ks[-1] + 1)]
        for i, n in enumerate(s):
            t = (ks[0] + i) / 10
            if not (0.3 * T <= t <= T + 3):
                continue
            around = [s[j] for j in range(max(0, i - 10), min(len(s), i + 11)) if j != i]
            assert n <= 2 * statistics.median(around) + 2000, (t, n, s[max(0, i - 10):i + 11])
        assert not errors, errors
        browser.close()
