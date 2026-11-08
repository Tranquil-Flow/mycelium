"""R11 hero (owner feedback 9 Nov 2026): nodes are always the densest mats,
persistent strands carry fuzz, pulse growth leaves a residue that retreats from
the far edges back toward the nodes, no disc light-up at pulse departure, a
load-in grow-out intro (once per page, skipped for reduced motion / hidden /
re-mounts), and sparse, irregular hyphal knots. Real local stack only."""
import pytest
from playwright.sync_api import sync_playwright
from test_site_v2_browser import local, CLS

READY = '#page-mycelium[data-ready="1"]'
LONGTASKS = """window.__lt=[];try{new PerformanceObserver(l=>{for(const e of l.getEntries())__lt.push([e.startTime,e.duration])}).observe({type:'longtask',buffered:true})}catch{}"""


def _open(p, local, width, theme='dark', init=(), **kw):
    browser = p.chromium.launch()
    context = browser.new_context(viewport={'width': width, 'height': 900}, color_scheme=theme, **kw)
    context.add_init_script(CLS)
    for script in init:
        context.add_init_script(script)
    page = context.new_page()
    errors = []
    page.on('pageerror', lambda e: errors.append(str(e)))
    page.goto(local['directory'].origin + '/')
    page.wait_for_selector(READY, state='attached', timeout=30000)
    return browser, page, errors


def _hero_alpha(page):
    """Count of hero raster pixels with any light (the established-state proxy)."""
    return page.evaluate('''()=>{const c=document.getElementById('hyphae'),d=c.getContext('2d').getImageData(0,0,c.width,c.height).data;
      let n=0;for(let i=3;i<d.length;i+=16)if(d[i]>8)n++;return n}''')


@pytest.mark.parametrize('width', [1440, 1920])
def test_nodes_are_always_the_densest_mats(local, width):
    with sync_playwright() as p:
        browser, page, errors = _open(p, local, width)
        page.evaluate('__setPulseTime(24)')
        density = page.evaluate('__colonyDensity()')
        cores = [c for c in density['colonies'] if c['visible']]
        assert len(cores) >= 4
        weakest = min(c['core']['mean'] for c in cores)
        for cord in density['cords']:
            # No cord window beyond the mats is as dense as any colony core ...
            assert cord['max']['mean'] < weakest, (cord, weakest)
            # ... and every cord is far narrower than the matted core it leaves.
            assert cord['w0'] < min(c['coreWidth'] for c in density['colonies']) / 4, cord
        links = [c for c in page.evaluate('__cordAudit') if c['link']]
        assert links and all(c['w0'] <= 7.5 for c in links), links
        assert not errors
        browser.close()


def test_persistent_strands_carry_fine_hair_fuzz(local):
    with sync_playwright() as p:
        browser, page, errors = _open(p, local, 1440)
        fuzz = page.evaluate('__fuzzAudit()')
        for kind in ('explorer', 'web', 'brush'):
            f = fuzz[kind]
            assert f['strands'] >= 10, (kind, f)
            # Pulse-lit hairs on colony hyphae run ~10-14 per 100 px; persistent
            # strands carry the same order of fuzz, and almost none are bare.
            assert f['p50'] >= 7 and f['p10'] >= 3, (kind, f)
            assert f['bare'] / f['strands'] < 0.05, (kind, f)
        assert not errors
        browser.close()


def test_pulse_leaves_as_a_ripple_without_a_disc(local):
    """The departing colony must not light up as a disc before the ripple."""
    with sync_playwright() as p:
        browser, page, errors = _open(p, local, 1440)
        trip = page.evaluate('__pulseTrips[0]')
        node = [n for n in page.evaluate('__growthGeometry.nodes') if n['id'] == trip['from']][0]
        R0 = page.evaluate(f"__myceliumScene.geometry.colonies[{trip['from']}].R0")
        ring = '''([x,y,r0,r1])=>{const c=document.querySelector('.page-mycelium-live'),k=c.getContext('2d'),s=c.width/innerWidth,
          top=__myceliumScene.hero.top-scrollY,d=k.getImageData(0,0,c.width,c.height).data;let n=0;
          for(let yy=Math.max(0,(y+top-r1)*s|0);yy<Math.min(c.height,(y+top+r1)*s);yy+=1)for(let xx=Math.max(0,(x-r1)*s|0);xx<Math.min(c.width,(x+r1)*s);xx+=1){
            const q=Math.hypot(xx/s-x,yy/s-y-top);if(q<r0||q>r1)continue;const i=(yy*c.width+xx)*4;n+=d[i]+d[i+1]+d[i+2];}return n}'''
        args = [node['x'], node['y'], 0.3 * R0, 0.7 * R0]
        page.evaluate(f"__setPulseTime({trip['start'] - 0.3})")
        before = page.evaluate(ring, args)
        lit = []
        for dt in (0.3, 0.6, 0.9):
            page.evaluate(f"__setPulseTime({trip['start'] + dt})")
            lit.append(page.evaluate(ring, args))
        # The ripple has not reached 0.3 R0 yet: the annulus stays as it was.
        assert max(lit) <= before * 1.15 + 2000, (before, lit)
        assert not errors
        browser.close()


def test_pulse_growth_residue_lingers_then_retreats_edge_first(local):
    with sync_playwright() as p:
        browser, page, errors = _open(p, local, 1440)
        probe = page.evaluate('''()=>{const out=[];const r=__myceliumScene.routes[0];
          for(let a=0;a<=__myceliumScene.residueProbe(0,0).end+1;a+=0.5)out.push([a,__myceliumScene.residueProbe(0,a).layers.map(l=>l.alpha)]);
          return {series:out,layers:__myceliumScene.residueProbe(0,0).layers,tail:__myceliumScene.pulseTail}}''')
        layers, series, tail = probe['layers'], probe['series'], probe['tail']
        assert len(layers) >= 6
        for i, layer in enumerate(layers):
            alive = [a for a, alphas in series if alphas[i] > 0.05]
            assert alive, layer
            # Lingers well past the pulse envelope (r10 faded after ~5.6 s) ...
            assert alive[-1] - layer['arrival'] > tail + 4, (layer, alive[-1])
        # ... fades latest-reached (far edge) first, back toward the nodes ...
        gone = [max(a for a, alphas in series if alphas[i] > 0.05) for i in range(len(layers))]
        for i in range(len(layers)):
            for j in range(len(layers)):
                if layers[j]['arrival'] > layers[i]['arrival'] + 3.9 and layers[j]['far'] >= layers[i]['far']:
                    assert gone[j] <= gone[i] + 1e-6, (layers[i], layers[j], gone[i], gone[j])
        # ... and never silts up: all residue is gone by the end of the trip window.
        assert all(alpha == 0 for alpha in series[-1][1])
        # Bounded bitmaps while a whole trip plays.
        trip = page.evaluate('__pulseTrips[0]')
        for t in range(int(trip['start']), int(trip['end']) + 20, 3):
            page.evaluate(f'__setPulseTime({t})')
        stats = page.evaluate('__residueStats')
        assert 0 < stats['peakBytes'] < 96e6 and stats['layers'] <= stats['limit'], stats
        assert not errors
        browser.close()


def test_load_in_grows_out_from_empty_and_never_replays(local):
    with sync_playwright() as p:
        browser, page, errors = _open(p, local, 1440, init=[LONGTASKS])
        scene = page.evaluate('({intro:__myceliumScene.intro,first:__myceliumScene.timings.firstPaint,INTRO_MS:MyceliumHero.INTRO_MS})')
        assert scene['intro']['on'] and scene['INTRO_MS'] == 6000
        assert scene['first'] < 150, scene
        # The intro is still running when the scene is ready (~1-2 s in).
        assert 0 < page.evaluate('MyceliumHero.introProgress()') < 1
        # Frozen-clock growth: from (nearly) empty at t=0 to established by 6 s.
        counts = []
        for t in (0.02, 0.8, 1.6, 3, 4.5, 6, 24):
            page.evaluate(f'__setPulseTime({t})')
            counts.append(_hero_alpha(page))
        full = counts[-1]
        assert counts[0] < 0.02 * full, counts
        assert all(a < b for a, b in zip(counts[:4], counts[1:5])), counts
        assert counts[5] >= 0.95 * full, counts
        page.wait_for_timeout(6500)
        assert not [t for t in page.evaluate('__lt') if t[1] > 120], page.evaluate('__lt')
        # A hue switch re-mounts without replaying the intro.
        page.click('[data-glow-choice="blue"]')
        page.wait_for_selector(READY, state='attached', timeout=30000)
        for _ in range(300):
            if page.evaluate('()=>!!(window.__myceliumScene && __myceliumScene.ready)'):
                break
            page.wait_for_timeout(100)
        assert not page.evaluate('__myceliumScene.intro.on')
        assert _hero_alpha(page) >= 0.9 * full
        assert not errors
        browser.close()


@pytest.mark.parametrize('mode', ['reduce', 'hidden'])
def test_reduced_motion_and_hidden_tabs_show_the_established_state(local, mode):
    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(viewport={'width': 1440, 'height': 900}, color_scheme='dark',
                                      reduced_motion='reduce' if mode == 'reduce' else 'no-preference')
        if mode == 'hidden':
            context.add_init_script("Object.defineProperty(document,'hidden',{configurable:true,get:()=>true})")
        page = context.new_page()
        errors = []
        page.on('pageerror', lambda e: errors.append(str(e)))
        page.goto(local['directory'].origin + '/')
        for _ in range(300):  # CSP forbids string predicates (no unsafe-eval)
            if page.evaluate('()=>!!(window.__myceliumScene && __myceliumScene.ready)'):
                break
            page.wait_for_timeout(100)
        assert not page.evaluate('__myceliumScene.intro.on')
        assert page.evaluate('MyceliumHero.introProgress()') == 1
        assert _hero_alpha(page) > 10000
        assert not errors
        browser.close()


def test_hyphal_knots_are_sparse_irregular_tangles_on_strands(local):
    with sync_playwright() as p:
        browser, page, errors = _open(p, local, 1440)
        knots = page.evaluate('__knotAudit()')
        assert 12 <= len(knots) <= 140, len(knots)
        assert all(k['onGraph'] and k['fibres'] >= 3 and 3 <= k['lobes'] <= 6 for k in knots)
        plain = [k for k in knots if not k['primordium']]
        radii = sorted(k['r'] for k in plain)
        # Heavy-tailed sizes: mostly tiny, a few larger; never uniform beads.
        assert radii[len(radii) // 2] < 1.6 and radii[-1] <= 3.01 and radii[-1] > 1.8 * radii[0], radii
        prim = [k for k in knots if k['primordium']]
        assert all(k['colonyDistance'] < 0.75 and k['r'] <= 4.01 and k['el'] >= 1.4 for k in prim), prim
        # Irregular spacing: nearest-neighbour distances vary widely (groups and gaps).
        import math
        nn = [min(math.hypot(a['x'] - b['x'], a['y'] - b['y']) for b in knots if b is not a) for a in knots]
        mean = sum(nn) / len(nn)
        cv = (sum((v - mean) ** 2 for v in nn) / len(nn)) ** 0.5 / mean
        assert cv > 0.5, cv
        assert sum(v < 32 for v in nn) >= 0.25 * len(nn), nn
        assert not errors
        browser.close()
