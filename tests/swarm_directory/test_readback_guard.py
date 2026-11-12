"""r11.4 canvas readback integrity (the reported LibreWolf "giant circles").

Fingerprinting-protected browsers — LibreWolf 160, Firefox under
resistFingerprinting, canvas-defence extensions — return randomised pixels
from getImageData (measured: a solid #35df47 fill read back as (149,153,140),
(148,110,137), (194,129,116) across three reads, where Firefox returns the
exact colour). The colony fill and the attribution passes turn those reads into
per-ring glow gains, so a lying browser inflates every ring to its alpha cap
and paints giant green discs around the nodes. The page detects untrusted
readback and keeps its deterministic geometry instead.

Density is measured from a screenshot in Python on purpose: a poisoned
browser's own readbacks are exactly what we do not trust. Real local stack.
"""
import pytest
from playwright.sync_api import sync_playwright
from test_site_v2_browser import local, CLS

READY = '#page-mycelium[data-ready="1"]'

# LibreWolf's noise is a coherent per-read channel shift, not per-pixel static:
# a solid #35df47 fill read back as (149,153,140), (148,110,137) and
# (194,129,116), which is exactly (53,223,71) plus (+96,-70,+69), (+95,-113,+66)
# and (+141,-94,+45). Because every pixel in a read moves together, the ring
# averages cannot cancel it — the measured envelope comes out systematically
# wrong, which is what inflates the fill.
POISON = """(() => {
  const real = CanvasRenderingContext2D.prototype.getImageData;
  const SHIFTS = [[96, -70, 69], [95, -113, 66], [141, -94, 45], [-61, 87, -40]];
  let reads = 0;
  CanvasRenderingContext2D.prototype.getImageData = function (x0, y0, w, h) {
    const out = real.call(this, x0, y0, w, h);
    reads++;
    const s = SHIFTS[reads % SHIFTS.length];
    const d = out.data;
    for (let i = 0; i < d.length; i += 4) {
      d[i]     = Math.max(0, Math.min(255, d[i] + s[0]));
      d[i + 1] = Math.max(0, Math.min(255, d[i + 1] + s[1]));
      d[i + 2] = Math.max(0, Math.min(255, d[i + 2] + s[2]));
    }
    return out;
  };
})();"""

# Healthy dark art measures 0.05-0.09 green; the glitch measured 0.35-0.85.
GLITCH_BOUND = 0.25


def _open(p, local, init=()):
    browser = p.chromium.launch()
    context = browser.new_context(viewport={'width': 1440, 'height': 900}, color_scheme='dark')
    context.add_init_script(CLS)
    for script in init:
        context.add_init_script(script)
    page = context.new_page()
    errors = []
    page.on('pageerror', lambda e: errors.append(str(e)))
    page.goto(local['directory'].origin + '/')
    page.wait_for_selector(READY, state='attached', timeout=30000)
    return browser, page, errors


def _green_density(page, path):
    page.screenshot(path=str(path))
    from PIL import Image
    im = Image.open(path).convert('RGB')
    w, h = im.size
    im = im.crop((int(w * 0.5), int(h * 0.15), w, int(h * 0.95)))
    px = im.load()
    cw, ch = im.size
    lit = n = 0
    for y in range(0, ch, 4):
        for x in range(0, cw, 4):
            r, g, b = px[x, y]
            lit += 1 if (g > 90 and g > r * 1.4 and g > b * 1.4) else 0
            n += 1
    return lit / n


def _wait_for_passes(page):
    """Let the colony fill settle and the dips pass finish (or give up)."""
    for _ in range(200):
        done = page.evaluate('!!(window.__myceliumScene && window.__myceliumScene.timings'
                             ' && window.__myceliumScene.timings.dipsDone)')
        if done:
            break
        page.wait_for_timeout(200)
    page.wait_for_timeout(3000)


def test_fingerprinting_noise_never_paints_giant_circles(local, tmp_path):
    with sync_playwright() as p:
        # Control: this stack's healthy art.
        browser, page, errors = _open(p, local)
        _wait_for_passes(page)
        healthy = _green_density(page, tmp_path / 'healthy.png')
        assert not errors, errors
        browser.close()

        # Same stack with LibreWolf-style poisoned readback.
        browser, page, errors = _open(p, local, init=(POISON,))
        # The poison must actually be live, or the test would pass vacuously:
        # two reads of a solid colour must not agree with each other.
        probe = page.evaluate("""() => {
          const c = document.createElement('canvas'); c.width = c.height = 8;
          const x = c.getContext('2d');
          x.fillStyle = 'rgb(53,223,71)'; x.fillRect(0, 0, 8, 8);
          const r = () => Array.from(x.getImageData(0, 0, 8, 8).data.slice(0, 4));
          return [r(), r()];
        }""")
        assert probe[0] != probe[1], f'readback poison not active: {probe}'
        _wait_for_passes(page)
        poisoned = _green_density(page, tmp_path / 'poisoned.png')
        assert not errors, errors
        browser.close()

    assert healthy < GLITCH_BOUND, f'control art itself looks wrong: {healthy:.3f}'
    assert poisoned < GLITCH_BOUND, (
        f'giant circles under readback noise: {poisoned:.3f} (control {healthy:.3f})')
