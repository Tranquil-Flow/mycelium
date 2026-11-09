// SPDX-License-Identifier: AGPL-3.0-only
/** R10 page compositor and below-fold field. One page-space mycelium: the hero
 * graph and this field share vertices, edges and pulse routes. The field uses
 * the sparse (ref-8) vocabulary: long fine hyphae holding their heading along a
 * low-frequency flow away from the hero, the margin colonies and off-screen
 * sources, branching acutely into 2nd/3rd orders that run alongside the parent
 * and occasionally fuse into elongated cells. Density falls with distance from
 * sources and towards text. Layers (r11 v2): full-DPR horizontal band tiles of
 * the static field and a hero-box static composite, both moved by compositor
 * transforms on scroll (never re-rastered on scroll), and a viewport-sized live
 * layer (fronts, pulses, blooms).
 */
import { pacer, rng, gauss, hash01, smooth, persistWalk, measurePaths, auditPaths, sceneClock, tipCycle, lifeTiming, INTRO_MS as SHARED_INTRO_MS, INTRO_SCALE } from "./mycelium-geometry.js";

function contentBounds(offset = 0) {
  const main = document.querySelector("main"),
    box = main.getBoundingClientRect(),
    style = getComputedStyle(main);
  let left = box.left + parseFloat(style.paddingLeft || 0),
    right = box.right - parseFloat(style.paddingRight || 0);
  const rects = [...main.querySelectorAll(".card,.sect,.panel,.hero-copy")].map((e) => e.getBoundingClientRect()).filter((r) => r.width && r.height);
  for (const r of rects) {
    left = Math.min(left, r.left);
    right = Math.max(right, r.right);
  }
  // r11.2b (owner: "for the mobile, doesn't seem to show much background
  // mycelium when you scroll down"): below 1100 px there are no side margins;
  // compact viewports get small edge colonies instead (see extendFieldGraph).
  return { left, right, offset, rects: rects.length, enabled: innerWidth >= 1100 && Math.min(left, innerWidth - right) >= 60, compact: innerWidth < 1100 };
}

/** Hyphal knots (r11; refs/hyphal-knots, recipe in design/r11-coordination.md):
 * tiny tangled balls ON strands and at junctions, in small irregular groups,
 * rarer in the sparse field than near the margin colonies. Each knot is a few
 * overlapping soft lobes (never a circle) with short fibres wound tangentially
 * into it; radius at DPR1 is heavy-tailed, r = 1 + 2u^2.2 (diameter 2-6 px). */
/** Bead test for one knot group: order members along their principal axis;
 * consecutive spacing CV <= 0.4, or any 3 members collinear at near-equal
 * spacing, reads as a string of beads. */
export function beadLike(members) {
  if (members.length < 3) return false;
  const mx = members.reduce((n, m) => n + m.x, 0) / members.length,
    my = members.reduce((n, m) => n + m.y, 0) / members.length;
  let sxx = 0, sxy = 0, syy = 0;
  for (const m of members) {
    sxx += (m.x - mx) ** 2;
    sxy += (m.x - mx) * (m.y - my);
    syy += (m.y - my) ** 2;
  }
  const th = 0.5 * Math.atan2(2 * sxy, sxx - syy),
    ord = [...members].sort((a, b) => (a.x - b.x) * Math.cos(th) + (a.y - b.y) * Math.sin(th));
  const gaps = ord.slice(1).map((m, k) => Math.hypot(m.x - ord[k].x, m.y - ord[k].y)),
    mean = gaps.reduce((n, v) => n + v, 0) / gaps.length,
    cv = Math.sqrt(gaps.reduce((n, v) => n + (v - mean) ** 2, 0) / gaps.length) / Math.max(1e-6, mean);
  if (cv <= 0.4) return true;
  for (let i = 0; i < ord.length; i++)
    for (let j = i + 1; j < ord.length; j++)
      for (let k = j + 1; k < ord.length; k++) {
        const [a, b, c] = [ord[i], ord[j], ord[k]],
          span = Math.hypot(c.x - a.x, c.y - a.y),
          off = Math.abs((c.x - a.x) * (a.y - b.y) - (a.x - b.x) * (c.y - a.y)) / Math.max(1e-6, span),
          g1 = Math.hypot(b.x - a.x, b.y - a.y),
          g2 = Math.hypot(c.x - b.x, c.y - b.y);
        if (off < 0.2 * span && Math.max(g1, g2) / Math.max(1e-6, Math.min(g1, g2)) < 1.4) return true;
      }
  return false;
}

export function placeKnots(opts) {
  for (const r = placeKnotsGen(opts); ; ) {
    const n = r.next();
    if (n.done) return n.value;
  }
}
/** r11.3: placeKnots with time-bounded yields (the field build's last step
 * was one 30-50 ms task at 1440/1920); same RNG order, same knots. */
function* placeKnotsGen({ strands, junctions, marginKnots, layout, w, top, height, budget = () => false }) {
  const R = rng("r11-hyphal-knots"),
    knots = [];
  const nearColony = (q) => marginKnots.reduce((m, k) => Math.min(m, Math.hypot(q.x - k.x, q.y - k.y)), Infinity);
  // r11 v2: a group is never a row of beads. Members differ in size (one
  // dominant ball, smaller satellites) and lobe shape, sit at alternately short
  // and long gaps (ratio >= 2.5) and often on a lateral leaving the host, and a
  // group that still measures as even or collinear is trimmed.
  const make = (p, i, group, r) => {
    const a = p.points[Math.max(0, i - 1)],
      b = p.points[Math.min(p.points.length - 1, i + 1)],
      q = p.points[i],
      lobes = [],
      fibres = [];
    for (let j = 0, n = 2 + Math.floor(R() * 5); j < n; j++) {
      const o = R() * 6.283,
        d = r * (0.15 + R() * 0.55);
      lobes.push([Math.cos(o) * d, Math.sin(o) * d, r * (0.4 + R() * 0.6)]);
    }
    for (let j = 0, n = 3 + Math.floor(R() * 5); j < n; j++) fibres.push([R() * 6.283, (1.5 + R() * 2) * (R() < 0.5 ? -1 : 1), 0.6 + R() * 0.8, 0.8 + R() * 0.5]);
    return { x: q.x, y: q.y, r, el: 1 + R() * 0.9, ang: Math.atan2(b.y - a.y, b.x - a.x) + gauss(R) * 0.35, lobes, fibres, p, i, group, bright: 0.55 + R() * 0.35 };
  };
  const arcs = new Map();
  const arcOf = (p) => {
    let a = arcs.get(p);
    if (!a) {
      a = [0];
      for (let i = 1; i < p.points.length; i++) a.push(a[i - 1] + Math.hypot(p.points[i].x - p.points[i - 1].x, p.points[i].y - p.points[i - 1].y));
      arcs.set(p, a);
    }
    return a;
  };
  const at = (p, s) => {
    const a = arcOf(p);
    let i = 0;
    while (i < a.length - 1 && a[i] < s) i++;
    return i;
  };
  const kids = new Map();
  for (const p of strands) if (p.static && p.introParent) (kids.get(p.introParent) || kids.set(p.introParent, []).get(p.introParent)).push(p);
  let groups = 0;
  const area = w * height,
    sparseTarget = area / 120000,
    colonyTarget = marginKnots.length * 2.4;
  const tryGroup = (p, i) => {
    const q = p.points[i];
    if (!q || q.y < top || q.y > top + height) return false;
    const n = 1 + Math.floor(Math.pow(R(), 1.8) * 4),
      g = groups++,
      lead = 1.3 + 1.7 * Math.pow(R(), 1.6),
      members = [make(p, i, g, lead)];
    let host = p,
      s = arcOf(p)[i],
      dir = R() < 0.5 ? -1 : 1,
      short = R() < 0.5;
    for (let k = 1; k < n; k++) {
      const gap = short ? 3 + R() * 3 : 15 + R() * 13;
      short = !short;
      // Often step onto a lateral leaving near the current member (off the host's line).
      const lat = host === p ? (kids.get(p) || []).filter((c) => Math.abs((c.introAt ?? -99) - s) < 20 && arcOf(c).at(-1) > gap + 2) : [];
      if (lat.length && R() < 0.6) {
        host = lat[Math.floor(R() * lat.length)];
        s = gap;
        dir = 1;
      } else s += dir * gap;
      const a = arcOf(host);
      if (s <= 1 || s >= a.at(-1) - 1) break;
      const j = at(host, s);
      if (j <= 0 || j >= host.points.length - 1) break;
      members.push(make(host, j, g, Math.max(1, lead * (0.4 + R() * 0.45))));
    }
    while (members.length > 2 && beadLike(members)) members.pop();
    for (const m of members) {
      knots.push(m);
      (m.p.knots ||= []).push(m);
    }
    return true;
  };
  // Near colonies: on colony hyphae and their junctions.
  const colonyCands = junctions.filter((c) => nearColony(c.p.points[c.i]) < 140);
  for (let k = 0; k < colonyTarget && colonyCands.length; k++) {
    const c = colonyCands[Math.floor(R() * colonyCands.length)];
    tryGroup(c.p, c.i);
    if ((k & 15) === 15 && budget()) yield "knots";
  }
  if (budget()) yield "knots";
  // Sparse field: mostly at junctions, sometimes mid-strand.
  const mains = strands.filter((p) => p.static && !p.hair && p.points.length > 12);
  for (let k = 0, made = 0; made < sparseTarget && k < sparseTarget * 6; k++) {
    if (R() < 0.7 && junctions.length) {
      const c = junctions[Math.floor(R() * junctions.length)];
      if (nearColony(c.p.points[c.i]) < 140) continue;
      made += tryGroup(c.p, c.i);
    } else if (mains.length) {
      const p = mains[Math.floor(R() * mains.length)];
      made += tryGroup(p, 2 + Math.floor(R() * (p.points.length - 4)));
    }
    if ((k & 15) === 15 && budget()) yield "knots";
  }
  return knots;
}

/** r11.1 cottony core: every strand that reaches a margin colony vertex other
 * than the mat's own tufts (the cords, the long hyphae and runners leaving the
 * mat, their laterals, field threads crossing it) fades out inside the mat,
 * from full strength FADE_R1 px from the vertex to zero at FADE_R0, so nothing
 * converges on a bright point. Drawing only: graph, routes and RNG unchanged. */
const MAT_R = 24, // r11.2 dense felt core radius of a margin colony (CSS px)
  MAT_FELT = 260;
const FADE_R0 = 5, // r11.2b: 5 / 20 (r11.2: 4 / 18 left the junction 2-3% over the mat peak; r11.1: 11 / 32 left a visible gap)
  FADE_R1 = 20;

/** Generator: lays the field into the shared graph; returns the field record. */
export function* extendFieldGraph({ vs, ss, paths, vertex, edge, nodes = [], w, h, top: pageTop = 0, budget = () => false, seedOk = () => true }) {
  const R = rng("r10-sparse-field"),
    R2 = rng("r11-field-runners"),
    W = document.documentElement.clientWidth || innerWidth,
    height = Math.max(1800, Math.round(innerHeight * 2.4)),
    top = h - 150,
    layout = contentBounds(0),
    left = 0;
  w = W;
  // r11.2b compact (< 1100 px): edge colonies partly off-screen, scaled to the
  // width (MK), and a denser sparse field (1.35x) so every scroll position of
  // a phone carries visible mycelium. Wider viewports are byte-identical.
  // Home page only: app pages keep their sparse r10 column (a compact colony
  // under the /swarms/ footer dropped its contrast to 3.6:1).
  const compact = !layout.enabled && layout.compact && document.body.classList.contains("apex"),
    MK = compact ? Math.max(0.5, Math.min(0.8, w / 960)) : 1;
  const baseCount = Math.round((w * height) / 26000),
    count = Math.round(baseCount * (compact ? 1.35 : 1.2));
  const strands = [],
    fusions = [],
    grid = new Map(),
    GC = 8;
  const gk = (x, y) => ((Math.floor(y / GC) + 8192) << 14) ^ (Math.floor(x / GC) + 8192);
  const index = (q, p) => {
    const k = gk(q.x, q.y);
    let l = grid.get(k);
    if (!l) grid.set(k, (l = []));
    l.push({ q, p });
  };
  // Column weight: dense in wide side margins, sparse and faint under content.
  const marginWeight = (x) => {
    if (!layout.enabled) return 0;
    const dd = x < layout.left ? layout.left - x : x > layout.right ? x - layout.right : 0,
      q = Math.max(0, Math.min(1, dd / 72));
    return q * q * (3 - 2 * q);
  };
  const add = (points, o = {}) => {
    const poly = [points[0].edges ? points[0] : vertex(points[0].x, points[0].y, 0)],
      segments = [];
    let a = poly[0];
    for (let i = 1; i < points.length; i++) {
      const q = points[i],
        b = q.edges ? q : vertex(q.x, q.y, a.source),
        s = edge(a, b, o.width ?? 0.45, o.alpha ?? 0.5, 1, -1, o.birth ? o.birth + (i - 1) * 0.5 : 0);
      if (!s) break;
      s.field = true;
      s.explorer = true;
      segments.push(s);
      poly.push(b);
      a = b;
    }
    const p = { points: poly, segments, width: o.width ?? 0.45, alpha: o.alpha ?? 0.5, birth: o.birth ?? 0, phase: o.phase ?? R() * 160, period: o.period ?? 110 + R() * 90, static: !o.birth, margin: !!o.margin, hair: !!o.hair, order: o.order ?? 0, taper: o.taper ?? 0.55 };
    for (let i = 0; i < segments.length; i++) {
      segments[i].fieldPath = p;
      segments[i].fieldIndex = i;
    }
    strands.push(p);
    paths.push({ kind: "field", points: poly });
    for (const q of poly) index(q, p);
    return p;
  };
  // Low-frequency flow (600-900 px): away from the hero and from off-screen sources.
  const virtual = [
    { x: -220, y: top + height * (0.2 + R() * 0.2) },
    { x: w + 220, y: top + height * (0.45 + R() * 0.2) },
    { x: -220 + (R() < 0.5 ? 0 : w + 440), y: top + height * (0.75 + R() * 0.15) },
  ];
  const ph = [R() * 6.28, R() * 6.28, R() * 6.28, R() * 6.28];
  const flow = (x, y) => {
    let fx = 0,
      fy = 0.55;
    for (const s of virtual) {
      const dx = x - s.x,
        dy = y - s.y,
        dd = Math.hypot(dx, dy) || 1,
        k = 1 / (1 + dd / 650);
      fx += (dx / dd) * k;
      fy += (dy / dd) * k;
    }
    const n = 0.55 * Math.sin(x / 740 + ph[0] + Math.sin(y / 910 + ph[1])) + 0.35 * Math.sin(y / 640 + ph[2] + x / 1300);
    const a = Math.atan2(fy, fx) + n;
    return a;
  };
  const pullFlow = (x, y, mean) => {
    const f = flow(x, y);
    return Math.atan2(Math.sin(f - mean), Math.cos(f - mean)) * 0.01;
  };
  const sparseWalk = { k: 0.013, run: 60, jit: 0.038, pull: pullFlow, vary: true };
  // Sources: hero continuations, virtual edge sources, margin colonies.
  const seeds = [];
  const heroBand = nodes.length ? vs.filter((v) => v.y > top + 6 && v.y < h + 200 && v.edges.length > 1 && seedOk(v)) : [];
  for (let i = 0; i < Math.round(count * 0.3) && heroBand.length; i++) seeds.push({ v: heroBand[Math.floor(R() * heroBand.length)], hero: true });
  for (let i = 0; seeds.length < count && i < count * 6; i++) {
    const s = virtual[Math.floor(R() * virtual.length)],
      x = R() < 0.35 ? (R() < 0.5 ? R() * Math.max(80, layout.left) : w - R() * Math.max(80, w - layout.right)) : R() * w,
      y = top + 40 + R() * (height - 80),
      dd = Math.hypot(x - s.x, y - s.y);
    // Density by distance from sources and columns: margins carry more.
    if (R() > (0.62 + 0.38 * marginWeight(x)) * (0.55 + 0.45 / (1 + dd / 900))) continue;
    seeds.push({ x, y });
  }
  const mainRoots = [];
  for (const seed of seeds) {
    const start = seed.v ?? vertex(seed.x, seed.y, 0),
      heading = flow(start.x, start.y) + gauss(R) * 0.2,
      len = 220 + R() * 330,
      pts = persistWalk(start, heading, len, R, sparseWalk);
    // Fuse into elongated net cells: a tip meeting an earlier strand joins it.
    let hit = null;
    for (let j = 24; j < pts.length; j++) {
      const q = pts[j],
        near = grid.get(gk(q.x, q.y)) || [];
      const t = near.find((c) => Math.hypot(c.q.x - q.x, c.q.y - q.y) < 3.2);
      if (t && R() < 0.35) {
        pts.length = j;
        pts.push(t.q);
        hit = t.q;
        break;
      }
    }
    const alpha = 0.85; // column weight is applied per chunk when drawn
    const p = add(pts, { width: 0.55, alpha });
    p.heroFamily = !!seed.hero;
    p.introStart = seed.hero ? 0 : R2() * 120; // own field sources start at once (r11 intro)
    if (hit) fusions.push({ point: hit, parent: p, birth: 0 });
    mainRoots.push(p);
    // Acute forks running alongside (2nd order), sometimes a 3rd.
    let s = 40 + -Math.log(1 - R()) * 70;
    while (s < len - 30) {
      const i = Math.min(p.points.length - 2, Math.round(s / 2.5)),
        a = p.points[i],
        b = p.points[i + 1],
        hh = Math.atan2(b.y - a.y, b.x - a.x),
        side = R() < 0.5 ? -1 : 1,
        cl = (len - s) * (0.35 + R() * 0.45);
      const c = persistWalk(b, hh + side * (0.26 + R() * 0.26), cl, R, { ...sparseWalk, pull: (x, y, m) => Math.atan2(Math.sin(hh - m), Math.cos(hh - m)) * 0.018 });
      const child = add(c, { width: 0.4, alpha: alpha * 0.8, order: 0 });
      child.introParent = p;
      child.introAt = s;
      mainRoots.push(child);
      s += 50 + -Math.log(1 - R()) * 80;
    }
    budget() && (yield "field");
  }
  // Living branches: tips that grow, hold and dim in place (never retract).
  const live = [];
  for (const parent of mainRoots.filter(() => R() < 0.3)) {
    const at = Math.floor(parent.points.length * (0.4 + R() * 0.4)),
      a = parent.points[Math.max(0, at - 1)],
      b = parent.points[at];
    if (!b) continue;
    const hh = Math.atan2(b.y - a.y, b.x - a.x) + (R() < 0.5 ? -1 : 1) * (0.3 + R() * 0.25);
    const branch = add(persistWalk(b, hh, 60 + R() * 90, R, sparseWalk), { width: 0.4, alpha: parent.alpha * 0.9, birth: 1 + R() * 3 });
    branch.introParent = parent;
    live.push(branch);
    // A live tip meeting another strand fuses there (seamless, no bead).
    const end = branch.points.at(-1),
      near = (grid.get(gk(end.x, end.y)) || []).find((c) => c.p !== branch && c.p !== parent);
    if (near) fusions.push({ point: end, parent: branch, birth: branch.birth + branch.points.length * 0.5 });
    budget() && (yield "field-live");
  }
  if (!fusions.length && live.length) fusions.push({ point: live[0].points.at(-1), parent: live[0], birth: live[0].birth });
  yield "field-live";

  // Minor margin colonies: small asymmetric tufts dissolving into ref-8 hyphae.
  const marginRoutes = [],
    mats = [],
    marginKnots = [],
    marginDensityRatio = 0.4;
  const join = (a, b, width = 0.9) => {
    const raw = persistWalk(a, Math.atan2(b.y - a.y, b.x - a.x), Math.hypot(b.x - a.x, b.y - a.y), R, { k: 0.016, run: 60, jit: 0.025 }),
      last = raw.at(-1),
      pts = raw.map((q, i) => ({ x: q.x + ((b.x - last.x) * i) / (raw.length - 1), y: q.y + ((b.y - last.y) * i) / (raw.length - 1) }));
    pts[0] = a;
    pts[pts.length - 1] = b;
    const p = add(pts, { width, alpha: 0.95, margin: true });
    p.introStart = 0;
    // r11.1 cottony core: the cord dissolves into each colony mat it joins (no
    // bright point where two cords converge on the colony vertex). Drawing
    // only; the graph and the pulse route are unchanged.
    p.endFade = [a, b].filter((v) => marginKnots.includes(v));
    return p;
  };
  if (layout.enabled || compact)
    for (const side of [-1, 1]) {
      const px = compact ? (side < 0 ? 3 : w - 3) : side < 0 ? layout.left * 0.45 : layout.right + (w - layout.right) * 0.55,
        knots = [];
      // Compact: the two sides alternate (offset by half a spacing).
      for (let y = top + 120 + R() * 120 + (compact && side > 0 ? 260 : 0); y < top + height - 60; y += compact ? 420 + R() * 280 : 380 + R() * 360) {
        const centre = vertex(px + gauss(R) * 14, y + gauss(R) * 30, 0);
        knots.push(centre);
        marginKnots.push(centre);
        // r11 v2 cottony mat (refs; owner: no neuron/starburst): no point centre.
        // A broad lobed core of short curled felt hyphae whose origins are spread
        // over the whole mat, short forward-pointing fuzz on the fan side, and
        // long hyphae that leave through the rim after a bent inner course.
        const fan = Math.PI / 2 + gauss(R) * 0.4 + (R() < 0.5 ? Math.PI : 0),
          lobes = [];
        for (let j = 0, n = 2 + Math.floor(R() * 3); j < n; j++) {
          const along = (R() - 0.25) * 20 * MK,
            across = gauss(R) * 9 * MK;
          lobes.push({ x: centre.x + Math.cos(fan) * along - Math.sin(fan) * across, y: centre.y + Math.sin(fan) * along + Math.cos(fan) * across, r: (9 + R() * 9) * MK });
        }
        const inMat = (front = 0) => {
          for (;;) {
            const l = lobes[Math.floor(R() * lobes.length)],
              o = R() * 6.283,
              rr = l.r * Math.sqrt(R()),
              q = { x: l.x + Math.cos(o) * rr, y: l.y + Math.sin(o) * rr };
            // front > 0 biases origins to the leading half of the mat
            if (front <= 0 || (q.x - centre.x) * Math.cos(fan) + (q.y - centre.y) * Math.sin(fan) > -4 || R() > front) return q;
          }
        };
        const curl = (o, h, len, turn) => {
          const pts = [o],
            n = Math.max(3, Math.ceil(len / 1.8));
          let a = h,
            t = gauss(R) * turn;
          for (let k = 0; k < n; k++) {
            t = t * 0.8 + gauss(R) * turn * 0.5;
            a += t;
            const q = pts.at(-1);
            pts.push({ x: q.x + (Math.cos(a) * len) / n, y: q.y + (Math.sin(a) * len) / n });
          }
          return pts;
        };
        const tuft = (pts, width, alpha) => {
          const p = add(pts, { width, alpha, margin: true });
          p.hair = true;
          p.tuft = true;
          p.marginRoot = centre;
          p.introStart = R2() * 30;
          return p;
        };
        // Felt: the soft core (random headings, tightly curled, short).
        // r11.3: time-bounded yields inside a colony (one colony was a 78 ms task).
        for (let j = 0; j < Math.round(56 * MK); j++) {
          tuft(curl(inMat(), R() * 6.283, (6 + R() * 12) * MK, 0.32), 0.22 + R() * 0.16, 0.62);
          if ((j & 7) === 7 && budget()) yield "margin";
        }
        // Forward fuzz: near-parallel along the fan, gently curving, heavy-tailed lengths.
        for (let j = 0; j < Math.round(36 * MK); j++) {
          tuft(curl(inMat(0.7), fan + gauss(R) * 0.45, (8 + 34 * Math.pow(R(), 1.6)) * MK, 0.07), 0.22 + R() * 0.14, 0.6);
          if ((j & 7) === 7 && budget()) yield "margin";
        }
        // A short rear/side fringe.
        for (let j = 0; j < 8; j++) tuft(curl(inMat(), fan + Math.PI + gauss(R) * 0.9, 4 + R() * 6, 0.12), 0.2, 0.45);
        // Long hyphae stay graph-connected to the colony vertex through two
        // meandering spines inside the mat; each leaves from its own spine vertex
        // and exits at a scattered rim point, so nothing converges on one point.
        const spines = [fan, fan + Math.PI].map((h, k) => {
          const p = tuft(curl(centre, h + gauss(R) * 0.5, (k ? 12 + R() * 8 : 22 + R() * 12) * MK, 0.3), 0.3, 0.5);
          if (!k) {
            p.haze = lobes; // the broad soft glow of the mat (no peak at the centre)
            mats.push({ centre, lobes, fan, haze: p, k: MK });
          }
          return p.points.slice(2);
        });
        const hubs = spines.flat();
        const leave = (hh) => {
          const l = lobes[Math.floor(R() * lobes.length)],
            e = hh + gauss(R) * 0.7,
            o = { x: l.x + Math.cos(e) * l.r * (0.8 + R() * 0.3), y: l.y + Math.sin(e) * l.r * (0.8 + R() * 0.3) },
            diff = (v) => Math.abs(Math.atan2(Math.sin(Math.atan2(o.y - v.y, o.x - v.x) - hh), Math.cos(Math.atan2(o.y - v.y, o.x - v.x) - hh)));
          // A hub behind the exit point (no U-turn or sharp kink into the walk).
          const ok = hubs.filter((v) => Math.hypot(o.x - v.x, o.y - v.y) > 5 && diff(v) < 0.5);
          if (!ok.length) {
            const v = hubs[Math.floor(R() * hubs.length)];
            return { inner: [], o: v };
          }
          const v = ok[Math.floor(R() * ok.length)];
          return { inner: [v, { x: (v.x + o.x) / 2 + gauss(R) * 1.2, y: (v.y + o.y) / 2 + gauss(R) * 1.2 }], o };
        };
        // ...dissolving into a few long sparse hyphae.
        for (let j = 0; j < 7; j++) {
          const hh = fan + gauss(R) * 1.2,
            { inner, o } = leave(hh),
            walk = persistWalk(o, hh, 140 + R() * 220, R, { ...sparseWalk, pull: undefined });
          const p = add([...inner, ...walk], { width: 0.45, alpha: 0.75, margin: true });
          p.marginRoot = centre;
          p.endFade = [centre]; // r11.1: emerges from the felt, never from the vertex
          mainRoots.push(p);
          if (budget()) yield "margin";
        }
        // r11: runners leave the colony for the content column (ref-8 sparse
        // hyphae flowing inwards), so text sections carry mycelium as in r9.
        const inward = side < 0 ? 0 : Math.PI;
        for (let j = 0, n = 4 + Math.floor(R2() * 4) + (compact ? 1 : 0); j < n; j++) {
          const hh = inward + gauss(R2) * (compact ? 0.8 : 0.6),
            { inner, o } = leave(hh);
          const p = add([...inner, ...persistWalk(o, hh, 380 + R2() * 420, R2, { ...sparseWalk, k: 0.016, pull: undefined })], { width: 0.5, alpha: 0.8 });
          p.runner = true;
          p.endFade = [centre];
          mainRoots.push(p);
          if (budget()) yield "margin";
        }
        budget() && (yield "margin");
      }
      if (!knots.length) continue;
      const first = knots[0],
        pool = nodes.length ? nodes : [first];
      const source = pool.reduce((a, b) => (Math.hypot(b.x - first.x, b.y - first.y) < Math.hypot(a.x - first.x, a.y - first.y) ? b : a));
      const route = source === first ? [] : [join(source, first, 1.3)];
      for (let j = 1; j < knots.length; j++) route.push(join(knots[j - 1], knots[j], 1.25));
      marginRoutes.push({ side, paths: route, knots, source });
      budget() && (yield "margin");
    }

  // Fine laterals (orders 1-3) at Poisson positions on random sides; acute and
  // long-running alongside; fusing into the web. No combs, no herringbone.
  const webR = rng("r10-irregular-root-web"),
    orders = { 1: 0, 2: 0, 3: 0 },
    gaps = [],
    junctions = [];
  let webFusions = 0,
    lateralCount = 0,
    minLateralsPer100px = Infinity;
  function fineWeb(parent, order) {
    const arc = [0];
    for (let i = 1; i < parent.points.length; i++) arc.push(arc.at(-1) + Math.hypot(parent.points[i].x - parent.points[i - 1].x, parent.points[i].y - parent.points[i - 1].y));
    const length = arc.at(-1);
    if (!length) return;
    const n = order === 1 ? Math.max(2, Math.ceil(length / 22)) : length > 18 ? 2 : 1;
    const positions = Array.from({ length: n }, () => length * (0.04 + webR() * 0.88)).sort((a, b) => a - b);
    parent.laterals = 0;
    for (let k = 0; k < positions.length; k++) {
      let at = 1;
      while (at < arc.length - 1 && arc[at] < positions[k]) at++;
      const start = parent.points[at],
        prev = parent.points[at - 1],
        heading = Math.atan2(start.y - prev.y, start.x - prev.x),
        spacing = Math.max(6, ((positions[k + 1] ?? length) - (positions[k - 1] ?? 0)) / 2),
        distance = spacing * (0.4 + webR() * 1.3),
        side = webR() < 0.5 ? -1 : 1,
        angle = heading + side * (Math.PI / 14 + (webR() * Math.PI) / 7),
        steps = Math.max(2, Math.ceil(distance / 3));
      let direction = angle,
        turn = 0;
      const points = [start];
      for (let j = 1; j <= steps; j++) {
        turn = turn * 0.7 + (webR() - 0.5) * 0.1;
        direction += turn - Math.atan2(Math.sin(direction - heading), Math.cos(direction - heading)) * 0.06;
        const a = points.at(-1),
          q = { x: a.x + (Math.cos(direction) * distance) / steps, y: a.y + (Math.sin(direction) * distance) / steps };
        let hit = null,
          best = 2.4;
        if (j > steps * 0.55)
          for (let dx = -1; dx <= 1; dx++)
            for (let dy = -1; dy <= 1; dy++)
              for (const c of grid.get(gk(q.x + dx * GC, q.y + dy * GC)) || []) {
                if (c.p === parent || c.p.static !== parent.static) continue;
                const dd = Math.hypot(q.x - c.q.x, q.y - c.q.y);
                if (dd < best && Math.hypot(start.x - c.q.x, start.y - c.q.y) > 5) {
                  hit = c.q;
                  best = dd;
                }
              }
        points.push(hit || q);
        if (hit) {
          webFusions++;
          break;
        }
      }
      const child = add(points, { width: parent.width * (order === 1 ? 0.6 : 0.7), alpha: parent.alpha * (order === 1 ? 0.62 : 0.8), birth: parent.birth ? parent.birth + at * 0.5 : 0, hair: true, order, margin: parent.margin, phase: parent.phase, period: parent.period });
      child.introParent = parent;
      child.introAt = arc[at];
      if (parent.endFade?.length) child.endFade = parent.endFade;
      if (order <= 2 && parent.static) junctions.push({ p: parent, i: at });
      child.marginRoot = parent.marginRoot || parent.points[0];
      child.marginDistance = (parent.marginDistance || 0) + arc[at];
      orders[order]++;
      lateralCount++;
      parent.laterals++;
      if (order < 3 && distance > 7) fineWeb(child, order + 1);
    }
    if (order === 1) {
      for (let k = 1; k < positions.length; k++) gaps.push((positions[k] - positions[k - 1]) / (length / n));
      minLateralsPer100px = Math.min(minLateralsPer100px, (parent.laterals * 100) / length);
    }
  }
  const rooted = strands.filter((p) => !p.hair && p.points.length > 4);
  for (let i = 0; i < rooted.length; i++) {
    fineWeb(rooted[i], 1);
    if (budget()) yield "web";
  }
  const mean = gaps.reduce((n, v) => n + v, 0) / Math.max(1, gaps.length),
    spacingCV = Math.sqrt(gaps.reduce((n, v) => n + (v - mean) ** 2, 0) / Math.max(1, gaps.length)) / mean;
  const sample = gaps.slice(0, 512),
    power = [];
  for (let k = 1; k <= 64; k++) {
    let re = 0,
      im = 0;
    for (let j = 0; j < sample.length; j++) {
      const phase = (2 * Math.PI * k * j) / sample.length;
      re += (sample[j] - mean) * Math.cos(phase);
      im += (sample[j] - mean) * Math.sin(phase);
    }
    power.push(re * re + im * im);
  }
  // r11.2 (owner): margin colonies need more permanent density around their
  // points, like a smaller hero colony, with density falling monotonically
  // from the centre (no fainter ring) and every attached strand visibly
  // joining the mat. A centre-heavy felt core (own RNG stream, laid after the
  // web so no other geometry or stream moves) and a soft central haze lobe.
  const feltR = rng("r112-margin-felt");
  for (const m of mats) {
    const { centre, fan } = m,
      MR = MAT_R * m.k; // r11.2b: compact edge mats are scaled to the width
    for (let j = 0, n = Math.round(MAT_FELT * m.k); j < n; j++) {
      const rr = MR * Math.pow(feltR(), 1.6),
        o = feltR() * 6.283,
        u = rr / MR,
        len = (5 + feltR() * 11 * (1 - 0.4 * u)) * Math.sqrt(m.k),
        h = u < 0.45 ? feltR() * 6.283 : o + gauss(feltR) * 0.9, // tangled core, outward-leaning rim (no radial star)
        pts = [{ x: centre.x + Math.cos(o) * rr, y: centre.y + Math.sin(o) * rr }];
      let a = h,
        tw = gauss(feltR) * 0.25;
      for (let k = 0, n = Math.max(3, Math.ceil(len / 1.8)); k < n; k++) {
        tw = tw * 0.8 + gauss(feltR) * 0.14;
        a += tw;
        const q = pts.at(-1);
        pts.push({ x: q.x + (Math.cos(a) * len) / n, y: q.y + (Math.sin(a) * len) / n });
      }
      const t = add(pts, { width: 0.2 + feltR() * 0.18, alpha: 0.8 * (1 - 0.4 * u), margin: true });
      t.hair = true;
      t.tuft = true;
      t.marginRoot = centre;
      t.introStart = feltR() * 24 * u;
    }
    void fan;
    // A central lobe makes the mat's glow peak at its point and fall off.
    m.lobes.push({ x: centre.x, y: centre.y, r: MR * 0.95 }, { x: centre.x, y: centre.y, r: MR * 3.2, k: 1.7 });
    budget() && (yield "margin-felt");
  }
  // Strands crossing a colony core (not its own felt) dissolve there too.
  if (marginKnots.length)
    for (const p of strands) {
      if (p.tuft) continue;
      let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
      for (const q of p.points) {
        if (q.x < x0) x0 = q.x;
        if (q.x > x1) x1 = q.x;
        if (q.y < y0) y0 = q.y;
        if (q.y > y1) y1 = q.y;
      }
      for (const k of marginKnots) {
        if (k.x < x0 - FADE_R1 || k.x > x1 + FADE_R1 || k.y < y0 - FADE_R1 || k.y > y1 + FADE_R1 || p.endFade?.includes(k)) continue;
        if (p.points.some((q) => Math.hypot(q.x - k.x, q.y - k.y) < FADE_R1)) (p.endFade ||= []).push(k);
      }
      budget() && (yield "core-fade");
    }
  // r11.2 cascade (owner: a hero pulse "slowly extend all the way down the
  // sides of the page as it ... hits each node"): each side's last colony joins
  // the first colony of the next page copy (the field repeats every `height`),
  // so a pulse can relay colony by colony down the whole page. Own RNG stream,
  // laid last: no other geometry or stream moves.
  const wrapR = rng("r112-margin-wrap");
  for (const r of marginRoutes) {
    const a = r.knots.at(-1),
      f = r.knots[0],
      b = vertex(f.x, f.y + height, 0),
      raw = persistWalk(a, Math.atan2(b.y - a.y, b.x - a.x), Math.hypot(b.x - a.x, b.y - a.y), wrapR, { k: 0.016, run: 60, jit: 0.025 }),
      last = raw.at(-1),
      pts = raw.map((q, i) => ({ x: q.x + ((b.x - last.x) * i) / (raw.length - 1), y: q.y + ((b.y - last.y) * i) / (raw.length - 1) }));
    pts[0] = a;
    pts[pts.length - 1] = b;
    const p = add(pts, { width: 1.25, alpha: 0.95, margin: true });
    p.introStart = 0;
    p.endFade = [a, b]; // dissolves into both colony mats (b is the next copy's first colony)
    p.wrap = true;
    r.wrap = p;
  }
  yield "knots";
  const hyphalKnots = yield* placeKnotsGen({ strands, junctions, marginKnots, layout, w, top, height, budget });
  const rootAudit = {
    mainStrands: rooted.length, lateralCount, minLateralsPer100px: Number.isFinite(minLateralsPer100px) ? minLateralsPer100px : 0, loneThreads: rooted.filter((p) => !p.laterals).length,
    web: { orders, spacingCV, spectralPeakFraction: Math.max(...power) / power.reduce((n, v) => n + v, 0), fusions: webFusions, spacingSamples: gaps.length },
  };
  return {
    strands, fusions, connectors: [], top, height, width: w, left, layout, baseCount, count, rootAudit, marginKnots, marginRoutes, marginDensityRatio, pageTop, hyphalKnots,
    segments: ss.filter((s) => s.field), densityMultiplier: 1.2, flowSources: virtual.length,
  };
}

/** r11.2 passive life (owner: "strands still don't seem to be growing/fading
 * much passively"). About 55% of the field and margin hyphae carry a living
 * distal tip (35-150 px) and ~60% of the static laterals (orders 1-2) regrow
 * whole; each tip regrows
 * (glowing slightly), holds, dies back tip-first with a fading tail, rests,
 * on its own 20-60 s period and phase. Laterals rooted on a living tip follow
 * it (they emerge behind the advancing tip and vanish before it retreats past
 * them). The base stays in the static raster; living parts are drawn on a
 * page-anchored life layer at LIFE_HZ (sub-pixel steps). */
const LIFE = Object.freeze({ TIP_P: 0.18, LATERAL_P: 0.1, MIN_LEN: 50, TIP_MIN: 35, TIP_SPAN: 115, TIP_FRAC: 0.42, PERIOD: 16, PERIOD_SPAN: 30, SLOW_PERIOD: 40, SLOW_SPAN: 50, FOLLOW_LAG: 3, FOLLOW_GAIN: 1.3 });
/** r11.2 margin pulses at ~92% of the hero (owner: "pulses on the sidebar to be
 * upped ... to almost same extent hero has"): the hero's crest speed (38 px/s
 * on the cord, 22 px/s through fine threads), reach (600 of the hero's 650 px
 * of fine distance, decay 276 of 300 px), brightness, residue that retreats
 * far-edge first, and an arrival bloom travelling out to the colony rim. At
 * most one per side; rate ~0.93 of the hero's. */
const MP = Object.freeze({ GAIN: 0.92, FAST: 38, SLOW: 22, CUTOFF: 600, LAMBDA: 276, STEP: 0.3, TAIL: 5.6, RSTEP: 4, RES: 0.15, RES_HOLD: 2, RES_BACK: 0.2, RES_FAR: 3, RES_FADE: 6, RATE: 0.93, BLOOM_R: 48 });
const LIFE_HZ = 15, // life ticks per second (each repaints only what changed)
  LIFE_TILE = 128, // CSS px; the life layer is a page-anchored grid of tiles
  LIFE_PAD = 192, // tiles kept above and below the viewport
  // A strand repaints when its tip has moved 1 CSS px (2 device px at DPR2):
  // at 8-20 px/s that is 8-20 repaints a second, not one every tick.
  LIFE_STEP = 1;

/** Shared load-in contract (design/r11-coordination.md): everything starts at
 * the first rendered frame and is established by INTRO_MS, with ease-out pacing. */
export const INTRO_MS = SHARED_INTRO_MS; // 6000 by default (shared, see mycelium-geometry.js)
const easeOut = (u) => 1 - Math.pow(1 - Math.max(0, Math.min(1, u)), 3);
/** r11.2 field front: a quadratic ease-out (the cubic one left every colony
 * at ~350-490 px/s on the first frames, a flash of strands at each margin mat). */
const frontEase = (u) => 1 - Math.pow(1 - Math.max(0, Math.min(1, u)), 2);
const HERO_EDGE_S = 3.6 * INTRO_SCALE; // fallback: hero reaches its edges (~0.75 of its 0-4.5 s growth at 6 s)
/** r11.2 (owner: "a fast paced burst of sparks erupts from the bottom two
 * nodes" ~2/3 into the grow-in). All ~40-60 hero-edge continuation families
 * (3-5k strands, rooted on the bottom colonies) used to start together when the
 * hero reached 75% and cross 550 px in the remaining 1.5-2.5 s with a cubic
 * ease-out: ~650 px/s glowing fronts. Now each family starts when the hero has
 * grown its own seed vertex, never later than HERO_FAMILY_LATEST of the intro,
 * and its front advances on a trapezoid (gentle start, constant cruise, eased
 * arrival) that ends exactly at INTRO_MS. */
const HERO_FAMILY_LATEST = 0.62,
  HERO_FAMILY_GAP = 0.065, // s between family starts (6 s base)
  HERO_FAMILY_VMAX = 200; // px/s at the 6 s base (scaled by 1/INTRO_SCALE)
function trapEase(u, a = 0.25, b = 0.35) {
  u = Math.max(0, Math.min(1, u));
  const v = 1 / (1 - a / 2 - b / 2);
  if (u < a) return (v * u * u) / (2 * a);
  if (u <= 1 - b) return v * (a / 2 + (u - a));
  return 1 - (v * (1 - u) * (1 - u)) / (2 * b);
}
const trapSpeed = (u, a = 0.25, b = 0.35) => {
  const v = 1 / (1 - a / 2 - b / 2);
  return u <= 0 || u >= 1 ? 0 : u < a ? (v * u) / a : u <= 1 - b ? v : (v * (1 - u)) / b;
};
let introUsed = false; // switches (theme, hue, style, resize) never replay the intro

function rgba(colour, a) {
  const s = colour.trim();
  let r = 255,
    g = 255,
    b = 255;
  if (s[0] === "#") {
    const h = s.length === 4 ? s.slice(1).replace(/./g, (c) => c + c) : s.slice(1, 7);
    [r, g, b] = [0, 2, 4].map((i) => parseInt(h.slice(i, i + 2), 16));
  } else {
    const m = s.match(/[\d.]+/g);
    if (m) [r, g, b] = m.map(Number);
  }
  return `rgba(${r},${g},${b},${Math.max(0, Math.min(1, a)).toFixed(3)})`;
}

/** Fine-hair fuzz on a persistent strand: short acute hairs that curl back
 * alongside the parent at exponential (aperiodic) spacing. Sub-edge fringe of
 * a graph edge, like the cord fringe: drawn with its parent, never separately routed. */
function makeFuzz(p, k) {
  const r = rng("r11-fuzz-" + k),
    pts = p.points,
    n = pts.length,
    out = [],
    mean = p.order === 1 ? 12 : 7.5;
  let s = r() * mean,
    i = 1;
  for (;;) {
    s += -Math.log(1 - r()) * mean;
    while (i < n - 1 && p.arc[i] < s) i++;
    if (i >= n - 1) break;
    const a = pts[i - 1],
      b = pts[i],
      h = Math.atan2(b.y - a.y, b.x - a.x),
      side = r() < 0.5 ? -1 : 1,
      L = (2.5 + 9 * Math.pow(r(), 1.6)) * (p.order === 1 ? 0.7 : 1),
      a1 = h + side * (0.35 + r() * 0.5),
      a2 = a1 - side * (0.15 + r() * 0.35),
      mx = b.x + Math.cos(a1) * L * 0.5,
      my = b.y + Math.sin(a1) * L * 0.5;
    out.push(i, b.x, b.y, mx, my, mx + Math.cos(a2) * L * 0.5, my + Math.sin(a2) * L * 0.5);
    if (out.length === 7 && s > p.arc[n - 1]) break;
  }
  if (!out.length && n > 3) {
    // A short strand still carries at least one hair (never a bare line).
    const i = n >> 1,
      a = pts[i - 1],
      b = pts[i],
      h = Math.atan2(b.y - a.y, b.x - a.x) + (r() < 0.5 ? -0.5 : 0.5);
    out.push(i, b.x, b.y, b.x + Math.cos(h) * 2, b.y + Math.sin(h) * 2, b.x + Math.cos(h - 0.2) * 4, b.y + Math.sin(h - 0.2) * 4);
  }
  return new Float32Array(out);
}

export function createField({ stage, state, reduce }) {
  const c = document.getElementById("page-mycelium"),
    x = c.getContext("2d");
  const live = document.createElement("canvas");
  live.className = "page-mycelium page-mycelium-live";
  live.setAttribute("aria-hidden", "true");
  c.after(live);
  const lx = live.getContext("2d");
  // r11.2 life layer: living tips and regrowing laterals on a page-anchored
  // grid of full-DPR tiles (one container moved by a compositor transform on
  // scroll). A tick repaints only the tiles a moving tip touched, so the cost
  // follows the number of active tips, not the viewport (a whole-viewport
  // canvas redrawn and re-uploaded at 20 Hz cost 30% of the presented frames).
  const lifeBox = document.createElement("div"),
    lifeInner = document.createElement("div");
  lifeBox.className = "page-mycelium-field page-mycelium-life";
  lifeBox.setAttribute("aria-hidden", "true");
  lifeInner.style.cssText = "position:absolute;left:0;top:0;width:0;height:0;will-change:transform";
  lifeBox.appendChild(lifeInner);
  c.after(lifeBox);
  // Static field: horizontal band tiles at device pixels (sharp), as DOM
  // canvases in a fixed box. Scrolling only moves them (compositor transform);
  // r11 blitted one 4x-pixel tile into the viewport canvas on every scroll.
  const bandBox = document.createElement("div");
  bandBox.className = "page-mycelium-field";
  bandBox.setAttribute("aria-hidden", "true");
  c.before(bandBox);
  let bands = [];
  const hero = document.getElementById("hyphae"),
    css = getComputedStyle(stage),
    light = state.theme === "light",
    glow = css.getPropertyValue("--glow").trim(),
    core = css.getPropertyValue("--core").trim();
  const width = Math.round(document.documentElement.clientWidth || innerWidth),
    height = Math.round(innerHeight),
    d = Math.min(devicePixelRatio || 1, 2);
  live.width = width * d;
  live.height = height * d;
  // The static composite holds only the hero (page-anchored, hero-box sized)
  // and is translated by the compositor on scroll.
  c.width = width * d;
  c.height = 2;
  c.style.bottom = "auto";
  c.style.height = "1px";
  if (!light) c.style.mixBlendMode = "plus-lighter"; // hero adds onto the field, as the old 'lighter' composite
  // (r11.2) no blend mode on the life layer: over the dark ground source-over of its own additive strokes reads the same and keeps compositing cheap
  x.setTransform(d, 0, 0, d, 0, 0);
  lx.setTransform(d, 0, 0, d, 0, 0);
  const fieldAlpha = light ? 0.24 : 0.62,
    lightMul = light ? 0.62 : 1;
  let scene = hero ? window.__myceliumScene : null,
    field = null,
    graph = null,
    disposed = false,
    raf = 0,
    attached = false,
    frozen = null,
    clock = scene?.clock || sceneClock(),
    lastScroll = NaN,
    lastVersion = -1,
    tileVersion = 0,
    dirty = null,
    lifeList = [],
    lifeStamp = 0,
    lifeY = NaN,
    lifeRows = [NaN, NaN],
    lifeNearby = null,
    lifeScroll = NaN,
    lifePending = false,
    lifeDrawn = -Infinity,
    marginSchedules = [],
    cascades = [],
    heroInterval = 24,
    colonySprites = new Map(),
    introEnded = 0,
    span = { A: 1, H: 1 },
    tips = [];
  const metrics = (window.__understoryMetrics = { costs: [], frames: 0, paused: false, staticRedraws: 0, liveCosts: [], fieldCosts: [] });
  const params = new URLSearchParams(location.search);
  if (params.has("t") && Number.isFinite(+params.get("t"))) frozen = +params.get("t");
  // Intro: only on the first field of a fresh page load (never on switches).
  let intro =
    !introUsed && performance.now() < 4000 && !reduce.matches && !document.hidden && frozen === null
      ? { start: null, cursor: 0, manual: null }
      : null;
  introUsed = true;
  const introTime = (now = performance.now()) => (!intro ? INTRO_MS / 1000 : intro.manual !== null ? intro.manual / 1000 : intro.start === null ? 0 : (now - intro.start) / 1000);
  window.MyceliumField = Object.freeze({ INTRO_MS, INTRO_SCALE, get introProgress() { return Math.min(1, introTime() / (INTRO_MS / 1000)); } });
  /** Load-in activity probe (r11.2): field growth-front speeds (px/s along the
   * strands) and the number of glowing intro tips per family. */
  window.__introActivity = () => {
    const t = introTime(),
      T = INTRO_MS / 1000,
      dEase = (u) => (u <= 0 || u >= 1 ? 0 : (frontEase(u + 1e-4) - frontEase(u - 1e-4)) / 2e-4),
      roots = field ? heroRoots() : [];
    const rateH = (r) => (intro && r.introT0 !== undefined ? (trapSpeed((t - r.introT0) / (T - r.introT0)) / (T - r.introT0)) * r.famReach : 0);
    return {
      t, running: !!intro, spanA: span.A, spanH: span.H, roots: roots.length, rootStarts: roots.map((r) => +(r.introT0 ?? -1).toFixed(2)),
      rateA: intro ? (dEase(t / T) / T) * span.A : 0,
      rateH: Math.max(0, ...roots.map(rateH)),
      rootsStarted: intro ? roots.filter((r) => r.introT0 !== undefined && t >= r.introT0).length : roots.length,
      tipsA: intro ? tips.filter(([p]) => !p.heroFamily).length : 0,
      tipsH: intro ? tips.filter(([p]) => p.heroFamily).length : 0,
      // glowing tips whose front moves faster than 210 px/s at the 6 s base:
      // hero-edge families (the burst) and the field's own fronts
      fastTips: intro ? tips.filter(([p]) => (p.heroFamily ? rateH(p.heroRoot) : (dEase(t / T) / T) * span.A) > 210 / INTRO_SCALE).length : 0,
      fastH: intro ? tips.filter(([p]) => p.heroFamily && rateH(p.heroRoot) > 210 / INTRO_SCALE).length : 0,
    };
  };
  const firstFrame = (now) => {
    if (intro && intro.start === null) intro.start = now ?? performance.now();
  };

  // r11.2b: compact edge colonies (< 1100 px) carry no margin pulses or
  // cascades: on a phone the text column reaches to ~20 px from the edge, and
  // even at 0.35 of the margin gain with leaks cut to ~50 px a crest dropped
  // the busiest pixel under body text to 4.1:1 (3.0:1 at 0.7 and 600 px).
  // They grow, breathe and live like the desktop margins.
  const mpGain = () => MP.GAIN,
    COMPACT_LEAK = Object.freeze({ cutoff: 48, lambda: 24 });
  const marginWeight = (px) => {
    const l = field.layout;
    if (!l.enabled) {
      // r11.2b compact: the edge colonies (centres 3 px inside the edge) carry
      // margin weight out to ~40 px; the text column stays at column weight.
      if (!field.marginKnots.length) return 0;
      const q = Math.max(0, Math.min(1, (40 - Math.min(px, field.width - px)) / 32));
      return q * q * (3 - 2 * q);
    }
    const dd = px < l.left ? l.left - px : px > l.right ? px - l.right : 0,
      q = Math.max(0, Math.min(1, dd / 72));
    return q * q * (3 - 2 * q);
  };
  // r11: on the home page the content column carries r9-level mycelium; text is
  // protected locally by a background-coloured glyph halo (design.css), never
  // by dimming the whole column. App pages keep the r10 column weight.
  const apex = document.body.classList.contains("apex"),
    column = apex ? (light ? 0.4 : 0.7) : 0.32;
  const columnWeight = (px) => column + (1 - column) * marginWeight(px);
  const CH = 10;
  const endFadeAt = (p, q) => {
    let f = 1;
    for (const e of p.endFade) f *= smooth((Math.hypot(q.x - e.x, q.y - e.y) - FADE_R0) / (FADE_R1 - FADE_R0));
    return f;
  };
  /** Linear gradient along points [k0, k1] of a fading strand: each point's
   * fade at its projection on the chunk's start->end axis (monotone stops). */
  function fadeGradient(ctx, p, k0, k1, colour) {
    const pts = p.points,
      a = pts[k0],
      b = pts[Math.min(pts.length - 1, k1)],
      dx = b.x - a.x,
      dy = b.y - a.y,
      L2 = dx * dx + dy * dy || 1,
      g = ctx.createLinearGradient(a.x, a.y, b.x, b.y),
      c = typeof colour === "string" ? colour : glow;
    let last = 0;
    for (let k = k0; k <= Math.min(pts.length - 1, k1); k++) {
      const t = Math.max(last, Math.min(1, ((pts[k].x - a.x) * dx + (pts[k].y - a.y) * dy) / L2));
      g.addColorStop(t, rgba(c, p.fades[k]));
      last = t;
    }
    return g;
  }
  const hazeIn = rgba(glow, 0.16),
    hazeMid = rgba(glow, 0.07),
    hazeOut = rgba(glow, 0);
  /** Draws points [from, to) of a strand (fractional ends), chunked with a
   * per-chunk column weight and distal taper fixed by chunk position, so
   * incremental (intro) and one-shot rasters match. Butt caps throughout: no
   * alpha beads at joints. Fuzz and knots inside the range are drawn too. */
  /** Cottony mat glow: overlapping broad soft lobes, never a bright point. */
  function drawHaze(ctx, p, alpha, mulAt = null) {
    for (const l of p.haze) {
      const R1 = l.r * 1.6,
        g = ctx.createRadialGradient(l.x, l.y, 0, l.x, l.y, R1);
      g.addColorStop(0, hazeIn);
      g.addColorStop(0.55, hazeMid);
      g.addColorStop(1, hazeOut);
      ctx.globalAlpha = Math.min(1, alpha * (l.k || 1) * marginWeight(l.x) * lightMul * (mulAt ? mulAt(l.y) : 1));
      ctx.fillStyle = g;
      ctx.fillRect(l.x - R1, l.y - R1, R1 * 2, R1 * 2);
    }
  }
  function drawRange(ctx, p, from, to, alpha, detail = false, yr = null) {
    const pts = p.points,
      n = pts.length - 1,
      end = Math.min(n, to);
    if (end <= from) return;
    ctx.lineCap = "butt";
    let f = 0;
    if (detail && !p.fuzz && p.fuzzSeed >= 0) p.fuzz = makeFuzz(p, p.fuzzSeed);
    // r11.2: during the load-in the mat glow builds up on the live layer and
    // is baked into the bands once, at the end of the intro (no pop).
    if (p.haze && from === 0 && !(intro && detail)) drawHaze(ctx, p, alpha);
    const fz = detail ? p.fuzz : null;
    if (fz) while (f < fz.length && fz[f] < from) f += 7;
    const fades = p.fades; // r11.1: per-point fade into a colony core (fading strands only)
    for (let c0 = Math.floor(from / CH) * CH; c0 < end; c0 += CH) {
      const j = Math.min(c0 + CH, Math.ceil(end)),
        i0 = Math.max(c0, from),
        mid = pts[Math.min(n, c0 + CH / 2)],
        w = p.margin ? marginWeight(mid.x) : columnWeight(mid.x);
      // A chunk reaching a colony core keeps its single path (no extra joints,
      // so no beads) and fades along it through a linear-gradient stroke.
      let fmin = 1,
        fmax = 0;
      if (fades)
        for (let k = Math.floor(i0); k <= j; k++) {
          if (fades[k] < fmin) fmin = fades[k];
          if (fades[k] > fmax) fmax = fades[k];
        }
      let skip = w <= 0.01 || (fades && fmax <= 0.004);
      if (!skip && yr) {
        // Band culling: chunks wholly outside this band (plus hair overhang) are skipped.
        let y0 = Infinity,
          y1 = -Infinity;
        for (let k = c0; k <= j; k++) {
          const yy = pts[k].y;
          if (yy < y0) y0 = yy;
          if (yy > y1) y1 = yy;
        }
        skip = y1 < yr[0] - 14 || y0 > yr[1] + 14;
      }
      if (skip) {
        if (fz) while (f < fz.length && fz[f] < j) f += 7;
        continue;
      }
      const distal = c0 + CH / 2 > n * 0.5,
        ga = alpha * w * lightMul * (distal ? p.taper : 1);
      ctx.globalAlpha = ga;
      ctx.lineWidth = p.width * (distal ? 0.7 : 1);
      const k0 = Math.floor(i0),
        u = i0 - k0,
        a0 = pts[k0],
        b0 = pts[Math.min(n, k0 + 1)];
      const plain = ctx.strokeStyle;
      if (fades && fmin < 1) ctx.strokeStyle = fadeGradient(ctx, p, k0, j, plain);
      ctx.beginPath();
      ctx.moveTo(a0.x + (b0.x - a0.x) * u, a0.y + (b0.y - a0.y) * u);
      for (let k = k0 + 1; k <= j; k++) {
        const fr = Math.min(1, end - k + 1);
        if (fr <= 0) break;
        ctx.lineTo(pts[k - 1].x + (pts[k].x - pts[k - 1].x) * fr, pts[k - 1].y + (pts[k].y - pts[k - 1].y) * fr);
      }
      ctx.stroke();
      if (fz && f < fz.length && fz[f] < Math.min(j, end)) {
        ctx.globalAlpha = ga * 0.6;
        ctx.lineWidth = Math.max(0.28, p.width * 0.55);
        ctx.beginPath();
        for (; f < fz.length && fz[f] < Math.min(j, end); f += 7) {
          ctx.moveTo(fz[f + 1], fz[f + 2]);
          ctx.quadraticCurveTo(fz[f + 3], fz[f + 4], fz[f + 5], fz[f + 6]);
        }
        ctx.stroke();
      }
      if (fades && fmin < 1) ctx.strokeStyle = plain;
    }
    if (detail && p.knots)
      for (const k of p.knots) if (k.i >= from && k.i < end && !k.underText && (!yr || (k.y > yr[0] - 10 && k.y < yr[1] + 10))) drawKnot(ctx, k, alpha * (p.margin ? marginWeight(k.x) : columnWeight(k.x)) * lightMul * (p.endFade ? endFadeAt(p, k) : 1));
  }
  const knotCore = rgba(light ? glow : core, 1),
    knotMid = rgba(glow, 0.55),
    knotEdge = rgba(glow, 0);
  /** A tangled ball: overlapping soft lobes (no circle path) plus fibres wound
   * tangentially into it. No arc() anywhere. */
  function drawKnot(ctx, k, a, boost = 1) {
    const A = Math.min(1, a * k.bright * 1.7 * boost);
    if (A <= 0.01 || k.underText) return;
    ctx.save();
    ctx.translate(k.x, k.y);
    ctx.rotate(k.ang);
    ctx.scale(k.el, 1);
    ctx.globalAlpha = A;
    for (const [dx, dy, rr] of k.lobes) {
      const R1 = rr * 1.7,
        g = ctx.createRadialGradient(dx, dy, 0, dx, dy, R1);
      g.addColorStop(0, knotCore);
      g.addColorStop(0.45, knotMid);
      g.addColorStop(1, knotEdge);
      ctx.fillStyle = g;
      ctx.fillRect(dx - R1, dy - R1, R1 * 2, R1 * 2);
    }
    ctx.strokeStyle = light ? glow : core;
    ctx.lineWidth = 0.32;
    ctx.globalAlpha = A * 0.75;
    ctx.beginPath();
    for (const [o, sweep, rad, len] of k.fibres) {
      const r0 = k.r * (1.3 + rad),
        o1 = o + sweep * 0.2 * len,
        o2 = o + sweep * 0.36 * len;
      ctx.moveTo(Math.cos(o) * r0, Math.sin(o) * r0);
      ctx.quadraticCurveTo(Math.cos(o1) * r0 * 0.95, Math.sin(o1) * r0 * 0.95, Math.cos(o2) * k.r * 0.35, Math.sin(o2) * k.r * 0.35);
    }
    ctx.stroke();
    ctx.restore();
  }
  const statics = () => field.strands.filter((p) => p.static && p.rasterEnd > 0);
  /** Calls fn(ctx, yr) for every band (and wrap copy) the strand overlaps;
   * yr is the band's range in the strand's own (unshifted) coordinates. */
  function eachShift(p, fn) {
    for (const shift of [-field.height, 0, field.height]) {
      const y0 = p.box[1] + shift - field.top,
        y1 = p.box[3] + shift - field.top;
      if (y1 < 0 || y0 > field.height) continue;
      for (const b of bands) {
        if (y1 < b.y0 || y0 > b.y0 + b.h) continue;
        b.cx.save();
        b.cx.translate(0, shift);
        fn(b.cx, [field.top + b.y0 - shift, field.top + b.y0 + b.h - shift]);
        b.cx.restore();
      }
    }
  }
  const BAND = 240;
  function resetTile() {
    // Full device-pixel resolution: r9/r10 rastered this tile at CSS pixels and
    // the DPR transform upscaled it (the blurry sidebars and field).
    const n = Math.ceil(field.height / BAND);
    if (bands.length !== n || bands[0]?.cv.width !== Math.ceil(field.width * d)) {
      for (const b of bands) {
        b.cv.width = b.cv.height = 0;
        b.cv.remove();
      }
      bands = [];
      for (let i = 0; i < n; i++) {
        const y0 = i * BAND,
          h = Math.min(BAND, field.height - y0),
          cv = document.createElement("canvas");
        cv.width = Math.ceil(field.width * d);
        cv.height = Math.ceil(h * d);
        cv.style.cssText = `left:${field.left}px;width:${field.width}px;height:${cv.height / d}px;visibility:hidden`;
        bandBox.appendChild(cv);
        bands.push({ cv, cx: cv.getContext("2d"), y0, h, k: null, ty: NaN, shown: false, ready: true, queued: false, job: null });
      }
    }
    for (const b of bands) prepBand(b);
    settled = false;
    bandQueue.length = 0;
    lastScroll = NaN;
    for (const p of field.strands) p.drawn = 0;
  }
  /** (Re)allocates a band's full-DPR backing and resets its drawing state. */
  function prepBand(b) {
    const w = Math.ceil(field.width * d),
      h = Math.ceil(b.h * d);
    if (b.cv.width !== w || b.cv.height !== h) {
      b.cv.width = w;
      b.cv.height = h;
    }
    const g = b.cx;
    g.setTransform(1, 0, 0, 1, 0, 0);
    g.globalAlpha = 1;
    g.globalCompositeOperation = "source-over";
    g.clearRect(0, 0, w, h);
    g.setTransform(d, 0, 0, d, -field.left * d, -(field.top + b.y0) * d);
    g.strokeStyle = glow;
    g.globalCompositeOperation = light ? "source-over" : "lighter";
    b.k = null;
    b.ready = true;
    b.queued = false;
  }
  // Residency (after the intro and the first full raster): when the resident
  // bands exceed the byte budget the farthest ones (never within KEEP CSS px of
  // the viewport) free their backing; bands within KEEP px are re-rastered from
  // the vector strands ahead of time (bounded slices), and a band that becomes
  // visible before it is ready is finished synchronously.
  const KEEP = BAND,
    BAND_BUDGET = 60e6; // bytes of resident band backing (all bands fit at <=1440 DPR2)
  let settled = false;
  const bandQueue = [];
  function rasterBand(b, budgetMs = Infinity) {
    if (!b.job) {
      const q = b.queued;
      prepBand(b);
      b.ready = false;
      b.queued = q;
      b.job = { list: statics(), i: 0 };
    }
    const job = b.job,
      t0 = performance.now(),
      top = field.top + b.y0,
      bottom = top + b.h;
    while (job.i < job.list.length && performance.now() - t0 < budgetMs) {
      const p = job.list[job.i++],
        n = p.rasterEnd;
      for (const shift of [-field.height, 0, field.height]) {
        if (p.box[3] + shift < top || p.box[1] + shift > bottom) continue;
        b.cx.save();
        b.cx.translate(0, shift);
        drawRange(b.cx, p, 0, n, p.alpha * fieldAlpha, true, [top - shift, bottom - shift]);
        b.cx.restore();
      }
    }
    metrics.bandRasterMs = (metrics.bandRasterMs || 0) + (performance.now() - t0);
    if (job.i < job.list.length) return false;
    b.job = null;
    b.ready = true;
    b.queued = false;
    metrics.bandRasters = (metrics.bandRasters || 0) + 1;
    (metrics.bandRasterLog ||= []).push(bands.indexOf(b));
    if (metrics.bandRasterLog.length > 200) metrics.bandRasterLog.shift();
    return true;
  }
  // r11.4: band rasters are paced slices of at most 4 ms (see pacer).
  let bandJob = null;
  const bandSlice = (deadline) => {
    if (disposed) return true;
    while (bandQueue[0] && bandQueue[0].ready && !bandQueue[0].job) bandQueue.shift(); // finished elsewhere
    const b = bandQueue[0];
    if (!b) return true;
    if (rasterBand(b, Math.min(4, deadline - performance.now()))) {
      bandQueue.shift();
      lastScroll = NaN; // present it
      if (frozen !== null || reduce.matches) paint(frozen ?? clock.elapsed);
    }
    return !bandQueue.length;
  };
  const bandKick = () => {
    if (bandJob && !bandJob.done) return;
    bandJob = pacer.add({ prio: 1, name: "band", run: (deadline) => (bandJob.done = bandSlice(deadline)) });
  };
  function evict(b) {
    b.cv.width = b.cv.height = 0;
    b.ready = false;
    b.queued = false;
    b.job = null;
    b.k = null;
  }
  /** Places each band on the page copy nearest the viewport (compositor
   * transform, device-pixel snapped); the first copy fades in beneath the hero. */
  function placeBands(origin) {
    for (const b of bands) {
      // The nearest page copy of this band (the field repeats every field.height).
      const k0 = Math.round((-origin - b.y0 + (height - b.h) / 2) / field.height);
      b.dist = Infinity;
      for (let k = k0 - 1; k <= k0 + 1; k++) {
        if (scene && k < 0) continue;
        const y = origin + k * field.height + b.y0,
          dist = y + b.h < 0 ? -(y + b.h) : y > height ? y - height : 0;
        if (dist < b.dist) {
          b.dist = dist;
          b.vy = y;
          b.vk = k;
        }
      }
      b.visible = b.dist === 0 && b.vy < height && b.vy + b.h > 0;
    }
    if (settled) {
      // Least-needed eviction only when over the byte budget, never inside KEEP.
      const per = Math.ceil(field.width * d) * Math.ceil(BAND * d) * 4,
        maxRes = Math.max(Math.ceil((height + 2 * KEEP) / BAND) + 1, Math.floor(BAND_BUDGET / per));
      let resident = bands.filter((b) => b.ready || b.job).length;
      const want = bands.filter((b) => !b.ready && !b.job && b.dist <= KEEP).length;
      for (const b of [...bands].sort((p, q) => q.dist - p.dist)) {
        if (resident + want <= maxRes || b.dist <= KEEP) break;
        if (b.ready && !b.job) {
          evict(b);
          resident--;
        }
      }
      for (const b of [...bands].sort((p, q) => p.dist - q.dist)) {
        if (b.ready) continue;
        if (b.visible) {
          rasterBand(b); // finish now: never show an empty band
          const at = bandQueue.indexOf(b);
          if (at >= 0) bandQueue.splice(at, 1);
        } else if (!b.queued && b.dist <= KEEP) {
          b.queued = true;
          bandQueue.push(b);
          bandKick(); // idempotent while a band job runs
        }
      }
    }
    for (const b of bands) {
      const k = b.vk,
        y = b.vy,
        show = b.visible && b.ready;
      if (show !== b.shown) {
        b.cv.style.visibility = show ? "visible" : "hidden";
        b.shown = show;
      }
      if (!show) continue;
      const ty = Math.round(y * d) / d;
      if (ty !== b.ty) {
        b.cv.style.transform = `translate3d(0,${ty}px,0)`;
        b.ty = ty;
      }
      if (k !== b.k) {
        b.k = k;
        const FADE = 280;
        if (scene && k === 0 && b.y0 < FADE) {
          const a0 = (b.y0 / FADE).toFixed(3),
            stop = FADE - b.y0,
            m = stop < b.h ? `linear-gradient(to bottom, rgba(0,0,0,${a0}) 0px, #000 ${stop}px)` : `linear-gradient(to bottom, rgba(0,0,0,${a0}), rgba(0,0,0,${((b.y0 + b.h) / FADE).toFixed(3)}))`;
          b.cv.style.maskImage = b.cv.style.webkitMaskImage = m;
        } else b.cv.style.maskImage = b.cv.style.webkitMaskImage = "";
      }
    }
  }
  // The static tile is rasterised in bounded slices (no long task); it is
  // presented as it fills, like the hero.
  let rasterJob = 0;
  function rasterStatic() {
    resetTile();
    const list = statics(),
      job = ++rasterJob;
    let i = 0,
      spent = 0;
    // r11.4: paced slices (see pacer).
    pacer.add({ prio: 1, name: "field-raster", run: (deadline) => {
      if (disposed || job !== rasterJob) return true;
      const t0 = performance.now();
      while (i < list.length && performance.now() < deadline) {
        const p = list[i++],
          n = p.rasterEnd;
        eachShift(p, (g, yr) => drawRange(g, p, 0, n, p.alpha * fieldAlpha, true, yr));
        p.drawn = n;
      }
      spent += performance.now() - t0;
      tileVersion++;
      if (i < list.length) return false;
      metrics.rasterMs = +spent.toFixed(1);
      c.dataset.tile = "1";
      settled = true;
      lastScroll = NaN; // re-evaluate band residency
      return true;
    } });
  }
  const indexAt = (p, px) => {
    const arc = p.arc,
      n = arc.length - 1;
    if (px <= 0) return 0;
    if (px >= arc[n]) return n;
    let lo = 0,
      hi = n;
    while (hi - lo > 1) {
      const m = (lo + hi) >> 1;
      if (arc[m] < px) lo = m;
      else hi = m;
    }
    return lo + (px - arc[lo]) / Math.max(1e-6, arc[hi] - arc[lo]);
  };
  /** Intro step: grows the static tile incrementally to the current intro
   * time. Field sources and margin colonies start at t=0; hero-edge
   * continuations join when the hero reaches its edges. */
  let rootList = null;
  /** Hero-edge family roots with their start times (seconds into the intro):
   * when the hero has grown the seed vertex (its age map), at most
   * HERO_FAMILY_LATEST of the intro. Deterministic (manual sweeps match). */
  function heroRoots() {
    if (rootList) return rootList;
    const T = INTRO_MS / 1000;
    rootList = field.strands.filter((p) => p.heroFamily && p.heroRoot === p);
    for (const r of rootList) {
      const b = scene?.vertexAge ? scene.vertexAge(r.points[0]) : r.points[0].birth,
        at = scene?.introTimeOfAge && Number.isFinite(b) ? scene.introTimeOfAge(b) : HERO_EDGE_S;
      // Late seeds start a little ahead of the hero so no front cruises faster
      // than HERO_FAMILY_VMAX (the old burst moved at ~650 px/s).
      const cruise = (r.famReach || 1) / (1 - 0.25 / 2 - 0.35 / 2);
      r.introCap = Math.min(HERO_FAMILY_LATEST * T, T - cruise / (HERO_FAMILY_VMAX / INTRO_SCALE));
      r.introT0 = Math.max(0, Math.min(r.introCap, at));
      r.front = -1;
    }
    // Seeds the hero grows at the same age (a colony centre) set off one after
    // another, HERO_FAMILY_GAP apart (7 left within 250 ms at 6 s).
    const gap = HERO_FAMILY_GAP * INTRO_SCALE,
      order = [...rootList].sort((a, b) => a.introT0 - b.introT0);
    for (let i = 1; i < order.length; i++) order[i].introT0 = Math.min(order[i].introCap, Math.max(order[i].introT0, order[i - 1].introT0 + gap));
    order.sort((a, b) => a.introT0 - b.introT0);
    for (let i = order.length - 2; i >= 0; i--) order[i].introT0 = Math.max(0, Math.min(order[i].introT0, order[i + 1].introT0 - gap));
    return rootList;
  }
  function introStep(now, unbounded = false) {
    if (!intro || !field) return;
    const t = introTime(now),
      T = INTRO_MS / 1000;
    const dA = frontEase(t / T) * span.A;
    for (const r of heroRoots()) r.front = t < r.introT0 ? -1 : trapEase((t - r.introT0) / (T - r.introT0)) * r.famReach;
    intro.dA = dA; // living tips follow the same fronts (life layer)
    const list = intro.list,
      t0 = performance.now();
    let drawn = false,
      pending = false;
    tips = [];
    for (let k = 0; k < list.length; k++) {
      const p = list[(intro.cursor + k) % list.length],
        n = p.rasterEnd,
        target = Math.min(n, indexAt(p, (p.heroFamily ? p.heroRoot.front : dA) - p.s0));
      if (target > 0 && target < n) tips.push([p, target]);
      if (target <= p.drawn + 1e-6) continue;
      if (!unbounded && performance.now() - t0 > 3.5) {
        intro.cursor = (intro.cursor + k) % list.length;
        pending = true;
        break;
      }
      const from = p.drawn;
      eachShift(p, (g, yr) => drawRange(g, p, from, target, p.alpha * fieldAlpha, true, yr));
      p.drawn = target;
      drawn = true;
    }
    if (drawn) tileVersion++;
    // At INTRO_MS everything is established (endIntro closes float remainders).
    if (!pending && t >= INTRO_MS / 1000) endIntro();
  }
  function endIntro() {
    if (!intro) return;
    if (field) {
      for (const p of field.strands) if (p.haze && p.static) eachShift(p, (g) => drawHaze(g, p, p.alpha * fieldAlpha));
      for (const p of intro.list || statics()) {
        const n = p.rasterEnd;
        if (p.drawn < n) {
          const from = p.drawn;
          eachShift(p, (g, yr) => drawRange(g, p, from, n, p.alpha * fieldAlpha, true, yr));
          p.drawn = n;
        }
      }
      tileVersion++;
      c.dataset.tile = "1";
      settled = true;
      lastScroll = NaN;
    }
    intro = null;
    tips = [];
    introEnded = performance.now();
    c.dataset.intro = "done";
  }
  /** Per-strand arcs, boxes, fades and intro offsets, in bounded slices
   * (`yieldTask` ends the current task; parents precede children). */
  async function prepare(yieldTask) {
    let k = 0,
      t = performance.now();
    span = { A: 1, H: 1 };
    rootList = null;
    for (const p of field.strands) {
      if (performance.now() > stageEnd) {
        if (await yieldTask()) return;
        t = performance.now();
      }
      let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
      const arc = new Float32Array(p.points.length);
      for (let i = 0; i < p.points.length; i++) {
        const q = p.points[i];
        if (q.x < x0) x0 = q.x;
        if (q.y < y0) y0 = q.y;
        if (q.x > x1) x1 = q.x;
        if (q.y > y1) y1 = q.y;
        if (i) arc[i] = arc[i - 1] + Math.hypot(q.x - p.points[i - 1].x, q.y - p.points[i - 1].y);
      }
      p.box = [x0 - 14, y0 - 14, x1 + 14, y1 + 14]; // fuzz and knots overhang the polyline
      // r11.2: the mat's haze lobes reach well past its spine (bands that
      // only clipped the polyline box cut the glow off at a band edge).
      if (p.haze)
        for (const l of p.haze) {
          const R1 = l.r * 1.6;
          p.box = [Math.min(p.box[0], l.x - R1), Math.min(p.box[1], l.y - R1), Math.max(p.box[2], l.x + R1), Math.max(p.box[3], l.y + R1)];
        }
      p.arc = arc;
      if (p.endFade?.length) {
        if (!p.fades) p.fades = Float32Array.from(p.points, (q) => endFadeAt(p, q));
      } else p.endFade = null;
      const par = p.introParent;
      p.heroFamily = !!(p.heroFamily || par?.heroFamily);
      p.s0 = par ? (par.s0 ?? 0) + (p.introAt ?? 0) : p.introStart ?? 0;
      // r11.2 (owner: margin mats "pop"): a colony's own tufts start on a time
      // schedule (a few at the centre first, then filling outward by 0.72 of
      // the intro); converted to front distance once span.A is known.
      if (p.tuft && p.marginRoot && !par) {
        const u = Math.min(1, Math.hypot(p.points[0].x - p.marginRoot.x, p.points[0].y - p.marginRoot.y) / (MAT_R * 1.6)),
          hq = hash01(k * 31 + 7);
        p.matKey = Math.min(1, 0.6 * u + 0.4 * hash01(k * 37 + 3)) * (hq < 0.08 ? 0.15 : 1);
        p.introFrac = 0.03 + 0.69 * p.matKey; // replaced below by the ink-ranked schedule
      } else if (!par && !p.introStart && (p.marginRoot || p.runner || p.endFade?.includes(p.points[0]))) {
        // Hyphae, runners and cords leaving a margin colony set off one by one.
        p.introFrac = 0.01 + 0.22 * hash01(k * 41 + 13);
      }
      const reach = p.introFrac !== undefined ? 0 : p.s0 + arc[arc.length - 1];
      // r11.2: each hero-edge family grows on its own clock (see trapEase).
      p.heroRoot = p.heroFamily ? par?.heroRoot || p : null;
      if (p.heroRoot && p.static) p.heroRoot.famReach = Math.max(p.heroRoot.famReach || 1, reach);
      if (p.static) span[p.heroFamily ? "H" : "A"] = Math.max(span[p.heroFamily ? "H" : "A"], reach);
      // Every persistent strand carries fine hairs except the colony tuft hairs (they are the fuzz).
      // Fuzz is generated lazily on the strand's first raster (no start-up long task).
      p.fuzzSeed = p.static && (!p.hair || p.order === 1) && !p.tuft ? k : -1;
      k++;
    }
    // The tufts sit mostly near the centre, so a schedule by radius alone
    // finished 99% of a mat's ink by half the intro. Ranked by the same key, a
    // tuft starts when the tufts before it hold its share of the colony's ink:
    // the mat fills at a steady rate from 0.03 to 0.72 of the intro.
    {
      const byRoot = new Map();
      for (const p of field.strands) if (p.matKey !== undefined) (byRoot.get(p.marginRoot) || byRoot.set(p.marginRoot, []).get(p.marginRoot)).push(p);
      for (const list of byRoot.values()) {
        list.sort((a, b) => a.matKey - b.matKey);
        const ink = (p) => p.arc[p.arc.length - 1] * p.alpha,
          total = list.reduce((n, p) => n + ink(p), 0) || 1;
        let acc = 0;
        for (const p of list) {
          p.introFrac = 0.03 + 0.69 * (acc / total);
          acc += ink(p);
          delete p.matKey;
        }
      }
    }
    // Time-scheduled tufts (and anything rooted on them) take their front offset.
    for (const p of field.strands) {
      const par = p.introParent;
      if (p.introFrac !== undefined) p.s0 = Math.min(span.A - p.arc[p.arc.length - 1], frontEase(p.introFrac) * span.A);
      else if (par && par.introFrac !== undefined) p.s0 = par.s0 + (p.introAt ?? 0);
      else continue;
      if (p.static && !p.heroFamily) span.A = Math.max(span.A, p.s0 + p.arc[p.arc.length - 1]);
    }
    // r11.2 passive life: living tips, whole regrowing laterals and their
    // followers, from per-strand hashes (geometry and RNG streams untouched).
    const routes = new Set(field.marginRoutes.flatMap((r) => (r.wrap ? [...r.paths, r.wrap] : r.paths))),
      anchors = new Set(field.marginKnots);
    for (const p of field.strands) if (p.points[0].owner !== undefined) anchors.add(p.points[0]); // hero vertices
    lifeList = [];
    lifeBins = null;
    lifeNearby = null;
    lifeRows = [NaN, NaN];
    let j = 0;
    for (const p of field.strands) {
      if (performance.now() > stageEnd) {
        if (await yieldTask()) return;
        t = performance.now();
      }
      const n = p.points.length - 1,
        L = p.arc[n],
        id = j++,
        h = (m) => hash01(id * 13 + m * 7919 + 17);
      p.rasterEnd = n;
      p.dyn = null;
      if (p.tuft || routes.has(p)) continue;
      const par = p.introParent;
      let at = p.introAt;
      if (par && at === undefined) {
        const i = par.points.indexOf(p.points[0]);
        at = i >= 0 ? par.arc[i] : 0;
      }
      // Tips cycle every 16-46 s; whole small branches and laterals, which read
      // less, every 40-90 s (fewer changing at once: cheaper life ticks).
      const cycle = (len, slow = false) => {
          const period0 = slow ? LIFE.SLOW_PERIOD + LIFE.SLOW_SPAN * h(4) : LIFE.PERIOD + LIFE.PERIOD_SPAN * h(4),
            y = lifeTiming(len, period0, h(6), h(7), h(8));
          return { ...y, phase: h(5) * y.period };
        };
      if (par?.dyn && at >= (par.dyn.follow ? 0 : par.dyn.base) - 1) p.dyn = { follow: true, parent: par, at, base: 0, len: L };
      else if (!p.static) p.dyn = { base: 0, len: L, ...cycle(L, true) }; // r10 living branches regrow whole
      else if (!p.hair && L >= LIFE.MIN_LEN && h(1) < LIFE.TIP_P) {
        let base = L - Math.min(L * LIFE.TIP_FRAC, LIFE.TIP_MIN + LIFE.TIP_SPAN * h(2));
        // Knots and points a colony or the hero network meets stay in the static base.
        for (const kn of p.knots || []) base = Math.max(base, p.arc[Math.min(n, Math.ceil(kn.i) + 1)] + 4);
        for (let i = 1; i <= n; i++) if (p.arc[i] > base - 4 && anchors.has(p.points[i])) base = Math.max(base, p.arc[i] + 4);
        if (base <= L - 20) p.dyn = { base, len: L - base, ...cycle(L - base) };
      } else if (p.hair && p.order <= 2 && p.static && L >= 8 && h(3) < LIFE.LATERAL_P) p.dyn = { base: 0, len: L, ...cycle(L, true) };
      if (p.dyn) {
        p.rasterEnd = p.dyn.base > 0 ? indexAt(p, p.dyn.base) : 0;
        lifeList.push(p);
      }
    }
  }
  /** Knots never sit under glyphs: any tiled page copy of a knot that falls in
   * a (padded) text line box is skipped, since a bright point there would beat
   * the glyph halo. Recomputed whenever the layout changes. */
  function markKnotsUnderText() {
    const range = document.createRange(),
      rects = [],
      heroTop = scene ? scene.hero.top : 0,
      docH = document.documentElement.scrollHeight;
    for (const root of document.querySelectorAll("main, footer")) {
      const walk = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
      for (let n = walk.nextNode(); n; n = walk.nextNode()) {
        if (!n.nodeValue.trim()) continue;
        range.selectNodeContents(n);
        for (const r of range.getClientRects()) if (r.width > 1) rects.push([r.left - 18, r.top + scrollY - 18, r.right + 18, r.bottom + scrollY + 18]);
      }
    }
    let n = 0;
    for (const k of field.hyphalKnots) {
      k.underText = false;
      for (let m = -2; !k.underText && m <= Math.ceil(docH / field.height) + 2; m++) {
        const py = heroTop + k.y + m * field.height;
        if (py < -50 || py > docH + 50) continue;
        k.underText = rects.some((r) => k.x > r[0] && k.x < r[2] && py > r[1] && py < r[3]);
      }
      n += k.underText;
    }
    return n;
  }
  /** Margin colony tufts as small full-DPR sprites, for compositor-cheap breathing. */
  // r11.4: each margin colony's tufts, indexed once (a filter over every
  // field strand per colony made the first breathing frame after the intro
  // 60-120 ms at 1920 DPR2). Same tufts in the same order.
  let tuftIndex = null;
  function tuftsOf(knot) {
    if (!tuftIndex || tuftIndex.strands !== field.strands) {
      tuftIndex = { strands: field.strands, map: new Map() };
      for (const p of field.strands) {
        if (!p.tuft) continue;
        const l = tuftIndex.map.get(p.marginRoot);
        if (l) l.push(p);
        else tuftIndex.map.set(p.marginRoot, [p]);
      }
    }
    return tuftIndex.map.get(knot) || [];
  }
  function colonySprite(knot) {
    let s = colonySprites.get(knot);
    if (s) return s;
    const tufts = tuftsOf(knot);
    if (!tufts.length) return null;
    let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
    for (const p of tufts) {
      x0 = Math.min(x0, p.box[0]);
      y0 = Math.min(y0, p.box[1]);
      x1 = Math.max(x1, p.box[2]);
      y1 = Math.max(y1, p.box[3]);
    }
    const cv = document.createElement("canvas"),
      cx = cv.getContext("2d");
    cv.width = Math.ceil((x1 - x0) * d);
    cv.height = Math.ceil((y1 - y0) * d);
    cx.setTransform(d, 0, 0, d, -x0 * d, -y0 * d);
    cx.strokeStyle = light ? glow : core;
    cx.globalCompositeOperation = light ? "source-over" : "lighter";
    for (const p of tufts) drawRange(cx, p, 0, p.points.length - 1, p.alpha * fieldAlpha);
    s = { cv, x0, y0, w: x1 - x0, h: y1 - y0, period: 9 + ((knot.x * 7 + knot.y * 13) % 7), phase: (knot.x + knot.y) % 6.28 };
    colonySprites.set(knot, s);
    return s;
  }
  /** A fresh task: attach() runs in short stages (r11.1: one ~55-90 ms task at 1920 before). */
  // r11.4: a paced task, right after a frame (see pacer); prepare's slices
  // end at its deadline (they were a fixed 8 ms).
  const nextTask = () => pacer.slot(0, "attach");
  let stageEnd = 0;
  async function attach() {
    // Each stage (and each prepare slice) is its own task; durations are recorded.
    const stages = (metrics.attachStages = []);
    let t0 = performance.now();
    const labels = (metrics.attachLabels = []);
    const stage = async (label = "prepare") => {
      stages.push(+(performance.now() - t0).toFixed(1));
      labels.push(label);
      stageEnd = await nextTask();
      t0 = performance.now();
      return disposed;
    };
    stageEnd = performance.now() + 4;
    field = scene ? scene.field : field;
    graph = scene ? scene.graph : graph;
    await prepare(stage);
    if (disposed) return;
    if (await stage("prepared")) return;
    const knotsHidden = markKnotsUnderText();
    if (await stage("knots")) return;
    if (intro && !(intro.start !== null && introTime() >= INTRO_MS / 1000)) {
      resetTile();
      intro.list = statics();
      intro.cursor = 0;
      c.dataset.intro = "running";
    } else {
      intro = null;
      c.dataset.intro = "skipped";
      rasterStatic();
    }
    if (await stage("intro")) return;
    heroInterval = scene?.sched ? Math.max(16, ...scene.routes.map((r) => r.arrivalMax + scene.pulseTail + 2)) : 24;
    marginSchedules = field.marginRoutes.map((r, side) => {
      const legs = r.paths.filter(() => field.layout.enabled).map((p) => {
        let distance = 0;
        const segments = p.segments.map((s) => {
          const start = distance;
          distance += s.len;
          return { s, arrival: start / MP.FAST };
        });
        return { p, segments, duration: distance / MP.FAST, leak: null };
      });
      // As the hero: one crest at a time per side (the crest window: the
      // slowest leak's arrival plus the tail); a trip's residue may still be
      // fading when the next one leaves.
      const period = Math.max(heroInterval / MP.RATE, ...legs.map((l) => l.duration + MP.CUTOFF / MP.SLOW + MP.TAIL + 2));
      const landing = scene?.routes?.[0]?.landings.find((l) => l.node === r.source.source)?.time || 0;
      // Never before the first regular hero pulse (the load-in has no pulses).
      const first = (window.MyceliumHero?.firstPulseAt ?? INTRO_MS / 1000 + 2.5 * INTRO_SCALE) + 1.5;
      return { ...r, legs, period, phase: Math.max(first, landing + 3 + 8.5 * (INTRO_SCALE - 1)) + side * (period / 2), chosen: new Map() };
    });
    if (await stage("schedules")) return;
    cascades = buildCascades();
    if (await stage("cascades")) return; // r11.2b: schedules, cascades and leaks in separate tasks (one 30-32 ms stage at 1920)
    buildMarginLeaks();
    if (await stage("leaks")) return;
    const fuzzCount = () => field.strands.reduce((n, p) => n + (p.fuzz ? p.fuzz.length / 7 : 0), 0);
    window.__understoryAudit = {
      paths: field.strands.length, branches: field.strands.filter((p) => !p.static).length, fusions: field.fusions.length, nodes: field.marginKnots.length, sporeNodes: 0,
      densityMultiplier: field.layout.enabled || !field.layout.compact ? 1.2 : 1.35, baseCount: field.baseCount, count: field.count, sharedGraph: !!scene, connectors: 0, canvasCount: 2, flowSources: field.flowSources,
      ...field.rootAudit, explorerLoneThreads: scene?.graph.explorerAudit?.loneThreads ?? 0, explorerAudit: scene?.graph.explorerAudit,
      marginKnots: field.marginKnots.length, marginDensityRatio: field.marginDensityRatio, marginPulseIntensity: MP.GAIN, marginPulseLeak: MP.CUTOFF,
      marginPulsePeriods: marginSchedules.map((s) => s.period), marginPulseRateRelative: marginSchedules.map((s) => heroInterval / s.period), protectedRects: field.layout.rects,
      canvasPixels: live.width * live.height, bands: bands.length, tileScale: Math.ceil(field.width * d) / field.width, dpr: d,
      runners: field.strands.filter((p) => p.runner).length,
      knots: field.hyphalKnots.length, knotsUnderText: knotsHidden, knotGroups: new Set(field.hyphalKnots.map((k) => k.group)).size, knotRadii: field.hyphalKnots.map((k) => +k.r.toFixed(2)),
      introMs: INTRO_MS, columnWeight: column,
    };
    Object.defineProperty(window.__understoryAudit, "fuzz", { enumerable: true, get: fuzzCount });
    // Resident buffer bytes (bands are evicted/re-rastered by viewport distance).
    const bandBytes = () => bands.reduce((n, b) => n + b.cv.width * b.cv.height, 0) * 4;
    Object.defineProperty(window.__understoryAudit, "bandBytes", { enumerable: true, get: bandBytes });
    Object.defineProperty(window.__understoryAudit, "bandBytesMax", { enumerable: true, get: () => Math.ceil(field.width * d) * Math.ceil(field.height * d) * 4 });
    // `bytes` is the fixed upper bound (every band resident, as during the intro);
    // `bytesResident` is what is allocated now.
    Object.defineProperty(window.__understoryAudit, "bytes", { enumerable: true, get: () => (c.width * c.height + live.width * live.height) * 4 + window.__understoryAudit.bandBytesMax });
    Object.defineProperty(window.__understoryAudit, "bytesResident", { enumerable: true, get: () => (c.width * c.height + live.width * live.height) * 4 + bandBytes() });
    // Heavy audits are computed lazily on first read, never during start-up.
    let straightness = null,
      morphology = null;
    Object.defineProperty(window.__understoryAudit, "straightness", { enumerable: true, get: () => (straightness ??= measurePaths(field.strands.filter((p) => !p.hair).map((p) => ({ kind: "understory", points: p.points })))) });
    Object.defineProperty(window.__understoryAudit, "morphology", { enumerable: true, get: () => (morphology ??= auditPaths(field.strands.filter((p) => !p.hair).map((p) => ({ kind: "understory", points: p.points }))).understory) });
    /** r11.1: brightness where the margin cords meet a margin colony, read from
     * the displayed band pixels, against the mat's own peak (the colony's tufts
     * rastered alone with the same renderer). Linear luminance x alpha, mean of
     * 3x3 CSS px windows; junction = windows centred within 8 px of the vertex
     * where the cords end. */
    window.__marginJunctionProbe = () => {
      const lin = (v) => ((v /= 255) <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4);
      const lum = (data, k) => (0.2126 * lin(data[k]) + 0.7152 * lin(data[k + 1]) + 0.0722 * lin(data[k + 2])) * (data[k + 3] / 255);
      const peak = (img, W, H, cx, cy, rad) => {
        const lm = new Float32Array(W * H);
        for (let i = 0; i < W * H; i++) lm[i] = lum(img, i * 4);
        const h = Math.max(1, Math.round(1.5 * d));
        let best = 0;
        for (let y = h; y < H - h; y++)
          for (let x = h; x < W - h; x++) {
            if (rad !== undefined && Math.hypot(x - cx, y - cy) > rad * d) continue;
            let s = 0;
            for (let yy = -h; yy < h; yy++) for (let xx = -h; xx < h; xx++) s += lm[(y + yy) * W + x + xx];
            best = Math.max(best, s / (4 * h * h));
          }
        return best;
      };
      const ends = new Set(field.marginRoutes.flatMap((r) => r.paths.flatMap((p) => [p.points[0], p.points.at(-1)])));
      return field.marginKnots.filter((k) => ends.has(k)).map((k) => {
        const half = 30,
          by = k.y - field.top,
          over = bands.filter((q) => by + half > q.y0 && by - half < q.y0 + q.h);
        if (!over.length || over.some((b) => !b.ready || b.job || !b.cv.width)) return { x: k.x, y: k.y, resident: false };
        const W = Math.round(2 * half * d),
          s = document.createElement("canvas");
        s.width = s.height = W;
        const sx = s.getContext("2d");
        for (const b of over) sx.drawImage(b.cv, -Math.round((k.x - half - field.left) * d), Math.round((b.y0 - (by - half)) * d));
        const shown = sx.getImageData(0, 0, W, W).data;
        s.width = s.height = 0;
        const m = document.createElement("canvas");
        m.width = m.height = W;
        const mx = m.getContext("2d");
        mx.setTransform(d, 0, 0, d, -(k.x - half) * d, -(k.y - half) * d);
        mx.strokeStyle = glow;
        mx.globalCompositeOperation = light ? "source-over" : "lighter";
        for (const p of field.strands) if (p.static && p.tuft && p.marginRoot === k) drawRange(mx, p, 0, p.points.length - 1, p.alpha * fieldAlpha, true);
        const mat = mx.getImageData(0, 0, W, W).data;
        m.width = m.height = 0;
        const c0 = half * d;
        return { x: k.x, y: k.y, resident: true, junction: peak(shown, W, W, c0, c0, 8), shownPeak: peak(shown, W, W), matPeak: peak(mat, W, W), matAtJunction: peak(mat, W, W, c0, c0, 8) };
      });
    };
    /** r11.2 item B (owner: margin strands looked cut off): every strand of
     * margin colony i (its hyphae, runners and cords, not the laterals
     * branching off them; with an end inside the mat) visibly joins the mat.
     * From the displayed band pixels (linear luminance, 3x3 CSS px max): the
     * strand's level is its median 40-64 px from the centre; its rendered
     * start, walking out from the colony end, is the first 1 px sample at
     * >= 50% of that level, which must lie within the mat radius + 2 px, and
     * the straight path from the centre to it may not have a run of more than
     * 4 px below that level. (Same rule as the evidence script radial.py.) */
    window.__colonyGapProbe = (i) => {
      const k = field.marginKnots[i];
      if (!k) return null;
      const half = 70,
        by = k.y - field.top,
        over = bands.filter((q) => by + half > q.y0 && by - half < q.y0 + q.h);
      if (!over.length || over.some((b) => !b.ready || b.job || !b.cv.width)) return { resident: false };
      const W = Math.round(2 * half * d),
        cv = document.createElement("canvas");
      cv.width = cv.height = W;
      const cx = cv.getContext("2d");
      for (const b of over) cx.drawImage(b.cv, -Math.round((k.x - half - field.left) * d), Math.round((b.y0 - (by - half)) * d));
      const img = cx.getImageData(0, 0, W, W).data;
      cv.width = cv.height = 0;
      const lin = (v) => ((v /= 255) <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4);
      const at = (x, y) => {
        let m = 0;
        const px = Math.round((x - k.x + half) * d),
          py = Math.round((y - k.y + half) * d),
          r = Math.max(1, Math.round(1.5 * d));
        for (let yy = py - r; yy <= py + r; yy++)
          for (let xx = px - r; xx <= px + r; xx++) {
            if (xx < 0 || yy < 0 || xx >= W || yy >= W) continue;
            const o = (yy * W + xx) * 4;
            m = Math.max(m, (0.2126 * lin(img[o]) + 0.7152 * lin(img[o + 1]) + 0.0722 * lin(img[o + 2])) * (img[o + 3] / 255));
          }
        return m;
      };
      const hz = field.strands.find((p) => p.haze && p.marginRoot === k),
        matR = hz ? Math.max(...hz.haze.filter((l) => l.r < 20).map((l) => Math.hypot(l.x - k.x, l.y - k.y) + l.r)) : MAT_R,
        routes = field.marginRoutes.flatMap((r) => (r.wrap ? [...r.paths, r.wrap] : r.paths)),
        attached = field.strands.filter((p) => {
          if (p.tuft || p.introParent) return false;
          const d0 = Math.min(Math.hypot(p.points[0].x - k.x, p.points[0].y - k.y), Math.hypot(p.points.at(-1).x - k.x, p.points.at(-1).y - k.y));
          return d0 < matR + 4 && (p.marginRoot === k || p.endFade?.includes(k) || (routes.includes(p) && (p.points[0] === k || p.points.at(-1) === k)));
        });
      const rows = [];
      for (const p of attached) {
        const pts = p.points,
          near = Math.hypot(pts[0].x - k.x, pts[0].y - k.y) <= Math.hypot(pts.at(-1).x - k.x, pts.at(-1).y - k.y),
          seq = near ? pts : [...pts].reverse(),
          samples = [];
        for (let j = 1; j < seq.length; j++) {
          const a = seq[j - 1],
            b = seq[j],
            L = Math.max(Math.hypot(b.x - a.x, b.y - a.y), 1e-6);
          if (Math.hypot(a.x - k.x, a.y - k.y) > half - 4) break;
          for (let u = 0; u < L; u += 1) {
            const x = a.x + ((b.x - a.x) * u) / L,
              y = a.y + ((b.y - a.y) * u) / L;
            samples.push([x, y, Math.hypot(x - k.x, y - k.y), at(x, y)]);
          }
        }
        let outer = samples.filter((q) => q[2] >= 40 && q[2] <= 64).map((q) => q[3]);
        if (!outer.length) outer = samples.slice(-8).map((q) => q[3]);
        outer.sort((a, b) => a - b);
        const level = outer.length ? (outer[(outer.length - 1) >> 1] + outer[outer.length >> 1]) / 2 : 0,
          thr = 0.5 * level,
          start = level > 1e-5 ? samples.find((q) => q[3] >= thr) : null;
        if (!start) {
          rows.push({ invisible: true });
          continue;
        }
        const D = Math.hypot(start[0] - k.x, start[1] - k.y);
        let run = 0,
          gap = 0;
        for (let u = 0; u <= D + 1e-6; u += 1) {
          const f = u / Math.max(D, 1e-6);
          if (at(k.x + (start[0] - k.x) * f, k.y + (start[1] - k.y) * f) >= thr) run = 0;
          else gap = Math.max(gap, ++run);
        }
        rows.push({ startDist: +D.toFixed(1), gap, wrap: !!p.wrap });
      }
      const vis = rows.filter((r) => !r.invisible);
      return {
        resident: true, matR, strands: rows.length, invisible: rows.length - vis.length, maxStartDist: Math.max(0, ...vis.map((r) => r.startDist)),
        maxGap: Math.max(0, ...vis.map((r) => r.gap)), startsBeyondMat: vis.filter((r) => r.startDist > matR + 2).length, rows,
      };
    };
    /** r11.2 load-in probe: share of each margin colony's mat (its tufts,
     * drawn length x alpha) in the bands now, and its glow build-up. */
    const matTufts = new Map(field.marginKnots.map((k) => [k, []]));
    for (const p of field.strands) if (p.tuft && matTufts.has(p.marginRoot)) matTufts.get(p.marginRoot).push(p);
    window.__marginMatProbe = () =>
      field.marginKnots.map((k) => {
        let have = 0,
          all = 0;
        for (const p of matTufts.get(k)) {
          all += p.arc[p.rasterEnd] * p.alpha;
          have += p.arc[Math.min(p.rasterEnd, Math.floor(p.drawn || 0))] * p.alpha;
        }
        return { x: k.x, y: k.y, ink: all ? have / all : 1, glow: intro ? smooth((introTime() / (INTRO_MS / 1000) - 0.04) / 0.7) : 1, tufts: matTufts.get(k).length };
      });
    window.__marginPulseProbe = (t) => {
      const trips = marginTrips(t).filter((s) => !s.residueOnly),
        base = (scene ? scene.hero.top : 0) + Math.max(0, Math.floor((scrollY - (scene ? scene.hero.top : 0) - field.top) / field.height)) * field.height;
      return {
        active: trips.map((s) => s.side), periods: marginSchedules.map((s) => s.period), intensity: MP.GAIN, heroInterval,
        reach: trips.map((s) => s.leg.leak?.maxFine ?? 0), built: marginSchedules.flatMap((s) => s.legs).filter((l) => l.leak).length,
        legs_total: marginSchedules.flatMap((s) => s.legs).length,
        legs: trips.map((s) => ({ side: s.side, y0: base + Math.min(s.leg.p.points[0].y, s.leg.p.points.at(-1).y), y1: base + Math.max(s.leg.p.points[0].y, s.leg.p.points.at(-1).y) })),
      };
    };
    /** r11.2 cascade probe: per side, the hops (page y of each receiving
     * colony, duration, gain), the start times and what is active at time t. */
    window.__cascadeProbe = (t = clock.elapsed) => ({
      built: cascades.every((c) => c.hops.every((h) => h.leak)),
      sides: cascades.map((c) => ({
        side: c.side, starts: c.starts.map((e) => Math.round(e * 1e6) / 1e6), events: c.events, span: c.span, period: c.period,
        hops: c.hops.map((h) => ({ copy: h.copy, nodeY: (scene ? scene.hero.top : 0) + h.nodeCopy * field.height + h.node.y, nodeX: h.node.x, len: Math.round(h.len), dur: +h.dur.toFixed(2), off: +h.off.toFixed(2), gain: +h.gain.toFixed(3), reach: h.leak?.maxFine ?? null, wrap: !!h.p.wrap })),
      })),
      active: cascadeAt(t).map(({ c, age, crest }) => ({ side: c.side, age, crest, hop: c.hops.findLastIndex((h) => age >= h.off) })),
    });
    /** r11.2 life probe: visible arc (px) of strands at time t (static strands
     * report their full length), and the living populations. */
    window.__lifeProbe = {
      field: (list, t) => {
        lifeStamp++;
        return list.map((p) => (p.dyn ? lifeAt(p, t).vis : p.arc[p.arc.length - 1]));
      },
      hero: (list, t) => (scene?.visibleArcs ? scene.visibleArcs(list, t) : list.map((P) => P.len)),
      stats: () => {
        const own = lifeList.filter((p) => !p.dyn.follow),
          strands = field.strands.filter((p) => !p.tuft);
        return {
          living: lifeList.length, strands: strands.length, tips: own.filter((p) => p.dyn.base > 0).length, whole: own.filter((p) => !p.dyn.base).length,
          followers: lifeList.length - own.length, mains: strands.filter((p) => !p.hair).length, livingMains: strands.filter((p) => !p.hair && p.dyn).length,
          tipLen: own.filter((p) => p.dyn.base > 0).map((p) => +p.dyn.len.toFixed(1)), periods: own.map((p) => +p.dyn.period.toFixed(1)), layer: { ...lifeStats },
          // [average, peak] px/s of growth and retreat (trapezoid profile, 15% ease)
          speeds: own.flatMap((p) => [p.dyn.len / p.dyn.g, p.dyn.len / p.dyn.r].map((v) => [+v.toFixed(2), +(v / 0.85).toFixed(2)])),
          hero: scene?.lifeStats ? scene.lifeStats() : null,
        };
      },
    };
    // Diagnostic seeks show the established field (the intro is a load-in only).
    window.__setUnderstoryTime = (t) => {
      endIntro();
      frozen = t;
      schedule();
      paint(t, true);
    };
    if (scene) {
      window.__setPulseTime = (t) => {
        endIntro();
        frozen = t;
        schedule();
        paint(t, true);
      };
      window.__setGrowthAge = (age, { pulses = true } = {}) => {
        scene.startAge = age - (frozen ?? clock.elapsed);
        scene.pulsesOff = !pulses;
        paint(frozen ?? clock.elapsed, true);
      };
    }
    window.__paintUnderstory = (t) => {
      endIntro();
      paint(t, true);
    };
    /** Capture hook: render the field intro at `ms` (forward seeks are incremental). */
    window.__setFieldIntro = (ms, t = ms / 1000) => {
      if (!intro || intro.manual === null || ms < intro.manual) {
        intro = { start: 0, cursor: 0, manual: ms, list: statics() };
        resetTile();
      }
      intro.manual = ms;
      frozen = t;
      introStep(performance.now(), true);
      schedule();
      paint(t, true);
      return { drawn: field.strands.filter((p) => p.static && p.rasterEnd > 0 && p.drawn >= p.rasterEnd).length, statics: intro?.list.length ?? 0, done: !intro };
    };
    // r11.3: the audits and probes above in a task of their own; the fade-in,
    // the attach and the first forced paint in the next (one 33-39 ms stage).
    if (await stage("audit")) return;
    // r11.2c (owner: abrupt load-in): the field layers fade in over 0.9 s when
    // the compositor attaches (they appeared at once with the field fronts
    // already 40-60% out); the hero composite keeps showing as before.
    if (!reduce.matches && !document.hidden)
      for (const el of [bandBox, lifeBox, live]) {
        el.style.opacity = "0";
        el.getBoundingClientRect();
        el.style.transition = "opacity 0.9s ease-out";
        el.style.opacity = "";
      }
    attached = true;
    // r11.4: the breathing sprites, rastered ahead in paced slices (they were
    // all drawn in the first frame after the intro).
    if (!reduce.matches && field.marginKnots.length) {
      let kb = 0;
      const knots = field.marginKnots;
      pacer.add({ prio: 1, name: "colony-sprites", run: (deadline) => {
        if (disposed) return true;
        while (kb < knots.length && performance.now() < deadline) colonySprite(knots[kb++]);
        return kb >= knots.length;
      } });
    }
    lastVersion = -1;
    paint(frozen ?? clock.elapsed, true);
    c.dataset.ready = "1";
    schedule();
    stages.push(+(performance.now() - t0).toFixed(1));
  }
  /** Independent margin trips at time t: the current cycle's crest (at most
   * one per side, never while that side carries a cascade) and the previous
   * cycle's fading residue. */
  function marginTrips(t) {
    const viewTop = scrollY,
      viewBottom = scrollY + innerHeight,
      out = [];
    for (const s of marginSchedules) {
      const elapsed = t - s.phase;
      if (elapsed < 0 || !s.legs.length) continue;
      const cycle = Math.floor(elapsed / s.period);
      for (const cy of [cycle - 1, cycle]) {
        if (cy < 0) continue;
        const age = elapsed - cy * s.period;
        // Pick the leg crossing the current viewport when the trip starts.
        let leg = s.chosen.get(cy);
        if (!leg) {
          const pageY = (q) => (scene ? scene.hero.top : 0) + q.y;
          const k = Math.floor((viewTop - (scene ? scene.hero.top : 0) - field.top) / field.height);
          const shift = Math.max(0, k) * field.height;
          leg = s.legs
            .map((l) => ({ l, score: Math.abs((pageY(l.p.points[0]) + pageY(l.p.points.at(-1))) / 2 + shift - (viewTop + viewBottom) / 2) }))
            .sort((a, b) => a.score - b.score)[0].l;
          s.chosen.set(cy, leg);
          if (s.chosen.size > 8) s.chosen.delete(s.chosen.keys().next().value);
        }
        const crestEnd = leg.leak ? leg.leak.crestEnd : leg.duration + MP.TAIL,
          end = leg.leak ? leg.leak.end : crestEnd;
        if (age >= end) continue;
        // A cascade owns its side: no independent crest overlaps it.
        const start = t - age;
        if (cascadeBusy(s.side, start, start + crestEnd)) continue;
        out.push({ side: s.side, leg, age, leak: leg.leak, gain: mpGain(), p: leg.p, duration: leg.duration, speed: MP.FAST, residueOnly: age >= crestEnd || cy < cycle });
      }
    }
    return out;
  }
  /** Whether a cascade's crest on `side` overlaps [t0, t1]. */
  function cascadeBusy(side, t0, t1) {
    for (const c of cascades) {
      if (c.side !== side) continue;
      for (const S of c.starts)
        for (const m of [-1, 0, 1]) {
          const a = S + m * c.period;
          if (a < t1 + 1 && a + c.span + 1 > t0) return true;
        }
    }
    return false;
  }
  const envelope = (dt) => (dt < 1.4 ? Math.sin(((dt / 1.4) * Math.PI) / 2) ** 2 : Math.cos((((dt - 1.4) / 4.2) * Math.PI) / 2) ** 2) * 0.7;

  // ---- r11.2 passive life ----
  /** Visible arc (px from the strand start) and cycle state of a living strand
   * at time t, memoised per paint (lifeStamp). Followers emerge behind their
   * parent's advancing tip and vanish before it retreats past them. */
  function lifeAt(p, t) {
    const y = p.dyn;
    if (y.stamp === lifeStamp) return y;
    y.stamp = lifeStamp;
    if (reduce.matches) {
      y.vis = y.base + y.len;
      y.state = 1;
    } else if (y.follow) {
      const q = lifeAt(y.parent, t);
      y.vis = Math.max(0, Math.min(y.len, (q.vis - y.at - LIFE.FOLLOW_LAG) * LIFE.FOLLOW_GAIN));
      y.state = q.state;
    } else {
      const cy = tipCycle(t + y.phase, y.period, y.g, y.h, y.r);
      y.vis = y.base + y.len * cy.f;
      y.state = cy.state;
      // During the load-in a tip never runs ahead of its own growth front.
      if (intro && intro.dA !== undefined) y.vis = Math.min(y.vis, (p.heroFamily ? p.heroRoot.front : intro.dA) - p.s0);
    }
    return y;
  }
  const lifeGroups = new Map();
  const lerpPt = (pts, e) => {
    const k = Math.floor(e),
      a = pts[k],
      b = pts[Math.min(pts.length - 1, k + 1)],
      u = e - k;
    return [a.x + (b.x - a.x) * u, a.y + (b.y - a.y) * u];
  };
  /** Adds the arc range [s0, s1] of a strand (and its fine hairs) to quantised
   * stroke groups: the chunk weights, taper and widths of drawRange, so a tip
   * continues its rastered base seamlessly. */
  function lifePiece(p, s0, s1, mul, core = false) {
    if (s1 <= s0 + 0.05) return;
    const pts = p.points,
      n = pts.length - 1,
      i0 = indexAt(p, s0),
      i1 = indexAt(p, s1);
    if (!core && !p.fuzz && p.fuzzSeed >= 0) p.fuzz = makeFuzz(p, p.fuzzSeed);
    const fz = core ? null : p.fuzz;
    let f = 0;
    if (fz) while (f < fz.length && fz[f] < i0) f += 7;
    for (let c0 = Math.floor(i0 / CH) * CH; c0 < i1; c0 += CH) {
      const a = Math.max(c0, i0),
        b = Math.min(c0 + CH, n, i1);
      if (b <= a) continue;
      const mid = pts[Math.min(n, c0 + CH / 2)],
        w = p.margin ? marginWeight(mid.x) : columnWeight(mid.x),
        distal = c0 + CH / 2 > n * 0.5;
      let fade = 1;
      if (p.fades) for (let k = Math.floor(a); k <= Math.min(n, Math.ceil(b)); k++) fade = Math.min(fade, p.fades[k]);
      const ga = p.alpha * fieldAlpha * w * lightMul * (distal ? p.taper : 1) * mul * fade;
      if (ga < 0.004) {
        if (fz) while (f < fz.length && fz[f] < b) f += 7;
        continue;
      }
      const lw = p.width * (distal ? 0.7 : 1),
        key = (core ? 1e7 : 0) + Math.min(999, Math.round(ga * 240)) * 1000 + Math.round(lw * 40);
      let path = lifeGroups.get(key);
      if (!path) lifeGroups.set(key, (path = new Path2D()));
      const ka = Math.floor(a),
        kb = Math.floor(b),
        q0 = lerpPt(pts, a);
      path.moveTo(q0[0], q0[1]);
      for (let k = ka + 1; k <= kb; k++) path.lineTo(pts[k].x, pts[k].y);
      if (b > kb) {
        const q1 = lerpPt(pts, b);
        path.lineTo(q1[0], q1[1]);
      }
      if (fz && f < fz.length && fz[f] < b) {
        const hk = Math.min(999, Math.round(ga * 0.6 * 240)) * 1000 + Math.round(Math.max(0.28, p.width * 0.55) * 40);
        let hp = lifeGroups.get(hk);
        if (!hp) lifeGroups.set(hk, (hp = new Path2D()));
        for (; f < fz.length && fz[f] < b; f += 7) {
          hp.moveTo(fz[f + 1], fz[f + 2]);
          hp.quadraticCurveTo(fz[f + 3], fz[f + 4], fz[f + 5], fz[f + 6]);
        }
      }
    }
  }
  function flushLife(ctx) {
    ctx.lineCap = "butt";
    for (const [key, path] of lifeGroups) {
      ctx.strokeStyle = key >= 1e7 ? core : glow;
      ctx.globalAlpha = Math.floor((key % 1e7) / 1000) / 240;
      ctx.lineWidth = (key % 1000) / 40;
      ctx.stroke(path);
    }
    lifeGroups.clear();
    ctx.globalAlpha = 1;
  }
  const RETREAT_STEPS = [0.75, 0.5, 0.28, 0.1],
    TIP_GLOW = light ? 0.45 : 0.8;
  /** One living strand: the regrowing part, a slight glow at a growing tip,
   * and a die-back that fades from the tip toward the base. */
  function lifeStrand(p, t) {
    const y = lifeAt(p, t),
      s0 = y.follow ? 0 : y.base,
      s1 = y.vis;
    if (s1 <= s0 + 0.05) return false;
    if (y.state === 2) {
      const F = Math.min(y.follow ? 10 : 36, (s1 - s0) * 0.6 + 3),
        q = F / RETREAT_STEPS.length;
      lifePiece(p, s0, s1 - F, 1);
      RETREAT_STEPS.forEach((m, k) => lifePiece(p, s1 - F + k * q, s1 - F + (k + 1) * q, m));
    } else {
      lifePiece(p, s0, s1, 1);
      // A soft glow over the last ~14 px of a growing tip (never a spark).
      if (y.state === 0 && !y.follow && s1 < y.base + y.len - 0.5) {
        lifePiece(p, Math.max(s0, s1 - 14), Math.max(s0, s1 - 6), TIP_GLOW * 0.5, true);
        lifePiece(p, Math.max(s0, s1 - 6), s1, TIP_GLOW, true);
      }
    }
    return true;
  }
  // ---- life tiles ----
  const lifeTiles = new Map(),
    lifePool = [],
    lifeStats = { drawn: 0, ms: 0, redraws: 0, tiles: 0, dirty: 0, maxMs: 0 };
  /** Living strands by 64 px bins of scene y (a tile repaint visits only its
   * bins, not the whole list). */
  let lifeBins = null,
    lifeRanges = new Map();
  function lifeNear(y0, y1) {
    if (!lifeBins) {
      lifeBins = new Map();
      lifeRanges = new Map();
      for (const p of lifeList)
        for (let b = Math.floor(p.box[1] / 64); b <= Math.floor(p.box[3] / 64); b++) (lifeBins.get(b) || lifeBins.set(b, []).get(b)).push(p);
    }
    // Tile rows ask for the same few bin ranges: built once, as arrays.
    const b0 = Math.floor(y0 / 64),
      b1 = Math.floor(y1 / 64),
      key = b0 * 4096 + b1;
    let out = lifeRanges.get(key);
    if (!out) {
      const set = new Set();
      for (let b = b0; b <= b1; b++) for (const p of lifeBins.get(b) || []) set.add(p);
      out = [...set];
      if (lifeRanges.size > 512) lifeRanges.clear();
      lifeRanges.set(key, out);
    }
    return out;
  }
  /** Bounds (scene coords) of the arc range [a0, a1] of a polyline. */
  function arcBox(pts, arcs, a0, a1, pad) {
    let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
    for (let i = 0; i < pts.length; i++) {
      if (arcs[i] < a0 - 2 && (i + 1 >= pts.length || arcs[i + 1] < a0)) continue;
      if (arcs[i] > a1 + 2) break;
      const q = pts[i];
      if (q.x < x0) x0 = q.x;
      if (q.y < y0) y0 = q.y;
      if (q.x > x1) x1 = q.x;
      if (q.y > y1) y1 = q.y;
    }
    return x0 === Infinity ? null : [x0 - pad, y0 - pad, x1 + pad, y1 + pad];
  }
  // Dirty reach behind a changed tip: the retreat fade tail (<= 36 px) while
  // dying back, the tip glow (14 px) otherwise.
  const lifePad = (state) => (state === 2 ? 40 : 18);
  /** Page copies of the field that intersect page range [y0, y1]. */
  function copiesIn(y0, y1) {
    const heroTop = scene ? scene.hero.top : 0,
      out = [];
    const k0 = Math.floor((y0 - heroTop - field.top - field.height) / field.height),
      k1 = Math.floor((y1 - heroTop - field.top) / field.height);
    for (let k = Math.max(scene ? 0 : -1e9, k0); k <= k1; k++) out.push(heroTop + k * field.height);
    return out;
  }
  /** The tile range around the viewport (page coords). */
  const tileRows = (scroll) => [Math.floor((scroll - LIFE_PAD) / LIFE_TILE), Math.floor((scroll + height + LIFE_PAD) / LIFE_TILE)];
  function tileAt(col, row, create) {
    const key = row * 1024 + col;
    let tl = lifeTiles.get(key);
    if (!tl && create) {
      const cv = lifePool.pop() || document.createElement("canvas");
      cv.width = cv.height = 1; // sized by its first paint (if anything lives there)
      cv.style.cssText = `position:absolute;left:${col * LIFE_TILE}px;top:${row * LIFE_TILE}px;width:${LIFE_TILE}px;height:${LIFE_TILE}px`;
      lifeInner.appendChild(cv);
      tl = { cv, cx: cv.getContext("2d"), col, row, x: col * LIFE_TILE, y: row * LIFE_TILE, dirty: true, empty: true };
      lifeTiles.set(key, tl);
    }
    return tl;
  }
  function dropTile(tl) {
    lifeTiles.delete(tl.row * 1024 + tl.col);
    tl.cv.remove();
    if (lifePool.length < 24) lifePool.push(tl.cv);
    else tl.cv.width = tl.cv.height = 0;
  }
  /** Marks every tile a page rectangle touches as dirty. */
  function dirtyRect(x0, y0, x1, y1, r0, r1) {
    const c0 = Math.max(0, Math.floor(x0 / LIFE_TILE)),
      c1 = Math.min(Math.ceil(width / LIFE_TILE) - 1, Math.floor(x1 / LIFE_TILE)),
      ra = Math.max(r0, Math.floor(y0 / LIFE_TILE)),
      rb = Math.min(r1, Math.floor(y1 / LIFE_TILE));
    for (let r = ra; r <= rb; r++) for (let cc = c0; cc <= c1; cc++) tileAt(cc, r, true).dirty = true;
  }
  /** Repaints one tile: every living strand (each field copy, the hero) that
   * overlaps it, clipped to the tile. */
  function paintTile(tl, t, A) {
    const g = tl.cx,
      X0 = tl.x,
      Y0 = tl.y,
      X1 = X0 + LIFE_TILE,
      Y1 = Y0 + LIFE_TILE;
    const full = Math.ceil(LIFE_TILE * d);
    // Tiles no living strand reaches stay 1x1 placeholders for good; the rest
    // keep their backing store (re-allocating a tile whenever its strands
    // had all died back cost more than the paint).
    if (tl.cands === undefined) {
      tl.cands = false;
      for (const base of copiesIn(Y0, Y1))
        for (const p of lifeNear(Y0 - base, Y1 - base)) {
          const b = p.box;
          if (!(b[2] < X0 || b[0] > X1 || b[3] + base < Y0 || b[1] + base > Y1)) tl.cands = true;
        }
      if (scene?.lifeChanges && scene.hero.top + scene.hero.H > Y0 && scene.hero.top < Y1) tl.cands = true;
    }
    if (!tl.cands) {
      tl.dirty = false;
      tl.empty = true;
      tl.cv.width = tl.cv.height = 1;
      tl.cv.style.visibility = "hidden";
      return 0;
    }
    if (tl.cv.width !== full) tl.cv.width = tl.cv.height = full;
    g.setTransform(1, 0, 0, 1, 0, 0);
    g.globalAlpha = 1;
    g.globalCompositeOperation = "source-over";
    g.clearRect(0, 0, full, full);
    g.globalCompositeOperation = light ? "source-over" : "lighter";
    let drawn = 0;
    for (const base of copiesIn(Y0, Y1)) {
      for (const p of lifeNear(Y0 - base, Y1 - base)) {
        const b = p.box;
        if (b[2] < X0 || b[0] > X1 || b[3] + base < Y0 || b[1] + base > Y1) continue;
        drawn += lifeStrand(p, t);
      }
      g.setTransform(d, 0, 0, d, -X0 * d, (base - Y0) * d);
      flushLife(g);
    }
    if (scene?.drawLife) {
      const top = scene.hero.top;
      if (top + scene.hero.H > Y0 && top < Y1) {
        g.setTransform(d, 0, 0, d, -X0 * d, (top - Y0) * d);
        g.lineCap = "butt";
        drawn += scene.drawLife(g, t, A, X0, X1, Y0 - top, Y1 - top);
      }
    }
    tl.dirty = false;
    tl.empty = !drawn;
    tl.cv.style.visibility = tl.empty ? "hidden" : "";
    return drawn;
  }
  /** One life tick: the visible arc of every living strand near the viewport
   * is compared with what its tiles show; the tiles a change touches repaint.
   * `all` repaints every tile in range (seeks, switches). */
  function lifeTick(t, A, scroll, all = false) {
    const t0 = performance.now(),
      [r0, r1] = tileRows(scroll),
      cols = Math.ceil(width / LIFE_TILE);
    lifeStamp++;
    // Tiles leaving the range are released; new rows start dirty.
    if (r0 !== lifeRows[0] || r1 !== lifeRows[1]) {
      for (const tl of [...lifeTiles.values()]) if (tl.row < r0 || tl.row > r1) dropTile(tl);
      lifeRows = [r0, r1];
      lifeNearby = null;
    }
    const fresh = [];
    for (let r = r0; r <= r1; r++) if (!lifeTiles.has(r * 1024)) fresh.push(r);
    const Y0 = r0 * LIFE_TILE,
      Y1 = (r1 + 1) * LIFE_TILE,
      bases = copiesIn(Y0, Y1);
    const mark = (box, base, a, b) => {
      if (box && b !== undefined) dirtyRect(box[0], box[1] + base, box[2], box[3] + base, r0, r1);
    };
    if (!lifeNearby) {
      const set = new Set();
      for (const base of bases) for (const p of lifeNear(Y0 - base, Y1 - base)) set.add(p);
      lifeNearby = [...set];
    }
    for (const p of lifeNearby) {
      const y = lifeAt(p, t),
        s0 = y.follow ? 0 : y.base,
        last = p.lifeShown;
      if (last && !all && Math.abs(last.vis - y.vis) < LIFE_STEP && last.state === y.state) continue;
      const lo = Math.min(last ? last.vis : s0, y.vis) - Math.max(lifePad(y.state), last ? lifePad(last.state) : 40),
        hi = Math.max(last ? last.vis : y.vis, y.vis) + 4;
      if (last) {
        last.vis = y.vis;
        last.state = y.state;
      } else p.lifeShown = { vis: y.vis, state: y.state };
      if (all) continue;
      const box = arcBox(p.points, p.arc, Math.max(0, lo), hi, 14); // fuzz hairs overhang the polyline
      for (const base of bases) mark(box, base, lo, hi);
    }
    if (scene?.lifeChanges) {
      const top = scene.hero.top;
      if (top + scene.hero.H > Y0 && top < Y1) for (const b of scene.lifeChanges(t, A, all)) dirtyRect(b[0], b[1] + top, b[2], b[3] + top, r0, r1);
    }
    // Rows that just came into range: every column that has life repaints.
    for (const r of all ? Array.from({ length: r1 - r0 + 1 }, (_, i) => r0 + i) : fresh) for (let cc = 0; cc < cols; cc++) tileAt(cc, r, true).dirty = true;
    // Visible tiles first; a live tick stops after ~6 ms (the rest repaint on
    // the next frames), diagnostic seeks (frozen clock) repaint everything.
    let n = 0,
      drawn = 0;
    const order = [...lifeTiles.values()].filter((tl) => tl.dirty);
    order.sort((a, b) => Math.abs(a.y + LIFE_TILE / 2 - scroll - height / 2) - Math.abs(b.y + LIFE_TILE / 2 - scroll - height / 2));
    for (const tl of order) {
      if (frozen === null && n && performance.now() - t0 > 6) break;
      drawn += paintTile(tl, t, A);
      n++;
    }
    lifePending = order.length > n;
    lifeStats.drawn = drawn;
    lifeStats.dirty = n;
    lifeStats.tiles = lifeTiles.size;
    lifeStats.ms = performance.now() - t0;
    lifeStats.maxMs = Math.max(lifeStats.maxMs, lifeStats.ms);
    lifeStats.redraws++;
  }
  function placeLife(scroll) {
    const ty = -Math.round(scroll * d) / d;
    if (ty !== lifeY) {
      lifeInner.style.transform = `translate3d(0,${ty}px,0)`;
      lifeY = ty;
    }
  }

  // ---- r11.2 margin pulses and cascades: leak routes built in idle slices ----
  const MP_BLOOM = [0, 0.2, 0.32, 0.44, 0.56, 0.68, 0.8, 0.92, 1.05, 1.2, 1.4];
  /** Cascade (owner): a hero pulse relays colony by colony down a side of the
   * page. Hops take 1.5-3 s (HOP_V px/s, clamped), each colony re-emits after
   * RELAY s, brightness tapers by TAPER per hop to at least FLOOR, the leak is
   * shorter than an independent margin pulse (CUTOFF px of fine distance). */
  const CAS = Object.freeze({ HOP_V: 260, HOP_MIN: 1.5, HOP_MAX: 3, RELAY: 0.45, SLOW: 30, CUTOFF: 340, LAMBDA: 170, TAPER: 0.965, FLOOR: 0.62, EVERY: 2 });
  const LEAK_MP = Object.freeze({ fast: MP.FAST, slow: MP.SLOW, cutoff: MP.CUTOFF, lambda: MP.LAMBDA });
  const boxAdd = (b, x0, y0, x1, y1) => {
    if (x0 < b[0]) b[0] = x0;
    if (y0 < b[1]) b[1] = y0;
    if (x1 > b[2]) b[2] = x1;
    if (y1 > b[3]) b[3] = y1;
  };
  /** Dijkstra from a leg's cord through the field graph (`o.fast` px/s on the
   * cord, `o.slow` through fine threads, cut off at `o.cutoff` px of fine
   * distance), then the lit segments as cached vector groups with their
   * bounds: 0.3 s arrival buckets x amplitude level x (main | hair) for the
   * crest, 4 s bands x near/far for the residue; living (cycling) parts stay
   * per-frame. Returns the leak. */
  function* leakLeg(leg, o = LEAK_MP) {
    const { vs, ss } = graph,
      at = new Map(),
      fine = new Map(),
      heap = [],
      onLeg = new Set(leg.p.segments),
      push = (v) => {
        let i = heap.length;
        heap.push(v);
        while (i) {
          const q = (i - 1) >> 1;
          if (heap[q][0] <= v[0]) break;
          heap[i] = heap[q];
          i = q;
        }
        heap[i] = v;
      },
      pop = () => {
        const top = heap[0],
          last = heap.pop();
        if (heap.length) {
          let i = 0;
          while (i * 2 + 1 < heap.length) {
            let k = i * 2 + 1;
            if (k + 1 < heap.length && heap[k + 1][0] < heap[k][0]) k++;
            if (heap[k][0] >= last[0]) break;
            heap[i] = heap[k];
            i = k;
          }
          heap[i] = last;
        }
        return top;
      };
    let dist = 0;
    leg.p.points.forEach((v, j) => {
      if (j) dist += Math.hypot(v.x - leg.p.points[j - 1].x, v.y - leg.p.points[j - 1].y);
      at.set(v.id, dist / o.fast);
      fine.set(v.id, 0);
      push([dist / o.fast, v.id]);
    });
    const duration = dist / o.fast;
    let pops = 0;
    while (heap.length) {
      const [time, id] = pop();
      if (++pops % 4000 === 0) yield "leak";
      if (time > at.get(id) + 1e-5) continue;
      const v = vs[id];
      for (const si of v.edges) {
        const sg = ss[si];
        if (!sg.field) continue;
        const q = sg.a === v ? sg.b : sg.a,
          cord = onLeg.has(sg),
          dd = cord ? 0 : fine.get(id) + sg.len,
          next = time + sg.len / (cord ? o.fast : o.slow);
        if (dd > o.cutoff || next >= (at.get(q.id) ?? Infinity)) continue;
        at.set(q.id, next);
        fine.set(q.id, dd);
        push([next, q.id]);
      }
    }
    const buckets = new Map(),
      residue = new Map(),
      late = [];
    let arrivalMax = 0,
      maxFine = 0,
      count = 0,
      n = 0;
    const group = (map, key, make) => map.get(key) || (map.set(key, make()), map.get(key));
    for (const sg of ss) {
      if (++n % 6000 === 0) yield "leak-list";
      if (!sg.field) continue;
      const ta = at.get(sg.a.id),
        tb = at.get(sg.b.id);
      if (ta === undefined && tb === undefined) continue;
      const a = ta === undefined || (tb !== undefined && tb < ta) ? sg.b : sg.a,
        cord = onLeg.has(sg),
        dd = fine.get(a.id) + (cord ? 0 : sg.len / 2),
        p = sg.fieldPath;
      if (dd > o.cutoff || !p) continue;
      const mx = (sg.a.x + sg.b.x) / 2,
        my = (sg.a.y + sg.b.y) / 2,
        w = (p.margin ? marginWeight(mx) : columnWeight(mx)) * (p.endFade ? endFadeAt(p, { x: mx, y: my }) : 1),
        amp = cord ? w : Math.exp(-dd / o.lambda) * w;
      if (amp < 0.05) continue;
      const arrival = at.get(a.id) + sg.len / (cord ? o.fast : o.slow) / 2,
        main = !p.hair && !cord ? 1 : 0,
        x0 = Math.min(sg.a.x, sg.b.x) - 3,
        y0 = Math.min(sg.a.y, sg.b.y) - 3,
        x1 = Math.max(sg.a.x, sg.b.x) + 3,
        y1 = Math.max(sg.a.y, sg.b.y) + 3;
      count++;
      arrivalMax = Math.max(arrivalMax, arrival);
      maxFine = Math.max(maxFine, dd);
      const living = p.dyn && p.arc[Math.min(p.arc.length - 1, (sg.fieldIndex || 0) + 1)] > (p.dyn.follow ? 0 : p.dyn.base) + 0.5;
      if (living) {
        late.push({ s: sg, p, i: sg.fieldIndex || 0, arrival, amp, main });
        continue;
      }
      const level = cord ? 5 : Math.min(4, Math.floor(amp * 5)),
        bucket = group(buckets, Math.floor(arrival / MP.STEP), () => new Map()),
        it = group(bucket, level * 2 + main, () => ({ path: new Path2D(), amp: cord ? amp : (level + 0.5) / 5, cord, main, box: [Infinity, Infinity, -Infinity, -Infinity] }));
      it.path.moveTo(sg.a.x, sg.a.y);
      it.path.lineTo(sg.b.x, sg.b.y);
      boxAdd(it.box, x0, y0, x1, y1);
      if (!cord) {
        const far = dd >= 160 ? 1 : 0,
          band = Math.floor(arrival / MP.RSTEP),
          layer = group(residue, band * 2 + far, () => ({ arrival: (band + 0.5) * MP.RSTEP, far, items: new Map(), box: [Infinity, Infinity, -Infinity, -Infinity] })),
          rl = Math.min(2, Math.floor(amp * 3)),
          ri = group(layer.items, rl * 2 + main, () => ({ path: new Path2D(), amp: (rl + 0.6) / 3, main }));
        ri.path.moveTo(sg.a.x, sg.a.y);
        ri.path.lineTo(sg.b.x, sg.b.y);
        boxAdd(layer.box, x0, y0, x1, y1);
      }
    }
    late.sort((x, y) => x.arrival - y.arrival);
    const res = [...residue.values()].sort((x, y) => x.arrival - y.arrival);
    return {
      buckets, residue: res, late, arrivalMax, maxFine, count, duration, crestEnd: Math.max(duration, arrivalMax) + MP.TAIL,
      end: Math.max(arrivalMax + MP.TAIL, arrivalMax + MP.RES_HOLD + arrivalMax * MP.RES_BACK + MP.RES_FADE, duration + 7),
    };
  }
  /** Arrival bloom of a margin colony: its own felt and hyphae in radial bands
   * (computed once per colony). */
  const blooms = new Map();
  function bloomFor(node) {
    if (blooms.has(node)) return blooms.get(node);
    const bands = MP_BLOOM.slice(0, -1).map((r0, k) => ({ r0, r1: MP_BLOOM[k + 1], groups: new Map() })),
      R = MP.BLOOM_R * 1.4;
    for (const p of field.strands) {
      if (p.box[0] > node.x + R || p.box[2] < node.x - R || p.box[1] > node.y + R || p.box[3] < node.y - R) continue;
      const pts = p.points,
        base = p.alpha * fieldAlpha * marginWeight(node.x);
      for (let i = 1; i < pts.length; i++) {
        if (p.rasterEnd !== undefined && i > p.rasterEnd) break;
        const q = pts[i],
          r = Math.hypot(q.x - node.x, q.y - node.y) / MP.BLOOM_R;
        if (r >= 1.4) continue;
        let bi = 0;
        while (bi < MP_BLOOM.length - 2 && r >= MP_BLOOM[bi + 1]) bi++;
        const al = Math.min(15, Math.round(Math.min(1, base * 1.4) * 15)),
          kw = Math.round(((p.width || 0.4) + 0.15) * 5);
        if (!al) continue;
        const g = bands[bi].groups,
          key = al * 64 + kw;
        if (!g.has(key)) g.set(key, new Path2D());
        g.get(key).moveTo(pts[i - 1].x, pts[i - 1].y);
        g.get(key).lineTo(q.x, q.y);
      }
    }
    blooms.set(node, bands);
    return bands;
  }
  /** Leaks are built in idle slices: independent legs first, then cascade hops. */
  function buildMarginLeaks() {
    const jobs = [];
    const compactLeak = field.layout.enabled ? null : COMPACT_LEAK;
    for (const s of marginSchedules) for (const l of s.legs) jobs.push(() => leakLeg(l, compactLeak ? { ...LEAK_MP, ...compactLeak } : LEAK_MP));
    for (const c of cascades)
      for (const h of c.hops) {
        jobs.push(function* () {
          const key = h.p.id + ":" + h.dur.toFixed(3);
          h.leak = c.cache.get(key) || (yield* leakLeg({ p: h.p }, { fast: h.speed, slow: CAS.SLOW, cutoff: CAS.CUTOFF, lambda: CAS.LAMBDA, ...compactLeak }));
          c.cache.set(key, h.leak);
          return h.leak;
        });
      }
    let i = 0,
      gen = null,
      owner = null;
    const legs = marginSchedules.flatMap((s) => s.legs);
    let kb = 0;
    // r11.4: paced slices of at most 6 ms (see pacer).
    pacer.add({ prio: 1, name: "margin-legs", run: (deadline) => {
      if (disposed) return true;
      const t0 = performance.now(),
        end = Math.min(deadline, t0 + 6);
      while (performance.now() < end) {
        if (!gen) {
          if (i >= jobs.length) {
            // r11.3: the margin blooms in 6 ms slices too (all at once was
            // one 70 ms task at 1920 DPR2, inside the intro on a fast host).
            const knots = field.marginKnots;
            while (kb < knots.length && performance.now() < end) bloomFor(knots[kb++]);
            return kb >= knots.length;
          }
          owner = i < legs.length ? legs[i] : null;
          gen = jobs[i++]();
          if (!gen.next) gen = null;
          continue;
        }
        const r = gen.next();
        if (r.done) {
          if (owner) owner.leak = r.value;
          gen = null;
        }
      }
      return false;
    } });
  }
  /** Cascade routes and start times per side (deterministic): hops follow the
   * side's cords colony by colony down every page copy to the page bottom; a
   * cascade starts on every CAS.EVERY-th hero pulse reaching or leaving the
   * side's hero colony, never overlapping the previous one on that side. */
  function buildCascades() {
    if (!scene?.sched || !field.layout.enabled) return [];
    const docH = document.documentElement.scrollHeight,
      heroTop = scene.hero.top,
      copies = Math.max(1, Math.ceil((docH - heroTop - field.top) / field.height)),
      P = scene.sched.period,
      first = window.MyceliumHero?.firstPulseAt ?? INTRO_MS / 1000 + 2.5 * INTRO_SCALE;
    return field.marginRoutes.map((r, side) => {
      const src = r.source,
        lead = r.paths[0] && r.paths[0].points[0] === src ? r.paths[0] : null,
        tile = r.paths.filter((p) => p !== lead),
        hops = [],
        pageY = (c, q) => heroTop + c * field.height + q.y;
      const push = (p, copy, node, nodeCopy) => {
        const len = p.arc ? p.arc[p.arc.length - 1] : p.segments.reduce((n, sg) => n + sg.len, 0),
          dur = Math.max(CAS.HOP_MIN, Math.min(CAS.HOP_MAX, len / CAS.HOP_V));
        hops.push({ p, copy, node, nodeCopy, len, dur, speed: len / dur, leak: null, off: 0, gain: 0 });
      };
      outer: for (let c = 0; c < copies; c++) {
        if (c === 0 && lead && field.layout.enabled) push(lead, 0, lead.points.at(-1), 0);
        for (const p of tile) {
          if (pageY(c, p.points.at(-1)) > docH - 40) break outer;
          push(p, c, p.points.at(-1), c);
        }
        if (!r.wrap || pageY(c + 1, r.knots[0]) > docH - 40) break;
        push(r.wrap, c, r.knots[0], c + 1);
      }
      let off = 0;
      hops.forEach((h, i) => {
        h.off = off;
        h.gain = mpGain() * Math.max(CAS.FLOOR, Math.pow(CAS.TAPER, i));
        off += h.dur + CAS.RELAY;
      });
      const span = hops.length ? hops.at(-1).off + hops.at(-1).dur + MP.TAIL : 0,
        events = [];
      for (const tr of scene.sched.trips) {
        if (tr.route.from === src.source) events.push(tr.start);
        for (const l of tr.route.landings) if (l.node === src.source && l.time >= 1.5) events.push(tr.start + l.time);
      }
      events.sort((a, b) => a - b);
      const starts = [];
      let n = 0;
      for (const e of events) {
        if (e < first || (starts.length && e < starts.at(-1) + span + 2)) continue;
        if (n++ % CAS.EVERY === side % CAS.EVERY) starts.push(e);
      }
      if (!starts.length && hops.length) for (let e = first + 6 + side * 20; e < P - span; e += 3 * heroInterval) starts.push(e);
      while (starts.length > 1 && starts.at(-1) + span + 2 > starts[0] + P) starts.pop(); // the schedule repeats every P
      return { side: r.side, hops, starts, events: [...new Set(events.map((e) => Math.round(e * 1e6) / 1e6))], span, period: P, cache: new Map() };
    });
  }
  /** Active cascades at time t: [{ c, age }] (age since it left the hero). */
  function cascadeAt(t, side = null) {
    const out = [];
    for (const c of cascades) {
      if (side !== null && c.side !== side) continue;
      const tt = ((t % c.period) + c.period) % c.period;
      for (const S of c.starts)
        for (const age of [tt - S, tt + c.period - S]) {
          const end = c.hops.reduce((m, h) => Math.max(m, h.off + (h.leak ? h.leak.end : h.dur + 12)), c.span);
          if (age >= 0 && age < end && t - age >= 0) out.push({ c, age, crest: age < c.span });
        }
    }
    return out;
  }
  const mpResidue = (leak, layer, age) => {
    const rise = smooth((age - layer.arrival - 0.6) / 2.6);
    if (rise <= 0) return 0;
    const F = leak.arrivalMax + MP.RES_HOLD + (leak.arrivalMax - layer.arrival) * MP.RES_BACK - layer.far * MP.RES_FAR;
    return MP.RES * rise * (1 - smooth((age - F) / MP.RES_FADE));
  };
  /** One margin trip or cascade hop on the live layer (scene coordinates of
   * its field copy): residue, crest, living parts, route knots.
   * trip = { leak, age, gain, p, duration, speed, residueOnly } */
  function drawMarginTrip(trip, t, add, shift) {
    const leak = trip.leak,
      age = trip.age,
      G = trip.gain * (light ? 0.4 : 1),
      dirty = (b) => b[0] < Infinity && add(b[0], b[1] + shift, b[2], b[3] + shift);
    lx.lineCap = "butt";
    if (!leak) {
      if (trip.residueOnly) return;
      // Not built yet: the crest along the cord only.
      lx.strokeStyle = core;
      let dist = 0;
      for (const sg of trip.p.segments) {
        const dt = age - dist / trip.speed;
        dist += sg.len;
        if (dt < 0 || dt > MP.TAIL) continue;
        lx.globalAlpha = envelope(dt) * G * 0.85 * marginWeight((sg.a.x + sg.b.x) / 2);
        lx.lineWidth = 1.1;
        lx.beginPath();
        lx.moveTo(sg.a.x, sg.a.y);
        lx.lineTo(sg.b.x, sg.b.y);
        lx.stroke();
        add(Math.min(sg.a.x, sg.b.x) - 3, Math.min(sg.a.y, sg.b.y) + shift - 3, Math.max(sg.a.x, sg.b.x) + 3, Math.max(sg.a.y, sg.b.y) + shift + 3);
      }
      return;
    }
    // Residue: what the pulse lit lingers faintly, then retreats far-edge first.
    lx.strokeStyle = glow;
    for (const layer of leak.residue) {
      const a = mpResidue(leak, layer, age);
      if (a < 0.006) continue;
      for (const it of layer.items.values()) {
        lx.globalAlpha = Math.min(1, a * it.amp * (light ? 0.5 : 0.9) * trip.gain);
        lx.lineWidth = it.main ? 0.7 : 0.5;
        lx.stroke(it.path);
      }
      dirty(layer.box);
    }
    if (trip.residueOnly) return;
    // The crest: rolling arrival buckets (cord body + core, fine leak).
    const lo = Math.max(0, Math.floor((age - MP.TAIL) / MP.STEP)),
      hi = Math.floor(age / MP.STEP);
    for (let i = lo; i <= hi; i++) {
      const bucket = leak.buckets.get(i);
      if (!bucket) continue;
      const dt = age - (i + 0.5) * MP.STEP;
      if (dt < 0 || dt > MP.TAIL) continue;
      const env = envelope(dt) * (trip.cascade ? 1 : Math.exp((-i * MP.STEP * MP.FAST) / 2000));
      if (env < 0.012) continue;
      for (const it of bucket.values()) {
        if (it.cord) {
          lx.strokeStyle = core;
          lx.globalAlpha = Math.min(1, env * (light ? 0.1 : 0.22) * G * it.amp);
          lx.lineWidth = 4.2;
          lx.stroke(it.path);
          lx.globalAlpha = Math.min(1, env * (light ? 0.34 : 0.85) * G * it.amp);
          lx.lineWidth = 1.1;
          lx.stroke(it.path);
        } else {
          lx.strokeStyle = glow;
          lx.globalAlpha = Math.min(1, env * it.amp * 0.85 * G * (it.main ? 1.25 : 1));
          lx.lineWidth = it.main ? 0.85 : 0.55;
          lx.stroke(it.path);
        }
        dirty(it.box);
      }
    }
    // Living parts glow only where drawn now.
    for (const it of leak.late) {
      const dt = age - it.arrival;
      if (dt > MP.TAIL) continue;
      if (dt < 0) break;
      if (!segmentShown(it.p, it.i, t)) continue;
      const ga = Math.min(1, envelope(dt) * it.amp * 0.8 * G),
        key = 1e7 + Math.round(ga * 240) * 1000 + Math.round((it.main ? 0.85 : 0.55) * 40);
      if (ga < 0.004) continue;
      let path = lifeGroups.get(key);
      if (!path) lifeGroups.set(key, (path = new Path2D()));
      path.moveTo(it.s.a.x, it.s.a.y);
      path.lineTo(it.s.b.x, it.s.b.y);
      add(Math.min(it.s.a.x, it.s.b.x) - 3, Math.min(it.s.a.y, it.s.b.y) + shift - 3, Math.max(it.s.a.x, it.s.b.x) + 3, Math.max(it.s.a.y, it.s.b.y) + shift + 3);
    }
    flushLife(lx);
    // Knots on the route brighten as the crest passes.
    for (const kn of trip.p.knots || []) {
      const dt = age - (trip.p.arc?.[kn.i] ?? 0) / trip.speed;
      if (dt < 0 || dt > MP.TAIL) continue;
      drawKnot(lx, kn, envelope(dt) * G * 0.85 * marginWeight(kn.x) * (trip.p.endFade ? endFadeAt(trip.p, kn) : 1), 1.4);
      add(kn.x - 8, kn.y + shift - 8, kn.x + 8, kn.y + shift + 8);
    }
  }
  /** Arrival bloom: a narrow ring travelling from the colony core out to its rim. */
  function drawBloom(node, landed, gain, add, shift) {
    if (landed < 0 || landed >= 7) return;
    const bands = bloomFor(node),
      front = 0.2 + (landed / 4.8) * 1.25;
    lx.strokeStyle = core;
    for (const b of bands) {
      if (!b.r0) continue;
      const mid = (b.r0 + b.r1) / 2,
        g = Math.exp(-(((mid - front) / 0.16) ** 2)) * smooth((landed - 0.15) / 0.6) * (0.55 + 0.45 * Math.min(1, mid)) * smooth((7 - landed) / 2);
      if (g < 0.02) continue;
      for (const [key, path] of b.groups) {
        lx.globalAlpha = Math.min(1, (Math.floor(key / 64) / 15) * g * (light ? 0.34 : 0.7) * gain);
        lx.lineWidth = (key % 64) / 5;
        lx.stroke(path);
      }
    }
    add(node.x - MP.BLOOM_R * 1.5, node.y + shift - MP.BLOOM_R * 1.5, node.x + MP.BLOOM_R * 1.5, node.y + shift + MP.BLOOM_R * 1.5);
  }
  /** Everything margin-pulse related for page copy k (independent trips and cascades). */
  function drawMarginCopy(k, t, add, shift) {
    for (const trip of marginTrips(t)) {
      drawMarginTrip(trip, t, add, shift);
      const node = trip.p.points.at(-1);
      if (!trip.residueOnly && field.marginKnots.includes(node)) drawBloom(node, trip.age - trip.duration, mpGain(), add, shift);
    }
    for (const { c, age } of cascadeAt(t))
      for (const h of c.hops) {
        const ha = age - h.off;
        if (ha < 0) break;
        if (h.copy === k && ha < (h.leak ? h.leak.end : h.dur + MP.TAIL))
          drawMarginTrip({ leak: h.leak, age: ha, gain: h.gain, p: h.p, duration: h.dur, speed: h.speed, cascade: true, residueOnly: ha > (h.leak ? h.leak.crestEnd : h.dur + MP.TAIL) }, t, add, shift);
        if (h.nodeCopy === k) drawBloom(h.node, ha - h.dur, h.gain, add, shift);
      }
  }
  /** Whether segment i of a field strand is drawn now (static base grown, or
   * inside the living tip's visible extent). */
  function segmentShown(p, i, t) {
    const end = p.arc[Math.min(p.arc.length - 1, i + 1)];
    if (p.dyn && end > (p.dyn.follow ? 0 : p.dyn.base) + 0.5) return lifeAt(p, t).vis >= end;
    return !(intro && p.drawn < i + 1);
  }
  function paint(T, force = false) {
    if (disposed || !attached) return;
    const begin = performance.now(),
      reduced = reduce.matches,
      t = reduced ? 32 : T,
      A = scene ? (reduced ? 1e5 : (scene.ageAt ? scene.ageAt(t) : scene.startAge + t)) : 0,
      scroll = scrollY,
      heroTop = scene ? scene.hero.top : 0,
      oy = heroTop - scroll; // page-space origin of scene coordinates
    let heroCost = 0;
    lifeStamp++;
    if (intro && (reduced || document.hidden)) endIntro();
    if (intro && intro.manual === null) introStep(begin);
    // Bakes are batched to 4 s while running (one static redraw per batch at most).
    if (scene?.bakeUntil) frozen !== null || reduced ? scene.bakeUntil(A) : scene.bakeUntil(Math.floor(A / 4) * 4, true);
    // ---- static layers: scroll only moves them; the hero box redraws on bakes ----
    const version = scene ? scene.hero.version : 0;
    if (scene && (force || version !== lastVersion)) {
      drawHero();
      lastVersion = version;
      metrics.staticRedraws++;
    }
    if (force || scroll !== lastScroll) {
      placeBands(oy + field.top);
      if (scene) placeHero(oy);
      lastScroll = scroll;
    }
    // ---- life layer (r11.2): page-anchored tiles; a tick repaints what changed ----
    if (force || intro || lifePending || scroll !== lifeScroll || begin - lifeDrawn >= 1000 / LIFE_HZ - 2) {
      lifeTick(t, A, scroll, force);
      lifeDrawn = begin;
      lifeScroll = scroll;
    }
    placeLife(scroll);
    // ---- live layer ----
    const lb = performance.now();
    // Dirty-rect live layer: clear only what was drawn last frame.
    const box = [Infinity, Infinity, -Infinity, -Infinity];
    const add = (x0, y0, x1, y1) => {
      if (x0 < box[0]) box[0] = x0;
      if (y0 < box[1]) box[1] = y0;
      if (x1 > box[2]) box[2] = x1;
      if (y1 > box[3]) box[3] = y1;
    };
    if (force) lx.clearRect(0, 0, width, height);
    else if (dirty) lx.clearRect(dirty[0] - 2, dirty[1] - 2, dirty[2] - dirty[0] + 4, dirty[3] - dirty[1] + 4);
    lx.globalCompositeOperation = light ? "source-over" : "lighter";
    let liveStats = null;
    if (scene?.drawLive && oy < height && oy + scene.hero.H > 0) {
      const h0 = performance.now();
      lx.save();
      lx.translate(0, oy);
      const hb = [Infinity, Infinity, -Infinity, -Infinity];
      liveStats = scene.drawLive(lx, scene.pulsesOff || reduced ? -1e6 : t, A, hb);
      lx.restore();
      if (hb[0] < Infinity) add(hb[0], hb[1] + oy, hb[2], hb[3] + oy);
      heroCost = performance.now() - h0;
    }
    const origin = oy + field.top,
      first = Math.floor(-origin / field.height),
      settle = intro ? 0 : smooth((performance.now() - introEnded) / (2500 * INTRO_SCALE));
    for (let k = Math.max(scene ? 0 : -1e9, first - 1); k <= first + 1; k++) {
      const y = origin + k * field.height;
      if (y > height || y + field.height < 0) continue;
      lx.save();
      lx.translate(0, y - field.top);
      lx.strokeStyle = glow;
      const shift = y - field.top;
      // r11.2 intro: the margin mats' glow builds with their tufts.
      if (intro) {
        const gm = smooth((introTime() / (INTRO_MS / 1000) - 0.04) / 0.7);
        if (gm > 0)
          for (const p of field.strands) {
            if (!p.haze || !p.static || p.box[3] + shift < 0 || p.box[1] + shift > height) continue;
            // The first page copy's bands fade in over their top 280 px (CSS mask).
            drawHaze(lx, p, p.alpha * fieldAlpha * gm, scene && k === 0 ? (y) => Math.max(0, Math.min(1, (y - field.top) / 280)) : null);
            add(p.box[0], p.box[1] + shift, p.box[2], p.box[3] + shift);
          }
      }
      // Intro: the growing rim is brightest (pulse-like light at every tip).
      if (intro && tips.length) {
        lx.strokeStyle = core;
        lx.lineCap = "butt";
        // r11.2: mat tufts never glow (hundreds of glowing felt tips read as a
        // flash), and the glow fades out over the last 12% of the intro.
        const tipFade = smooth((1 - introTime() / (INTRO_MS / 1000)) / 0.12);
        for (const [p, at] of tips) {
          if (p.tuft || tipFade <= 0) continue;
          const q = p.points[Math.floor(at)];
          if (q.y + shift < -20 || q.y + shift > height + 20) continue;
          const w = p.margin ? marginWeight(q.x) : columnWeight(q.x);
          if (w <= 0.01) continue;
          const from = Math.max(0, at - 7);
          // ...and a tip's glow rises as it leaves its source (no flash at a colony).
          const emerge = smooth(((p.s0 || 0) + p.arc[Math.floor(at)] - 16) / 48);
          if (emerge <= 0) continue;
          lx.globalAlpha = Math.min(1, (light ? 0.35 : 0.6) * w * lightMul * (p.hair ? 0.6 : 1)) * tipFade * emerge;
          lx.lineWidth = p.width * 1.6;
          lx.beginPath();
          lx.moveTo(p.points[Math.floor(from)].x, p.points[Math.floor(from)].y);
          for (let i = Math.floor(from) + 1; i <= Math.floor(at); i++) lx.lineTo(p.points[i].x, p.points[i].y);
          lx.stroke();
          add(q.x - 22, q.y + shift - 22, q.x + 22, q.y + shift + 22);
        }
      }
      // Breathing: margin colony mats brighten and settle, de-synchronised.
      if (!reduced && settle > 0)
        for (const knot of field.marginKnots) {
          if (knot.y + shift < -120 || knot.y + shift > height + 120) continue;
          const s = colonySprite(knot);
          if (!s) continue;
          lx.globalAlpha = settle * (light ? 0.18 : 0.32) * (0.5 - 0.5 * Math.cos((t / s.period) * 6.283 + s.phase));
          lx.drawImage(s.cv, s.x0, s.y0, s.w, s.h);
          add(s.x0, s.y0 + shift, s.x0 + s.w, s.y0 + s.h + shift);
        }
      if (!reduced && !intro && !scene?.pulsesOff)
        drawMarginCopy(k, t, add, shift);
      lx.restore();
    }
    // Hero pulses leak into field threads along their actual arrival times.
    if (scene?.activeTrips && !reduced && !scene.pulsesOff) {
      lx.save();
      lx.translate(0, oy);
      lx.strokeStyle = core;
      lx.lineCap = "butt";
      for (const trip of scene.activeTrips(t))
        for (const item of trip.route.fieldList) {
          const s = graph.ss[item.segment],
            dt = trip.age - item.arrival;
          if (dt < 0 || dt > 5.6) continue;
          const sy = (s.a.y + s.b.y) / 2 + oy;
          if (sy < -20 || sy > height + 20) continue;
          const p = s.fieldPath;
          if (p && !segmentShown(p, s.fieldIndex || 0, t)) continue;
          const ga = Math.min(1, envelope(dt) * item.amp * (light ? 0.24 : 0.75));
          if (ga < 0.004) continue;
          add(Math.min(s.a.x, s.b.x) - 3, Math.min(s.a.y, s.b.y) + oy - 3, Math.max(s.a.x, s.b.x) + 3, Math.max(s.a.y, s.b.y) + oy + 3);
          // r11.2: batched into quantised groups (was one stroke per segment).
          const key = 1e7 + Math.round(ga * 240) * 1000 + Math.round(Math.max(0.5, s.width * 1.8) * 40);
          let path = lifeGroups.get(key);
          if (!path) lifeGroups.set(key, (path = new Path2D()));
          path.moveTo(s.a.x, s.a.y);
          path.lineTo(s.b.x, s.b.y);
        }
      flushLife(lx);
      lx.restore();
    }
    // r11.2c: the moving light fades out under the hero copy (text-safe zone,
    // see textSafeMask in mycelium-hero.js): one destination-out blit of the
    // soft mask, limited to what was drawn this frame. Nearest-neighbour: the
    // 2 px mask cells sit on a 64 px ramp (no visible steps), and a filtered
    // upscale cost ~3.7 ms per frame in headless software raster (now ~0.9).
    const safe = scene?.textSafe;
    if (safe && box[0] < Infinity) {
      const sx0 = Math.max(box[0], safe.x),
        sy0 = Math.max(box[1], safe.y + oy, 0),
        sx1 = Math.min(box[2], safe.x + safe.w),
        sy1 = Math.min(box[3], safe.y + oy + safe.h, height);
      if (sx1 > sx0 && sy1 > sy0) {
        const k = safe.k;
        lx.globalCompositeOperation = "destination-out";
        lx.globalAlpha = 1;
        lx.imageSmoothingEnabled = false;
        lx.drawImage(safe.canvas, (sx0 - safe.x) * k, (sy0 - oy - safe.y) * k, (sx1 - sx0) * k, (sy1 - sy0) * k, sx0, sy0, sx1 - sx0, sy1 - sy0);
        lx.imageSmoothingEnabled = true;
        metrics.textSafeBlits = (metrics.textSafeBlits || 0) + 1;
      }
    }
    lx.globalAlpha = 1;
    dirty = box[0] < Infinity ? box : null;
    const cost = performance.now() - begin;
    metrics.frames++;
    metrics.costs.push(cost);
    metrics.liveCosts.push(performance.now() - lb);
    metrics.fieldCosts.push(cost - heroCost);
    if (metrics.costs.length > 1200) {
      metrics.costs.shift();
      metrics.liveCosts.shift();
      metrics.fieldCosts.shift();
    }
    if (scene) {
      const pm = window.__pulseMetrics;
      if (pm) {
        pm.frames++;
        pm.costs.push(cost);
        if (pm.costs.length > 1200) pm.costs.shift();
      }
      Object.assign(hero.dataset, { growthTime: t.toFixed(3), growthAge: A.toFixed(2), live: liveStats?.segs ?? 0, activeTips: liveStats?.active ?? 0 });
      // Breathing silhouettes belong to mature colonies only (diagnostic regrowth).
      const breath = document.querySelector(".mycelium-breath"),
        bo = smooth((A - 55) / 20).toFixed(2);
      if (breath && breath.dataset.gate !== bo) {
        breath.dataset.gate = bo;
        breath.style.opacity = bo;
      }
    }
    Object.assign(c.dataset, { time: t.toFixed(3), paths: field.strands.length, nodes: field.marginKnots.length, sporeNodes: 0, canvasPixels: live.width * live.height });
  }
  let tickDrawn = -Infinity;
  function tick(now) {
    raf = 0;
    // r11.4: 60 Hz while background work runs on a 120 Hz display (see pacer.skip).
    if (pacer.skip(now - tickDrawn)) return schedule();
    tickDrawn = now;
    firstFrame(now);
    const T = clock.tick(now);
    paint(T);
    schedule();
  }
  function schedule() {
    const moving = !disposed && attached && !reduce.matches && !document.hidden && frozen === null;
    metrics.paused = !moving;
    const pm = window.__pulseMetrics;
    if (pm) {
      pm.paused = !moving;
      pm.reason = reduce.matches ? "reduced-motion" : document.hidden ? "hidden" : frozen !== null ? "frozen" : !attached ? "building" : "running";
    }
    const box = document.querySelector(".mycelium-breath");
    if (box) box.classList.toggle("paused", !moving);
    if (box && frozen !== null) scene?.freezeBreath?.(frozen);
    if (moving && !raf) raf = requestAnimationFrame(tick);
    else if (!moving) {
      cancelAnimationFrame(raf);
      raf = 0;
    }
  }
  /** Hero-box static composite at page coordinates (redrawn only when the hero bakes). */
  function drawHero() {
    const hc = scene.hero.canvas,
      w = Math.ceil(scene.hero.W * d),
      h = Math.ceil(scene.hero.H * d);
    if (c.width !== w || c.height !== h) {
      c.width = w;
      c.height = h;
      c.style.height = `${h / d}px`;
      c.style.width = `${w / d}px`;
      x.setTransform(d, 0, 0, d, 0, 0);
      heroY = NaN;
    }
    x.clearRect(0, 0, scene.hero.W, scene.hero.H);
    x.drawImage(hc, 0, 0, scene.hero.W, scene.hero.H);
    scene.drawIntroCores?.(x);
  }
  let heroY = NaN,
    heroShown = true;
  function placeHero(oy) {
    const show = oy < height && oy + scene.hero.H > 0;
    if (show !== heroShown) {
      c.style.visibility = show ? "" : "hidden";
      heroShown = show;
    }
    const ty = Math.round(oy * d) / d;
    if (show && ty !== heroY) {
      c.style.transform = `translate3d(0,${ty}px,0)`;
      heroY = ty;
    }
  }
  // Progressive presentation while the scene builds: blit the static raster as it fills.
  let buildRaf = 0;
  function buildTick(now) {
    buildRaf = 0;
    if (disposed || attached) return;
    firstFrame(now);
    if (scene) {
      const oy = scene.hero.top - scrollY;
      if (scene.hero.version !== lastVersion && !reduce.matches) {
        drawHero();
        lastVersion = scene.hero.version;
      }
      if (scrollY !== lastScroll) {
        placeHero(oy);
        lastScroll = scrollY;
      }
      clock.tick(now);
    }
    buildRaf = requestAnimationFrame(buildTick);
  }
  const changed = () => {
    if (document.hidden || reduce.matches) {
      clock.pause();
      endIntro(); // hidden tab or reduced motion: the final state immediately
    }
    if (attached) paint(frozen ?? clock.elapsed, true);
    schedule();
  };
  const scrolled = () => {
    if (!attached) return;
    if (frozen !== null || reduce.matches) paint(frozen ?? clock.elapsed);
    else schedule();
  };
  if (scene) {
    buildRaf = requestAnimationFrame(buildTick);
    scene.onReady(() => {
      if (disposed) return;
      attach(); // the build presenter keeps running until attached
    });
  } else {
    // Swarms app pages: a standalone field graph, also built in slices.
    buildRaf = requestAnimationFrame(buildTick);
    const vs = [],
      ss = [],
      paths = [];
    const vertex = (vx, vy, source = 0) => {
      const v = { x: vx, y: vy, source, id: vs.length, edges: [] };
      vs.push(v);
      return v;
    };
    const edge = (a, b, w, al, tone, cord, birth) => {
      const s = { id: ss.length, a, b, width: w, alpha: al, tone, cord, birth, len: Math.hypot(b.x - a.x, b.y - a.y) };
      ss.push(s);
      a.edges.push(s.id);
      b.edges.push(s.id);
      return s;
    };
    // r11.4: paced slices (see pacer); the generator yields at the deadline.
    let end = 0;
    const budget = () => performance.now() > end;
    const gen = extendFieldGraph({ vs, ss, paths, vertex, edge, nodes: [], w: width, h: 150, budget });
    pacer.add({ prio: 0, name: "app-field", run: (deadline) => {
      if (disposed) return true;
      end = deadline;
      let r;
      do r = gen.next();
      while (!r.done && performance.now() < deadline);
      if (!r.done) return false;
      field = r.value;
      graph = { vs, ss };
      attach();
      return true;
    } });
  }
  const layoutObserver = new MutationObserver(() => {
    if (!attached) return;
    const previous = field.layout;
    field.layout = contentBounds(previous.offset);
    if (window.__understoryAudit) window.__understoryAudit.protectedRects = field.layout.rects;
    const moved = field.hyphalKnots.map((k) => k.underText).join() !== (markKnotsUnderText(), field.hyphalKnots.map((k) => k.underText).join());
    if (moved || previous.left !== field.layout.left || previous.right !== field.layout.right) {
      intro = null;
      colonySprites.clear();
      rasterStatic();
    }
  });
  layoutObserver.observe(document.querySelector("main"), { subtree: true, childList: true, attributes: true, attributeFilter: ["hidden"] });
  window.addEventListener("scroll", scrolled, { passive: true });
  document.addEventListener("visibilitychange", changed);
  reduce.addEventListener("change", changed);
  schedule();
  return () => {
    disposed = true;
    c.dataset.ready = "0";
    layoutObserver.disconnect();
    cancelAnimationFrame(raf);
    cancelAnimationFrame(buildRaf);
    window.removeEventListener("scroll", scrolled);
    document.removeEventListener("visibilitychange", changed);
    reduce.removeEventListener("change", changed);
    for (const b of bands) b.cv.width = b.cv.height = 0;
    bands = [];
    bandBox.remove();
    c.style.cssText = "";
    for (const s of colonySprites.values()) s.cv.width = s.cv.height = 0;
    colonySprites.clear();
    live.width = live.height = 0;
    live.remove();
    for (const tl of lifeTiles.values()) tl.cv.width = tl.cv.height = 0;
    lifeTiles.clear();
    lifeBox.remove();
  };
}
