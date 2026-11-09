"""R11.2 cascade (owner, 9 Nov 2026): "would be cool to see for example a pulse
on the hero section slowly extend all the way down the sides of the page as it
spreads out and hits each node". Some hero pulses leave the hero via the edge
cord into the side's margin network and relay colony by colony (1.5-3 s per
hop, each colony blooms and re-emits) down the whole page, tapering gently.
At most one cascade per side; independent margin pulses never overlap it.
Real local stack only."""
import pytest
from playwright.sync_api import sync_playwright

from test_site_v2_browser import CLS, local  # noqa: F401  (fixture)

READY = '#page-mycelium[data-ready="1"]'


def _open(p, local, width, reduced=False):  # noqa: F811
    browser = p.chromium.launch()
    context = browser.new_context(viewport={'width': width, 'height': 900}, color_scheme='dark',
                                  reduced_motion='reduce' if reduced else 'no-preference')
    context.add_init_script(CLS)
    page = context.new_page()
    errors = []
    page.on('pageerror', lambda e: errors.append(str(e)))
    page.goto(local['directory'].origin + '/')
    page.wait_for_selector(READY, state='attached', timeout=90000)
    for _ in range(400):  # leaks are built in idle slices after ready (CSP: no string predicates)
        if page.evaluate('()=>!!(window.__cascadeProbe && __cascadeProbe().built)'):
            break
        page.wait_for_timeout(100)
    return browser, page, errors


@pytest.mark.parametrize('width', [1440, 1920])
def test_cascades_relay_down_the_whole_page(local, width):  # noqa: F811
    with sync_playwright() as p:
        browser, page, errors = _open(p, local, width)
        probe = page.evaluate('__cascadeProbe()')
        doc = page.evaluate('document.documentElement.scrollHeight')
        trips = page.evaluate('__pulseTrips.map((t,i)=>({start:t.start,landings:__myceliumScene.sched.trips[i].route.landings.map(l=>l.time)}))')
        first = page.evaluate('MyceliumHero.firstPulseAt')
        assert probe['built'] and len(probe['sides']) == 2
        for side in probe['sides']:
            hops = side['hops']
            # colony by colony, down to the last colony above the page bottom (across page copies)
            assert len(hops) >= 4, hops
            ys = [h['nodeY'] for h in hops]
            assert all(b > a for a, b in zip(ys, ys[1:])), ys
            assert ys[-1] >= doc - 800, (ys[-1], doc)
            assert all(1.5 - 1e-6 <= h['dur'] <= 3 + 1e-6 for h in hops)
            gains = [h['gain'] for h in hops]
            assert gains[0] >= 0.9 and all(b <= a for a, b in zip(gains, gains[1:])) and gains[-1] >= 0.62 - 1e-6, gains
            assert all(h['reach'] and h['reach'] >= 250 for h in hops)  # it spreads into the threads around each cord
            # a full-page run takes 10-40 s
            assert 8 <= side['span'] <= 40, side['span']
            # triggered by hero pulses: each start is a hero pulse reaching or leaving the side's colony
            starts = side['starts']
            assert starts and starts[0] >= first
            assert all(b - a >= side['span'] for a, b in zip(starts, starts[1:]))  # one cascade per side at a time
            # (pulse departures from / landings at the side's hero colony; exact times)
            assert set(starts) <= set(side['events']) and len(starts) <= len(side['events']) / 2 + 1
            assert set(side['events']) <= {round(t['start'] + x, 6) for t in trips for x in [0] + t['landings']}
            # independent margin crests never overlap a cascade on that side
            for s0 in starts[:3]:
                for k in range(0, int(side['span'] * 2)):
                    t = s0 + k / 2
                    active = page.evaluate(f'__marginPulseProbe({t}).active')
                    assert side['side'] not in active, (t, active)
                    casc = [a for a in page.evaluate(f'__cascadeProbe({t}).active') if a['side'] == side['side']]
                    assert len(casc) == 1, (t, casc)
        assert not errors, errors
        browser.close()


def test_a_cascade_is_visible_far_down_the_page(local):  # noqa: F811
    """Frozen clock: when the cascade reaches a colony three or more hops down
    (below the fold), that colony visibly blooms (pulses on vs off), and the
    cord leading to it carries the crest."""
    with sync_playwright() as p:
        browser, page, errors = _open(p, local, 1440)
        side = page.evaluate('__cascadeProbe()')['sides'][0]
        hop = side['hops'][3]
        t_land = side['starts'][0] + hop['off'] + hop['dur'] + 1.2
        page.evaluate(f"scrollTo(0,{max(0, hop['nodeY'] - 450)})")
        sy = page.evaluate('scrollY')
        clip = {'x': max(0, hop['nodeX'] - 60), 'y': hop['nodeY'] - sy - 60, 'width': 120, 'height': 120}

        def shot(pulses):
            page.evaluate(f'__myceliumScene.pulsesOff={"false" if pulses else "true"}')
            page.evaluate(f'__setPulseTime({t_land})')
            return page.screenshot(clip=clip)
        on, off = shot(True), shot(False)
        assert on != off  # the bloom is drawn at that colony
        assert page.evaluate(f'__cascadeProbe({t_land}).active.length') >= 1
        assert not errors, errors
        browser.close()


def test_reduced_motion_has_no_cascade(local):  # noqa: F811
    with sync_playwright() as p:
        browser, page, errors = _open(p, local, 1440, reduced=True)
        side = page.evaluate('__cascadeProbe()')['sides'][0]
        hop = side['hops'][1]
        t = side['starts'][0] + hop['off'] + hop['dur'] + 1
        page.evaluate(f"scrollTo(0,{max(0, hop['nodeY'] - 450)})")
        sy = page.evaluate('scrollY')
        clip = {'x': max(0, hop['nodeX'] - 60), 'y': hop['nodeY'] - sy - 60, 'width': 120, 'height': 120}
        page.evaluate(f'__setPulseTime({t})')
        a = page.screenshot(clip=clip)
        page.evaluate('__myceliumScene.pulsesOff=true')
        page.evaluate(f'__setPulseTime({t})')
        assert page.screenshot(clip=clip) == a
        assert not errors, errors
        browser.close()
