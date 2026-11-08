"""R11 field lane: full-DPR sharp field and margins, r9-level mycelium behind the
below-fold text, fine-hair fuzz on every persistent strand, sparse hyphal knots
(never under glyphs, never arcs), a living (never frozen) field, and the shared
6 s load-in intro. Real local stack only."""
import pytest
from playwright.sync_api import sync_playwright
from test_site_v2_browser import local, CLS  # noqa: F401  (fixture)

READY = '#page-mycelium[data-ready="1"]'
ARC_PROBE = """window.__arcCalls={};const arc=CanvasRenderingContext2D.prototype.arc;
CanvasRenderingContext2D.prototype.arc=function(...a){const k=this.canvas.id||this.canvas.className||'offscreen';
window.__arcCalls[k]=(window.__arcCalls[k]||0)+1;return arc.apply(this,a)};"""
# Painted share of the content column (static field canvas only), at a below-fold scroll.
_UNUSED_COLUMN = """()=>{const c=document.getElementById('page-mycelium'),x=c.getContext('2d'),d=c.width/innerWidth;
const s=[...document.querySelectorAll('main .sect')].map(e=>e.getBoundingClientRect());
const l=Math.min(...s.map(r=>r.left)),r=Math.max(...s.map(r=>r.right));
const img=x.getImageData(Math.round(l*d),Math.round(80*d),Math.round((r-l)*d),Math.round((innerHeight-80)*d)).data;
let n=0;for(let i=3;i<img.length;i+=4)if(img[i]>=10)n++;return n/(img.length/4)}"""


def _open(p, local, width, dpr=1, theme='dark', reduced=None):
    browser = p.chromium.launch()
    kw = {'reduced_motion': reduced} if reduced else {}
    context = browser.new_context(viewport={'width': width, 'height': 900}, device_scale_factor=dpr, color_scheme=theme, **kw)
    context.add_init_script(CLS)
    context.add_init_script(ARC_PROBE)
    page = context.new_page()
    errors = []
    page.on('pageerror', lambda e: errors.append(str(e)))
    page.goto(local['directory'].origin + '/')
    page.wait_for_selector(READY, state='attached', timeout=30000)
    return browser, page, errors


def test_field_is_full_dpr_hairy_knotted_and_r9_dense_behind_text(local):  # noqa: F811
    with sync_playwright() as p:
        browser, page, errors = _open(p, local, 1440, dpr=2)
        page.evaluate('__setUnderstoryTime(24)')  # diagnostic seek: the established field
        audit = page.evaluate('__understoryAudit')
        # (9) Crisp: the cached tile is rastered at device pixels, not CSS pixels.
        assert audit['dpr'] == 2 and audit['tileScale'] == 2, audit
        # (3) Every persistent non-tuft strand carries fine hairs.
        bare = page.evaluate('''()=>__myceliumScene.field.strands.filter(p=>p.static&&!p.tuft&&(!p.hair||p.order===1)
          &&p.arc[p.arc.length-1]>40&&!(p.fuzz&&p.fuzz.length)).length''')  # rastered by now
        assert bare == 0 and audit['fuzz'] > 5000, (bare, audit['fuzz'])
        # (7) Sparse, varied, grouped knots; none under glyphs; no arcs anywhere on the field.
        radii = audit['knotRadii']
        assert 20 <= audit['knots'] <= 400 and audit['knotGroups'] < audit['knots'], audit['knots']
        assert min(radii) >= 1 and max(radii) <= 3 and len(set(radii)) > 10, radii[:20]
        # (8) Mycelium behind the below-fold text: column strand length per area x
        # its column weight. r10 (02e3dcad): 82 (1440) / 63 (1920); r11: ~470 / ~410.
        dens = page.evaluate('''()=>{const f=__myceliumScene.field,l=f.layout;let L=0;for(const p of f.strands){if(!p.static||p.margin)continue;
          for(let i=1;i<p.points.length;i++){const a=p.points[i-1],b=p.points[i],x=(a.x+b.x)/2;if(x>l.left&&x<l.right)L+=Math.hypot(b.x-a.x,b.y-a.y)}}
          return __understoryAudit.columnWeight*L/((l.right-l.left)*f.height)*1e4}''')
        assert dens >= 250, dens
        page.wait_for_timeout(300)
        arcs = page.evaluate('__arcCalls')
        assert not arcs.get('page-mycelium', 0) and not arcs.get('page-mycelium page-mycelium-live', 0), arcs
        assert not errors, errors
        browser.close()


def test_knots_never_sit_under_text_and_glyphs_have_a_local_halo(local):  # noqa: F811
    with sync_playwright() as p:
        browser, page, errors = _open(p, local, 1440)
        hidden = page.evaluate('''()=>{const r=document.createRange(),rects=[];
          for(const n of (function*(){const w=document.createTreeWalker(document.querySelector('main'),NodeFilter.SHOW_TEXT);for(let n=w.nextNode();n;n=w.nextNode())yield n})()){
            if(!n.nodeValue.trim())continue;r.selectNodeContents(n);for(const b of r.getClientRects())rects.push([b.left,b.top+scrollY,b.right,b.bottom+scrollY])}
          const s=__myceliumScene,f=s.field,H=document.documentElement.scrollHeight;let bad=0;
          for(const k of f.hyphalKnots){if(k.underText)continue;for(let m=0;m<=Math.ceil(H/f.height);m++){const y=s.hero.top+k.y+m*f.height;
            if(rects.some(b=>k.x>b[0]&&k.x<b[2]&&y>b[1]&&y<b[3]))bad++}}return bad}''')
        assert hidden == 0
        halo = page.evaluate("getComputedStyle(document.querySelector('main .sect p')).textShadow")
        assert halo and halo != 'none', halo
        assert page.evaluate("getComputedStyle(document.querySelector('main .sect .card:not(.flat) p, main .sect .btn') || document.body).textShadow") in ('none', '')
        assert sum(page.evaluate('__cls')) == 0
        assert not errors, errors
        browser.close()


def test_intro_contract_and_living_field(local):  # noqa: F811
    with sync_playwright() as p:
        browser, page, errors = _open(p, local, 1440)
        assert page.evaluate('MyceliumField.INTRO_MS') == 6000
        state = page.get_attribute('#page-mycelium', 'data-intro')
        assert state in ('running', 'done'), state  # never 'skipped' on a fresh load
        page.wait_for_selector('#page-mycelium[data-intro="done"]', state='attached', timeout=20000)
        assert page.evaluate('MyceliumField.introProgress') == 1
        # Capture hook: early intro is sparse, the end matches the established field.
        early = page.evaluate('__setFieldIntro(800)')
        assert early['drawn'] < early['statics']
        late = page.evaluate('__setFieldIntro(6000)')
        assert late['done'] or late['drawn'] == late['statics'], late
        browser.close()
        # Never frozen: below the fold the live layer changes over time (growth, breathing, pulses).
        browser, page, errors = _open(p, local, 1440)
        page.wait_for_selector('#page-mycelium[data-intro="done"]', state='attached', timeout=20000)
        page.evaluate('scrollTo(0,1300)')
        page.wait_for_timeout(600)
        snap = "document.querySelector('.page-mycelium-live').toDataURL()"
        a = page.evaluate(snap)
        page.wait_for_timeout(1500)
        assert page.evaluate(snap) != a
        assert not errors, errors
        browser.close()


@pytest.mark.parametrize('how', ['reduced', 'switch'])
def test_intro_skipped_for_reduced_motion_and_never_replayed(local, how):  # noqa: F811
    with sync_playwright() as p:
        if how == 'reduced':
            browser, page, errors = _open(p, local, 1440, reduced='reduce')
            assert page.get_attribute('#page-mycelium', 'data-intro') == 'skipped'
        else:
            browser, page, errors = _open(p, local, 1440)
            page.click('[data-glow-choice="blue"]')
            page.wait_for_selector(READY, state='attached', timeout=30000)
            page.wait_for_timeout(300)
            assert page.get_attribute('#page-mycelium', 'data-intro') == 'skipped'
        assert not errors, errors
        browser.close()
