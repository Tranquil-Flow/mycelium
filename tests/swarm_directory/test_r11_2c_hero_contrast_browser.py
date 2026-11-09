"""R11.2c: hero copy keeps >= 4.5:1 contrast at hero-pulse peaks.

Hero pulses (and their field leaks, margin pulses and cascades) passing behind
the eyebrow, H1, lead and CTAs dropped the busiest-pixel contrast under the
text to 1.02:1 (768), 2.7:1 (1440) and 1.44:1 (1920) at pulse peaks, on
production 43dd59f4 and on r11.2b. The live (pulse) layer is now attenuated
under every text line box near the hero (a soft-edged text-safe zone at the
static strands' own 5% floor; see textSafeMask in mycelium-hero.js).

Method (as the contrast evidence script): the pulse clock is frozen at every
0.5 s over the first two hero trips, the live canvas is read back under the
line boxes in view at the top of '/', and its brightest moments (crest, bloom
and residue under the copy) plus each trip's mid-crest / bloom / residue time
and the first margin and cascade peaks are screenshotted with the text fill
transparent; every line box's busiest backdrop pixel must give >= 4.5:1.
Real local stack only.
"""
import io
import re

import numpy as np
import pytest
from PIL import Image
from playwright.sync_api import sync_playwright

from test_site_v2_browser import CLS, local  # noqa: F401  (fixture)

READY = '#page-mycelium[data-ready="1"]'
SIZES = {390: 844, 768: 1024, 1440: 900, 1920: 900}

RECTS = '''()=>{const out=[];const hb=document.querySelector('.top').getBoundingClientRect().bottom+4;
 const w=document.createTreeWalker(document.querySelector('main'),NodeFilter.SHOW_TEXT);let n;
 while(n=w.nextNode()){if(!n.textContent.trim())continue;const el=n.parentElement;
  if(el.closest('.card,.goal-today,.panel,button'))continue;if(el.closest('.btn')&&!el.closest('.btn.sec'))continue;
  const range=document.createRange();range.selectNodeContents(n);const color=getComputedStyle(el).color;
  for(const r of range.getClientRects())if(r.y>hb&&r.bottom<innerHeight&&r.width>2)out.push({rect:r.toJSON(),color,text:n.textContent.trim().slice(0,40)});}
 return out}'''
HIDE = "()=>{for(const e of document.querySelectorAll('main, main *')){e.style.webkitTextFillColor='transparent';e.style.textDecorationColor='transparent'}}"
SHOW = "()=>{for(const e of document.querySelectorAll('main, main *')){e.style.removeProperty('-webkit-text-fill-color');e.style.removeProperty('text-decoration-color')}}"
# Pulse light under the line boxes (live canvas read back), per frozen time.
SCAN = '''([times,rects,light])=>{const cv=document.querySelector('.page-mycelium-live'),x=cv.getContext('2d'),d=cv.width/innerWidth;
 let X0=1e9,Y0=1e9,X1=-1e9,Y1=-1e9;for(const r of rects){X0=Math.min(X0,r.x);Y0=Math.min(Y0,r.y);X1=Math.max(X1,r.right);Y1=Math.max(Y1,r.bottom)}
 X0=Math.max(0,Math.floor(X0*d));Y0=Math.max(0,Math.floor(Y0*d));X1=Math.min(cv.width,Math.ceil(X1*d));Y1=Math.min(cv.height,Math.ceil(Y1*d));
 const W=X1-X0,H=Y1-Y0,mask=new Uint8Array(W*H);for(const r of rects)for(let y=Math.max(0,Math.floor(r.y*d)-Y0);y<Math.min(H,Math.ceil(r.bottom*d)-Y0);y++)for(let i=Math.max(0,Math.floor(r.x*d)-X0);i<Math.min(W,Math.ceil(r.right*d)-X0);i++)mask[y*W+i]=1;
 const out=[];for(const t of times){__setPulseTime(t);const p=x.getImageData(X0,Y0,W,H).data;let m=0,s=0;
  for(let i=0;i<W*H;i++){if(!mask[i])continue;const a=p[4*i+3];if(!a)continue;const v=light?a:a*Math.max(p[4*i],p[4*i+1],p[4*i+2])/255;s+=v;if(v>m)m=v}
  out.push([t,m,s])}return out}'''
# Total pulse light on the live canvas (the whole viewport): pulses stay visible.
LIGHT = '''(light)=>{const cv=document.querySelector('.page-mycelium-live'),p=cv.getContext('2d').getImageData(0,0,cv.width,cv.height).data;let s=0;
 for(let i=0;i<p.length;i+=4){const a=p[i+3];if(a)s+=light?a:a*Math.max(p[i],p[i+1],p[i+2])/255}return s/255}'''
CANDIDATES = '''()=>{const tr=__pulseTrips.slice(0,2),o=[];for(const t of tr)o.push(t.start+(t.arrival-t.start)*0.5,t.arrival+1.2,t.arrival+4);
 let prev=0,m=[];for(let i=0;i<1600&&m.length<2;i++){const t=10+i/4,n=__marginPulseProbe(t).active.length;if(n>prev)m.push(t);prev=n}
 for(const t of m)o.push(t+2,t+4,t+7);
 for(const sd of __cascadeProbe().sides){const S=sd.starts[0],h=sd.hops[0];if(S!==undefined&&h)o.push(S+h.off+h.dur*0.6,S+h.off+h.dur+1.2)}
 return {extra:o,scan:[tr[0].start,tr[1].end]}}'''


def _lum(a):
    v = np.asarray(a, dtype=float) / 255
    return np.sum(np.where(v <= .04045, v / 12.92, ((v + .055) / 1.055) ** 2.4) * [.2126, .7152, .0722], axis=-1)


def _ratios(page, width, height):
    page.wait_for_timeout(40)
    regions = page.evaluate(RECTS)
    page.evaluate(HIDE)
    px = np.asarray(Image.open(io.BytesIO(page.screenshot())).convert('RGB'))
    page.evaluate(SHOW)
    out = []
    for r in regions:
        b = r['rect']
        x0, x1, y0, y1 = max(0, int(b['x'])), min(width, int(np.ceil(b['right']))), max(0, int(b['y'])), min(height, int(np.ceil(b['bottom'])))
        if x1 <= x0 or y1 <= y0:
            continue
        fg = _lum([float(v) for v in re.findall(r'[\d.]+', r['color'])[:3]])
        bg = _lum(px[y0:y1, x0:x1])
        out.append((float(np.min((np.maximum(fg, bg) + .05) / (np.minimum(fg, bg) + .05))), r['text']))
    return out


def _peaks(series, top=6):
    out = []
    for t, m, s in sorted(series, key=lambda r: (-r[1], -r[2])):
        if m <= 0 or len(out) >= top:
            break
        if all(abs(t - u) >= 0.8 for u in out):
            out.append(t)
    return out


def _open(p, local, width, theme, hue):  # noqa: F811
    browser = p.chromium.launch()
    context = browser.new_context(viewport={'width': width, 'height': SIZES[width]}, color_scheme=theme)
    context.add_init_script(CLS)
    if hue == 'blue':
        context.add_init_script("localStorage.setItem('mycelium-site-glow-v1','blue')")
    page = context.new_page()
    errors = []
    page.on('pageerror', lambda e: errors.append(str(e)))
    page.goto(local['directory'].origin + '/')
    page.wait_for_selector(READY, state='attached', timeout=60000)
    for _ in range(600):  # CSP: poll, no string wait_for_function
        if page.evaluate('!!(window.__myceliumScene?.textSafe && window.__setPulseTime && window.__pulseTrips && window.__cascadeProbe)'):
            break
        page.wait_for_timeout(100)
    page.evaluate('scrollTo(0,0)')
    page.wait_for_timeout(100)
    return browser, page, errors


@pytest.mark.parametrize('hue', ['green', 'blue'])
@pytest.mark.parametrize('theme', ['dark', 'light'])
@pytest.mark.parametrize('width', [390, 768, 1440, 1920])
def test_hero_copy_contrast_holds_at_pulse_peaks(local, width, theme, hue):  # noqa: F811
    height = SIZES[width]
    with sync_playwright() as p:
        browser, page, errors = _open(p, local, width, theme, hue)
        safe = page.evaluate('({floor: __myceliumScene.textSafe.floor, rects: __myceliumScene.textSafe.rects})')
        assert safe['floor'] <= 0.05 and safe['rects'] >= 8, safe
        cand = page.evaluate(CANDIDATES)
        rects = [r['rect'] for r in page.evaluate(RECTS)]
        t0, t1 = cand['scan']
        series = page.evaluate(SCAN, [[t0 + 0.5 * k for k in range(int((t1 - t0) / 0.5))], rects, theme == 'light'])
        peaks = _peaks(series)
        worst = []
        for t in [24, 40] + peaks + cand['extra']:
            page.evaluate(f'__setPulseTime({t})')
            for ratio, text in _ratios(page, width, height):
                worst.append((round(ratio, 2), round(t, 2), text))
        worst.sort()
        assert worst and worst[0][0] >= 4.5, worst[:6]
        # Pulses stay clearly visible elsewhere: at the brightest moment of the
        # scanned trips the live layer still carries pulse light.
        brightest = max(series, key=lambda r: r[2])[0] if peaks else t0 + (t1 - t0) * 0.3
        page.evaluate(f'__setPulseTime({brightest})')
        assert page.evaluate(LIGHT, theme == 'light') > 200
        assert sum(page.evaluate('__cls')) == 0
        assert not errors, errors
        browser.close()


def test_text_safe_zone_is_what_keeps_the_copy_readable(local):  # noqa: F811
    """Negative control at 768 dark (1.02:1 on 43dd59f4 and r11.2b): without
    the zone the same frozen peaks put pulse light under the copy; with it the
    pulse light there is at most ~the 5% floor, while the pulse light outside
    the zone is unchanged."""
    with sync_playwright() as p:
        browser, page, errors = _open(p, local, 768, 'dark', 'green')
        cand = page.evaluate(CANDIDATES)
        rects = [r['rect'] for r in page.evaluate(RECTS)]
        t0, t1 = cand['scan']
        times = [t0 + 0.5 * k for k in range(int((t1 - t0) / 0.5))]
        masked = page.evaluate(SCAN, [times, rects, False])
        page.evaluate('window.__safeKeep = __myceliumScene.textSafe; __myceliumScene.textSafe = null')
        bare = page.evaluate(SCAN, [times, rects, False])
        peak = max(bare, key=lambda r: (r[1], r[2]))
        assert peak[1] >= 200, peak  # crests do pass under the copy
        page.evaluate(f'__setPulseTime({peak[0]})')
        total_bare = page.evaluate(LIGHT, False)
        page.evaluate('__myceliumScene.textSafe = window.__safeKeep')
        page.evaluate(f'__setPulseTime({peak[0]})')
        total_masked = page.evaluate(LIGHT, False)
        assert max(m for _, m, _ in masked) <= 0.08 * 255, max(masked, key=lambda r: r[1])
        # Only the light near the copy is removed: most of the pulse remains.
        assert total_masked >= 0.5 * total_bare, (total_masked, total_bare)
        assert not errors, errors
        browser.close()
