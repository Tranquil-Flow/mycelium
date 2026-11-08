"""R10 structure and growth: two-sided visible-scale audit, radiating tufts,
rhizomorph cords, soft page-space edges, coherent fronts with accretion, no
generated-looking tells, progressive first frame. Real local stack only."""
import pytest
from playwright.sync_api import sync_playwright
from test_site_v2_browser import local, CLS

READY = '#page-mycelium[data-ready="1"]'
# Counts every arc() on the visible scene canvases (fusion beads were arcs).
ARC_PROBE = """window.__arcCalls={};const arc=CanvasRenderingContext2D.prototype.arc;
CanvasRenderingContext2D.prototype.arc=function(...a){const k=this.canvas.id||this.canvas.className||'offscreen';
window.__arcCalls[k]=(window.__arcCalls[k]||0)+1;return arc.apply(this,a)};"""


def _open(p, local, width, theme='dark', **kw):
    browser = p.chromium.launch()
    context = browser.new_context(viewport={'width': width, 'height': 900}, color_scheme=theme, **kw)
    context.add_init_script(CLS)
    context.add_init_script(ARC_PROBE)
    page = context.new_page()
    errors = []
    page.on('pageerror', lambda e: errors.append(str(e)))
    page.goto(local['directory'].origin + '/')
    page.wait_for_selector(READY, state='attached', timeout=30000)
    return browser, page, errors


def test_audit_discriminates_ruler_scribble_and_waves(local):
    """Negative controls: the audit must flag the defects it guards against."""
    with sync_playwright() as p:
        browser, page, errors = _open(p, local, 1440)
        result = page.evaluate('''async()=>{const g=await import('/swarms/mycelium-geometry.js');
          const ruler=[{kind:'r',points:Array.from({length:120},(_,i)=>({x:i*2.5,y:0}))}];
          const wave=[{kind:'w',points:Array.from({length:240},(_,i)=>({x:i*2.5,y:14*Math.sin(i*2.5/36)}))}];
          let s=1;const R=()=>((s=Math.imul(s,1103515245)+12345>>>0)/4294967296);
          const scribble=[];for(let k=0;k<20;k++){let h=0,x=0,y=0;const pts=[{x,y}];for(let i=0;i<120;i++){h+=(R()-.5)*1.8;x+=Math.cos(h)*3.5;y+=Math.sin(h)*3.5;pts.push({x,y})}scribble.push({kind:'s',points:pts})}
          const walk=[];const Q=g.rng('control');for(let k=0;k<60;k++)walk.push({kind:'p',points:g.persistWalk({x:0,y:0},Q()*6.28,400,Q)});
          return {ruler:g.auditPaths(ruler).r,wave:g.auditPaths(wave).w,scribble:g.auditPaths(scribble).s,walk:g.auditPaths(walk).p}}''')
        assert result['ruler']['window150']['above099'] == result['ruler']['window150']['count']
        assert result['wave']['periodicity']['p50'] > 0.8
        assert result['scribble']['turningPer100px']['p50'] > 4 and result['scribble']['uturnsPer1000px'] > 1
        walk = result['walk']
        assert walk['window150']['above099'] / walk['window150']['count'] < 0.06
        assert walk['turningPer100px']['p95'] < 2.2 and walk['uturnsPer1000px'] == 0
        assert walk['periodicity']['p50'] < 0.35
        assert not errors
        browser.close()


@pytest.mark.parametrize('width', [1280, 1440, 1920, 2560])
def test_two_sided_morphology_structure_and_soft_edges(local, width):
    with sync_playwright() as p:
        browser, page, errors = _open(p, local, width)
        audit = page.evaluate('__morphologyAudit')
        for kind in ('colony', 'cord-centre', 'sparse', 'field'):
            a = audit[kind]
            assert a['paths'] >= 5, (kind, a['paths'])
            w = a['window150']
            # Not ruler-straight at visible scale (>=150 px windows) ...
            # (The sparse vocabulary deliberately includes some nearly straight long
            # runs, so its share of >0.99 windows may be higher; none is ruler-straight.)
            limit = 0.2 if kind in ('sparse', 'field') else 0.12
            assert w['above099'] / w['count'] < limit and w['max'] < 0.999, (kind, w)
            assert a['window250']['max'] < 0.997, (kind, a['window250'])
            # ... and not scribbled or wave-like.
            assert a['turningPer100px']['p95'] < 2.2, (kind, a['turningPer100px'])
            assert a['uturnsPer1000px'] < 0.05, (kind, a['uturnsPer1000px'])
            assert a['periodicity']['p50'] < 0.35, (kind, a['periodicity'])
            if kind in ('sparse', 'field'):
                # ... nor uniformly curvy: strands differ in turning rate and
                # some carry one or two strong bends (owner: no uniform arcs).
                assert a['strandTurnCV'] >= 0.19, (kind, a['strandTurnCV'])  # 6405a6fa: 0.15-0.18
                assert a['bendContrast']['p95'] >= 3.5, (kind, a['bendContrast'])
        cords = page.evaluate('__cordAudit')
        links = [c for c in cords if c['link']]
        # r11 (owner): nodes are always the densest mats, so link cords are
        # thinner bundles than r10's 30-40 fibres at 8-12 px (D46).
        assert links and all(22 <= c['fibres'] <= 28 and 5 <= c['w0'] <= 7 and 1.4 <= c['w1'] <= 1.8 for c in links), links
        assert any(c['brush'] for c in cords if not c['link'])
        # D4: page-space geometry reaches the right edge; no empty band or clip line.
        cols = page.evaluate('''()=>{const c=document.getElementById('hyphae'),x=c.getContext('2d'),w=c.width,h=c.height,
          d=x.getImageData(0,0,w,h).data,n=64,cols=new Array(n).fill(0);
          for(let y=0;y<h;y+=3)for(let i=0;i<w;i+=2){cols[Math.min(n-1,Math.floor(i/w*n))]+=d[(y*w+i)*4+3];}return cols}''')
        right = sum(cols[-6:]) / 6
        middle = sum(cols[38:58]) / 20
        assert right > 0.25 * middle, (right, middle)
        steps = [cols[i + 1] / max(1, cols[i]) for i in range(32, 63)]
        assert min(steps) > 0.15, steps
        # D6/D7 tells: no fusion beads (arcs) on the static raster or the live layer.
        page.wait_for_timeout(600)
        arcs = page.evaluate('__arcCalls')
        nodes = int(page.locator('#hyphae').get_attribute('data-nodes'))
        assert arcs.get('hyphae', 0) <= nodes and arcs.get('page-mycelium page-mycelium-live', 0) == 0, arcs
        audit = page.evaluate('__understoryAudit')
        assert audit['loneThreads'] == 0 and audit['explorerLoneThreads'] == 0
        assert not errors
        browser.close()


def test_progressive_first_frame_bounded_slices_and_growth(local):
    with sync_playwright() as p:
        browser, page, errors = _open(p, local, 1440)
        timings = page.evaluate('__myceliumScene.timings')
        assert timings['firstPaint'] < 300, timings
        assert timings['maxSlice'] < 60, timings
        assert timings['complete'] < 8000, timings
        growth = page.evaluate('[12,30,60,140].map(a=>__growthAudit(a))')
        fronts = [[c['meanFront'] for c in g['colonies']] for g in growth]
        # Coherent fronts advance by distance from the source, never retract.
        assert all(a < b for a, b in zip(fronts[0], fronts[1]))
        assert all(a <= b + 1e-6 for g1, g2 in zip(fronts[1:], fronts[2:]) for a, b in zip(g1, g2))
        assert all(c['frontCV'] < 0.3 for c in growth[0]['colonies'])
        # Accretion: cords gain fibres at their base after the first front passes.
        base = [[c['fibresAtBase'] for c in g['cords']] for g in growth]
        assert sum(base[1]) < sum(base[2]) <= sum(base[3])
        assert sum(base[3]) >= 20 * len(base[3])
        probe = page.evaluate('Array.from({length:120},(_,i)=>__dendriticProbe(i*5))')
        assert min(s['activeTips'] for s in probe) > 0
        assert all(s['regressingTips'] == 0 for s in probe)
        assert all(s['connectedSplits'] == s['fusions'] > 0 for s in probe)
        # Breathing is per colony and desynchronised.
        durations = page.evaluate("[...document.querySelectorAll('.mycelium-breath canvas')].map(c=>c.style.animationDuration)")
        assert len(durations) >= 4 and len(set(durations)) == len(durations)
        assert not errors
        browser.close()


def test_margin_pulse_leg_crosses_the_reader_viewport(local):
    with sync_playwright() as p:
        browser, page, errors = _open(p, local, 1920)
        page.evaluate('scrollTo(0,1300)')
        page.wait_for_timeout(200)
        legs = page.evaluate('''()=>{const out=[];for(let t=0;t<400;t+=0.5){const s=__marginPulseProbe(t);
          if(s.active.length)out.push(...s.legs)}return out}''')
        assert legs
        visible = [l for l in legs if l['y1'] >= 1300 - 200 and l['y0'] <= 1300 + 900 + 200]
        assert len(visible) >= 0.5 * len(legs), legs[:6]
        assert not errors
        browser.close()
