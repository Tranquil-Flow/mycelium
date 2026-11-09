"""R11.2 (owner): passive growth/fade clearly visible everywhere, sidebar pulses
nearly hero-level, and colonies whose density falls monotonically from the
point (hero and margins) with every margin strand joined to its colony.
Real local stack only."""
import pytest
from playwright.sync_api import sync_playwright
from test_site_v2_browser import local, CLS  # noqa: F401  (fixture)

READY = '#page-mycelium[data-ready="1"]'


def _open(p, local, width, dpr=1, reduced=False):  # noqa: F811
    browser = p.chromium.launch()
    context = browser.new_context(viewport={'width': width, 'height': 900}, device_scale_factor=dpr, color_scheme='dark',
                                  reduced_motion='reduce' if reduced else 'no-preference')
    context.add_init_script(CLS)
    page = context.new_page()
    errors = []
    page.on('pageerror', lambda e: errors.append(str(e)))
    page.goto(local['directory'].origin + '/')
    page.wait_for_selector(READY, state='attached', timeout=90000)
    for _ in range(300):  # the routes and the hero's life list follow ready
        if page.evaluate('!!(__myceliumScene && __myceliumScene.ready && __myceliumScene.geometry && __myceliumScene.geometry.lifeList)'):
            break
        page.wait_for_timeout(100)
    return browser, page, errors


LENGTHS = """(ts)=>{const s=__myceliumScene,f=s.field.strands.filter(p=>!p.tuft),m=f.filter(p=>!p.hair),
  H=s.geometry.paths.filter(P=>/explorer|hair/.test(P.kind));
  return ts.map(t=>({f:__lifeProbe.field(f,t),m:__lifeProbe.field(m,t),h:__lifeProbe.hero(H,t)}))}"""

# Strands visible in the viewport (field copies and hero), their drawn length at times ts.
VIEW = """([ts,sy])=>{const s=__myceliumScene,F=s.field,top=s.hero.top,H=innerHeight,W=innerWidth;
 const copies=[];for(let k=0;k<12;k++){const b=top+k*F.height;if(b+F.top>sy+H)break;if(b+F.top+F.height>=sy)copies.push(b)}
 const f=F.strands.filter(p=>!p.tuft&&p.box[2]>0&&p.box[0]<W&&copies.some(b=>p.box[3]+b>sy&&p.box[1]+b<sy+H));
 const hv=s.geometry.paths.filter(P=>P.box[2]>0&&P.box[0]<W&&P.box[3]+top>sy&&P.box[1]+top<sy+H&&(P.dyn||P.unit>=0));
 return ts.map(t=>({f:__lifeProbe.field(f,t),h:__lifeProbe.hero(hv,t)}))}"""


def _moving(rows, lag, px):
    """Per sample: strands whose drawn length changed by more than px over `lag` samples."""
    out = []
    for i in range(len(rows) - lag):
        out.append(sum(1 for k in ('f', 'h') for a, b in zip(rows[i][k], rows[i + lag][k]) if abs(a - b) > px))
    return out


@pytest.mark.parametrize('width', [1440, 1920])
def test_passive_life_is_visible_at_every_scroll_position(local, width):  # noqa: F811
    """Owner: "if a human is paused at a point on the page, should be able to
    visibly see it is growing/fading". At six scroll positions (hero, the
    below-fold sections, the page bottom) at least 4 strands in the viewport
    are extending or retracting at every moment (>= 8 px/s over 0.5 s), and at
    least 6 change by more than 15 px within every 4 s window. Tips move at
    8-20 px/s while active (a near-constant-speed profile), never a creep.
    416e55b8: field strands changed visibly in 1.9% of 10 s windows and hero
    periphery in 1.2%."""
    with sync_playwright() as p:
        browser, page, errors = _open(p, local, width)
        doc = page.evaluate('document.documentElement.scrollHeight')
        for sy in (0, 700, 1400, 2100, max(0, doc - 1800), max(0, doc - 900)):
            page.evaluate(f'scrollTo(0,{sy})')
            sy = page.evaluate('scrollY')
            rows = page.evaluate(VIEW, [[30 + i * 0.5 for i in range(61)], sy])
            now = _moving(rows, 1, 4)
            win = _moving(rows, 8, 15)
            assert min(now) >= 4 and min(win) >= 6, (sy, min(now), min(win))
        st = page.evaluate('__lifeProbe.stats()')
        speeds = st['speeds']
        assert speeds and min(s[0] for s in speeds) >= 7 - 1e-6 and max(s[0] for s in speeds) <= 17 + 1e-6, speeds[:5]
        assert max(s[1] for s in speeds) <= 20 + 1e-6  # peak of the trapezoid profile
        assert sum(st['tipLen']) / len(st['tipLen']) >= 40  # tens of px, not a wobble
        assert not errors, errors
        browser.close()


def test_reduced_motion_life_is_static(local):  # noqa: F811
    with sync_playwright() as p:
        browser, page, errors = _open(p, local, 1440, reduced=True)
        rows = page.evaluate(LENGTHS, [30, 37, 45])
        assert rows[0]['f'] == rows[1]['f'] == rows[2]['f']
        assert rows[0]['h'] == rows[1]['h'] == rows[2]['h']
        assert not errors, errors
        browser.close()


def test_margin_pulses_nearly_hero_level(local):  # noqa: F811
    """416e55b8: margin pulses at 0.75 intensity with a 135 px leak and 0.75-0.85
    of the hero's rate. Now ~0.92 of the hero's brightness and reach (600 of
    650 px of fine distance), the hero's crest speed and residue, at most one
    crest per side and >= 0.9 of the hero's rate."""
    with sync_playwright() as p:
        browser, page, errors = _open(p, local, 1440)
        for _ in range(100):
            if page.evaluate('__marginPulseProbe(0).built') >= len(page.evaluate('__marginPulseProbe(0).periods')):
                break
            page.wait_for_timeout(100)
        probe = page.evaluate('Array.from({length:400},(_,i)=>__marginPulseProbe(i/2))')
        assert 0.9 <= probe[0]['intensity'] <= 0.95, probe[0]
        sides = [p_['active'] for p_ in probe]
        assert all(len(a) == len(set(a)) for a in sides), 'two trips on one side'
        assert any(sides), 'no margin trip in 200 s'
        assert max(max(p_['reach'] or [0]) for p_ in probe) >= 450
        hero = probe[0]['heroInterval']
        # Owner: about 90-95% of the hero's rate (one crest at a time per side, as the hero).
        assert all(hero / per >= 0.9 for per in probe[0]['periods']), (hero, probe[0]['periods'])
        assert not errors, errors
        browser.close()


@pytest.mark.parametrize('width', [1440, 1920])
def test_margin_strands_join_their_colony(local, width):  # noqa: F811
    """Owner: margin strands looked cut off, starting some distance from the
    colony point. Every strand of a margin colony now starts inside its mat
    (centre-heavy felt core; junction fade 4-18 px, was 11-32 px)."""
    with sync_playwright() as p:
        browser, page, errors = _open(p, local, width)
        page.evaluate('__setUnderstoryTime(24)')
        n = page.evaluate('__myceliumScene.field.marginKnots.length')
        assert n >= 4
        for i in range(n):
            g = None
            for _ in range(30):
                g = page.evaluate(f'__colonyGapProbe({i})')
                if g and g.get('resident', True):
                    break
                page.wait_for_timeout(100)
            assert g['strands'] >= 3, g
            assert g['startsBeyondMat'] == 0 and g['maxGap'] <= 4, (i, {k: v for k, v in g.items() if k != 'rows'})
        assert not errors, errors
        browser.close()


@pytest.mark.parametrize('width', [1440, 1920])
def test_hero_colony_density_falls_from_the_point(local, width):  # noqa: F811
    """Owner: dense core, then a fainter ring, then thicker again. Each hero
    colony's established static raster (the page's own stroke code, colours,
    knots, cores and felt fills) is measured in 8 px rings (median over 24
    sectors) once the intro is over; its profile is non-increasing to 0.6 R0
    within 3%."""
    with sync_playwright() as p:
        browser, page, errors = _open(p, local, width)
        for _ in range(300):
            if page.evaluate('!!__myceliumScene.timings.dipsDone'):
                break
            page.wait_for_timeout(100)
        cols = page.evaluate('__myceliumScene.geometry.colonies.map(c=>({R0:c.R0,d:c.dips||null}))')
        assert cols and all(c['d'] for c in cols), cols
        for c in cols:
            prof = c['d']['after'][:max(3, int(0.6 * c['R0'] // 8))]
            for k in range(1, len(prof)):
                assert prof[k] <= 1.03 * min(prof[:k]) + 1e-9, (round(c['R0']), k, [round(v * 1000, 2) for v in prof])
        assert not errors, errors
        browser.close()
