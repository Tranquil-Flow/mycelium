# SPDX-License-Identifier: AGPL-3.0-only
"""Generate the /about illustrations as inline SVG (About page v4).

Writes five figures into mycelium_swarm_directory/web/about.html between
`<!-- fig:NAME -->` and `<!-- /fig:NAME -->` markers. Each figure has a wide
variant and a simplified tall one for phones (CSS shows one of them).

Style: the site's bioluminescent mycelium (hair-thin strands bundled into
cords, cottony tufts under the devices, fine side hairs), coloured only via
classes that about.css maps onto the theme variables (--strand, --glow,
--core, --ink, --muted, --card...), so the art follows Green/Blue and
dark/light. No inline styles (the site CSP has no 'unsafe-inline'), no
external references, deterministic (seeded).

    python3 scripts/generate_about_figures.py          # rewrite about.html
    python3 scripts/generate_about_figures.py --check  # exit 1 if stale
"""
from __future__ import annotations

import math
import random
import re
import sys
from pathlib import Path

ABOUT = Path(__file__).resolve().parents[1] / 'mycelium_swarm_directory' / 'web' / 'about.html'


def f(v: float) -> str:
    s = ('%.1f' % v).rstrip('0').rstrip('.')
    return '0' if s == '-0' else s


def esc(text: str) -> str:
    return text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


def smooth(points, fmt=None) -> str:
    """Catmull-Rom through the points, as cubic Beziers."""
    f = fmt or globals()['f']
    if len(points) < 3:
        return 'M%s %s' % (f(points[0][0]), f(points[0][1])) + ''.join(
            'L%s %s' % (f(x), f(y)) for x, y in points[1:])
    out = ['M%s %s' % (f(points[0][0]), f(points[0][1]))]
    pts = [points[0]] + list(points) + [points[-1]]
    for i in range(1, len(pts) - 2):
        p0, p1, p2, p3 = pts[i - 1], pts[i], pts[i + 1], pts[i + 2]
        c1 = (p1[0] + (p2[0] - p0[0]) / 6, p1[1] + (p2[1] - p0[1]) / 6)
        c2 = (p2[0] - (p3[0] - p1[0]) / 6, p2[1] - (p3[1] - p1[1]) / 6)
        out.append('C%s %s %s %s %s %s' % tuple(f(v) for v in (*c1, *c2, *p2)))
    return ''.join(out)


def rounded(points, r=18) -> str:
    """A polyline with rounded corners (quadratic joins)."""
    out = ['M%s %s' % (f(points[0][0]), f(points[0][1]))]
    for i in range(1, len(points) - 1):
        (x0, y0), (x1, y1), (x2, y2) = points[i - 1], points[i], points[i + 1]
        d0, d1 = math.hypot(x1 - x0, y1 - y0), math.hypot(x2 - x1, y2 - y1)
        k0, k1 = min(r, d0 / 2) / d0, min(r, d1 / 2) / d1
        out.append('L%s %s' % (f(x1 - (x1 - x0) * k0), f(y1 - (y1 - y0) * k0)))
        out.append('Q%s %s %s %s' % (f(x1), f(y1), f(x1 + (x2 - x1) * k1), f(y1 + (y2 - y1) * k1)))
    out.append('L%s %s' % (f(points[-1][0]), f(points[-1][1])))
    return ''.join(out)


class Art:
    """One SVG: layers drawn back to front."""

    def __init__(self, fid: str, w: int, h: int, seed: int):
        self.fid, self.w, self.h = fid, w, h
        self.rng = random.Random(seed)
        self.glow: list[str] = []      # blurred underlay
        self.hairs: dict = {}
        self.strand: list[str] = []
        self.top: list[str] = []       # devices, badges
        self.text: list[str] = []

    # ---- mycelium ----
    def _noise(self, n, amp, smooth_k=3):
        rng = self.rng
        vals = [rng.uniform(-1, 1) for _ in range(n)]
        for _ in range(smooth_k):
            vals = [(vals[max(0, i - 1)] + 2 * vals[i] + vals[min(n - 1, i + 1)]) / 4 for i in range(n)]
        m = max(1e-6, max(abs(v) for v in vals))
        return [amp * v / m for v in vals]

    def centreline(self, a, b, bow=None, n=None):
        rng = self.rng
        dx, dy = b[0] - a[0], b[1] - a[1]
        length = math.hypot(dx, dy)
        n = n or max(5, int(length / 16))
        nx, ny = -dy / length, dx / length
        bow = rng.uniform(-0.12, 0.12) * length if bow is None else bow
        wander = self._noise(n + 1, min(7.0, length * 0.03))
        pts = []
        for i in range(n + 1):
            t = i / n
            off = bow * 4 * t * (1 - t) + wander[i] * math.sin(math.pi * t)
            pts.append((a[0] + dx * t + nx * off, a[1] + dy * t + ny * off))
        return pts

    def cord(self, a, b, *, fibres=4, spread=3.2, planned=False, bow=None, hairs=True, glow=True, core=True):
        """A rhizomorph: a bundle of staggered fibres along a shared centreline."""
        rng = self.rng
        pts = self.centreline(a, b, bow=bow)
        n = len(pts)
        if glow and not planned:
            self.glow.append('<path class="fx-halo" d="%s"/>' % smooth(pts))
        for k in range(fibres):
            offs = self._noise(n, spread)
            fib = []
            for i, (x, y) in enumerate(pts):
                j0, j1 = max(0, i - 1), min(n - 1, i + 1)
                tx, ty = pts[j1][0] - pts[j0][0], pts[j1][1] - pts[j0][1]
                tl = math.hypot(tx, ty) or 1
                env = 0.35 + 0.65 * math.sin(math.pi * i / (n - 1))
                fib.append((x - ty / tl * offs[i] * env, y + tx / tl * offs[i] * env))
            width = (1.1, 0.85, 0.7, 0.55, 0.45, 0.4)[min(k, 5)] if core else 0.55
            cls = 'fx-plan' if planned else ('fx-core' if k == 0 and core else 'fx-strand')
            self.strand.append('<path class="%s" stroke-width="%s" d="%s"/>' % (cls, f(width), smooth(fib, fmt=lambda v: '%d' % round(v))))
        if hairs:
            self.side_hairs(pts, planned=planned)
        return pts

    def side_hairs(self, pts, *, planned=False, density=1.0):
        rng = self.rng
        n = len(pts)
        i = rng.randint(1, 3)
        while i < n - 2:
            x, y = pts[i]
            tx, ty = pts[i + 1][0] - pts[i - 1][0], pts[i + 1][1] - pts[i - 1][1]
            heading = math.atan2(ty, tx) + rng.choice((-1, 1)) * math.radians(rng.uniform(22, 50))
            if rng.random() < 0.3:
                heading += math.pi  # some run backwards along the cord
            self.hypha((x, y), heading, rng.uniform(8, 26), width=rng.uniform(0.35, 0.6), planned=planned,
                       fork=rng.random() < 0.35)
            i += max(1, int(rng.choice((2, 3, 3, 4, 6, 8)) / density))

    def hypha(self, start, heading, length, *, width=0.5, planned=False, fork=False, curl=0.06):
        """One fine hypha: a single quadratic curve (cheap markup), optionally forked."""
        rng = self.rng
        x, y = start
        bend = rng.uniform(-curl, curl) * 4 + rng.uniform(-0.1, 0.1)
        mid_h = heading + bend / 2
        cx, cy = x + math.cos(mid_h) * length * 0.5, y + math.sin(mid_h) * length * 0.5
        end_h = heading + bend
        ex, ey = cx + math.cos(end_h) * length * 0.5, cy + math.sin(end_h) * length * 0.5
        key = ('fx-plan-hair' if planned else 'fx-hair', 0.6 if width >= 0.48 else 0.4)
        self.hairs.setdefault(key, []).append('M%d %dQ%d %d %d %d' % tuple(round(v) for v in (x, y, cx, cy, ex, ey)))
        if fork:
            self.hypha((cx, cy), end_h + rng.choice((-1, 1)) * math.radians(rng.uniform(25, 40)), length * 0.5,
                       width=width * 0.8, planned=planned)

    def tuft(self, c, rx, ry, *, n=None, planned=False, explorers=0):
        """A cottony colony: soft lobed glow plus short curled felt fibres."""
        rng = self.rng
        cx, cy = c
        if not planned:
            for _ in range(3):
                ox, oy = rng.uniform(-rx * 0.3, rx * 0.3), rng.uniform(-ry * 0.25, ry * 0.25)
                self.glow.append('<ellipse class="fx-mat" cx="%s" cy="%s" rx="%s" ry="%s"/>' % (
                    f(cx + ox), f(cy + oy), f(rx * rng.uniform(0.55, 0.8)), f(ry * rng.uniform(0.55, 0.8))))
        n = n or max(8, int(rx * ry / 26))
        for _ in range(n):
            r = math.sqrt(rng.random()) ** 1.4
            a = rng.uniform(0, 2 * math.pi)
            sx, sy = cx + math.cos(a) * rx * r, cy + math.sin(a) * ry * r
            heading = a + rng.uniform(-1.2, 1.2)
            self.hypha((sx, sy), heading, rng.uniform(4, 11) * (1.2 - r * 0.5),
                       width=rng.uniform(0.3, 0.55), planned=planned, curl=0.35)
        for _ in range(explorers):
            a = rng.uniform(0, 2 * math.pi)
            self.hypha((cx + math.cos(a) * rx * 0.8, cy + math.sin(a) * ry * 0.8), a, rng.uniform(22, 48),
                       width=0.45, planned=planned, fork=True, curl=0.12)

    # ---- glyphs ----
    def device(self, kind, c, *, scale=1.0, piece=None):
        x, y = c
        s = scale
        g = ['<g class="fx-dev" transform="translate(%s %s)%s">' % (f(x), f(y), '' if s == 1 else ' scale(%s)' % f(s))]
        screen = None
        if kind == 'laptop':
            g.append('<rect class="fx-dev-body" x="-27" y="-30" width="54" height="35" rx="3"/>')
            g.append('<path class="fx-dev-body" d="M-35 7H35L31 13H-31Z"/>')
            screen = (-21, -24, 42, 23)
        elif kind == 'desktop':
            g.append('<rect class="fx-dev-body" x="-29" y="-36" width="58" height="40" rx="3"/>')
            g.append('<path class="fx-dev-body" d="M-5 4H5L7 13H-7Z"/>')
            g.append('<path class="fx-dev-line" d="M-15 14H15"/>')
            screen = (-23, -30, 46, 28)
        elif kind == 'phone':
            g.append('<rect class="fx-dev-body" x="-13" y="-28" width="26" height="44" rx="5"/>')
            g.append('<path class="fx-dev-line" d="M-4 11H4"/>')
            screen = (-8.5, -22, 17, 28)
        elif kind == 'server':
            g.append('<rect class="fx-dev-body" x="-19" y="-34" width="38" height="48" rx="3"/>')
            for yy in (-25, -16, -7):
                g.append('<path class="fx-dev-line" d="M-12 %sH6"/>' % yy)
                g.append('<circle class="fx-led" cx="11" cy="%s" r="1.6"/>' % yy)
            screen = None
        if piece:
            # The device's piece of the model: a few glowing layer bars.
            sx, sy, sw, sh = screen or (-12, 0, 24, 10)
            bars = 4 if kind != 'phone' else 3
            bh = sh / (bars * 1.6)
            for i in range(bars):
                g.append('<rect class="fx-p%s" x="%s" y="%s" width="%s" height="%s" rx="1"/>' % (
                    piece, f(sx + 4), f(sy + 3 + i * bh * 1.55), f(sw - 8), f(bh)))
        g.append('</g>')
        self.top.append(''.join(g))

    def chevron(self, c, angle, size=5, cls='fx-flow'):
        x, y = c
        a = math.radians(angle)
        pts = []
        for da in (2.5, 0, -2.5):
            if da == 0:
                pts.append((x + math.cos(a) * size, y + math.sin(a) * size))
            else:
                pts.append((x + math.cos(a + da) * size, y + math.sin(a + da) * size))
        self.top.append('<path class="%s" d="M%s %sL%s %sL%s %s"/>' % (cls, *(f(v) for p in pts for v in p)))

    def check(self, c, r=7):
        x, y = c
        self.top.append('<g class="fx-check"><circle cx="%s" cy="%s" r="%s"/><path d="M%s %sL%s %sL%s %s"/></g>' % (
            f(x), f(y), f(r), f(x - r * 0.45), f(y + r * 0.02), f(x - r * 0.1), f(y + r * 0.38), f(x + r * 0.5),
            f(y - r * 0.38)))

    def label(self, x, y, text, cls='fx-t', anchor='middle', size=None):
        self.text.append('<text class="%s"%s x="%s" y="%s"%s>%s</text>' % (
            cls, '' if anchor == 'start' else ' text-anchor="%s"' % anchor, f(x), f(y),
            '' if size is None else ' font-size="%s"' % f(size), esc(text)))

    def render(self, title, desc, variant):
        tid, did = '%s-%s-title' % (self.fid, variant), '%s-%s-desc' % (self.fid, variant)
        blur = 'fxb-%s-%s' % (self.fid, variant)
        return ''.join([
            '<svg class="fig-art fig-%s" viewBox="0 0 %d %d" width="%d" height="%d" role="img" aria-labelledby="%s" aria-describedby="%s" '
            'focusable="false" data-variant="%s">' % (variant, self.w, self.h, self.w, self.h, tid, did, variant),
            '<title id="%s">%s</title><desc id="%s">%s</desc>' % (tid, esc(title), did, esc(desc)),
            '<defs><filter id="%s" x="-20%%" y="-20%%" width="140%%" height="140%%"><feGaussianBlur stdDeviation="5"/></filter></defs>' % blur,
            '<g class="fx-glow" filter="url(#%s)">%s</g>' % (blur, ''.join(self.glow)),
            '<g class="fx-hairs">%s</g>' % ''.join('<path class="%s" stroke-width="%s" d="%s"/>' % (c, f(w), ''.join(d))
                                                   for (c, w), d in sorted(self.hairs.items())),
            '<g class="fx-strands">%s</g>' % ''.join(self.strand),
            '<g class="fx-top">%s</g>' % ''.join(self.top),
            '<g class="fx-text">%s</g>' % ''.join(self.text),
            '</svg>'])


# ---------------------------------------------------------------- figures

def layer_stack(art, x, y, w, *, bar=9, gap=4, group_gap=12, labels=True, size=15):
    """The model: 12 layers in three pieces separated by dashed cuts."""
    yy = y
    for g in range(3):
        top = yy
        for i in range(4):
            art.top.append('<rect class="fx-p%d" x="%s" y="%s" width="%s" height="%s" rx="2"/>' % (
                g + 1, f(x), f(yy), f(w), f(bar)))
            yy += bar + gap
        if labels:
            art.label(x - 12, (top + yy - gap) / 2 + size * 0.35, str(g + 1), cls='fx-s', anchor='end', size=size)
        if g < 2:
            cut = yy - gap + group_gap / 2
            art.top.append('<path class="fx-cut" d="M%s %sH%s"/>' % (f(x - 8), f(cut), f(x + w + 8)))
            yy += group_gap
    return yy - gap


def fig_model_split():
    title = 'One model, cut into pieces, run across everyday devices'
    desc = ('A large AI model, drawn as a stack of twelve layers, is cut into three pieces. A laptop, a desktop and '
            'a home server each hold one piece and are linked in a line by glowing threads, so a request passes '
            'through all three in turn.')
    out = []
    # wide
    a = Art('fig-split', 840, 290, 11)
    a.label(130, 34, 'One model', size=17)
    bottom = layer_stack(a, 60, 52, 140)
    a.label(130, bottom + 30, 'too big for one device', cls='fx-s', size=15)
    a.cord((222, 145), (300, 145), fibres=2, spread=1.2, hairs=False, glow=False, bow=0)
    a.chevron((302, 145), 0, size=7)
    xs, kinds, names = (400, 580, 760), ('laptop', 'desktop', 'server'), ('Laptop', 'Desktop', 'Home server')
    base = 168
    for i, x in enumerate(xs):
        a.tuft((x, base + 4), 50, 15, explorers=2)
    for i in range(2):
        a.cord((xs[i] + 30, base - 2), (xs[i + 1] - 30, base - 2), fibres=5)
        a.chevron(((xs[i] + xs[i + 1]) / 2 + 4, base - 2 + 0), 0)
    for i, (x, k, nm) in enumerate(zip(xs, kinds, names)):
        a.device(k, (x, 140 if k != 'server' else 146), piece=i + 1)
        a.label(x, 226, nm, size=16)
        a.label(x, 248, 'piece %d' % (i + 1), cls='fx-s', size=15)
    out.append(a.render(title, desc, 'wide'))
    # narrow
    b = Art('fig-split', 360, 410, 12)
    b.label(180, 28, 'One model', size=15)
    bottom = layer_stack(b, 115, 42, 130, bar=8, gap=3, group_gap=10, size=13)
    b.label(180, bottom + 24, 'too big for one device', cls='fx-s', size=13)
    xs = (62, 180, 298)
    base = 312
    for x in xs:
        b.tuft((x, base + 4), 40, 12, explorers=1)
    for i in range(2):
        b.cord((xs[i] + 28, base - 2), (xs[i + 1] - 28, base - 2), fibres=4, spread=2.4)
    for i, (x, k, nm) in enumerate(zip(xs, kinds, names)):
        b.device(k, (x, 286 if k != 'server' else 292), piece=i + 1, scale=0.9)
        b.label(x, 360, nm, size=13)
        b.label(x, 380, 'piece %d' % (i + 1), cls='fx-s', size=13)
    out.append(b.render(title, desc, 'narrow'))
    return out


def swarm(art, nodes, links, *, scale=1.0, explorers=2):
    for (k, x, y) in nodes:
        art.tuft((x, y + 12 * scale), 44 * scale, 14 * scale, explorers=explorers)
    for i, j in links:
        (_, x1, y1), (_, x2, y2) = nodes[i], nodes[j]
        art.cord((x1, y1 + 8 * scale), (x2, y2 + 8 * scale), fibres=4)
    for (k, x, y) in nodes:
        art.device(k, (x, y - 4 * scale), scale=scale)


def fig_swarm():
    title = 'A swarm'
    desc = ('Laptops, desktops, phones and a home server, each sitting on a small glowing colony, linked to one another '
            'by fine threads like mycelium. Every device connects directly to several others.')
    a = Art('fig-swarm', 840, 300, 21)
    nodes = [('laptop', 100, 165), ('desktop', 250, 78), ('phone', 285, 225), ('server', 445, 150),
             ('laptop', 610, 72), ('desktop', 640, 232), ('phone', 770, 140)]
    links = [(0, 1), (0, 2), (1, 2), (1, 3), (2, 3), (3, 4), (3, 5), (4, 5), (4, 6), (5, 6), (1, 4), (2, 5)]
    swarm(a, nodes, links)
    b = Art('fig-swarm', 360, 300, 22)
    nodes = [('laptop', 70, 80), ('desktop', 270, 70), ('phone', 178, 160), ('server', 80, 238), ('laptop', 282, 236)]
    links = [(0, 1), (0, 2), (1, 2), (0, 3), (2, 3), (2, 4), (1, 4), (3, 4)]
    swarm(b, nodes, links, scale=0.85, explorers=1)
    return [a.render(title, desc, 'wide'), b.render(title, desc, 'narrow')]


STAGES = (('1 Trusted swarms', 'Working now'), ('2 Connected communities', 'Designed'),
          ('3 A shared commons', 'Designed'), ('4 Proof instead of trust', 'Research'))


def cluster(art, c, s, *, planned=False):
    """One small swarm: three devices on a shared colony, joined by fine threads."""
    x, y = c
    pts = [(x - 20 * s, y + 4 * s), (x + 19 * s, y - 6 * s), (x + 3 * s, y + 19 * s)]
    art.tuft((x, y + 8 * s), 34 * s, 16 * s, explorers=1)
    for i in range(3):
        art.cord(pts[i], pts[(i + 1) % 3], fibres=2, spread=1.2 * s, glow=False, core=False)
    for p, k in zip(pts, ('laptop', 'desktop', 'phone')):
        art.device(k, (p[0], p[1] - 4 * s), scale=0.36 * s)
    return pts


def stage_art(art, stage, cx, cy, s):
    if stage == 0:
        cluster(art, (cx, cy), 1.6 * s)
        return
    centres = [(cx - 52 * s, cy - 28 * s), (cx + 52 * s, cy - 24 * s), (cx, cy + 46 * s)]
    for c in centres:
        cluster(art, c, 0.85 * s)
    if stage == 1:
        links = [(centres[0], centres[1]), (centres[1], centres[2]), (centres[2], centres[0])]
    else:
        pool = (cx, cy + 2 * s)
        art.top.append('<circle class="fx-pool" cx="%s" cy="%s" r="%s"/>' % (f(pool[0]), f(pool[1]), f(13 * s)))
        links = [(c, pool) for c in centres]
    for p, q in links:
        if stage >= 2:
            dx, dy = q[0] - p[0], q[1] - p[1]
            d = math.hypot(dx, dy)
            q = (q[0] - dx / d * 14 * s, q[1] - dy / d * 14 * s)
        art.cord(p, q, fibres=3, planned=True, spread=2 * s)
        if stage == 3:
            art.check((p[0] + (q[0] - p[0]) * 0.66, p[1] + (q[1] - p[1]) * 0.66), r=7.5 * s)


def fig_stages():
    title = 'Four stages, growing like a network'
    desc = ('Four panels. 1, Trusted swarms, working now: one small group of linked devices, drawn solid. '
            '2, Connected communities, designed: three groups linked to each other. 3, A shared commons, designed: the '
            'groups linked to a shared pool. 4, Proof instead of trust, research: the same network with a check on every '
            'link. Stages 2 to 4 are drawn dashed because they are not built yet.')
    a = Art('fig-stages', 840, 270, 31)
    for i, (name, status) in enumerate(STAGES):
        cx = 105 + i * 210
        stage_art(a, i, cx, 100, 1.0)
        a.label(cx, 208, name, size=15)
        a.label(cx, 232, status, cls='fx-s' if i else 'fx-a', size=14.5)
        if i < 3:
            a.chevron((cx + 105, 100), 0, size=6, cls='fx-step')
    b = Art('fig-stages', 360, 450, 32)
    for i, (name, status) in enumerate(STAGES):
        cx, cy = 90 + (i % 2) * 180, 82 + (i // 2) * 222
        stage_art(b, i, cx, cy, 0.78)
        b.label(cx, cy + 92, name if len(name) < 20 else name, size=13)
        b.label(cx, cy + 112, status, cls='fx-s' if i else 'fx-a', size=13)
    return [a.render(title, desc, 'wide'), b.render(title, desc, 'narrow')]


def fig_pipeline():
    title = 'How a request flows through a three-stage pipeline'
    desc = ('You send a prompt from your app to the entry device, which runs stage 1 (layers 1 to 12). It passes '
            'activations over an encrypted QUIC link to device B, which runs stage 2 (layers 13 to 24), and on to '
            'device C, which runs stage 3 (layers 25 to 36). The answer streams back to you word by word.')
    a = Art('fig-pipeline', 840, 232, 41)
    xs = (70, 300, 530, 760)
    kinds = ('laptop', 'desktop', 'laptop', 'server')
    names = ('You', 'Entry device', 'Device B', 'Device C')
    subs = ('your app', 'stage 1 · layers 1–12', 'stage 2 · layers 13–24', 'stage 3 · layers 25–36')
    y = 128
    for i, x in enumerate(xs):
        a.tuft((x, y + 22), 46 if i else 36, 13, explorers=1 if i else 0)
    gaps = ((112, 262), (342, 490), (572, 730))
    for (x0, x1), lab in zip(gaps, ('prompt', 'activations', 'activations')):
        a.cord((x0, y + 4), (x1, y + 4), fibres=5, bow=0)
        a.chevron((x1 + 4, y + 4), 0, size=6)
        mid = (x0 + x1) / 2
        a.label(mid, y - 30, lab, size=15)
        if lab == 'activations':
            a.label(mid, y - 12, 'encrypted QUIC', cls='fx-s', size=14)
    # Return path: under the labels, from the last stage back to you.
    # Return path: above the wire labels, from the last stage back to you.
    ret = [(760, 88), (760, 50), (70, 50), (70, 84)]
    a.glow.append('<path class="fx-halo" d="%s"/>' % rounded(ret, 22))
    a.strand.append('<path class="fx-core" stroke-width="1.2" d="%s"/>' % rounded(ret, 22))
    a.strand.append('<path class="fx-strand" stroke-width="0.6" d="%s"/>' % rounded([(x - 2, y - 2.5) for x, y in ret], 20))
    a.side_hairs(a.centreline((738, 50), (92, 50), bow=0), density=0.5)
    a.chevron((70, 87), 90, size=6)
    a.label(415, 30, 'answer, streamed back word by word', size=15)
    for i, (x, k, nm, sb) in enumerate(zip(xs, kinds, names, subs)):
        a.device(k, (x, y if k != 'server' else y + 6), piece=i if i else None)
        a.label(x, y + 58, nm, size=15.5)
        a.label(x, y + 78, sb, cls='fx-s', size=14)
    # narrow: a column, labels on the right, the return path up the right edge.
    b = Art('fig-pipeline', 360, 600, 42)
    ys = (84, 220, 356, 492)
    x = 70
    for i, yy in enumerate(ys):
        b.tuft((x, yy + 20), 36, 11, explorers=0)
    for (y0, y1), lab in zip(((ys[0] + 34, ys[1] - 44), (ys[1] + 34, ys[2] - 44), (ys[2] + 34, ys[3] - 46)),
                             ('prompt', 'activations', 'activations')):
        b.cord((x, y0), (x, y1), fibres=4, bow=0, spread=2.4)
        b.chevron((x, y1 + 4), 90, size=6)
        b.label(x + 32, (y0 + y1) / 2 + (0 if lab == 'activations' else 5), lab, anchor='start', size=14)
        if lab == 'activations':
            b.label(x + 32, (y0 + y1) / 2 + 18, 'encrypted QUIC', cls='fx-s', anchor='start', size=13)
    ret = [(x, ys[3] + 34), (x, ys[3] + 62), (328, ys[3] + 62), (328, ys[0] - 46), (x, ys[0] - 46), (x, ys[0] - 36)]
    b.glow.append('<path class="fx-halo" d="%s"/>' % rounded(ret, 22))
    b.strand.append('<path class="fx-core" stroke-width="1.1" d="%s"/>' % rounded(ret, 22))
    b.strand.append('<path class="fx-strand" stroke-width="0.55" d="%s"/>' % rounded([(px + 2.5, py + 2) for px, py in ret], 20))
    b.chevron((x, ys[0] - 34), 90, size=6)
    b.label(186, ys[3] + 88, 'answer, streamed back word by word', size=13)
    for i, (yy, k, nm, sb) in enumerate(zip(ys, kinds, names, subs)):
        b.device(k, (x, yy if k != 'server' else yy + 6), piece=i if i else None, scale=0.9)
        b.label(x + 52, yy - 6, nm, anchor='start', size=14)
        b.label(x + 52, yy + 13, sb, cls='fx-s', anchor='start', size=13)
    return [a.render(title, desc, 'wide'), b.render(title, desc, 'narrow')]


LAYERS = (('Spora', 'applications', 'Planned'), ('Mycorrhiza', 'coordination', 'Designed'),
          ('Mycelium', 'compute', 'Built: early preview'))


def mushroom(art, x, base, h, w):
    top = base - h
    cap = 'M%s %sC%s %s %s %s %s %sQ%s %s %s %sZ' % (
        f(x - w / 2), f(top), f(x - w / 2), f(top - w * 0.62), f(x + w / 2), f(top - w * 0.62), f(x + w / 2), f(top),
        f(x), f(top - w * 0.12), f(x - w / 2), f(top))
    art.top.append('<path class="fx-plan-shape" d="%s"/>' % cap)
    art.top.append('<path class="fx-plan-shape" d="M%s %sQ%s %s %s %sH%sQ%s %s %s %s"/>' % (
        f(x - 3.5), f(top - w * 0.09), f(x - 3), f(base - 4), f(x - 6), f(base), f(x + 6), f(x + 3), f(base - 4),
        f(x + 3.5), f(top - w * 0.09)))


def stack_art(art, band, x0, x1, y0, y1, s=1.0):
    w = x1 - x0
    mid = (y0 + y1) / 2
    if band == 0:  # applications: fruiting bodies, not built
        for i, (fx, h, cw) in enumerate(((0.15, 26, 34), (0.4, 36, 44), (0.65, 22, 30), (0.88, 30, 38))):
            mushroom(art, x0 + w * fx, y1 - 4, h * s, cw * s)
        art.top.append('<path class="fx-plan-shape" d="M%s %sh%sv%sh-%sl-%s %sz"/>' % (
            f(x0 + w * 0.27 - 14 * s), f(mid - 16 * s), f(28 * s), f(18 * s), f(18 * s), f(8 * s), f(7 * s)))
        art.label(x0 + w * 0.53, mid + 2 * s, '</>', cls='fx-s', size=15 * s)
    elif band == 1:  # coordination: groups linked across trust boundaries
        left, right = (x0 + w * 0.12, mid), (x0 + w * 0.88, mid)
        for c in (left, right):
            cluster(art, c, 0.8 * s)
        pool = (x0 + w * 0.5, mid)
        art.top.append('<circle class="fx-pool" cx="%s" cy="%s" r="%s"/>' % (f(pool[0]), f(pool[1]), f(12 * s)))
        art.cord((left[0] + 30 * s, mid), (pool[0] - 14 * s, mid), fibres=3, planned=True, bow=-10 * s)
        art.cord((pool[0] + 14 * s, mid), (right[0] - 30 * s, mid), fibres=3, planned=True, bow=10 * s)
        art.check((x0 + w * 0.31, mid - 4 * s), r=7 * s)
        art.check((x0 + w * 0.69, mid + 4 * s), r=7 * s)
    else:  # compute: devices on living threads, built
        kinds = ('laptop', 'desktop', 'phone', 'server')
        xs = [x0 + w * t for t in (0.12, 0.38, 0.62, 0.88)]
        yy = mid + 4 * s
        for x in xs:
            art.tuft((x, yy + 16 * s), 38 * s, 10 * s, explorers=1)
        for i in range(3):
            art.cord((xs[i] + 26 * s, yy + 12 * s), (xs[i + 1] - 26 * s, yy + 12 * s), fibres=4, spread=2.4 * s)
        for i, (x, k) in enumerate(zip(xs, kinds)):
            art.device(k, (x, yy - 2 * s), scale=0.78 * s)


def fig_stack():
    title = 'The three layers of CCAI'
    desc = ('Three layers, from the ground up. At the bottom, Mycelium, the compute layer, built as an early preview: '
            'devices on glowing threads. In the middle, Mycorrhiza, the coordination layer, designed: groups linked '
            'across trust boundaries through a shared pool, with checks. On top, Spora, the application layer, planned: '
            'apps growing like mushrooms, drawn as dashed outlines.')
    a = Art('fig-stack', 840, 330, 51)
    bands = ((10, 110), (110, 220), (220, 325))
    for i, ((y0, y1), (name, role, status)) in enumerate(zip(bands, LAYERS)):
        if i:
            a.top.append('<path class="fx-rule" d="M16 %sH824"/>' % y0)
        a.label(28, y0 + 42, name, anchor='start', size=17)
        a.label(28, y0 + 64, role, cls='fx-s', anchor='start', size=14.5)
        a.label(28, y0 + 86, status, cls='fx-a' if i == 2 else 'fx-s', anchor='start', size=14.5)
        stack_art(a, i, 250, 820, y0 + 6, y1 - 6)
    b = Art('fig-stack', 360, 560, 52)
    bands = ((0, 180), (180, 370), (370, 560))
    for i, ((y0, y1), (name, role, status)) in enumerate(zip(bands, LAYERS)):
        if i:
            b.top.append('<path class="fx-rule" d="M8 %sH352"/>' % y0)
        b.label(16, y0 + 30, name, anchor='start', size=15)
        b.label(16, y0 + 50, '%s · %s' % (role, status), cls='fx-a' if i == 2 else 'fx-s', anchor='start', size=13)
        stack_art(b, i, 16, 344, y0 + 62, y1 - 8, s=0.85)
    return [a.render(title, desc, 'wide'), b.render(title, desc, 'narrow')]


FIGURES = {
    'model-split': (fig_model_split, 'Each device holds one piece of the model and passes its result on to the next.'),
    'swarm': (fig_swarm, 'A swarm: devices linked directly to one another, like threads of mycelium.'),
    'stages': (fig_stages, 'Each stage grows out of the one before. Only the first is working today.'),
    'pipeline': (fig_pipeline, 'A three-stage pipeline. Only activations cross between the devices; the layer ranges '
                               'are an example, sized by the planner to each device\'s memory.'),
    'stack': (fig_stack, 'Three layers, from the devices up. Only the compute layer is built today.'),
}


def figure_html(name):
    make, caption = FIGURES[name]
    wide, narrow = make()
    return ('<figure class="vision-fig" data-fig="%s"><div class="fig-frame">%s%s</div>'
            '<figcaption>%s</figcaption></figure>' % (name, wide, narrow, esc(caption)))


def render(html: str) -> str:
    for name in FIGURES:
        pat = re.compile(r'(<!-- fig:%s -->).*?(<!-- /fig:%s -->)' % (name, name), re.S)
        if not pat.search(html):
            raise SystemExit('marker missing for %s' % name)
        html = pat.sub(lambda m: m.group(1) + figure_html(name) + m.group(2), html)
    return html


def main(argv):
    html = ABOUT.read_text()
    new = render(html)
    if '--check' in argv:
        if new != html:
            print('about.html figures are stale: run scripts/generate_about_figures.py')
            return 1
        return 0
    ABOUT.write_text(new)
    print('wrote', ABOUT, len(new), 'bytes')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
