// SPDX-License-Identifier: AGPL-3.0-only
/** R10 hero: one page-space mycelium grown, not drawn.
 * Geometry is laid in viewport/page coordinates (no hero clip) in idle slices,
 * so the first frame appears at once and fills in. Every polyline carries a
 * birth time from its distance to its source: fronts advance coherently, cords
 * accrete fibres from the base, the growing rim is brightest, and mature paths
 * are baked once into a static raster. Only fronts, rim glow, a bounded set of
 * recycling outer explorers, pulses and blooms are drawn per frame (by the
 * page compositor in mycelium-field.js). The disposer owns every resource.
 */
import { rng, gauss, hash01, smooth, persistWalk, steerWalk, arcLengths, measurePaths, auditPaths, rootLaterals, sceneClock, tipCycle, lifeTiming, INTRO_MS as SHARED_INTRO_MS, INTRO_SCALE } from "./mycelium-geometry.js";
import { extendFieldGraph } from "./mycelium-field.js";

export const START_AGE = 110; // visitors arrive to a mature core with living rims
/** r11.2 (owner: the dense mats "pop" in during the grow-in). Colony cotton and
 * mat hyphae wait for their own start age (P.matT) in the load-in: a few near
 * the centre first, then filling outward (60% by radius, 40% hashed), every
 * one finished by MAT_END, around the end of the hero's growth. Intro timing
 * only: geometry, births and the established raster are unchanged. */
const MAT_F0 = 0.04,
  MAT_F1 = 0.8, // share of the intro by which every mat path has started its last stretch
  MAT_DONE = 0.94, // ... and by which every mat (with its forks and hairs) is complete
  INTRO_AGE_POW = 1.6, // the hero's intro age map (see ageAt)
  ageOfIntro = (f) => START_AGE * (1 - Math.pow(1 - Math.max(0, Math.min(1, f)), INTRO_AGE_POW)),
  introOfAge = (A) => (A >= START_AGE ? 1 : 1 - Math.pow(Math.max(0, 1 - A / START_AGE), 1 / INTRO_AGE_POW)),
  MAT_END = ageOfIntro(MAT_F1);
const MAT_F_COLONY = 0.55, // radiating colony hyphae: all started by 55% of the intro
  MAT_INHERIT = /^(colony|hair|mat|knot-hair|explorer|explorer-hair)$/;
/** Start age of a mat path: a few near the centre first, then filling outward
 * (62% by radius, 38% hashed), in intro time (the age map is front-loaded: by
 * age, the mats were 80% complete a third of the way into the grow-in). */
function matAge(id, c, u, f1, dur) {
  const h = hash01(id * 17 + 5),
    q = Math.min(1, 0.62 * u + 0.38 * hash01(id * 23 + 11)) * (h < 0.08 ? 0.15 : 1),
    f0 = Math.max(MAT_F0, introOfAge(c.start + 2)),
    last = Math.max(c.start + 3, ageOfIntro(f1) - dur);
  return Math.min(last, Math.max(c.start + 3, ageOfIntro(f0 + q * Math.max(0, f1 - f0))));
}
function matTime(P, c, u) {
  P.matShift = Math.max(P.matShift || 0, matAge(P.id, c, u, MAT_F1, (P.len || 0) / Math.max(1e-6, P.v)) - P.t0);
}
/** Mat growth fraction of colony c at age A (cores and felt fills follow it). */
const matGrow = (c, A) => {
  const f0 = Math.max(MAT_F0, introOfAge(c.start + 2));
  return smooth((introOfAge(A) - f0) / Math.max(0.05, MAT_F1 - f0));
};
// r11 load-in: the hero grows out from its nodes over the first INTRO_S seconds
// (shared contract with the field lane), then runs at the normal rate.
export const INTRO_MS = SHARED_INTRO_MS; // 6000 by default (shared, see mycelium-geometry.js)
const INTRO_S = INTRO_MS / 1000,
  INTRO_POW = 1.6;
let introPlayed = false; // module state: theme/hue/style re-mounts never replay
const RIM = 1.5, // front brightness boost (D8)
  RIM_TAU = 4.5,
  RIM_WIN = 20, // seconds a laid segment keeps its rim glow before baking
  CAP = 760000,
  CHUNK = 6;
// r11 density between 6405a6fa and 02e3dcad (see D46).
const COTTON = 2200,
  COTTON_POW = 1.05,
  MAT = 2700,
  // Persistent fuzz: hairs per px of persistent strand (owner: like pulse growth).
  FUZZ_RATE = 1 / 9,
  CORD_W0 = 5, // link cords 5-7 px at the colony (r10: 8-12): always under the mat core
  CORD_A = 0.16;
const geometryCache = new Map();

/** Text-safe density field in scene coordinates (1 away from text, FLOOR in it). */
function protector(rects, floor) {
  return (x, y) => {
    let m = 1;
    for (const r of rects) {
      const dx = Math.max(r.l - x, 0, x - r.r),
        dy = Math.max(r.t - y, 0, y - r.b),
        v = floor + (1 - floor) * smooth(Math.hypot(dx, dy) / r.soft);
      if (v < m) m = v;
    }
    return m;
  };
}

function readLayout(canvas) {
  const section = canvas.closest("section") || canvas.parentElement,
    box = section.getBoundingClientRect(),
    W = document.documentElement.clientWidth || innerWidth,
    top = box.top + scrollY,
    Hh = box.height,
    EXT = W < 600 ? 140 : 260,
    rects = [];
  const add = (e, pad, soft) => {
    for (const r of e.getClientRects ? [e.getBoundingClientRect()] : [])
      if (r.width && r.height)
        rects.push({ l: r.left - pad, r: r.right + pad, t: r.top + scrollY - top - pad, b: r.bottom + scrollY - top + pad, soft });
  };
  for (const e of section.querySelectorAll(".hero-copy > *")) add(e, 10, 95);
  const next = section.nextElementSibling;
  if (next) for (const e of next.querySelectorAll("p,h2,h3,.eyebrow")) {
    const r = e.getBoundingClientRect();
    if (r.top + scrollY - top < Hh + EXT + 80) add(e, 8, 80);
  }
  const copy = section.querySelector(".hero-copy")?.getBoundingClientRect();
  return {
    W,
    Hh,
    top,
    EXT,
    H: Hh + EXT,
    rects,
    textLeft: copy ? copy.left : 0,
    textRight: copy ? Math.min(copy.right, copy.left + 640) : W * 0.5,
    mobile: W < 600,
    key: [W, Math.round(Hh), Math.round(top), ...rects.map((r) => [r.l, r.r, r.t, r.b].map(Math.round).join(","))].join("|"),
  };
}

/** r11.2c text-safe zone for the moving light (hero pulses, their field
 * leaks, margin pulses and cascades, intro rims). Static strands already fade
 * to 5% near the hero copy (protector), but the pulse light was drawn at full
 * strength and dropped the contrast under the H1, lead and CTAs to 1.0-2.7:1
 * at pulse peaks. The live layer is attenuated under every text line box (and
 * the CTA buttons) in and just below the hero to TEXT_SAFE.floor, ramping back
 * to full over TEXT_SAFE.soft px (smoothstep, no visible edge). The floor is
 * the static strands' own floor near text, so a pulse fades with its strand. Returned as a
 * soft alpha mask (alpha = removed fraction) in scene coordinates at half CSS
 * resolution, for one destination-out blit per frame. */
export const TEXT_SAFE = Object.freeze({ floor: 0.05, pad: 6, soft: 64, softMobile: 44, scale: 0.5 });
function* textSafeMask(section, L, out) {
  const top = L.top,
    rects = [],
    soft = L.mobile ? TEXT_SAFE.softMobile : TEXT_SAFE.soft,
    pad = TEXT_SAFE.pad,
    add = (r) => {
      if (r.width < 1 || r.height < 1) return;
      const t = r.top + scrollY - top;
      if (t > L.H + 200 || r.bottom + scrollY - top < -40) return;
      rects.push({ l: r.left - pad, r: r.right + pad, t: t - pad, b: r.bottom + scrollY - top + pad });
    };
  const roots = [section.querySelector(".hero-copy"), section.nextElementSibling].filter(Boolean);
  for (const root of roots) {
    const walk = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    let n;
    while ((n = walk.nextNode())) {
      if (!n.textContent.trim() || n.parentElement.closest(".btn,.card")) continue;
      const range = document.createRange();
      range.selectNodeContents(n);
      for (const r of range.getClientRects()) add(r);
    }
    for (const e of root.querySelectorAll(".btn")) add(e.getBoundingClientRect());
  }
  // The demo status keeps its reserved box (its copy changes when live).
  for (const e of section.querySelectorAll(".apex-demo")) add(e.getBoundingClientRect());
  if (!rects.length) return;
  const k = TEXT_SAFE.scale,
    x0 = Math.max(0, Math.floor(Math.min(...rects.map((r) => r.l)) - soft)),
    y0 = Math.floor(Math.min(...rects.map((r) => r.t)) - soft),
    x1 = Math.min(L.W, Math.ceil(Math.max(...rects.map((r) => r.r)) + soft)),
    y1 = Math.ceil(Math.max(...rects.map((r) => r.b)) + soft),
    w = Math.max(1, Math.ceil((x1 - x0) * k)),
    h = Math.max(1, Math.ceil((y1 - y0) * k)),
    cv = document.createElement("canvas");
  cv.width = w;
  cv.height = h;
  const cx = cv.getContext("2d"),
    im = cx.createImageData(w, h),
    px = im.data;
  for (let j = 0; j < h; j++) {
    const y = y0 + (j + 0.5) / k,
      near = rects.filter((r) => y > r.t - soft && y < r.b + soft);
    if (!near.length) continue;
    for (let i = 0; i < w; i++) {
      const x = x0 + (i + 0.5) / k;
      let keep = 1;
      for (const r of near) {
        const dx = Math.max(r.l - x, 0, x - r.r),
          dy = Math.max(r.t - y, 0, y - r.b),
          v = TEXT_SAFE.floor + (1 - TEXT_SAFE.floor) * smooth(Math.hypot(dx, dy) / soft);
        if (v < keep) keep = v;
      }
      px[(j * w + i) * 4 + 3] = Math.round((1 - keep) * 255);
    }
    if (j % 24 === 23) yield "text-safe";
  }
  cx.putImageData(im, 0, 0);
  out.mask = { canvas: cv, x: x0, y: y0, w: x1 - x0, h: y1 - y0, k, rects: rects.length, floor: TEXT_SAFE.floor, soft };
}

/** Pure geometry builder (a generator: yields between bounded work units). */
function* buildGeometry(L, budget, out = {}) {
  const { W, Hh, H, mobile } = L,
    protect = protector(L.rects, 0.05),
    R = rng("mycelium-r10-structure-1"),
    vs = [],
    ss = [],
    paths = [],
    units = [],
    cords = [],
    cordPaths = [],
    grid = new Map(),
    GC = 6;
  let fusions = 0,
    branchSerial = 0;
  function vertex(x, y, source = 0) {
    const v = { x, y, source, id: vs.length, edges: [], birth: 0 };
    vs.push(v);
    return v;
  }
  function edge(a, b, width, alpha, tone = 1, cord = -1, birth = 0, old = false) {
    if (ss.length >= CAP) return null;
    const s = { id: ss.length, a, b, len: Math.hypot(b.x - a.x, b.y - a.y), width, alpha, tone, cord, birth, old, source: a.source };
    ss.push(s);
    a.edges.push(s.id);
    b.edges.push(s.id);
    return s;
  }
  const gkey = (x, y) => ((Math.floor(y / GC) + 4096) << 13) ^ (Math.floor(x / GC) + 4096);
  function index(v, owner) {
    const k = gkey(v.x, v.y);
    let list = grid.get(k);
    if (!list) grid.set(k, (list = []));
    list.push(v);
    v.owner = owner;
  }
  function nearest(x, y, r, accept) {
    let best = null,
      bd = r;
    for (let dx = -1; dx <= 1; dx++)
      for (let dy = -1; dy <= 1; dy++) {
        const list = grid.get(gkey(x + dx * GC, y + dy * GC));
        if (list)
          for (const v of list) {
            const d = Math.hypot(v.x - x, v.y - y);
            if (d < bd && accept(v)) {
              bd = d;
              best = v;
            }
          }
      }
    return best;
  }
  /**
   * Lay one polyline as graph vertices/edges plus a drawable path record.
   *  pts: raw points (first may be an existing vertex), t0/v timing,
   *  w0->w1 width taper, a0->a1 alpha taper, fuse: interior anastomosis.
   */
  function lay(pts, o) {
    const first = pts[0].edges ? pts[0] : vertex(pts[0].x, pts[0].y, o.source ?? 0),
      poly = [first],
      P = { id: paths.length, kind: o.kind, t0: o.t0, v: o.v, tone: o.tone ?? 0, unit: o.unit ?? -1, cord: o.cord ?? -1, branch: o.branch ?? branchSerial++, group: o.group ?? -1, s0: o.s0 ?? 0, sub: o.sub ?? 0 };
    if (!first.birth) first.birth = o.t0;
    let a = first,
      arc = 0,
      fused = null;
    const n = pts.length;
    for (let i = 1; i < n; i++) {
      const p = pts[i];
      if (o.thin && R() < (1 - protect(p.x, p.y)) * o.thin) break; // density thinning near text
      if (p.y < -40 || p.y > H + (o.below ?? 0) || p.x < -160 || p.x > W + 160) break;
      arc += Math.hypot(p.x - a.x, p.y - a.y);
      const birth = o.t0 + arc / o.v;
      let b = null;
      if (o.fuse && i > 6 && arc > (o.fuseAfter ?? 0)) {
        // Fusion happens behind fronts: only with older material of another path.
        b = nearest(p.x, p.y, o.fuse, (v) => v.owner !== P.id && v.birth < birth - 2 && v !== first && v.edges.length >= 2);
        if (b) {
          fused = b;
          fusions++;
        }
      }
      if (!b) {
        b = p.edges ? p : vertex(p.x, p.y, a.source);
        b.birth = birth;
      }
      const s = edge(a, b, 0, 0, P.tone, P.cord, birth);
      if (!s) break;
      s.path = P;
      s.index = poly.length - 1;
      if (o.explorer) s.explorer = true;
      poly.push(b);
      a = b;
      if (fused) break;
    }
    if (poly.length < 2) return null;
    const m = poly.length,
      arcs = arcLengths(poly),
      len = arcs.at(-1),
      wv = new Float32Array(m),
      av = new Float32Array(m);
    for (let i = 0; i < m; i++) {
      const u = len ? arcs[i] / len : 0,
        q = poly[i];
      wv[i] = o.w0 + (o.w1 - o.w0) * Math.pow(u, o.wp ?? 1);
      // Soft page edges by density/alpha thinning, never a mask or a clip line.
      av[i] = (o.a0 + (o.a1 - o.a0) * u) * protect(q.x, q.y) * (o.alphaAt ? o.alphaAt(q.x, q.y) : 1) * smooth((H - q.y) / 150);
    }
    if (fused) for (let i = Math.max(0, m - 4); i < m; i++) wv[i] += 0.2; // seamless thickening, no bead
    for (let i = 1; i < m; i++) {
      const s = ss[poly[i].edges.at(-1)];
      if (s && s.path === P) {
        s.width = wv[i];
        s.alpha = av[i];
      }
    }
    let bx0 = Infinity, by0 = Infinity, bx1 = -Infinity, by1 = -Infinity;
    for (const q of poly) {
      if (q.x < bx0) bx0 = q.x;
      if (q.y < by0) by0 = q.y;
      if (q.x > bx1) bx1 = q.x;
      if (q.y > by1) by1 = q.y;
    }
    Object.assign(P, { box: [bx0 - 2, by0 - 2, bx1 + 2, by1 + 2], pts: poly, points: poly, arcs, len, w: wv, a: av, done: o.t0 + len / o.v, fused: !!fused });
    // r11.2 mat schedule (load-in only): colony hyphae start on their own
    // intro-time schedule; their forks, hairs, mats and cotton never start
    // before the part they grow from is shown (shift inherited).
    const host = first.owner >= 0 ? paths[first.owner] : null;
    if (host?.matShift > 0 && MAT_INHERIT.test(o.kind)) {
      P.matShift = host.matShift;
      P.matHost = host;
    }
    if (o.kind === "colony" && colonies[o.source]) {
      const c = colonies[o.source],
        u = Math.min(1, Math.hypot(first.x - c.node.x, first.y - c.node.y) / c.R0);
      P.matShift = Math.max(P.matShift || 0, matAge(P.id, c, u, MAT_F_COLONY, len / o.v) - o.t0);
    }
    for (const q of poly) if (q.owner === undefined) index(q, P.id);
    paths.push(P);
    if (P.unit >= 0) units[P.unit].paths.push(P);
    return P;
  }
  // Point and heading at arc s along a laid path.
  function along(P, s) {
    let i = 1;
    while (i < P.arcs.length - 1 && P.arcs[i] < s) i++;
    const a = P.pts[i - 1],
      b = P.pts[i];
    return { v: b, i, h: Math.atan2(b.y - a.y, b.x - a.x), t: P.t0 + P.arcs[i] / P.v };
  }
  /** Hypha with subapical forks at exponential spacing; dense or sparse (ref-8) vocabulary. */
  function hypha(start, heading, len, o, depth) {
    const walk = persistWalk(start, heading, len, R, o.walk);
    const P = lay(walk, o);
    if (!P) return null;
    if (depth > 0) {
      let s = (o.firstFork ?? 14) + -Math.log(1 - R()) * o.forkMean;
      while (s < P.len - 10) {
        const at = along(P, s),
          side = R() < 0.5 ? -1 : 1,
          angle = at.h + side * (o.forkAngle[0] + R() * (o.forkAngle[1] - o.forkAngle[0])),
          rest = (P.len - s) * (0.35 + R() * 0.5);
        const child = {
          ...o,
          t0: at.t + (o.forkLag ?? 2.5),
          w0: Math.max(o.w1, P.w[at.i] * 0.78),
          w1: o.w1 * 0.85,
          a0: o.a0 * 0.85,
          a1: o.a1,
          branch: P.branch,
        };
        if (o.alongside) {
          // ref-8: children bend back to run alongside their parent.
          const parentH = at.h;
          child.walk = { ...o.walk, pull: (x, y, mean) => Math.atan2(Math.sin(parentH - mean), Math.cos(parentH - mean)) * 0.008 };
        }
        hypha(at.v, angle, rest, child, depth - 1);
        s += -Math.log(1 - R()) * o.forkMean + 8;
      }
    }
    return P;
  }
  /** Forward-pointing fringe hairs at Poisson positions (no comb, no asterisk). */
  function fringe(P, rate, o) {
    let s = -Math.log(1 - R()) / rate;
    while (s < P.len) {
      const at = along(P, s),
        u = s / P.len,
        side = R() < 0.5 ? -1 : 1,
        len = Math.min(o.maxLen, o.len * (0.4 + -Math.log(1 - R()) * 0.7));
      lay(persistWalk(at.v, at.h + side * (0.2 + R() * 0.4), len, R, { k: 0.02, run: 20, jit: 0.04, step: 2 }), {
        kind: "hair", t0: at.t + 0.8 + R() * 2.5, v: P.v * 0.8, w0: o.w, w1: o.w * 0.7, a0: o.a, a1: o.a * 0.55, tone: R() < 0.12 ? 1 : 0, unit: P.unit, branch: P.branch, cord: -1,
      });
      s += -Math.log(1 - R()) / (rate * (0.5 + u));
    }
  }

  // ---- composition: colonies in page space, one partly off-screen ----
  const reg0 = mobile ? 0 : Math.min(W * 0.62, L.textRight + 30),
    rw = W - reg0,
    spots = mobile
      ? [[0.16 * W, Hh * 0.77], [0.66 * W, Hh * 0.81], [0.4 * W, Hh * 0.95], [W + 10, Hh * 0.93], [0.92 * W, Hh * 0.7]]
      : [[reg0 + 0.15 * rw, Hh * 0.28], [reg0 + 0.5 * rw, Hh * 0.52], [reg0 + 0.85 * rw, Hh * 0.15], [W - 34, Hh * 0.8], [reg0 + 0.28 * rw, Hh * 0.92]];
  const leftColony = !mobile && L.textLeft >= 230;
  if (leftColony) spots.push([L.textLeft * 0.42, Hh * 0.56]);
  const nodes = spots.map(([x, y], i) => {
    const n = vertex(x, y, i);
    n.birth = i * 1.6;
    n.owner = -1;
    return n;
  });
  const links = (mobile ? [[0, 1], [1, 2], [1, 3], [3, 4]] : [[0, 1], [1, 2], [1, 3], [3, 4]]).concat(leftColony ? [[4, 5]] : []);
  // r11: colonies scale with the hero region (1920 read too sparse at 1440 sizes).
  const areaScale = mobile ? 1 : Math.max(0.85, Math.min(1.25, Math.pow(rw / 660, 0.42))),
    R0 = mobile ? 100 : 172 * areaScale;
  const colonies = nodes.map((n, i) => {
    const away = Math.atan2(n.y - nodes[1].y, n.x - nodes[1].x);
    return {
      node: n, i, R0: R0 * (0.68 + R() * 0.62) * (i === 1 ? 1.15 : 1),
      lobes: [0.18 + R() * 0.16, R() * 6.28, 0.1 + R() * 0.12, R() * 6.28],
      fan: i === 1 ? R() * 6.28 : away + gauss(R) * 0.5,
      spread: 0.9 + R() * 0.7,
      elong: 0.18 + R() * 0.3,
      start: n.birth,
      vel: (mobile ? 3.8 : 4.6) * (0.9 + R() * 0.2),
      phases: [R() * 6.28, R() * 6.28, R() * 6.28],
    };
  });
  // Smooth per-sector front speed: coherent but irregular advancing rims.
  Object.assign(out, { paths, colonies, protect, nodes, units });
  const sectorSpeed = (c, h) => c.vel * (1 + 0.16 * Math.sin(h + c.phases[0]) + 0.1 * Math.sin(2 * h + c.phases[1]) + 0.06 * Math.sin(3 * h + c.phases[2]));
  yield "layout";

  // ---- D2 cottony radiating tufts: a broad soft mat (no point soma), fan-biased
  // and lobed (not circles), heavy-tailed radiating hyphae and dense forward fuzz.
  // Irregular colonies: elongated along their main cord axis, lobed, fan-biased,
  // and often 2-3 merged sub-tufts of different sizes and orientations.
  for (const c of colonies) {
    const mine = links.filter((l) => l.includes(c.i)).map((l) => nodes[l[0] === c.i ? l[1] : l[0]]);
    c.axis = mine.length ? Math.atan2(mine[0].y - c.node.y, mine[0].x - c.node.x) : c.fan;
  }
  const shapeR = (c, h, sub = c) =>
    (0.66 + 0.34 * Math.cos(h - sub.fan)) * (1 + c.lobes[0] * Math.sin(2 * h + c.lobes[1]) + c.lobes[2] * Math.sin(3 * h + c.lobes[3])) * (1 + c.elong * Math.cos(2 * (h - c.axis)));
  const densePerColony = mobile ? 220 : Math.round(380 * areaScale * areaScale);
  const pickSub = (c, r) => {
    let x = r() * c.subW;
    for (const sb of c.subs) if ((x -= sb.w) <= 0) return sb;
    return c.subs[0];
  };
  for (const c of colonies) {
    const n = c.node;
    c.subs = [{ v: n, x: n.x, y: n.y, R: c.R0, fan: c.fan, t: c.start, w: 1, k: 0 }];
    const extra = R() < 0.3 ? 0 : R() < 0.65 ? 1 : 2;
    for (let e = 0; e < extra; e++) {
      const dir = (R() < 0.65 ? c.axis : R() < 0.5 ? c.fan : c.axis + Math.PI) + gauss(R) * 0.55,
        dist = c.R0 * (0.32 + R() * 0.3),
        bridge = lay(persistWalk(n, dir, dist, R, { k: 0.012, run: 50, jit: 0.02 }), {
          kind: "colony", source: c.i, t0: c.start, v: c.vel, w0: 0.55, w1: 0.4, a0: 0.3, a1: 0.22, tone: 1,
        });
      if (!bridge) continue;
      const end = bridge.pts.at(-1),
        sR = c.R0 * (0.4 + R() * 0.38);
      end.source = c.i;
      c.subs.push({ v: end, x: end.x, y: end.y, R: sR, fan: dir + gauss(R) * 0.45, t: c.start + dist / c.vel, w: sR / c.R0, k: c.subs.length });
    }
    c.subW = c.subs.reduce((a, sb) => a + sb.w, 0);
    for (let k = 0; k < densePerColony; k++) {
      const sb = pickSub(c, R),
        h = R() < 0.62 ? sb.fan + gauss(R) * c.spread : R() * 6.28,
        reach = sb.R * shapeR(c, h, sb) * (R() < 0.08 ? 1.2 + R() * 0.45 : 0.16 + 0.84 * Math.pow(R(), 1.25)),
        r0 = Math.sqrt(R()) * sb.R * 0.3, // a disc of origins: no point convergence
        o0 = h + gauss(R) * 0.8,
        start = { x: sb.x + Math.cos(o0) * r0, y: sb.y + Math.sin(o0) * r0 },
        v = sectorSpeed(c, h);
      const outward = (x, y, mean) => Math.atan2(Math.sin(Math.atan2(y - sb.y, x - sb.x) - mean), Math.cos(Math.atan2(y - sb.y, x - sb.x) - mean)) * 0.005;
      const radial = (x, y) => {
        const r = Math.hypot(x - sb.x, y - sb.y) / sb.R;
        // r11: a defined (not dimmed) matted centre; still no point soma.
        // r11: a defined matted centre without a convergence point (no soma).
        return r < 0.35 ? 0.62 + 0.42 * smooth(r / 0.35) : r < 0.75 ? 1 : 1 - 0.25 * smooth((r - 0.75) / 0.5);
      };
      const P = hypha(start, h, reach, {
        kind: "colony", source: c.i, sub: sb.k, t0: sb.t + r0 / v, v, w0: 0.5, w1: 0.17, wp: 0.8, a0: 0.36, a1: 0.13, tone: R() < 0.16 ? 1 : 0,
        fuse: 1.8, thin: 0.06, alphaAt: radial,
        walk: { k: 0.016, run: 60, jit: 0.03, pull: outward },
        forkMean: 32, forkAngle: [0.33, 0.62], forkLag: 2.2, firstFork: 14,
      }, 2);
      if (P && P.pts[0] !== sb.v) {
        edge(sb.v, P.pts[0], 0.6, 0.4, 1, -1, sb.t); // origin joins the (sub-)tuft vertex
      }
      budget() && (yield "colony");
    }
    // Forward-pointing fringe on the outer half of colony hyphae (~half the old 44k hairs).
    for (const P of paths) if (P.kind === "colony" && P.pts[0].source === c.i && !P.fringed) {
      P.fringed = true;
      const sb = c.subs[P.sub || 0] || c.subs[0],
        rOut = Math.hypot(P.pts.at(-1).x - sb.x, P.pts.at(-1).y - sb.y) / sb.R;
      if (rOut > 0.22) fringe(P, 1 / 7, { len: 6, maxLen: 22, w: 0.16, a: 0.27 });
      budget() && (yield "fringe");
    }
  }

  // Cotton: dense short tangled-forward fuzz over the whole colony area,
  // attached to laid vertices (graph-connected), density falling outwards.
  const fuzzR = rng("r10-colony-cotton");
  for (const c of colonies) {
    // r11: fewer, more central cotton hairs: a defined, densely matted colony
    // centre (always denser than any cord leaving it) with dark space around.
    const count = Math.round((mobile ? 800 : COTTON * areaScale * areaScale) * (c.R0 / R0) * (0.8 + 0.25 * c.subW));
    for (let k = 0; k < count; k++) {
      const sb = pickSub(c, fuzzR),
        th = fuzzR() < 0.6 ? sb.fan + gauss(fuzzR) * c.spread * 1.1 : fuzzR() * 6.28,
        rr = sb.R * shapeR(c, th, sb) * Math.pow(fuzzR(), COTTON_POW) * 0.9,
        at = nearest(sb.x + Math.cos(th) * rr, sb.y + Math.sin(th) * rr, 7, (v) => v.owner !== undefined && v.owner >= 0 && v.source === c.i);
      if (!at) continue;
      const u = Math.min(1, rr / sb.R),
        len = Math.min(34, 5 + -Math.log(1 - fuzzR()) * 9 * (1.2 - 0.5 * u));
      // Inside the core the cotton is tangled (any heading): radial hairs there
      // converge on a point and read as a neuron soma.
      const radialH = Math.atan2(at.y - sb.y, at.x - sb.x),
        hh = u < 0.32 ? fuzzR() * 6.28 : radialH + gauss(fuzzR) * (0.75 + 0.9 * smooth((0.5 - u) / 0.2));
      const Pc = lay(persistWalk(at, hh, len, fuzzR, { k: 0.03, run: 14, jit: 0.06, step: 2 }), {
        kind: "hair", source: c.i, t0: (at.birth || c.start) + 1 + fuzzR() * 4, v: c.vel * 0.7, w0: 0.2 + fuzzR() * 0.06, w1: 0.15, a0: 0.27 * (1 - 0.45 * u), a1: 0.11, tone: fuzzR() < 0.12 ? 1 : 0, cord: -1,
      });
      if (Pc) matTime(Pc, c, u);
      if (budget()) yield "cotton"; // r11.1: checked every hair (k % 60 let one slice run 30-50 ms)
    }
  }
  // ---- D3/D5 rhizomorph cords: two halves grown from both colonies fuse mid-way ----
  function* cord(from, target, o) {
    const id = cords.length,
      centre = steerWalk(from, target, R, { k: 0.019, run: 70, jit: 0.016, steer: 0.014, restore: 0.03, launch: 0.14 });
    if (o.meet) centre[centre.length - 1] = o.meet;
    // Braids: the bundle diverges into 2-3 sub-cords over an "eye" and fuses
    // again; the spine (pulse route) follows one of the sub-cords.
    const arcC = [0],
      ncx = [],
      ncy = [];
    for (let i = 1; i < centre.length; i++) arcC.push(arcC[i - 1] + Math.hypot(centre[i].x - centre[i - 1].x, centre[i].y - centre[i - 1].y));
    for (let i = 0; i < centre.length; i++) {
      const a = centre[Math.max(0, i - 3)],
        b = centre[Math.min(centre.length - 1, i + 3)],
        h = Math.atan2(b.y - a.y, b.x - a.x);
      ncx.push(-Math.sin(h));
      ncy.push(Math.cos(h));
    }
    const Ltot = arcC.at(-1),
      islands = [];
    if (o.braid !== false && Ltot > 140) {
      const want = Ltot > 280 && R() < 0.65 ? 2 : 1;
      for (let tries = 0; islands.length < want && tries < 8; tries++) {
        const span = Math.min(Ltot * 0.62, 100 + R() * 90),
          sa = Ltot * 0.16 + R() * Math.max(1, Ltot * 0.76 - span),
          sb = sa + span;
        if (islands.some((q) => sa < q.sb + 20 && sb > q.sa - 20)) continue;
        const ways = R() < 0.6 ? 2 : 3;
        islands.push({ sa, sb, ways, amp: 3.6 + R() * 4, skew: Array.from({ length: ways }, () => 0.7 + R() * 0.6), follow: Math.floor(R() * ways) });
      }
    }
    const eye = (q, s) => (s <= q.sa || s >= q.sb ? 0 : Math.pow(Math.sin((Math.PI * (s - q.sa)) / (q.sb - q.sa)), 0.75));
    const binOffset = (q, b, s) => (b - (q.ways - 1) / 2) * 2 * q.amp * q.skew[b] * eye(q, s);
    if (islands.length)
      for (let i = 1; i < centre.length - 1; i++) {
        let off = 0;
        for (const q of islands) off += binOffset(q, q.follow, arcC[i]);
        if (off) centre[i] = { x: centre[i].x + ncx[i] * off, y: centre[i].y + ncy[i] * off };
      }
    // r11: the spine (pulse route) is invisible inside the mat it leaves: drawn
    // from the colony centre, cord spines converged on a point (a soma).
    const emergeR = Math.max(o.emerge ?? 0, 4),
      spineAlpha = (x, y) => smooth((Math.hypot(x - from.x, y - from.y) - emergeR * 0.6) / (emergeR * 1.4));
    const spine = lay(centre, { kind: "cord-centre", cord: id, t0: o.t0, v: o.v, w0: 0.7, w1: 0.45, a0: 0.3, a1: 0.2, tone: 1, source: from.source, unit: o.unit, alphaAt: o.emerge ? spineAlpha : undefined });
    if (!spine) return null;
    const C = { id, a: from, b: spine.pts.at(-1), spine, length: spine.len, poly: spine.pts };
    cords.push(C);
    const Lc = spine.len,
      width = (s) => o.w1 + (o.w0 - o.w1) * Math.pow(1 - s / Lc, 1.25),
      F = o.fibres;
    // Normals sampled every centre vertex.
    const nx = [],
      ny = [];
    for (let i = 0; i < spine.pts.length; i++) {
      const a = spine.pts[Math.max(0, i - 2)],
        b = spine.pts[Math.min(spine.pts.length - 1, i + 2)],
        h = Math.atan2(b.y - a.y, b.x - a.x);
      nx.push(-Math.sin(h));
      ny.push(Math.cos(h));
    }
    let laidFibres = 0;
    for (let f = 0; f < F; f++) {
      // Staggered start/end: fibres join from the base over time (accretion).
      // Fibres emerge from the mat, not from one point at the node.
      let s0 = Math.min(Lc * 0.5, (o.emerge ?? 0) * (0.6 + 0.8 * R()) + Lc * 0.3 * Math.pow(R(), 2.2));
      const
        s1 = Lc * (1 - (o.link ? 0.06 + 0.5 * Math.pow(R(), 1.3) : 0.45 * Math.pow(R(), 2.4))),
        delay = (f / F) * o.accrete + R() * 3,
        pts = [];
      let u = (R() - 0.5) * 1.6,
        du = 0;
      // Fibres ride the undisplaced centre; inside a braid each joins the
      // sub-cord its lateral position falls in.
      const binOf = (q) => Math.min(q.ways - 1, Math.floor(((u + 1) / 2) * q.ways));
      // A fibre of another sub-cord starts in the shared bundle before the eye.
      for (const q of islands) if (s0 > q.sa && s0 < q.sb && binOf(q) !== q.follow) s0 = Math.max(0, q.sa - 6);
      for (let i = 0; i < spine.pts.length; i += 2) {
        const s = spine.arcs[i];
        if (s < s0) continue;
        if (s > s1) break;
        du = du * 0.88 + gauss(R) * 0.045;
        u += du;
        if (u > 0.92 || u < -0.92) {
          du = -du * 0.5;
          u = Math.max(-0.92, Math.min(0.92, u));
        }
        let lateral = 0,
          squeeze = 0;
        for (const q of islands) {
          const e = eye(q, s);
          if (!e) continue;
          const b = binOf(q);
          // Offset relative to the (possibly displaced) spine at this point.
          lateral += binOffset(q, b, s) - binOffset(q, q.follow, s);
          squeeze = Math.max(squeeze, e);
        }
        const half = (width(s) / 2) * (1 - 0.45 * squeeze),
          q = spine.pts[i];
        pts.push({ x: q.x + nx[i] * (u * half + lateral), y: q.y + ny[i] * (u * half + lateral) });
      }
      if (pts.length < 3) continue;
      const startAt = along(spine, s0);
      pts[0] = startAt.v; // fibres attach to the centre line: reachable by pulses
      const fw = 0.22 + R() * 0.28;
      const P = lay(pts, {
        kind: "cord-fibre", cord: -1, group: id, s0, t0: o.t0 + s0 / o.v + delay, v: o.v, w0: fw * 1.15, w1: fw * 0.65, a0: CORD_A + R() * 0.07, a1: 0.06,
        tone: f % 9 === 0 ? 1 : 0, source: from.source, unit: o.unit,
      });
      if (P) laidFibres++;
      if (P && R() < 0.7) fringe(P, (1 / 15) * o.fringe, { len: 10, maxLen: 40, w: 0.19, a: 0.24 });
      budget() && (yield "fibre");
    }
    // Brush tip: 2-5 fibrous fingers splaying forwards.
    if (o.brush) {
      const end = spine.pts.at(-1),
        prev = spine.pts.at(-6) || spine.pts[0],
        h = Math.atan2(end.y - prev.y, end.x - prev.x),
        fingers = 2 + Math.floor(R() * 4),
        tEnd = spine.done;
      for (let k = 0; k < fingers; k++) {
        const fh = h + (k - (fingers - 1) / 2) * (0.2 + R() * 0.16) + gauss(R) * 0.06,
          fl = 30 + R() * 55;
        for (let j = 0; j < 4 + Math.floor(R() * 4); j++) {
          const P = hypha({ x: end.x + gauss(R) * 1.2, y: end.y + gauss(R) * 1.2 }, fh + gauss(R) * 0.05, fl * (0.6 + R() * 0.5), {
            kind: "brush", t0: tEnd + j * 1.5, v: o.v * 0.8, w0: 0.42, w1: 0.16, a0: 0.3, a1: 0.1, tone: 0, source: from.source, unit: o.unit,
            walk: { k: 0.01, run: 50, jit: 0.02 }, forkMean: 30, forkAngle: [0.3, 0.55], firstFork: 10,
          }, 1);
          if (P) {
            edge(end, P.pts[0], 0.3, 0.2, 0, -1, tEnd);
            fringe(P, 1 / 14, { len: 6, maxLen: 18, w: 0.17, a: 0.24 });
          }
        }
      }
    }
    // Fine fused web alongside the cord (owner item 7): acute, long-running, 2-3 orders.
    const webCount = Math.round((Lc / 6) * o.web);
    for (let k = 0; k < webCount; k++) {
      const s = Lc * (0.05 + 0.9 * R()),
        at = along(spine, s),
        side = R() < 0.5 ? -1 : 1,
        off = width(s) * 0.45 * side,
        start = { x: at.v.x + nx[at.i] * off, y: at.v.y + ny[at.i] * off },
        ph = at.h + side * (0.22 + R() * 0.3);
      const P = hypha(start, ph, 25 + -Math.log(1 - R()) * 45, {
        kind: "web", t0: at.t + 4 + R() * 10, v: o.v * 0.6, w0: 0.36, w1: 0.17, a0: 0.34, a1: 0.14, tone: 0, source: from.source, fuse: 2.4, thin: 0.15, unit: o.unit,
        walk: { k: 0.014, run: 50, jit: 0.03, pull: (x, y, mean) => Math.atan2(Math.sin(at.h - mean), Math.cos(at.h - mean)) * 0.012 },
        forkMean: 26, forkAngle: [0.26, 0.5], firstFork: 8, alongside: true,
      }, 2);
      if (P) edge(at.v, P.pts[0], 0.2, 0.15, 0, -1, P.t0);
      budget() && (yield "web");
    }
    C.width0 = o.w0;
    C.width1 = o.w1;
    C.fibres = laidFibres; // = paths with group id (only this loop lays them); r11.1: no full scan of paths
    C.brush = !!o.brush;
    C.link = !!o.meet && !o.tributary;
    C.tributary = !!o.tributary;
    C.islands = islands.length;
    // Tributary cords join mid-way from the surrounding mat/web.
    const tribs = o.tributary ? 0 : (o.tribs ?? 0) * (R() < 0.75 ? 1 : 0) + (R() < 0.3 ? 1 : 0) * Math.min(1, o.tribs ?? 0);
    for (let k = 0; k < tribs; k++) {
      const at = along(spine, Lc * (0.3 + 0.45 * R())),
        side = R() < 0.5 ? -1 : 1,
        back = at.h + Math.PI + side * (0.55 + R() * 0.5),
        dist = 70 + R() * 70,
        src = nearest(at.v.x + Math.cos(back) * dist, at.v.y + Math.sin(back) * dist, 30, (v) => v.owner !== undefined && v.owner >= 0);
      if (!src) continue;
      const tv = o.v * 0.8,
        tl = Math.hypot(at.v.x - src.x, at.v.y - src.y);
      yield* cord(src, at.v, { meet: at.v, tributary: true, braid: false, emerge: 0, t0: Math.max(o.t0, at.t - tl / tv + 6), v: tv, unit: o.unit, w0: 2.4 + R() * 1.1, w1: 1.2, fibres: 6 + Math.floor(R() * 5), accrete: 14, fringe: 1, web: 0.4, brush: false });
    }
    return C;
  }
  const linkCords = [];
  for (const [ia, ib] of links) {
    const A = colonies[ia],
      B = colonies[ib],
      a = A.node,
      b = B.node,
      f = 0.42 + R() * 0.16,
      dx = b.x - a.x,
      dy = b.y - a.y,
      len = Math.hypot(dx, dy);
    let meet = { x: a.x + dx * f + (-dy / len) * gauss(R) * len * 0.07, y: a.y + dy * f + (dx / len) * gauss(R) * len * 0.07 };
    if (ia === 4 && ib === 5) meet = { x: (L.textLeft + L.textRight) / 2, y: Hh - 26 }; // run beneath the copy
    const meetV = vertex(meet.x, meet.y, ia);
    const halves = [];
    for (const [from, C0, other] of [[a, A, B], [b, B, A]]) {
      const dist = Math.hypot(meet.x - from.x, meet.y - from.y),
        v = 6.2 * (0.9 + R() * 0.2),
        t0 = C0.start + 7;
      halves.push(yield* cord(from, meetV, { meet: meetV, emerge: C0.R0 * 0.3, t0, v, w0: CORD_W0 + R() * 2, w1: 1.6, fibres: 23 + Math.floor(R() * 6), accrete: 40, fringe: 1.2, web: 0.55, brush: false, tribs: 1, link: true }));
      void dist;
      void other;
    }
    const [ha, hb] = halves;
    if (ha && hb) {
      meetV.birth = Math.min(ha.spine.done, hb.spine.done);
      linkCords.push({ id: linkCords.length, a, b, poly: [...ha.poly, ...hb.poly.slice(0, -1).reverse()], length: ha.length + hb.length, halves });
    }
  }
  yield "cords";
  // Tributary cords off link halves, and dead-end exploring cords to the edges.
  for (const C of cords.slice()) {
    if (R() < 0.6) {
      const s = C.length * (0.3 + R() * 0.3),
        at = along(C.spine, s),
        h = at.h + (R() < 0.5 ? -1 : 1) * (0.3 + R() * 0.3),
        l = 90 + R() * 130;
      yield* cord(at.v, { x: at.v.x + Math.cos(h) * l, y: at.v.y + Math.sin(h) * l }, { t0: at.t + 6, v: 5.2, w0: 3 + R() * 1.4, w1: 1.2, fibres: 11 + Math.floor(R() * 6), accrete: 30, fringe: 1.3, web: 0.45, brush: true });
    }
    budget() && (yield "tributary");
  }
  const deadEnds = [];
  for (const c of colonies) {
    const count = 1 + Math.floor(R() * (mobile ? 1 : 2));
    for (let k = 0; k < count; k++) {
      // Choose an outward direction with the least text and existing cords.
      let best = null;
      for (let trial = 0; trial < 14; trial++) {
        const h = R() * 6.28,
          l = (mobile ? 110 : 200) + R() * (mobile ? 100 : 180);
        let score = 0;
        for (let s = 40; s <= l; s += 40) {
          const x = c.node.x + Math.cos(h) * s,
            y = c.node.y + Math.sin(h) * s;
          score += protect(x, y) * (y > -10 && y < H ? 1 : 0.2);
          for (const o of colonies) if (o !== c && Math.hypot(x - o.node.x, y - o.node.y) < o.R0) score -= 1.5;
        }
        for (const d of deadEnds) if (d.c === c && Math.abs(Math.atan2(Math.sin(d.h - h), Math.cos(d.h - h))) < 0.7) score -= 4;
        if (!best || score > best.score) best = { h, l, score };
      }
      deadEnds.push({ c, h: best.h });
      const at = { x: c.node.x + Math.cos(best.h) * best.l, y: c.node.y + Math.sin(best.h) * best.l };
      const unit = units.push({ id: units.length, paths: [], kind: "dead-end" }) - 1;
      units[unit].period = 1e9; // dead-end cords persist; their explorers recycle
      yield* cord(c.node, at, { emerge: c.R0 * 0.3, t0: c.start + 18 + R() * 30, v: 4.8, w0: 3.5 + R() * 1.5, w1: 1.2, fibres: 13 + Math.floor(R() * 6), accrete: 45, fringe: 1.6, web: 0.9, brush: true });
      budget() && (yield "dead-end");
    }
  }
  yield "dead-ends";

  // ---- dense-zone mat: fine anastomosing cobweb around colonies and along cords ----
  const matR = rng("r10-dense-mat");
  const anchorsFor = cords.flatMap((C) => C.spine.pts.filter((_, i) => i % 3 === 0).map((v) => ({ v, h: null, C })));
  const matCount = mobile ? 1000 : Math.round(MAT * areaScale * areaScale);
  for (let k = 0; k < matCount; k++) {
    let start, h, t0, src;
    if (matR() < 0.7) {
      const c = colonies[Math.floor(matR() * colonies.length)],
        a = matR() * 6.28,
        r = c.R0 * (0.12 + 0.85 * Math.pow(matR(), 0.75));
      start = nearest(c.node.x + Math.cos(a) * r, c.node.y + Math.sin(a) * r, 10, (v) => v.owner !== undefined && v.owner >= 0);
      if (!start) continue;
      h = Math.atan2(start.y - c.node.y, start.x - c.node.x) + gauss(matR) * 0.7;
      src = c.i;
    } else if (anchorsFor.length) {
      const an = anchorsFor[Math.floor(matR() * anchorsFor.length)],
        i = an.C.spine.pts.indexOf(an.v),
        b = an.C.spine.pts[Math.min(i + 2, an.C.spine.pts.length - 1)],
        ch = Math.atan2(b.y - an.v.y, b.x - an.v.x),
        side = matR() < 0.5 ? -1 : 1,
        off = (an.C.width0 || 4) * (0.4 + matR() * 2.2) * side;
      start = nearest(an.v.x - Math.sin(ch) * off, an.v.y + Math.cos(ch) * off, 9, (v) => v.owner !== undefined && v.owner >= 0) || an.v;
      h = ch + side * (0.15 + matR() * 0.5) + (matR() < 0.5 ? Math.PI : 0) * (matR() < 0.3 ? 1 : 0);
      src = start.source;
    }
    if (!start) continue;
    t0 = (start.birth || 0) + 6 + matR() * 14;
    const P = hypha(start, h, 18 + -Math.log(1 - matR()) * 34, {
      kind: "mat", t0, v: 3.4, w0: 0.26, w1: 0.15, a0: 0.28, a1: 0.11, tone: matR() < 0.1 ? 1 : 0, source: src, fuse: 2.6, thin: 0.4,
      walk: { k: 0.018, run: 35, jit: 0.035 }, forkMean: 22, forkAngle: [0.3, 0.6], firstFork: 8, forkLag: 1.5,
    }, 1);
    const mc = colonies[src];
    if (P && mc) matTime(P, mc, Math.min(1, Math.hypot(start.x - mc.node.x, start.y - mc.node.y) / mc.R0));
    if (budget()) yield "mat";
  }
  // ---- sparse ref-8 vocabulary: long fine explorers from rims and cords into gaps ----
  const sparse = (o) => ({
    w0: 0.6, w1: 0.24, a0: 0.3, a1: 0.12, tone: 0, explorer: true, fuse: 2.2, thin: 0.25, below: 0,
    walk: { k: 0.016, run: 60, jit: 0.038, vary: true }, forkMean: 58, forkAngle: [0.26, 0.5], forkLag: 3, firstFork: 30, alongside: true, ...o,
  });
  const explorerRoots = [];
  const sources = [];
  for (const C of cords) for (let k = 0; k < 2; k++) sources.push({ C, s: C.length * (0.15 + 0.8 * R()) });
  for (const c of colonies) for (let k = 0; k < (mobile ? 3 : 5); k++) sources.push({ c, h: R() * 6.28 });
  for (const src of sources) {
    let start, h, t0, from;
    if (src.C) {
      const at = along(src.C.spine, src.s);
      from = at.v;
      h = at.h + (R() < 0.5 ? -1 : 1) * (0.5 + R() * 0.7);
      t0 = at.t + 8;
    } else {
      const c = src.c,
        r = c.R0 * (0.7 + R() * 0.3);
      from = nearest(c.node.x + Math.cos(src.h) * r, c.node.y + Math.sin(src.h) * r, 18, (v) => v.owner !== undefined && v.owner >= 0) || c.node;
      h = Math.atan2(from.y - c.node.y, from.x - c.node.x) + gauss(R) * 0.3;
      t0 = (from.birth || c.start) + 4;
    }
    start = from;
    const total = (mobile ? 200 : 260) + R() * (mobile ? 160 : 270),
      trunkLen = total * (0.45 + R() * 0.15),
      unitId = units.push({ id: units.length, paths: [], kind: "explorer" }) - 1;
    const trunk = hypha(start, h, trunkLen, sparse({ kind: "explorer", t0, v: 4.4, source: from.source, w0: 0.66, w1: 0.42, a1: 0.24, fuseAfter: 1e9 }), 1);
    if (!trunk) continue;
    explorerRoots.push(trunk);
    if (trunk.fused || trunk.pts.length < 4) continue;
    // The distal continuation (and its forks) is a recycling unit far from the core.
    const end = trunk.pts.at(-1),
      eh = Math.atan2(end.y - trunk.pts.at(-3).y, end.x - trunk.pts.at(-3).x),
      cont = hypha(end, eh, total - trunkLen, sparse({ kind: "explorer", t0: trunk.done, v: 2.4, unit: unitId, source: from.source, w0: 0.42 }), 1);
    void cont;
    budget() && (yield "explorer");
  }
  // Fine laterals on explorers (random sides, Poisson spacing): no lone hairlines.
  const fineR = rng("r10-explorer-laterals");
  const explorerAudit = rootLaterals(explorerRoots, (parent, start, angle, length, at) => {
    lay(persistWalk(start, angle, length, fineR, { k: 0.02, run: 25, jit: 0.035, step: 2 }), {
      kind: "explorer-hair", t0: parent.t0 + parent.arcs[at] / parent.v + 2, v: parent.v, w0: 0.24, w1: 0.16, a0: 0.24, a1: 0.12, tone: 0, unit: parent.unit, explorer: true, branch: parent.branch,
    });
  }, fineR);
  yield "explorers";
  // ---- r11 persistent fuzz: every persistent strand carries short, fine, acute
  // hairs at about the density the pulse makes visible (owner: no bare lines).
  for (const P of paths.slice()) {
    if (P.fringed || P.len < 24) continue;
    const k = P.kind;
    let rate = 0;
    if (k === "explorer") rate = FUZZ_RATE * 1.15;
    else if (k === "web" || k === "brush") rate = FUZZ_RATE * 1.35;
    else if (k === "mat") rate = FUZZ_RATE * 0.9;
    else if (k === "cord-fibre") rate = R() < 0.4 ? FUZZ_RATE * 0.8 : 0;
    if (!rate) continue;
    P.fringed = true;
    fringe(P, rate, { len: 6.5, maxLen: 20, w: 0.19, a: 0.3 });
    budget() && (yield "fuzz");
  }
  // ---- r11 hyphal knots: small tangled balls ON strands and cords and inside
  // mats, in small irregular groups (shared recipe: r11-coordination.md). The
  // wound fibres are graph edges (a passing pulse lights them); soft lobes are
  // drawn with the fibres once they are laid.
  const knotR = rng("r11-hyphal-knots"),
    knots = [];
  function knot(v, h, t0, src, primordium = false) {
    // Never under or near text (the text-safe field is ~1 well clear of copy).
    if (protect(v.x, v.y) < 0.97) return null;
    const r = primordium ? 2.5 + knotR() * 1.5 : 1 + 2 * Math.pow(knotR(), 2.2),
      el = primordium ? 1.4 + knotR() * 0.6 : 1 + knotR() * 0.6,
      off = (knotR() - 0.5) * r * 0.7,
      c = { x: v.x - Math.sin(h) * off, y: v.y + Math.cos(h) * off },
      K = { x: c.x, y: c.y, r, el, h, v: v.id, t0, source: src, primordium, lobes: [], fibres: 0 };
    const nl = 3 + Math.floor(knotR() * 4);
    for (let i = 0; i < nl; i++) {
      const a = knotR() * 6.28,
        d = r * (0.2 + knotR() * 0.4);
      K.lobes.push({ x: c.x + Math.cos(a) * d * el, y: c.y + Math.sin(a) * d, rx: r * (0.6 + knotR() * 0.4) * el, ry: r * (0.6 + knotR() * 0.4), a: 0.55 + knotR() * 0.35 });
    }
    const nf = 3 + Math.floor(knotR() * 5);
    let first = null;
    for (let f = 0; f < nf; f++) {
      // A wound fibre: leaves the host strand, curls tangentially around the
      // ball (curvature 0.6-1.4 rad per r), and the first two exit again.
      const kappa = ((0.6 + knotR() * 0.8) / r) * (knotR() < 0.5 ? -1 : 1),
        arc = r * (1.5 + knotR() * 2) * (primordium ? 1.6 : 1),
        exit = f < 2 ? r * (2 + knotR() * 3) : 0,
        ds = Math.min(0.7, r / 3),
        pts = [v];
      let x = v.x,
        y = v.y,
        hh = h + (knotR() < 0.5 ? 0 : Math.PI) + gauss(knotR) * 0.5;
      for (let s = 0; s < arc + exit; s += ds) {
        const inBall = s < arc,
          dx = c.x - x,
          dy = c.y - y,
          dd = Math.hypot(dx, dy);
        hh += (inBall ? kappa : kappa * 0.12) * ds;
        if (inBall && dd > r * 1.1) hh += Math.atan2(Math.sin(Math.atan2(dy, dx) - hh), Math.cos(Math.atan2(dy, dx) - hh)) * 0.35;
        x += Math.cos(hh) * ds;
        y += Math.sin(hh) * ds;
        pts.push({ x, y });
      }
      const P = lay(pts, { kind: "knot-hair", t0: t0 + f * 0.6, v: 1.2, w0: primordium ? 0.46 : 0.38, w1: 0.26, a0: 0.46, a1: 0.3, tone: f % 3 === 0 ? 1 : 0, source: src, cord: -1 });
      if (!P) continue;
      K.fibres++;
      if (!first) first = P;
      P.fringed = true;
    }
    if (!first) return null;
    first.knot = K;
    K.path = first;
    knots.push(K);
    return K;
  }
  const group = (anchor, h, t0, src, pick) => {
    const n = 1 + Math.floor(Math.pow(knotR(), 1.6) * 4);
    for (let i = 0; i < n; i++) {
      const v = i ? pick(6 + knotR() * 24) : anchor;
      if (v) knot(v, h, t0 + i * (1 + knotR() * 3), src);
    }
  };
  for (const C of cords) {
    if ((C.tributary || !C.link) && knotR() < 0.6) continue;
    let s = C.length * 0.15 + -Math.log(1 - knotR()) * 320;
    while (s < C.length - 12) {
      const at = along(C.spine, s);
      group(at.v, at.h, at.t + 14 + knotR() * 26, at.v.source, (d) => along(C.spine, Math.min(C.length, s + d)).v);
      s += 170 + -Math.log(1 - knotR()) * 420;
    }
  }
  for (const c of colonies) {
    const groups = (mobile ? 1 : 1) + Math.floor(knotR() * 3);
    for (let k = 0; k < groups; k++) {
      const a = knotR() * 6.28,
        r = c.R0 * (0.25 + 0.75 * knotR()),
        x = c.node.x + Math.cos(a) * r,
        y = c.node.y + Math.sin(a) * r,
        junction = (v) => v.owner !== undefined && v.owner >= 0 && v.source === c.i && v.edges.length >= 3,
        v = nearest(x, y, 12, junction) || nearest(x, y, 8, (q) => q.owner !== undefined && q.owner >= 0);
      if (!v) continue;
      group(v, a + Math.PI / 2, c.start + 24 + knotR() * 30, c.i, (d) => {
        const b = knotR() * 6.28;
        return nearest(v.x + Math.cos(b) * d, v.y + Math.sin(b) * d, 8, junction);
      });
    }
    // Rare elongated primordia inside the mat (owner): a few, never beads.
    const prim = knotR() < 0.45 ? 1 : 0;
    for (let k = 0; k < prim; k++) {
      const a = knotR() * 6.28,
        r = c.R0 * (0.2 + 0.4 * knotR()),
        v = nearest(c.node.x + Math.cos(a) * r, c.node.y + Math.sin(a) * r, 10, (q) => q.owner !== undefined && q.owner >= 0 && q.source === c.i);
      if (v) knot(v, a + gauss(knotR) * 0.6, c.start + 40 + knotR() * 30, c.i, true);
    }
  }
  for (const P of explorerRoots)
    if (knotR() < 0 && P.len > 80) { // r11: no knots out in the dark gaps
      const at = along(P, P.len * (0.2 + 0.6 * knotR()));
      group(at.v, at.h, at.t + 10 + knotR() * 20, P.pts[0].source, (d) => along(P, Math.min(P.len, P.len * 0.2 + d)).v);
    }
  yield "knots";
  /** r11.2 (owner): density must fall monotonically from the colony centre,
   * with no fainter ring around the dense core ("max dense at the point,
   * lowering further away"). The cotton falls steeply while the radiating
   * hyphae, mat and web peak at 0.25-0.5 R0, which left a dip ring. Each
   * colony's isotropic layers are rastered offscreen (half scale, additive,
   * as the page), their luminance measured in 8 px rings (median over 24 sectors, so
   * cords and single strands do not count), and the alpha of every ring is
   * raised toward the upper envelope seen from outside (falling >= 4% per
   * ring; raise only, so the change can be added to what the intro already
   * drew); up to five passes; then a soft radial felt glow fills what
   * remains. Link and
   * dead-end cords strengthen toward the node and overlap the mat edge (they
   * still start beyond the centre: no star point). Alpha only: no geometry or
   * RNG changes. */
  function* monotoneColonies() {
    const ISO = /^(hair|colony|mat|web|explorer-hair|knot-hair)$/,
      STEP = 8,
      MQ = 0.5,
      SECT = 24,
      reach = colonies.map((c) => Math.min(220, c.R0 * 1.15));
    const owner = (x, y) => {
      let best = -1,
        bu = 1;
      for (let i = 0; i < colonies.length; i++) {
        const nd = colonies[i].node,
          u = Math.hypot(x - nd.x, y - nd.y) / reach[i];
        if (u < bu) {
          bu = u;
          best = i;
        }
      }
      return best;
    };
    // Everything near a colony is rastered (cords and their web included, as
    // the page shows them); only the isotropic layers are adjusted.
    const mine = colonies.map(() => []),
      near = colonies.map(() => []);
    let n = 0;
    for (const P of paths) {
      if (++n % 1500 === 0 && budget()) yield "monotone";
      const m = P.pts[P.pts.length >> 1];
      // Recycling explorer units live on the life layer, not in the static
      // colony: the profile is measured on what stays.
      const rest = !(P.unit >= 0 && units[P.unit]?.kind === "explorer");
      for (let i = 0; rest && i < colonies.length; i++) {
        const nd = colonies[i].node,
          r = reach[i] + 40;
        if (P.box[0] > nd.x + r || P.box[2] < nd.x - r || P.box[1] > nd.y + r || P.box[3] < nd.y - r) continue;
        near[i].push(P);
      }
      if (!rest || !ISO.test(P.kind)) continue;
      const c = owner(m.x, m.y);
      if (c >= 0) mine[c].push(P);
    }
    // All changes go to work copies (P.aw) and are committed in one step at
    // the end (the intro may draw these paths meanwhile; the scene re-bakes
    // the static raster afterwards, see rebakeStatic).
    const work = new Set();
    const aw = (P) => {
      if (!P.aw) {
        P.aw = Float32Array.from(P.a);
        work.add(P);
      }
      return P.aw;
    };
    // Cords strengthen toward the node and overlap the mat edge.
    n = 0;
    for (const P of paths) {
      if (++n % 3000 === 0 && budget()) yield "monotone";
      if (P.kind !== "cord-fibre" && P.kind !== "cord-centre") continue;
      for (let i = 0; i < P.pts.length; i++) {
        const q = P.pts[i],
          c = owner(q.x, q.y);
        if (c < 0) continue;
        const r = Math.hypot(q.x - colonies[c].node.x, q.y - colonies[c].node.y);
        const W = aw(P);
        W[i] = Math.min(0.95, W[i] * (1 + 0.5 * (1 - smooth((r - 0.15 * colonies[c].R0) / (0.35 * colonies[c].R0)))));
      }
    }
    for (const list of mine) for (const P of list) aw(P);
    const fills = new Map();
    const commit = () => {
      for (const P of work) {
        P.aOld = P.a;
        P.a = P.aw;
        P.aw = null;
        P.drawnAtCommit = P.drawn || 0;
      }
      for (const [c, f] of fills) c.fill = f;
    };
    if (typeof document === "undefined") return commit();
    const statics = new Map(),
      preMemo = new Map();
    const lin = new Float32Array(256);
    for (let i = 0; i < 256; i++) lin[i] = i / 255 <= 0.04045 ? i / 255 / 12.92 : ((i / 255 + 0.055) / 1.055) ** 2.4;
    const cv = document.createElement("canvas");
    for (let ci = 0; ci < colonies.length; ci++) {
      const c = colonies[ci],
        nd = c.node,
        Rm = reach[ci],
        K = Math.floor(Rm / STEP),
        S = Math.ceil((2 * Rm + 8) * MQ), // measured at half scale (MQ): 1/4 the pixels and garbage
        gain = new Float64Array(K).fill(1);
      cv.width = cv.height = S;
      const x = cv.getContext("2d", { willReadFrequently: true });
      const measure = function* () {
        x.setTransform(1, 0, 0, 1, 0, 0);
        x.globalCompositeOperation = "source-over";
        x.globalAlpha = 1;
        x.fillStyle = "#000"; // opaque: measure what the page shows over its dark ground
        x.fillRect(0, 0, S, S);
        x.setTransform(MQ, 0, 0, MQ, S / 2 - nd.x * MQ, S / 2 - nd.y * MQ);
        x.globalCompositeOperation = "lighter";
        x.strokeStyle = "#35df47"; // the dark-green glow (saturation as on the page)
        x.lineCap = "butt";
        const groups = new Map();
        let k = 0;
        for (const P of near[ci]) {
          // What stays in the static raster (living parts come and go).
          const ext = statics.get(P) ?? statics.set(P, preBaseOf(P, paths, preMemo)).get(P);
          for (let i = 0; i < P.pts.length - 1 && P.arcs[i] < ext; i += CHUNK) {
            const j = Math.min(P.pts.length - 1, i + CHUNK),
              mid = (i + j) >> 1,
              key = (P.tone ? 1e6 : 0) + Math.round(Math.min(1, (P.aw || P.a)[mid]) * 120) * 1000 + Math.round(P.w[mid] * 20);
            // Groups split every ~1500 segments: each stroke is a bounded raster.
            let g = groups.get(key);
            if (!g || g.n > 1500) groups.set(key, (g = { path: new Path2D(), n: 0, prev: g || null }));
            g.path.moveTo(P.pts[i].x, P.pts[i].y);
            for (let q = i + 1; q <= j; q++) g.path.lineTo(P.pts[q].x, P.pts[q].y);
            g.n += j - i;
          }
          if (++k % 400 === 0 && budget()) yield "monotone";
        }
        // Stroked in flushed batches (a 1 px read forces the raster), so no
        // slice holds the whole colony's raster work.
        let b = 0;
        for (const [key, last] of groups) {
          // The page's dark-green tones: the core tone (cords) is much brighter.
          x.strokeStyle = key >= 1e6 ? "#a4ffad" : "#35df47";
          x.globalAlpha = Math.floor((key % 1e6) / 1000) / 120;
          x.lineWidth = (key % 1000) / 20;
          for (let g = last; g; g = g.prev) {
            x.stroke(g.path);
            b += g.n;
            if (b > 1500) {
              b = 0;
              x.getImageData(0, 0, 1, 1);
              if (budget()) yield "monotone";
            }
          }
        }
        // The soft core glow the page adds (drawCores), so the envelope sees it.
        const cr = Math.max(20, Math.min(40, c.R0 * 0.2)),
          cg = x.createRadialGradient(nd.x, nd.y, 0, nd.x, nd.y, cr);
        cg.addColorStop(0, "#35df47");
        cg.addColorStop(0.45, "#35df47");
        cg.addColorStop(1, "rgba(0,0,0,0)");
        x.globalAlpha = 0.15 * protect(nd.x, nd.y);
        x.fillStyle = cg;
        x.fillRect(nd.x - cr, nd.y - cr, 2 * cr, 2 * cr);
        x.globalAlpha = 1;
        x.getImageData(0, 0, 1, 1);
        if (budget()) yield "monotone";
        return yield* ringProfile();
      };
      // Ring x sector cell of every pixel (computed once per colony).
      const cells = new Int16Array(S * S).fill(-1),
        cnt = new Float64Array(K * SECT);
      for (let yy = 0; yy < S; yy++) {
        for (let xx = 0; xx < S; xx++) {
          const dx = (xx + 0.5 - S / 2) / MQ,
            dy = (yy + 0.5 - S / 2) / MQ,
            kk = Math.floor(Math.hypot(dx, dy) / STEP);
          if (kk >= K) continue;
          const cell = kk * SECT + (Math.floor(((Math.atan2(dy, dx) + Math.PI) / (2 * Math.PI)) * SECT) % SECT);
          cells[yy * S + xx] = cell;
          cnt[cell]++;
        }
        if ((yy & 15) === 15 && budget()) yield "monotone";
      }
      const ringProfile = function* () {
        const d = x.getImageData(0, 0, S, S).data,
          sum = new Float64Array(K * SECT);
        if (budget()) yield "monotone";
        for (let yy = 0; yy < S; yy++) {
          for (let xx = 0, i = yy * S, o = i * 4; xx < S; xx++, i++, o += 4) {
            const cell = cells[i];
            if (cell >= 0) sum[cell] += 0.2126 * lin[d[o]] + 0.7152 * lin[d[o + 1]] + 0.0722 * lin[d[o + 2]];
          }
          if ((yy & 31) === 31 && budget()) yield "monotone";
        }
        const out = [];
        for (let kk = 0; kk < K; kk++) {
          const v = [];
          for (let sct = 0; sct < SECT; sct++) if (cnt[kk * SECT + sct]) v.push(sum[kk * SECT + sct] / cnt[kk * SECT + sct]);
          v.sort((a, b) => a - b);
          out.push(v.length ? (v[(v.length - 1) >> 1] + v[v.length >> 1]) / 2 : 0);
        }
        return out;
      };
      let M = null;
      for (let pass = 0; pass < 5; pass++) {
        M = yield* measure();
        // Target (raise only): the upper envelope seen from outside, falling
        // at least 4% per ring toward the rim; inner rings rise to it.
        const env = M.slice();
        for (let k = K - 2; k >= 0; k--) env[k] = Math.max(M[k], env[k + 1] * 1.04);
        let changed = false;
        const step = M.map((m, k) => {
          const gl = m > 1e-6 ? env[k] / m : 1;
          if (gl > 1.005) changed = true;
          // Raise only (the intro may already have drawn these paths; the
          // committed change is added on top, which cannot take ink away).
          return Math.max(1, Math.min(2, Math.pow(gl, 0.85))); // luminance grows faster than ink: damped
        });
        if (!changed) break;
        for (const P of mine[ci])
          for (let i = 0; i < P.pts.length; i++) {
            const q = P.pts[i],
              u = Math.hypot(q.x - nd.x, q.y - nd.y) / STEP - 0.5,
              k0 = Math.max(0, Math.min(K - 1, Math.floor(u))),
              f = Math.max(0, Math.min(1, u - k0)),
              gk = step[k0] + (step[Math.min(K - 1, k0 + 1)] - step[k0]) * f;
            if (gk !== 1) P.aw[i] = Math.min(0.95, P.aw[i] * gk);
          }
        for (let k = 0; k < K; k++) gain[k] *= step[k];
      }
      // Residual dips (where cords and web, which are not scaled, set the
      // level) are filled by a soft radial felt glow under the strands, sized
      // per ring so the profile falls >= 12% per 8 px ring inside 0.5 R0 (the
      // mat, where a dip ring read) and >= 5% beyond: the colony's own glow,
      // extended (never a ring or a disc edge).
      const base = document.createElement("canvas");
      base.width = base.height = S;
      yield* measure(); // strands alone
      base.getContext("2d").drawImage(cv, 0, 0);
      const fillA = new Float64Array(K);
      const fill = { r: K * STEP, stops: [] },
        fc = { node: nd, fill };
      fills.set(c, fill);
      for (let pass = 0; pass < 5; pass++) {
        fill.stops = Array.from(fillA, (a, k) => [((k + 0.5) * STEP) / (K * STEP), a]);
        x.setTransform(1, 0, 0, 1, 0, 0);
        x.globalCompositeOperation = "source-over";
        x.drawImage(base, 0, 0);
        x.setTransform(MQ, 0, 0, MQ, S / 2 - nd.x * MQ, S / 2 - nd.y * MQ);
        x.globalCompositeOperation = "lighter";
        colonyFill(x, fc, "53,223,71", 1);
        M = yield* ringProfile();
        const env = M.slice();
        for (let k = K - 2; k >= 0; k--) env[k] = Math.max(M[k], env[k + 1] * ((k + 1) * STEP < 0.5 * c.R0 ? 1.12 : 1.05));
        let worst = 0;
        for (let k = 0; k < K; k++) {
          const need = env[k] - M[k];
          worst = Math.max(worst, need / Math.max(1e-6, M[k]));
          // d(luminance)/d(alpha) of the glow over a dark base is ~0.2: damped.
          fillA[k] = Math.min(0.6, fillA[k] + (need / 0.2) * 0.8);
        }
        if (worst < 0.01) break;
        if (budget()) yield "monotone";
      }
      fill.stops = Array.from(fillA, (a, k) => [((k + 0.5) * STEP) / (K * STEP), a]);
      base.width = base.height = 0;
      c.monotoneGain = Array.from(gain);
      c.radialProfile = M;
    }
    cv.width = cv.height = 0;
    commit();
  }
  // r11.2c: the colony monotone pass (~1.3 s of slices) runs after the scene
  // is ready (scene.monotone), not inside the build: it held the compositor's
  // attach back and the field's load-in with it. Its alphas reach the
  // visible raster through the cross-faded re-bake.
  // r11.2 mat schedule, last step: a shifted host's whole subtree (forks,
  // hairs and mats inheriting its shift) must be complete by MAT_DONE of the
  // intro. A late fork could inherit a shift that pushed it past the intro end
  // (6% of a 1440 colony's mat ink was still missing 3 s after it). The
  // subtree moves earlier as one, so nothing starts before its parent part.
  {
    const LIMIT = ageOfIntro(MAT_DONE),
      kids = new Map(),
      roots = [];
    for (const P of paths) {
      if (!(P.matShift > 0)) continue;
      if (P.matHost && P.matHost.matShift === P.matShift) (kids.get(P.matHost) || kids.set(P.matHost, []).get(P.matHost)).push(P);
      else roots.push(P);
    }
    const subEnd = (P) => {
      let e = P.t0 + P.matShift + P.len / P.v;
      for (const k of kids.get(P) || []) e = Math.max(e, subEnd(k));
      return e;
    };
    const shiftAll = (P, by) => {
      P.matShift -= by;
      for (const k of kids.get(P) || []) shiftAll(k, by);
    };
    for (const P of roots) {
      const over = subEnd(P) - LIMIT;
      if (over > 0) shiftAll(P, Math.min(over, P.matShift));
    }
  }
  // Recycle units: per-unit period and phase from independent hashes.
  for (const u of units) {
    if (!u.paths.length) continue;
    u.start = Math.min(...u.paths.map((p) => p.t0));
    u.grow = Math.max(...u.paths.map((p) => p.done)) - u.start;
    if (u.kind === "explorer") u.period = u.grow + 20 + hash01(u.id * 7 + 3) * 55;
  }
  return {
    vs, ss, paths, units, nodes, colonies, cords, linkCords, vertex, edge, fusions, explorerRoots, explorerAudit, L, protect, knots,
    monotone: monotoneColonies,
  };
}

/** r11.2: the static extent a path keeps whatever the life assignment
 * decides: a tip candidate its shortest possible base (pins only lengthen a
 * base), a whole-hair or follower candidate nothing; same hashes as
 * assignLife. `memo` (optional Map) caches parents. */
function preBaseOf(P, paths, memo) {
  if (memo?.has(P)) return memo.get(P);
  if (!memo && P.pre !== undefined) return P.pre;
  let pre = P.len;
  if (!(P.knot || P.cord >= 0 || P.unit >= 0)) {
    const h = (m) => hash01(P.id * 31 + m * 7919 + 101),
      v0 = P.pts[0],
      par = v0.owner >= 0 && v0.owner !== P.id ? paths[v0.owner] : null;
    const pb = par && par.unit < 0 && !par.knot && !(par.cord >= 0) ? preBaseOf(par, paths, memo) : Infinity;
    if (par && pb < par.len) {
      const i = par.pts.indexOf(v0);
      if ((i >= 0 ? par.arcs[i] : 0) >= pb - 1) pre = 0;
    }
    if (pre > 0) {
      if (P.kind === "explorer" && !P.fused && P.len >= LIFE.MIN_LEN && h(1) < LIFE.TIP_P) pre = P.len - Math.min(P.len * LIFE.TIP_FRAC, LIFE.TIP_MIN + LIFE.TIP_SPAN * h(2));
      else if ((P.kind === "explorer-hair" || (P.kind === "hair" && par?.kind === "explorer" && par.unit < 0)) && h(3) < LIFE.HAIR_P) pre = 0;
    }
  }
  memo?.set(P, pre);
  return pre;
}
/** r11.2: a colony's soft radial felt glow (fills the residual dips of its
 * radial profile; stops in ring order, computed in monotoneColonies). */
function colonyFill(ctx, c, rgb, mul) {
  if (!c.fill || !c.fill.stops.some(([, a]) => a > 0.002) || mul <= 0) return;
  const n = c.node,
    R = c.fill.r,
    g = ctx.createRadialGradient(n.x, n.y, 0, n.x, n.y, R);
  g.addColorStop(0, `rgba(${rgb},${Math.min(1, c.fill.stops[0][1] * mul).toFixed(4)})`);
  for (const [t, a] of c.fill.stops) g.addColorStop(Math.min(1, t), `rgba(${rgb},${Math.min(1, a * mul).toFixed(4)})`);
  g.addColorStop(1, `rgba(${rgb},0)`);
  ctx.save();
  ctx.globalAlpha = 1;
  ctx.fillStyle = g;
  ctx.fillRect(n.x - R, n.y - R, 2 * R, 2 * R);
  ctx.restore();
}
const hexRgb = (h) => {
  const s = h.trim().replace("#", ""),
    f = s.length === 3 ? s.replace(/./g, (c) => c + c) : s.slice(0, 6);
  return [0, 2, 4].map((i) => parseInt(f.slice(i, i + 2), 16)).join(",");
};
/** Growth state of a path at scene age A and time T. r11.2: a recycling
 * explorer unit replays its own growth on a 24-60 s cycle (owner: strands must
 * visibly grow and fade back): it regrows in its original order, holds, then
 * dies back in reverse (forks and hairs first, then the continuation retracts
 * tip-first), and rests. The first growth is the natural one (age A). */
function unitClock(P, units, A, T = A - START_AGE) {
  if (P.unit < 0 || A >= 1e4) return { age: A, fade: 1, state: 1 };
  const u = units[P.unit];
  if (!u.period || u.period > 1e8 || !u.cyc) return { age: A, fade: 1, state: 1 };
  const c = tipCycle(T + u.ph, u.cyc),
    age = u.start + c.f * u.grow;
  return age < A ? { age, fade: 1, state: c.state } : { age: A, fade: 1, state: 0 };
}
// r11.2 living hero periphery: explorer tips and fine hairs (see assignLife).
const LIFE = Object.freeze({ TIP_P: 0.3, HAIR_P: 0.12, MIN_LEN: 50, TIP_MIN: 35, TIP_SPAN: 115, TIP_FRAC: 0.42, PERIOD: 16, PERIOD_SPAN: 30, FOLLOW_LAG: 3, FOLLOW_GAIN: 1.3 });

export function createHero(canvas, { stage, state, reduce }) {
  let disposed = false,
    task = 0;
  const L = readLayout(canvas),
    d = Math.min(devicePixelRatio || 1, 2),
    css = getComputedStyle(stage),
    light = state.theme === "light",
    palette = { core: css.getPropertyValue("--core").trim(), glow: css.getPropertyValue("--glow").trim(), light },
    begun = performance.now();
  // The hidden #hyphae canvas is the page-space static raster (viewport width).
  canvas.width = Math.round(L.W * d);
  canvas.height = Math.round(L.H * d);
  const sx = canvas.getContext("2d");
  sx.setTransform(d, 0, 0, d, 0, 0);
  sx.globalCompositeOperation = light ? "source-over" : "lighter";
  const lightAlpha = light ? 1.05 : 1;
  const scene = {
    ready: false,
    built: false,
    hero: { L, d, canvas, top: L.top, W: L.W, H: L.H, version: 0, palette },
    clock: sceneClock(),
    startAge: START_AGE,
    activeTrips: () => [],
    listeners: [],
    onReady(cb) {
      if (this.ready) cb(this);
      else this.listeners.push(cb);
    },
    timings: { start: begun },
  };
  window.__myceliumScene = scene;
  canvas.dataset.sporeNodes = "0";
  canvas.dataset.built = "0";

  function strokePath(ctx, P, sTo, mul, A) {
    const pts = P.pts,
      n = pts.length;
    // Held recycling paths: one cached stroke instead of per-chunk work.
    if (P.unit >= 0 && A !== undefined && sTo >= P.len && A - P.done > RIM_WIN) {
      if (!P.path2d) {
        P.path2d = new Path2D();
        P.path2d.moveTo(pts[0].x, pts[0].y);
        for (let i = 1; i < n; i++) P.path2d.lineTo(pts[i].x, pts[i].y);
        let a = 0,
          w = 0;
        for (let i = 0; i < n; i++) {
          a += P.a[i];
          w += P.w[i];
        }
        P.meanA = a / n;
        P.meanW = w / n;
      }
      ctx.globalAlpha = Math.min(1, P.meanA * mul * lightAlpha);
      ctx.lineWidth = P.meanW;
      ctx.lineCap = "butt";
      ctx.strokeStyle = P.tone ? palette.core : palette.glow;
      ctx.stroke(P.path2d);
      return;
    }
    let lastCap = "";
    for (let i = 0; i < n - 1; i += CHUNK) {
      if (P.arcs[i] >= sTo) break;
      const j = Math.min(n - 1, i + CHUNK),
        mid = (i + j) >> 1;
      let alpha = P.a[mid] * mul * lightAlpha;
      if (A !== undefined) {
        const dt = A - (P.t0 + P.arcs[mid] / P.v);
        if (dt >= 0) alpha *= 1 + RIM * Math.exp(-dt / RIM_TAU);
      }
      if (alpha < 0.004) continue;
      const width = P.w[mid];
      ctx.globalAlpha = Math.min(1, alpha);
      ctx.lineWidth = width;
      const cap = width < 0.55 ? "butt" : "round";
      if (cap !== lastCap) ctx.lineCap = lastCap = cap;
      ctx.strokeStyle = P.tone ? palette.core : palette.glow;
      ctx.beginPath();
      ctx.moveTo(pts[i].x, pts[i].y);
      for (let k = i + 1; k <= j; k++) {
        if (P.arcs[k] <= sTo) ctx.lineTo(pts[k].x, pts[k].y);
        else {
          const f = (sTo - P.arcs[k - 1]) / (P.arcs[k] - P.arcs[k - 1]);
          ctx.lineTo(pts[k - 1].x + (pts[k].x - pts[k - 1].x) * f, pts[k - 1].y + (pts[k].y - pts[k - 1].y) * f);
          break;
        }
      }
      ctx.stroke();
    }
    if (P.knot && sTo >= P.len) drawKnot(ctx, P.knot, mul);
  }
  /** A hyphal knot's soft core: overlapping gradient lobes (no arc(), never a
   * perfect circle); its wound fibres are ordinary strokes. */
  function drawKnot(ctx, K, mul = 1) {
    const save = ctx.globalAlpha;
    for (const l of K.lobes) {
      const g = ctx.createRadialGradient(l.x, l.y, 0, l.x, l.y, 1.3 * Math.max(l.rx, l.ry));
      g.addColorStop(0, palette.core);
      g.addColorStop(0.3, palette.glow);
      g.addColorStop(1, "rgba(0,0,0,0)");
      ctx.globalAlpha = Math.min(1, l.a * 0.3 * mul * (light ? 0.75 : 1));
      ctx.fillStyle = g;
      ctx.save();
      // Elongated along the host strand: scale about the lobe centre.
      ctx.translate(l.x, l.y);
      ctx.rotate(K.h);
      ctx.scale(l.rx / Math.max(l.rx, l.ry), l.ry / Math.max(l.rx, l.ry));
      ctx.translate(-l.x, -l.y);
      const e = 1.3 * Math.max(l.rx, l.ry);
      ctx.fillRect(l.x - e, l.y - e, 2 * e, 2 * e);
      ctx.restore();
    }
    ctx.globalAlpha = save;
  }
  function drawCores(ctx, g, A, live = null, box = null) {
    // A broad soft glow over the colony centre (20-40 px), never a point soma.
    for (const c of g.colonies) {
      const grow = matGrow(c, A) * (live ?? 1);
      if (grow <= 0) continue;
      const n = c.node,
        r = Math.max(20, Math.min(40, c.R0 * 0.2)) * (0.6 + 0.4 * grow),
        p = g.protect(n.x, n.y);
      const grad = ctx.createRadialGradient(n.x, n.y, 0, n.x, n.y, r);
      grad.addColorStop(0, palette.glow);
      grad.addColorStop(0.45, palette.glow);
      grad.addColorStop(1, "rgba(0,0,0,0)");
      ctx.globalAlpha = (light ? 0.08 : 0.15) * grow * p;
      ctx.fillStyle = grad;
      ctx.fillRect(n.x - r, n.y - r, 2 * r, 2 * r); // the gradient is clear at r (no arc on the field)
      colonyFill(ctx, c, hexRgb(palette.glow), (light ? 0.55 : 1) * grow);
      if (c.fill2) colonyFill(ctx, { node: c.node, fill: c.fill2 }, hexRgb(palette.glow), (light ? 0.55 : 1) * grow);
      if (box) {
        const R = Math.max(r, c.fill?.r || 0, c.fill2?.r || 0) + 2;
        grow_(box, [n.x - R, n.y - R, n.x + R, n.y + R]);
      }
      if (live === null) c.fillDrawn = !!c.fill;
    }
  }
  const mobile = () => L.mobile;

  // ---- progressive scheduler: bounded slices between frames ----
  const channel = new MessageChannel();
  let job = null;
  channel.port1.onmessage = () => step();
  function step() {
    if (disposed || !job) return;
    const t = performance.now();
    let result;
    try {
      do result = job.gen.next();
      while (!result.done && performance.now() - t < 9 && !result.value?.toString().startsWith("!"));
    } catch (e) {
      job = null;
      throw e;
    }
    job.onSlice?.();
    // Whole task, including the progressive bake: what a long-task observer sees.
    const spent = performance.now() - t;
    if (spent > 30 && (scene.timings.slowSlices ||= []).length < 8) scene.timings.slowSlices.push([Math.round(spent), typeof result.value === "string" ? result.value : result.done ? "done" : "?"]);
    if (spent > (scene.timings.maxSlice || 0)) {
      scene.timings.maxSlice = spent;
      scene.timings.maxSliceAt = String(result.value ?? "done");
    }
    if (result.done) {
      const j = job;
      job = null;
      j.resolve(result.value);
    } else channel.port2.postMessage(0);
  }
  /** A fresh task (r11.1): the completion work never shares a task with the last build slice. */
  const nextTask = () => new Promise((resolve) => {
    const ch = new MessageChannel();
    ch.port1.onmessage = () => resolve();
    ch.port2.postMessage(0);
  });
  function run(gen, onSlice) {
    return new Promise((resolve) => {
      job = { gen, resolve, onSlice };
      channel.port2.postMessage(0);
    });
  }
  let sliceStart = performance.now();
  const budget = () => {
    if (performance.now() - sliceStart > 8) {
      sliceStart = performance.now();
      return true;
    }
    return false;
  };

  // ---- r11 incremental growth raster ----
  // Every non-recycling path grows straight into the static raster, one piece
  // per frame (tip-only, never retracting): the load-in intro and the living
  // rims share one mechanism. The page compositor re-presents the static raster
  // only when the hero version changes (every frame during the intro, every
  // 4 s of scene age afterwards); the not-yet-presented tail and a bright rim
  // head per growing tip are drawn on the live layer.
  const HEAD = 16, // px of rim glow behind each growing tip
    HEAD_CAP = 7000;
  const growth = { epoch: 0, age: -Infinity, scan: 0, pending: [], active: [], recent: [], commit: null, presentKey: null, coresDrawn: false, changed: false };
  /** r11.2: before the life assignment (which needs the field) the intro
   * already grows the hero. Paths that may become living are held to a safe
   * static extent meanwhile: a tip candidate to its shortest possible base
   * (pins only lengthen a base), a whole-hair or follower candidate not at
   * all; so nothing living is ever baked into the static raster. Same hashes
   * as assignLife. */
  function preBase(P, g) {
    return P.pre !== undefined ? P.pre : (P.pre = preBaseOf(P, g.paths));
  }
  function growable(P, g) {
    if (P.unit >= 0 && g.units[P.unit]?.kind === "explorer") return false;
    if (!g.lifeAssigned) return preBase(P, g) > 0;
    return !(P.dyn && (P.dyn.follow || !P.dyn.base));
  }
  // Static-raster length: living tips (r11.2) keep only their base in the raster.
  const rasterLen = (P) => (P.dyn ? P.dyn.base : P.dyn === undefined && P.pre !== undefined ? Math.min(P.len, P.pre) : P.len);
  function resetGrowth(g) {
    if (growth.xfade) {
      growth.xfade.old.width = growth.xfade.old.height = growth.xfade.neu.width = growth.xfade.neu.height = 0;
      growth.xfade = null;
    }
    for (const P of g.paths) P.resumeDelay = 0; // a rebuilt raster has no held paths
    sx.save();
    sx.setTransform(1, 0, 0, 1, 0, 0);
    sx.clearRect(0, 0, canvas.width, canvas.height);
    sx.restore();
    for (const P of g.paths) P.drawn = P.presented = P.pi = 0;
    growth.pending = g.paths.filter((P) => growable(P, g));
    growth.scan = g.paths.length;
    growth.active = [];
    growth.recent = [];
    growth.commit = null;
    growth.coresDrawn = false;
    growth.age = -Infinity;
    growth.epoch++;
  }
  const lerpAt = (P, m, s) => {
    const a = P.pts[m],
      b = P.pts[Math.min(P.pts.length - 1, m + 1)],
      span = P.arcs[m + 1] - P.arcs[m] || 1,
      f = Math.max(0, Math.min(1, (s - P.arcs[m]) / span));
    return [a.x + (b.x - a.x) * f, a.y + (b.y - a.y) * f];
  };
  /** Adds the arc range [s0, s1] of P to quantised stroke groups (CHUNK-wise
   * taper, as in strokePath). `mul` scales alpha; `core` forces the core tone. */
  function addPiece(groups, P, s0, s1, mul = 1, core = false) {
    const pts = P.pts,
      arcs = P.arcs,
      n = pts.length;
    if (n < 2 || s1 <= s0) return;
    let i = 0;
    while (i < n - 2 && arcs[i + 1] <= s0) i++;
    let cur = lerpAt(P, i, s0);
    for (;;) {
      const c0 = Math.floor(i / CHUNK) * CHUNK,
        chunkEnd = Math.min(n - 1, c0 + CHUNK),
        stop = Math.min(s1, arcs[chunkEnd]),
        mid = (c0 + chunkEnd) >> 1,
        alpha = Math.min(1, P.a[mid] * mul * lightAlpha);
      let k = i + 1;
      if (alpha >= 0.004) {
        const key = (core || P.tone ? 1 : 0) * 1e6 + Math.round(alpha * 120) * 1000 + Math.round(P.w[mid] * 20);
        let path = groups.get(key);
        if (!path) groups.set(key, (path = new Path2D()));
        path.moveTo(cur[0], cur[1]);
        while (k <= chunkEnd && arcs[k] < stop) path.lineTo(pts[k].x, pts[k].y), k++;
        const end = lerpAt(P, k - 1, stop);
        path.lineTo(end[0], end[1]);
      } else while (k <= chunkEnd && arcs[k] < stop) k++;
      if (stop >= s1 - 1e-9 || chunkEnd >= n - 1) return;
      i = chunkEnd;
      cur = [pts[chunkEnd].x, pts[chunkEnd].y];
    }
  }
  function flushPieces(ctx, groups, ctx2 = null) {
    for (const c of ctx2 ? [ctx, ctx2] : [ctx]) {
      c.lineCap = "butt";
      for (const [key, path] of groups) {
        c.strokeStyle = key >= 1e6 ? palette.core : palette.glow;
        c.globalAlpha = Math.floor((key % 1e6) / 1000) / 120;
        c.lineWidth = (key % 1000) / 20;
        c.stroke(path);
      }
      c.globalAlpha = 1;
    }
    groups.clear();
  }
  /** r11.2c (owner: strands and rings around the nodes popped in late in the
   * grow phase): the static raster is replaced twice after it was first drawn
   * (final colony alphas, see rebakeStatic; the dip glow, see dipsGen). Both
   * now cross-fade over XFADE_MS instead of one blit; growth during the fade
   * is drawn into both the old and the new raster. */
  const XFADE_MS = 1600;
  const bakeTargets = () => (growth.xfade ? [growth.xfade.ox, growth.xfade.nx] : [sx]);
  function snapshot() {
    const cv = document.createElement("canvas");
    cv.width = canvas.width;
    cv.height = canvas.height;
    const cx = cv.getContext("2d");
    cx.drawImage(canvas, 0, 0);
    cx.setTransform(sx.getTransform());
    return cv;
  }
  function startXfade(neu) {
    // r11.2b: a running cross-fade is never cut short. The dip glow started
    // ~0.7 s into the re-bake's fade and finished it in one blit (+6.7k px in
    // 100 ms at 6 s); the new fade now starts from the blend on screen.
    const prev = growth.xfade,
      old = snapshot(),
      nx = neu.getContext("2d");
    if (prev) {
      prev.old.width = prev.old.height = 0;
      if (prev.neu !== neu) prev.neu.width = prev.neu.height = 0;
    }
    nx.setTransform(sx.getTransform());
    growth.xfade = { old, neu, ox: old.getContext("2d"), nx, t0: performance.now() };
    stepXfade();
  }
  function stepXfade(now = performance.now()) {
    const x = growth.xfade;
    if (!x) return;
    const f = smooth((now - x.t0) / XFADE_MS);
    sx.save();
    sx.setTransform(1, 0, 0, 1, 0, 0);
    sx.globalCompositeOperation = "copy";
    sx.globalAlpha = f >= 1 ? 1 : 1 - f;
    sx.drawImage(f >= 1 ? x.neu : x.old, 0, 0);
    if (f < 1) {
      sx.globalCompositeOperation = light ? "source-over" : "lighter";
      sx.globalAlpha = f;
      sx.drawImage(x.neu, 0, 0);
    }
    sx.restore();
    growth.changed = true;
    if (f >= 1) {
      x.old.width = x.old.height = x.neu.width = x.neu.height = 0;
      growth.xfade = null;
    }
  }
  function finishXfade() {
    if (!growth.xfade) return;
    growth.xfade.t0 = -Infinity;
    stepXfade();
  }
  const bakeGroups = new Map(),
    liveGroups = new Map(),
    pulseGroups = new Map();
  /** Grow every started path to scene age A in the static raster. With a
   * deadline the work is resumable (returns false until A is complete). */
  function growStep(A, g, deadline = 0) {
    if (A < growth.age - 1e-6) resetGrowth(g);
    const paths = g.paths;
    for (; growth.scan < paths.length; growth.scan++) {
      const P = paths[growth.scan];
      P.drawn = P.presented = P.pi = 0;
      if (growable(P, g)) growth.pending.push(P);
    }
    let w = 0;
    for (const P of growth.pending) {
      if (P.t0 <= A) growth.active.push(P);
      else growth.pending[w++] = P;
    }
    growth.pending.length = w;
    const act = growth.active,
      done = [];
    let k = 0,
      i = 0,
      broke = false;
    for (; i < act.length; i++) {
      const P = act[i],
        end = rasterLen(P),
        e = Math.min(end, (A - P.t0 - (P.matShift || 0) - (P.resumeDelay || 0)) * P.v);
      if (e > P.drawn + 1e-6) {
        addPiece(bakeGroups, P, P.drawn, e);
        P.drawn = e;
        growth.changed = true;
      }
      if (P.drawn >= end - 1e-6) done.push(P);
      else act[k++] = P;
      if (deadline && (i & 63) === 63 && performance.now() > deadline) {
        i++;
        broke = true;
        break;
      }
    }
    for (; i < act.length; i++) act[k++] = act[i];
    act.length = k;
    const tg = bakeTargets();
    for (const c of tg) c.globalCompositeOperation = light ? "source-over" : "lighter";
    flushPieces(tg[0], bakeGroups, tg[1]);
    for (const P of done) {
      if (P.knot) for (const c of tg) drawKnot(c, P.knot);
      growth.recent.push(P);
    }
    // r11.2: cores and felt fills build up with the mats (drawn over the
    // static raster by the compositor's hero blit, see drawIntroCores) and are
    // baked into the raster once complete.
    if (!growth.coresDrawn && A >= MAT_END + 4) {
      for (const c of tg) drawCores(c, g, A);
      growth.coresDrawn = growth.changed = true;
    } else if (!growth.coresDrawn && Math.floor(A) !== growth.coreStep) {
      growth.coreStep = Math.floor(A); // the blit's core layer follows the age
      growth.changed = true;
    }
    if (!broke) growth.age = A;
    return !broke;
  }
  /** Present the static raster (compositor redraw); the live tail of what was
   * presented is dropped on the NEXT frame, after the compositor has redrawn. */
  function present() {
    if (!growth.changed) return false;
    growth.changed = false;
    scene.hero.version++;
    growth.commit = growth.active.map((P) => [P, P.drawn]).concat(growth.recent.map((P) => [P, rasterLen(P)]));
    growth.recent = [];
    return true;
  }
  function commitPresented() {
    if (!growth.commit) return;
    for (const [P, e] of growth.commit) P.presented = Math.max(P.presented, e);
    growth.commit = null;
  }
  // Recycling paths (distal explorer units) are drawn live: they fade and regrow.
  function liveAt(A, g) {
    const out = [];
    for (const P of g.livePool) {
      const { age, fade } = unitClock(P, g.units, A);
      if (age < P.t0 || fade <= 0) continue;
      out.push({ P, age, fade });
    }
    return out;
  }
  function grow_(box, b) {
    grow(box, b);
  }
  function grow(box, b) {
    if (b[0] < box[0]) box[0] = b[0];
    if (b[1] < box[1]) box[1] = b[1];
    if (b[2] > box[2]) box[2] = b[2];
    if (b[3] > box[3]) box[3] = b[3];
  }
  let liveSince = null;
  function drawLive(ctx, A, g, box, intro) {
    let active = 0,
      segs = 0;
    // Unpresented tails at base alpha, then the rim: brightest at the tip.
    // r11.2: the rim heads fade in over 0.6 s once the compositor attaches
    // (before, every growing tip lit up in one frame mid-intro).
    liveSince ??= performance.now();
    const rim = (intro ? 1.25 : 0.9) * smooth((performance.now() - liveSince) / 600);
    let heads = 0;
    for (const list of [growth.active, growth.recent])
      for (const P of list) {
        const e = P.drawn;
        if (e <= 0) continue;
        if (P.presented < e) addPiece(liveGroups, P, P.presented, e);
        if (list === growth.active && heads < HEAD_CAP) {
          heads++;
          addPiece(liveGroups, P, Math.max(0, e - HEAD), Math.max(0, e - HEAD * 0.45), rim * 0.55, true);
          addPiece(liveGroups, P, Math.max(0, e - HEAD * 0.45), e, rim, true);
        }
        grow(box, P.box);
      }
    active += growth.active.length;
    flushPieces(ctx, liveGroups);

    return { active, segs, heads };
  }

  /** r11.2: the static raster the intro drew before the build finished used
   * provisional colony alphas (committed at the end of the build). Once the
   * life assignment is in, the raster is re-baked offscreen with the final
   * alphas, in short slices (each forced to raster before the task ends), up
   * to exactly what the visible raster holds, then swapped in with one blit.
   * Adding differences instead lost faint ink to 8-bit rounding. */
  function* rebakeStatic(g) {
    const epoch = growth.epoch,
      off = document.createElement("canvas");
    off.width = canvas.width;
    off.height = canvas.height;
    const ox = off.getContext("2d"),
      groups = new Map();
    ox.setTransform(sx.getTransform());
    ox.globalCompositeOperation = light ? "source-over" : "lighter";
    for (const P of g.paths) P.rd = 0;
    let n = 0;
    const catchUp = function* (sliced) {
      let any = false,
        pieces = 0,
        t0 = performance.now();
      for (const P of g.paths) {
        const e = P.drawn || 0;
        if (e > P.rd + 1e-6) {
          addPiece(groups, P, P.rd, e);
          P.rd = e;
          any = true;
          pieces++;
        }
        // A slice ends after ~250 pieces or 4 ms (paths with nothing new cost
        // only a compare: a fixed 250-path slice took ~1000 frames per pass).
        if (sliced && (pieces >= 250 || (++n & 255) === 0) && (pieces >= 250 || performance.now() - t0 > 4)) {
          pieces = 0;
          t0 = performance.now();
          flushPieces(ox, groups);
          ox.getImageData(0, 0, 1, 1); // raster this batch now, not all at the swap
          yield "!rebake";
          if (growth.epoch !== epoch) return false;
        }
      }
      return any;
    };
    for (let pass = 0; pass < 4; pass++) {
      const any = yield* catchUp(true);
      if (growth.epoch !== epoch) return (off.width = off.height = 0);
      if (!any) break;
    }
    // The swap: the last few pieces, knots, cores and felt fills, one blit.
    for (const _ of catchUp(false));
    flushPieces(ox, groups);
    for (const P of g.paths) if (P.knot && P.drawn > 0 && P.drawn >= rasterLen(P) - 1e-6) drawKnot(ox, P.knot);
    if (growth.coresDrawn) drawCores(ox, g, growth.age);
    startXfade(off); // cross-fade, not one blit (owner: rings popped in)
    for (const P of g.paths) P.aOld = null;
    growth.changed = true;
    scene.timings.rebaked = performance.now() - begun;
  }
  /** r11.2: the last check of the owner's rule (densest at the point, never a
   * fainter ring) on what the page draws. Once the intro is over, each
   * colony's ESTABLISHED static raster (every static path fully grown, knots,
   * cores, felt fills; the page's own stroke code and colours) is drawn
   * offscreen in short tasks, its luminance measured in 8 px rings (median
   * over 24 sectors), and any ring still fainter than the rings outside it
   * (4% per ring) is lifted with a soft radial felt glow (three measured
   * passes). The glow is drawn onto the hero now and kept on the colony
   * (fill2) so rebuilds draw it with the cores. Dark only. */
  const dips = { started: false, done: false };
  function ringsOf(data, w, h, ox0, oy0, n, K, scale) {
    const STEP = 8,
      SECT = 24,
      lin = (v) => ((v /= 255) <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4),
      sum = new Float64Array(K * SECT),
      cnt = new Float64Array(K * SECT),
      rgb = new Float64Array(K * 3),
      rc = new Float64Array(K),
      stride = Math.max(1, Math.round(scale));
    for (let yy = 0; yy < h; yy += stride)
      for (let xx = 0; xx < w; xx += stride) {
        const dx = (ox0 + xx + 0.5) / scale - n.x,
          dy = (oy0 + yy + 0.5) / scale - n.y,
          k = Math.floor(Math.hypot(dx, dy) / STEP);
        if (k >= K) continue;
        const o = (yy * w + xx) * 4,
          al = data[o + 3] / 255,
          r = data[o] * al,
          gg = data[o + 1] * al,
          b = data[o + 2] * al,
          cell = k * SECT + (Math.floor(((Math.atan2(dy, dx) + Math.PI) / (2 * Math.PI)) * SECT) % SECT);
        sum[cell] += 0.2126 * lin(r) + 0.7152 * lin(gg) + 0.0722 * lin(b);
        cnt[cell]++;
        rgb[k * 3] += r;
        rgb[k * 3 + 1] += gg;
        rgb[k * 3 + 2] += b;
        rc[k]++;
      }
    const M = [];
    for (let k = 0; k < K; k++) {
      const v = [];
      for (let q = 0; q < SECT; q++) if (cnt[k * SECT + q]) v.push(sum[k * SECT + q] / cnt[k * SECT + q]);
      v.sort((p, q) => p - q);
      M.push(v.length ? (v[(v.length - 1) >> 1] + v[v.length >> 1]) / 2 : 0);
    }
    return { M, mean: (k) => [rgb[k * 3] / rc[k], rgb[k * 3 + 1] / rc[k], rgb[k * 3 + 2] / rc[k]], lin };
  }
  /** Gradient stops for per-ring glow values: flat over each 8 px ring. */
  const ringStops = (added, K) => added.flatMap((v, q) => [[(q + 0.15) / K, v], [(q + 0.85) / K, v]]);
  function* dipsGen(g) {
    const fills = [],
      glowRgb = hexRgb(palette.glow),
      glow = glowRgb.split(",").map(Number),
      groups = new Map();
    for (const c of g.colonies) {
      const n = c.node,
        Rm = Math.min(200, 0.8 * c.R0),
        K = Math.floor(Rm / 8),
        R = Rm + 2,
        S = Math.ceil(2 * R * d);
      if (K < 3) continue;
      const off = document.createElement("canvas"),
        tmp = document.createElement("canvas");
      off.width = off.height = tmp.width = tmp.height = S;
      const ox = off.getContext("2d"),
        tx = tmp.getContext("2d", { willReadFrequently: true });
      ox.setTransform(d, 0, 0, d, -(n.x - R) * d, -(n.y - R) * d);
      ox.globalCompositeOperation = "lighter";
      let k = 0,
        batch = 0;
      for (const P of g.paths) {
        if (++k % 2000 === 0) yield;
        if (!growable(P, g) || P.box[0] > n.x + R || P.box[2] < n.x - R || P.box[1] > n.y + R || P.box[3] < n.y - R) continue;
        addPiece(groups, P, 0, rasterLen(P));
        if (P.knot) drawKnot(ox, P.knot);
        if (++batch % 120 === 0) {
          flushPieces(ox, groups);
          ox.getImageData(0, 0, 1, 1); // raster this batch in this task
          yield;
        }
      }
      flushPieces(ox, groups);
      const fill2 = c.fill2;
      c.fill2 = null;
      drawCores(ox, g, 1e9); // established cores and felt fills (without fill2)
      c.fill2 = fill2;
      ox.getImageData(0, 0, 1, 1);
      yield;
      const added = new Array(K).fill(0);
      let first = null,
        last = null;
      // Measured passes. Each ring's glow is flat across the ring (two stops,
      // 15% blends at its edges): with one stop per ring centre, raising a
      // ring also lifted its neighbours by half as much, and the correction
      // chased an outer bump inward without converging (a 4.5% ring stayed at
      // a 1920 colony after 8 passes).
      const PASSES = 6;
      for (let pass = 0; pass < PASSES; pass++) {
        tx.globalCompositeOperation = "copy";
        tx.setTransform(1, 0, 0, 1, 0, 0);
        tx.drawImage(off, 0, 0);
        tx.globalCompositeOperation = "lighter";
        tx.setTransform(d, 0, 0, d, -(n.x - R) * d, -(n.y - R) * d);
        if (added.some((v) => v > 0)) colonyFill(tx, { node: n, fill: { r: K * 8, stops: ringStops(added, K) } }, glowRgb, 1);
        const rings = ringsOf(tx.getImageData(0, 0, S, S).data, S, S, (n.x - R) * d, (n.y - R) * d, n, K, d),
          M = rings.M;
        first ??= M;
        last = M;
        yield;
        if (pass === PASSES - 1) break;
        const env = M.slice();
        for (let q = K - 2; q >= 0; q--) env[q] = Math.max(M[q], env[q + 1] * 1.04);
        let any = false;
        for (let q = 0; q < K; q++) {
          const need = env[q] - M[q];
          if (need <= M[q] * 0.005) continue;
          const [r, gg, b] = rings.mean(q),
            L = (x, y, z) => 0.2126 * rings.lin(x) + 0.7152 * rings.lin(y) + 0.0722 * rings.lin(z),
            slope = (L(Math.min(255, r + 0.01 * glow[0]), Math.min(255, gg + 0.01 * glow[1]), Math.min(255, b + 0.01 * glow[2])) - L(r, gg, b)) / 0.01;
          if (slope > 1e-6) {
            added[q] = Math.min(0.35, added[q] + (need / slope) * 0.9);
            any = true;
          }
        }
        if (!any) break;
      }
      off.width = off.height = tmp.width = tmp.height = 0;
      c.dips = { before: first, after: last, added: added.slice() };
      if (added.some((v) => v > 0)) {
        const f = { r: K * 8, stops: ringStops(added, K) };
        fills.push({ node: n, fill: f });
        c.fill2 = f;
      }
      yield;
    }
    // All dip glows fade in together (cross-fade, not one blit).
    if (fills.length) {
      // The target is the running fade's target (or the raster) plus the glows.
      let neu;
      if (growth.xfade) {
        neu = document.createElement("canvas");
        neu.width = canvas.width;
        neu.height = canvas.height;
        neu.getContext("2d").drawImage(growth.xfade.neu, 0, 0);
        neu.getContext("2d").setTransform(sx.getTransform());
      } else neu = snapshot();
      const nx = neu.getContext("2d");
      nx.globalCompositeOperation = "lighter";
      for (const f of fills) colonyFill(nx, f, glowRgb, 1);
      startXfade(neu);
    }
  }
  function startDips(g) {
    dips.started = true;
    // Light theme, or a re-mount whose colonies were already checked.
    if (light || g.colonies.every((c) => c.dips)) return void (dips.done = true);
    const gen = dipsGen(g),
      ch = new MessageChannel();
    ch.port1.onmessage = () => {
      if (disposed) return;
      const t0 = performance.now();
      let r;
      do r = gen.next();
      while (!r.done && performance.now() - t0 < 4);
      if (r.done) {
        dips.done = true;
        scene.timings.dipsDone = performance.now() - begun;
      } else ch.port2.postMessage(0);
    };
    ch.port2.postMessage(0);
  }
  // ---- r11.2 passive life: living periphery on the compositor's life layer ----
  let lifeStamp = 0;
  const lifeGroups = new Map(),
    RETREAT_STEPS = [0.75, 0.5, 0.28, 0.1];
  /** Visible arc and cycle state of a living (non-unit) path, memoised per stamp. */
  function heroVis(P, A, T) {
    const y = P.dyn;
    if (y.stamp === lifeStamp) return y;
    y.stamp = lifeStamp;
    const nat = Math.max(0, Math.min(P.len, (A - P.t0) * P.v));
    if (A >= 1e4) {
      y.vis = P.len;
      y.state = 1;
    } else if (y.follow) {
      const q = heroVis(y.parent, A, T);
      y.vis = Math.min(nat, Math.max(0, Math.min(P.len, (q.vis - y.at - LIFE.FOLLOW_LAG) * LIFE.FOLLOW_GAIN)));
      y.state = q.state;
    } else {
      const c = tipCycle(T + y.phase, y.period, y.g, y.h, y.r),
        cyc = y.base + (P.len - y.base) * c.f;
      y.vis = Math.min(nat, cyc);
      y.state = nat < cyc ? 0 : c.state;
    }
    return y;
  }
  /** Visible arc of any path (static, unit, living tip or follower). */
  function visibleArc(P, A, T, g) {
    if (P.unit >= 0) return Math.max(0, Math.min(P.len, (unitClock(P, g.units, A, T).age - P.t0) * P.v));
    if (P.dyn) return heroVis(P, A, T).vis;
    return Math.max(0, Math.min(P.len, (A - P.t0) * P.v));
  }
  /** visibleArc memoised per path and lifeStamp (the crest visits many
   * segments of one path per frame). */
  function arcNow(P, A, T, g) {
    if (P.arcStamp !== lifeStamp) {
      P.arcStamp = lifeStamp;
      P.arcVal = visibleArc(P, A, T, g);
    }
    return P.arcVal;
  }
  function lifeStroke(P, s0, s1, state, glow) {
    if (s1 <= s0 + 0.05) return 0;
    if (state === 2) {
      const F = Math.min(30, (s1 - s0) * 0.6 + 3),
        q = F / RETREAT_STEPS.length;
      addPiece(lifeGroups, P, s0, s1 - F);
      RETREAT_STEPS.forEach((m, k) => addPiece(lifeGroups, P, s1 - F + k * q, s1 - F + (k + 1) * q, m));
    } else {
      addPiece(lifeGroups, P, s0, s1);
      if (state === 0 && glow && s1 < P.len - 0.5) addPiece(lifeGroups, P, Math.max(s0, s1 - HEAD * 0.6), s1, 0.9, true);
    }
    return 1;
  }
  /** The living periphery at scene age A, time T (life layer, hero coordinates). */
  let lifeBins = null,
    lifeBinsOf = null;
  const lifeRanges = new Map();
  /** Living paths whose box meets [y0, y1] (64 px bins of hero y). */
  function lifeNear(g, y0, y1) {
    if (!Number.isFinite(y0) || !Number.isFinite(y1)) return g.lifeList;
    if (lifeBinsOf !== g.lifeList) {
      lifeBinsOf = g.lifeList;
      lifeBins = new Map();
      lifeRanges.clear();
      for (const P of g.lifeList)
        for (let b = Math.floor(P.box[1] / 64); b <= Math.floor(P.box[3] / 64); b++) (lifeBins.get(b) || lifeBins.set(b, []).get(b)).push(P);
    }
    const b0 = Math.floor(y0 / 64),
      b1 = Math.floor(y1 / 64),
      key = b0 * 4096 + b1;
    let out = lifeRanges.get(key);
    if (!out) {
      const set = new Set();
      for (let b = b0; b <= b1; b++) for (const P of lifeBins.get(b) || []) set.add(P);
      out = [...set];
      if (lifeRanges.size > 512) lifeRanges.clear();
      lifeRanges.set(key, out);
    }
    return out;
  }
  function drawLife(ctx, T, A, g, x0 = -Infinity, x1 = Infinity, y0 = -Infinity, y1 = Infinity) {
    lifeStamp++;
    let n = 0;
    for (const P of lifeNear(g, y0, y1)) {
      if (P.box[2] < x0 - 2 || P.box[0] > x1 + 2 || P.box[3] < y0 - 2 || P.box[1] > y1 + 2) continue;
      if (P.unit >= 0) {
        const c = unitClock(P, g.units, A, T);
        n += lifeStroke(P, 0, Math.max(0, Math.min(P.len, (c.age - P.t0) * P.v)), c.state, P.kind === "explorer");
      } else {
        const y = heroVis(P, A, T);
        n += lifeStroke(P, y.follow ? 0 : y.base, y.vis, y.state, !y.follow && P.kind === "explorer");
      }
    }
    flushPieces(ctx, lifeGroups);
    return n;
  }
  /** r11.2: which paths live. ~62% of explorer hyphae get a living distal tip
   * (35-150 px); half the fine hairs on explorers regrow whole; laterals and
   * hairs rooted on a living tip follow it; recycling explorer units replay
   * their growth (unitClock). Tips never contain field seeds or vertices other
   * paths fused into, and hold no knots. Deterministic hashes only. */
  function* assignLife(g) {
    const fieldV = new Set();
    for (const p of g.field.strands) {
      fieldV.add(p.points[0].id);
      fieldV.add(p.points.at(-1).id);
    }
    const h = (P, m) => hash01(P.id * 31 + m * 7919 + 101);
    for (const u of g.units)
      if (u.kind === "explorer" && u.paths.length && u.period < 1e8) {
        u.cyc = 24 + 36 * hash01(u.id * 29 + 3);
        u.ph = hash01(u.id * 37 + 7) * u.cyc;
      }
    g.lifeList = [];
    let n = 0;
    for (const P of g.paths) {
      if (++n % 1500 === 0 && budget()) yield "life";
      P.dyn = null;
      if (P.unit >= 0) {
        if (g.units[P.unit].cyc) g.lifeList.push(P);
        continue;
      }
      if (P.knot || P.cord >= 0) continue;
      const v0 = P.pts[0],
        par = v0.owner >= 0 && v0.owner !== P.id ? g.paths[v0.owner] : null;
      if (par?.dyn) {
        const i = par.pts.indexOf(v0),
          at = i >= 0 ? par.arcs[i] : 0;
        if (at >= (par.dyn.follow ? 0 : par.dyn.base) - 1) {
          P.dyn = { follow: true, parent: par, at, base: 0 };
          g.lifeList.push(P);
          continue;
        }
      }
      const cycle = (len, slow = false) => {
          const period0 = slow ? 40 + 50 * h(P, 4) : LIFE.PERIOD + LIFE.PERIOD_SPAN * h(P, 4),
            y = lifeTiming(len, period0, h(P, 6), h(P, 7), h(P, 8));
          return { ...y, phase: h(P, 5) * y.period };
        };
      if (P.kind === "explorer" && !P.fused && P.len >= LIFE.MIN_LEN && h(P, 1) < LIFE.TIP_P) {
        let base = P.len - Math.min(P.len * LIFE.TIP_FRAC, LIFE.TIP_MIN + LIFE.TIP_SPAN * h(P, 2));
        for (let i = 1; i < P.pts.length; i++) {
          if (P.arcs[i] <= base - 4) continue;
          const v = P.pts[i];
          let pin = fieldV.has(v.id);
          for (const e of v.edges) {
            if (pin) break;
            const sg = g.ss[e];
            if (sg.path === P) continue;
            pin = !!sg.field || !(sg.path && sg.path.pts[0] === v);
          }
          if (pin) base = Math.max(base, P.arcs[i] + 4);
        }
        if (base <= P.len - 20) {
          P.dyn = { base, ...cycle(P.len - base) };
          g.lifeList.push(P);
        }
      } else if ((P.kind === "explorer-hair" || (P.kind === "hair" && par?.kind === "explorer" && par.unit < 0)) && h(P, 3) < LIFE.HAIR_P) {
        P.dyn = { base: 0, ...cycle(P.len, true) };
        g.lifeList.push(P);
      }
    }
  }
  /** Per route: the field-leak list and the laid-late/living list, in arrival
   * order (a frame visits only the crest window), built in slices. */
  function* routeLists(g) {
    for (const r of g.routes) {
      r.fieldList = [];
      r.lateList = [];
      let n = 0;
      for (const v of r.list) {
        if (++n % 4000 === 0 && budget()) yield "route-lists";
        const s = g.ss[v.segment];
        if (s.field) {
          if (!s.fieldPath?.margin) r.fieldList.push(v);
        } else if (s.birth > START_AGE - 5 || (s.path && s.path.unit >= 0 && g.units[s.path.unit].period < 1e8) || livingSeg(s)) r.lateList.push(v);
      }
      yield "route-lists";
      r.lateList.sort((a, b) => a.arrival - b.arrival);
      yield "route-lists";
    }
  }
  /** Route segments on living parts never enter cached pulse sprites. */
  const livingSeg = (s) => !!s.path?.dyn && (s.path.dyn.follow || !s.path.dyn.base || s.path.arcs[s.index + 1] > s.path.dyn.base);

  // ---- arrival bloom: radial bands, the ring travels outward to the rim ----
  function* makeBlooms(g, out) {
    // r11: finer bands so the arrival bloom is a travelling ripple; the
    // innermost band (r < 0.2 R0) never lights on its own (no disc).
    const edges = [0, 0.2, 0.32, 0.44, 0.56, 0.68, 0.8, 0.92, 1.05, 1.2, 1.4];
    const blooms = g.colonies.map((c) => ({
      node: c.node,
      R0: c.R0,
      bands: edges.slice(0, -1).map((r0, k) => ({ r0, r1: edges[k + 1], segs: [], box: [Infinity, Infinity, -Infinity, -Infinity], image: null })),
    }));
    let n = 0;
    for (const P of g.paths) {
      if (++n % 2000 === 0) yield "bloom";
      if (P.unit >= 0 || P.dyn || P.cord >= 0 || P.kind === "explorer" || P.t0 >= START_AGE - 10) continue;
      const bl = blooms[P.pts[0].source];
      if (!bl) continue;
      const nx = bl.node.x,
        ny = bl.node.y,
        R0 = bl.R0;
      for (let i = 1; i < P.pts.length; i++) {
        const q = P.pts[i],
          r = Math.hypot(q.x - nx, q.y - ny) / R0;
        if (r >= 1.4) continue;
        let bi = 0;
        while (bi < edges.length - 2 && r >= edges[bi + 1]) bi++;
        const b = bl.bands[bi];
        b.segs.push([P.pts[i - 1], q, P.w[i], P.a[i]]);
        const bx = b.box;
        if (q.x < bx[0]) bx[0] = q.x;
        if (q.y < bx[1]) bx[1] = q.y;
        if (q.x > bx[2]) bx[2] = q.x;
        if (q.y > bx[3]) bx[3] = q.y;
      }
    }
    for (const bl of blooms) for (const b of bl.bands) b.box = [b.box[0] - 3, b.box[1] - 3, b.box[2] + 3, b.box[3] + 3];
    out.blooms = blooms;
  }
  // Segments grouped by quantised alpha/width into a few Path2D strokes.
  /** Segments grouped by quantised alpha/width into a few cached Path2D strokes. */
  function segGroups(segs, alphaMul, widthAdd) {
    const groups = new Map();
    for (const [a, q, w, al] of segs) {
      const ka = Math.min(15, Math.round(Math.min(1, al * alphaMul) * 15)),
        kw = Math.round((w + widthAdd) * 5);
      if (!ka) continue;
      const key = ka * 64 + kw;
      let path = groups.get(key);
      if (!path) groups.set(key, (path = new Path2D()));
      path.moveTo(a.x, a.y);
      path.lineTo(q.x, q.y);
    }
    return [...groups].map(([key, path]) => ({ alpha: Math.floor(key / 64) / 15, width: (key % 64) / 5, path }));
  }
  function strokeGroups(x, groups, colour, mul = 1) {
    x.strokeStyle = colour;
    x.lineCap = "butt";
    for (const g of groups) {
      x.globalAlpha = Math.min(1, g.alpha * mul);
      x.lineWidth = g.width;
      x.stroke(g.path);
    }
  }
  function paintSegs(x, segs, colour, alphaMul, widthAdd) {
    strokeGroups(x, segGroups(segs, alphaMul, widthAdd), colour);
  }
  // r11: bloom bands are cached vector groups stroked at full DPR (the r10
  // per-band bitmaps covered whole annulus boxes: up to ~25 MB each).
  function bandGroups(b) {
    if (!b.segs.length) return null;
    return (b.groups ??= segGroups(b.segs, 1.4, 0.15));
  }

  // ---- pulses: graph-routed arrival bands over the new geometry ----
  function buildSignalRoutes(g) {
    const { vs, ss } = g,
      routes = [],
      FAST = 38,
      SLOW = 22,
      LAMBDA = 300,
      CUTOFF = 650,
      STEP = 0.3,
      RSTEP = 4;
    const resident = new Map(),
      cacheStats = { bytes: 0, peakBytes: 0, buckets: 0, limit: 32, misses: 0, evictions: 0 };
    window.__signalCacheStats = cacheStats;
    class Heap {
      constructor() { this.a = []; }
      push(v) { const a = this.a; let i = a.length; a.push(v); while (i) { const p = (i - 1) >> 1; if (a[p][0] <= v[0]) break; a[i] = a[p]; i = p; } a[i] = v; }
      pop() { const a = this.a, top = a[0], last = a.pop(); if (a.length) { let i = 0; while (i * 2 + 1 < a.length) { let k = i * 2 + 1; if (k + 1 < a.length && a[k + 1][0] < a[k][0]) k++; if (a[k][0] >= last[0]) break; a[i] = a[k]; i = k; } a[i] = last; } return top; }
    }
    return (function* () {
      for (const reverse of [false, true]) {
        const cord = reverse ? g.linkCords.at(-1) : g.linkCords[0],
          at = new Float32Array(vs.length).fill(Infinity),
          fine = new Float32Array(vs.length).fill(Infinity),
          heap = new Heap(),
          poly = reverse ? [...cord.poly].reverse() : cord.poly;
        let distance = 0;
        for (let j = 0; j < poly.length; j++) {
          if (j) distance += Math.hypot(poly[j].x - poly[j - 1].x, poly[j].y - poly[j - 1].y);
          const v = poly[j];
          at[v.id] = distance / FAST;
          fine[v.id] = 0;
          heap.push([at[v.id], v.id]);
        }
        let pops = 0;
        while (heap.a.length) {
          const [time, id] = heap.pop();
          if (++pops % 12000 === 0) yield "route";
          if (time > at[id] + 0.00001) continue;
          const v = vs[id];
          for (const si of v.edges) {
            const s = ss[si],
              q = s.a === v ? s.b : s.a,
              highway = s.cord >= 0,
              dd = highway ? 0 : fine[id] + s.len,
              next = time + s.len / (highway ? FAST : SLOW);
            if (dd > CUTOFF || next >= at[q.id]) continue;
            at[q.id] = next;
            fine[q.id] = dd;
            heap.push([next, q.id]);
          }
        }
        const buckets = new Map(),
          residue = new Map(),
          list = [];
        let count = 0, highwayCount = 0, fineCount = 0, maxFine = 0;
        let seen = 0;
        for (const s of ss) {
          if (++seen % 15000 === 0) yield "route-list";
          const highway = s.cord >= 0,
            a = at[s.a.id] <= at[s.b.id] ? s.a : s.b,
            dd = fine[a.id] + (highway ? 0 : s.len / 2);
          if (!Number.isFinite(at[a.id]) || dd > CUTOFF) continue;
          const arrival = at[a.id] + s.len / (highway ? FAST : SLOW) / 2,
            amp = highway ? 1 : Math.exp(-dd / LAMBDA);
          if (amp < 0.05) continue;
          count++;
          if (highway) highwayCount++;
          else fineCount++;
          maxFine = Math.max(maxFine, dd);
          list.push({ segment: s.id, arrival, fine: dd, amp, highway });
          // Only segments already mature on arrival enter cached sprites;
          // later growth glows as live strokes once it has been laid.
          if (s.field || s.birth > START_AGE - 5 || (s.path && s.path.unit >= 0 && g.units[s.path.unit].period < 1e8) || livingSeg(s)) continue;
          if (!highway) {
            // r11 growth residue: coarse arrival bands x near/far from the cord.
            const far = dd >= 160 ? 1 : 0,
              band = Math.floor(arrival / RSTEP),
              rk = band * 2 + far,
              rlevel = Math.min(2, Math.floor(amp * 3)),
              tk = `${Math.floor((s.a.x + s.b.x) / 2 / 128)},${Math.floor((s.a.y + s.b.y) / 2 / 128)}|${rlevel}|${s.explorer ? 1 : 0}`;
            let layer = residue.get(rk);
            if (!layer) residue.set(rk, (layer = { band, far, arrival: (band + 0.5) * RSTEP, items: new Map(), images: null, bytes: 0 }));
            let it = layer.items.get(tk);
            if (!it) layer.items.set(tk, (it = { path: new Path2D(), bounds: [Infinity, Infinity, -Infinity, -Infinity], amp: (rlevel + 0.6) / 3, explorer: !!s.explorer, end: null }));
            const rb = it.bounds;
            rb[0] = Math.min(rb[0], s.a.x, s.b.x); rb[1] = Math.min(rb[1], s.a.y, s.b.y);
            rb[2] = Math.max(rb[2], s.a.x, s.b.x); rb[3] = Math.max(rb[3], s.a.y, s.b.y);
            if (it.end !== s.a) it.path.moveTo(s.a.x, s.a.y);
            it.path.lineTo(s.b.x, s.b.y);
            it.end = s.b;
          }
          const index = Math.floor(arrival / STEP),
            tileX = Math.floor((s.a.x + s.b.x) / 2 / 96),
            tileY = Math.floor((s.a.y + s.b.y) / 2 / 96),
            level = highway ? 5 : Math.min(4, Math.floor(amp * 5)),
            type = highway ? "cord" : s.explorer ? "explorer" : "fine",
            key = `${tileX},${tileY}|${level}|${type}`;
          let bucket = buckets.get(index);
          if (!bucket) buckets.set(index, (bucket = new Map()));
          let item = bucket.get(key);
          if (!item) bucket.set(key, (item = { tile: `${tileX},${tileY}`, bounds: [Infinity, Infinity, -Infinity, -Infinity], path: new Path2D(), amp: highway ? 1 : (level + 0.5) / 5, type, end: null }));
          const bb = item.bounds;
          bb[0] = Math.min(bb[0], s.a.x, s.b.x); bb[1] = Math.min(bb[1], s.a.y, s.b.y);
          bb[2] = Math.max(bb[2], s.a.x, s.b.x); bb[3] = Math.max(bb[3], s.a.y, s.b.y);
          if (item.end !== s.a) item.path.moveTo(s.a.x, s.a.y);
          item.path.lineTo(s.b.x, s.b.y);
          item.end = s.b;
        }
        yield "route-list";
        let nb = 0;
        for (const b of buckets.values()) {
          if (++nb % 150 === 0) yield "route-buckets";
          const groups = new Map();
          b.images = [];
          b.bytes = 0;
          for (const item of b.values()) {
            if (!groups.has(item.tile)) groups.set(item.tile, []);
            groups.get(item.tile).push(item);
          }
          b.ensureImages = () => {
            if (resident.has(b)) { resident.delete(b); resident.set(b, true); return; }
            while (resident.size >= cacheStats.limit) {
              const old = resident.keys().next().value;
              resident.delete(old);
              cacheStats.bytes -= old.bytes;
              for (const t of old.images) t.image.width = t.image.height = 0;
              old.images = [];
              old.bytes = 0;
              cacheStats.evictions++;
            }
            for (const items of groups.values()) {
              const l = Math.floor(Math.min(...items.map((p) => p.bounds[0]))) - 3,
                t = Math.floor(Math.min(...items.map((p) => p.bounds[1]))) - 3,
                r = Math.ceil(Math.max(...items.map((p) => p.bounds[2]))) + 3,
                bt = Math.ceil(Math.max(...items.map((p) => p.bounds[3]))) + 3,
                image = document.createElement("canvas");
              image.width = Math.ceil((r - l) * d);
              image.height = Math.ceil((bt - t) * d);
              const x = image.getContext("2d");
              x.setTransform(d, 0, 0, d, -l * d, -t * d);
              x.lineCap = "round";
              x.lineJoin = "round";
              x.globalCompositeOperation = light ? "source-over" : "lighter";
              for (const item of items) {
                const main = item.type === "cord";
                x.strokeStyle = main ? palette.core : palette.glow;
                if (main) {
                  // Soft body glow under a calmer core: the cord glows, not a spark.
                  x.globalAlpha = light ? 0.1 : 0.22;
                  x.lineWidth = 4.2;
                  x.stroke(item.path);
                }
                x.globalAlpha = Math.min(1, item.amp * (light ? 0.34 : 0.85) * (item.type === "explorer" ? 1.25 : 1));
                x.lineWidth = main ? 1.1 : item.type === "explorer" ? 0.85 : 0.55;
                x.stroke(item.path);
              }
              b.images.push({ image, x: l, y: t });
              b.bytes += image.width * image.height * 4;
            }
            resident.set(b, true);
            cacheStats.bytes += b.bytes;
            cacheStats.peakBytes = Math.max(cacheStats.peakBytes, cacheStats.bytes);
            cacheStats.buckets = resident.size;
            cacheStats.misses++;
          };
        }
        yield "route-sprites";
        let arrivalMin = Infinity,
          arrivalMax = -Infinity;
        for (const v of list) {
          if (v.arrival < arrivalMin) arrivalMin = v.arrival;
          if (v.arrival > arrivalMax) arrivalMax = v.arrival;
        }
        const destination = reverse ? g.nodes[0] : g.nodes.at(-1);
        routes.push({
          from: poly[0].source, to: destination.source, cord: cord.id, reverse,
          landings: g.nodes.map((n) => ({ node: n.source, time: at[n.id] })).filter((n) => Number.isFinite(n.time)),
          duration: at[destination.id], buckets, count, highway: highwayCount, fine: fineCount, maxFine,
          arrivalMin, arrivalMax, list, step: STEP, residue: [...residue.values()].sort((a, b) => a.arrival - b.arrival),
        });
        yield "route-done";
      }
      return routes;
    })();
  }

  // ---- pulse schedule + per-frame live drawing (called by the compositor) ----
  const TAIL = 5.6,
    RISE = 1.4;
  const envelope = (dt) => (dt < RISE ? Math.sin(((dt / RISE) * Math.PI) / 2) ** 2 : Math.cos((((dt - RISE) / (TAIL - RISE)) * Math.PI) / 2) ** 2) * 0.7;
  function schedule(routes) {
    const R = rng("r10-breath-trips"),
      trips = [];
    // r11: the first trip departs after the load-in intro.
    let start = INTRO_S + 2.5 * INTRO_SCALE, // 8.5 s at 6 s; 17 s at 12 s
      from = 0,
      previous = -1;
    for (let i = 0; i < 90; i++) {
      let choices = routes.filter((r) => r.from === from && r.to !== previous);
      if (!choices.length) choices = routes.filter((r) => r.from === from);
      const route = choices[Math.floor(R() * choices.length)];
      if (!route) break;
      trips.push({ route, start });
      start += Math.max(16, route.arrivalMax + TAIL + 2) + R() * 2.5;
      previous = from;
      from = route.to;
    }
    return { trips, period: start + 10 };
  }
  const metrics = (window.__pulseMetrics = { costs: [], frames: 0, maxConcurrent: 0, maxRipples: 0, paused: true, reason: "building", activeBuckets: 0 });

  // ---- r11 growth residue: what a pulse lit lingers faintly, then retreats
  // from the far edges back toward the nodes. Bounded: an LRU of tile bitmaps
  // per (arrival band x near/far) layer; never accumulates across trips. ----
  const RES = 0.15,
    RES_HOLD = 2,
    RES_BACK = 0.2,
    RES_FAR = 3,
    RES_FADE = 6;
  const residueCache = new Map(),
    residueStats = (window.__residueStats = { bytes: 0, peakBytes: 0, layers: 0, limit: 64, misses: 0, drawn: 0 });
  function residueImages(layer) {
    if (layer.images) {
      residueCache.delete(layer);
      residueCache.set(layer, true);
      return layer.images;
    }
    while (residueCache.size >= residueStats.limit) {
      const old = residueCache.keys().next().value;
      residueCache.delete(old);
      residueStats.bytes -= old.bytes;
      for (const t of old.images) t.image.width = t.image.height = 0;
      old.images = null;
      old.bytes = 0;
    }
    const tiles = new Map();
    for (const [k, it] of layer.items) {
      const tile = k.split("|")[0];
      if (!tiles.has(tile)) tiles.set(tile, []);
      tiles.get(tile).push(it);
    }
    layer.images = [];
    layer.bytes = 0;
    for (const items of tiles.values()) {
      let l = Infinity, t = Infinity, r = -Infinity, b = -Infinity;
      for (const it of items) {
        l = Math.min(l, it.bounds[0]);
        t = Math.min(t, it.bounds[1]);
        r = Math.max(r, it.bounds[2]);
        b = Math.max(b, it.bounds[3]);
      }
      l = Math.floor(l) - 2;
      t = Math.floor(t) - 2;
      r = Math.ceil(r) + 2;
      b = Math.ceil(b) + 2;
      const image = document.createElement("canvas");
      image.width = Math.ceil((r - l) * d);
      image.height = Math.ceil((b - t) * d);
      const x = image.getContext("2d");
      x.setTransform(d, 0, 0, d, -l * d, -t * d);
      x.lineCap = "butt";
      x.globalCompositeOperation = light ? "source-over" : "lighter";
      x.strokeStyle = palette.glow;
      for (const it of items) {
        x.globalAlpha = it.amp * (light ? 0.5 : 0.9);
        x.lineWidth = it.explorer ? 0.7 : 0.5;
        x.stroke(it.path);
      }
      layer.images.push({ image, x: l, y: t, w: r - l, h: b - t });
      layer.bytes += image.width * image.height * 4;
    }
    residueCache.set(layer, true);
    residueStats.bytes += layer.bytes;
    residueStats.peakBytes = Math.max(residueStats.peakBytes, residueStats.bytes);
    residueStats.layers = residueCache.size;
    residueStats.misses++;
    return layer.images;
  }
  // Residue alpha of one layer at trip age: rises as the crest passes, holds,
  // then fades latest-reached (far edge) first, back toward the source node.
  function residueAlpha(r, layer, age) {
    const rise = smooth((age - layer.arrival - 0.6) / 2.6);
    if (rise <= 0) return 0;
    const F = r.arrivalMax + RES_HOLD + (r.arrivalMax - layer.arrival) * RES_BACK - layer.far * RES_FAR;
    return RES * rise * (1 - smooth((age - F) / RES_FADE));
  }
  const residueEnd = (r) => r.arrivalMax + RES_HOLD + (r.arrivalMax - r.arrivalMin) * RES_BACK + RES_FADE;
  function drawResidue(ctx, t, sched, box) {
    let drawn = 0;
    for (const trip of sched.trips) {
      const age = t - trip.start;
      if (age < 0 || age > residueEnd(trip.route)) continue;
      for (const layer of trip.route.residue) {
        const a = residueAlpha(trip.route, layer, age);
        if (a < 0.008) continue;
        ctx.globalAlpha = a;
        for (const tile of residueImages(layer)) {
          ctx.drawImage(tile.image, tile.x, tile.y, tile.w, tile.h);
          grow(box, [tile.x, tile.y, tile.x + tile.w, tile.y + tile.h]);
        }
        drawn++;
      }
    }
    residueStats.drawn = drawn;
    return drawn;
  }
  function drawPulses(ctx, T, A, g, sched, box) {
    let drawn = 0,
      active = 0;
    const t = ((T % sched.period) + sched.period) % sched.period;
    drawResidue(ctx, t, sched, box);
    const trip = sched.trips.filter((p) => t >= p.start && t < p.start + p.route.arrivalMax + TAIL).at(-1);
    if (trip) {
      active = 1;
      const age = t - trip.start,
        r = trip.route,
        lo = Math.max(0, Math.floor((age - TAIL) / r.step)),
        hi = Math.floor(age / r.step);
      for (let i = lo; i <= hi; i++) {
        const bucket = r.buckets.get(i);
        if (!bucket) continue;
        const dt = age - (i + 0.5) * r.step;
        if (dt < 0 || dt > TAIL) continue;
        const env = envelope(dt);
        if (env < 0.012) continue;
        bucket.ensureImages();
        ctx.globalAlpha = env * Math.exp((-i * r.step * 38) / 2000);
        for (const tile of bucket.images) {
          ctx.drawImage(tile.image, tile.x, tile.y, tile.image.width / d, tile.image.height / d);
          grow(box, [tile.x, tile.y, tile.x + tile.image.width / d, tile.y + tile.image.height / d]);
        }
        drawn++;
      }
      // Laid-late and living segments glow as strokes, only where drawn now
      // (r11.2: batched into a few quantised groups).
      lifeStamp++;
      const late = r.lateList;
      let k0 = 0,
        k1 = late.length;
      while (k0 < k1) {
        const m = (k0 + k1) >> 1;
        if (late[m].arrival < age - TAIL) k0 = m + 1;
        else k1 = m;
      }
      for (let k = k0; k < late.length; k++) {
        const item = late[k],
          dt = age - item.arrival;
        if (dt < 0) break;
        const s = g.ss[item.segment],
          P = s.path;
        if (P && arcNow(P, A, T, g) < P.arcs[s.index + 1]) continue;
        const ga = Math.min(1, envelope(dt) * item.amp * (light ? 0.3 : 0.8)),
          key = 1e6 + Math.round(ga * 120) * 1000 + Math.round(Math.max(0.45, s.width * 1.5) * 20);
        if (ga < 0.004) continue;
        let path = pulseGroups.get(key);
        if (!path) pulseGroups.set(key, (path = new Path2D()));
        path.moveTo(s.a.x, s.a.y);
        path.lineTo(s.b.x, s.b.y);
        grow(box, [Math.min(s.a.x, s.b.x) - 2, Math.min(s.a.y, s.b.y) - 2, Math.max(s.a.x, s.b.x) + 2, Math.max(s.a.y, s.b.y) + 2]);
      }
      flushPieces(ctx, pulseGroups);
      for (const landing of r.landings) {
        // r11: no bloom at the departing node (that read as a disc lighting up
        // before the ripple); the pulse leaves as the ripple along the hyphae.
        if (landing.time < 1.5) continue;
        const landed = age - landing.time;
        if (landed < 0 || landed >= 7) continue;
        const bloom = scene.blooms[landing.node],
          front = 0.2 + (landed / 4.8) * 1.25; // ring radius in R0 units
        for (const b of bloom.bands) {
          if (!b.r0) continue; // the core band never lights as a whole
          const mid = (b.r0 + b.r1) / 2,
            gain = Math.exp(-(((mid - front) / 0.16) ** 2)) * smooth((landed - 0.15) / 0.6) * (0.55 + 0.45 * Math.min(1, mid)) * smooth((7 - landed) / 2);
          if (gain < 0.02) continue;
          const groups = bandGroups(b);
          if (!groups) continue;
          strokeGroups(ctx, groups, palette.core, gain * (light ? 0.34 : 0.7));
          grow(box, b.box);
          drawn++;
        }
      }
    }
    metrics.maxConcurrent = Math.max(metrics.maxConcurrent, active);
    metrics.activeBuckets = drawn;
    return active;
  }

  // ---- breathing: per-colony silhouettes, compositor opacity (desynchronised) ----
  let breathBox = null;
  function* makeBreath(g) {
    breathBox?.remove();
    breathBox = document.createElement("div");
    breathBox.className = "mycelium-breath";
    breathBox.setAttribute("aria-hidden", "true");
    Object.assign(breathBox.style, { left: "0px", top: L.top + "px", width: L.W + "px", height: L.H + "px" });
    document.getElementById("page-mycelium")?.after(breathBox);
    const R = rng("r10-breath-phase");
    for (const b of scene.blooms) {
      // r11.2: the whole colony breathes, strongest at its core and fading
      // outward (a 0.32-1.05 R0 annulus alone read as a brighter ring).
      const segs = b.bands.filter((x) => x.r1 <= 1.05).flatMap((x) =>
        x.segs.map(([a, q, w, al]) => [a, q, w, al * (1 - 0.65 * smooth((Math.hypot(q.x - b.node.x, q.y - b.node.y) / b.R0 - 0.1) / 0.95))]),
      );
      if (!segs.length) continue;
      const r = b.R0 * 1.05,
        im = document.createElement("canvas"),
        size = Math.ceil(r * 2);
      im.width = Math.ceil(size * d);
      im.height = Math.ceil(size * d);
      const x = im.getContext("2d");
      x.setTransform(d, 0, 0, d, -(b.node.x - r) * d, -(b.node.y - r) * d);
      x.globalCompositeOperation = light ? "source-over" : "lighter";
      paintSegs(x, segs, palette.glow, 0.7, 0);
      Object.assign(im.style, { left: b.node.x - r + "px", top: b.node.y - r + "px", width: size + "px", height: size + "px", animationDuration: (11 + R() * 7).toFixed(2) + "s", animationDelay: (-R() * 18).toFixed(2) + "s" });
      im.dataset.colony = b.node.source;
      breathBox.append(im);
      yield "breath";
    }
  }
  function freezeBreath(T) {
    if (!breathBox) return;
    for (const im of breathBox.children) {
      im.style.animationPlayState = "paused";
      const dur = parseFloat(im.style.animationDuration),
        base = parseFloat(im.dataset.delay ?? (im.dataset.delay = im.style.animationDelay));
      im.style.animationDelay = (base - (T % dur)).toFixed(3) + "s";
    }
  }

  // ---- build orchestration ----
  // r11 load-in intro: the first mount on a page grows the hero out from its
  // nodes over INTRO_S seconds of the shared clock (eased out: fast early
  // spread, gentle settle). Re-mounts (theme/hue/style), reduced motion and
  // tabs hidden at load show the established state at once.
  const intro = { on: !introPlayed && !reduce.matches && !document.hidden, skipAt: null, override: null };
  introPlayed = true;
  function ageAt(t) {
    if (intro.override !== null) return intro.override + t;
    if (!intro.on) return START_AGE + t;
    if (intro.skipAt !== null) return START_AGE + Math.max(0, t - intro.skipAt);
    if (t >= INTRO_S) return START_AGE + (t - INTRO_S);
    return START_AGE * (1 - Math.pow(1 - Math.max(0, t) / INTRO_S, INTRO_POW));
  }
  const introRunning = () => intro.on && intro.override === null && intro.skipAt === null && scene.clock.elapsed < INTRO_S + 0.25 * INTRO_SCALE;
  scene.ageAt = ageAt;
  /** Seconds into the intro at which the hero reaches age A (inverse map). */
  /** Hero age at which vertex v is shown in the load-in (mat schedule). */
  scene.vertexAge = (v) => {
    const P = scene.geometry?.paths?.[v.owner] || buildOut.paths?.[v.owner];
    return (v.birth || 0) + (P?.matShift || 0);
  };
  /** The compositor's hero blit adds the colony cores and felt fills while
   * they still build up (same additive composite as the baked raster). */
  scene.drawIntroCores = (ctx) => {
    const g = scene.geometry || buildOut;
    if (growth.coresDrawn || !g.colonies || !g.protect || !(growth.age > 0)) return;
    ctx.save();
    ctx.globalCompositeOperation = light ? "source-over" : "lighter";
    drawCores(ctx, g, growth.age, 1);
    ctx.restore();
  };
  scene.introTimeOfAge = (A) => (A >= START_AGE ? INTRO_S : INTRO_S * (1 - Math.pow(Math.max(0, 1 - A / START_AGE), 1 / INTRO_POW)));
  scene.intro = intro;
  // Compositors that predate ageAt read startAge (A = startAge + t); the
  // __setGrowthAge diagnostic assigns it, which pins the age mapping.
  Object.defineProperty(scene, "startAge", {
    configurable: true,
    enumerable: true,
    get: () => (intro.override !== null ? intro.override : ageAt(scene.clock.elapsed) - scene.clock.elapsed),
    set: (v) => {
      intro.override = v;
    },
  });
  scene.introProgress = () => Math.max(0, Math.min(1, ageAt(scene.clock.elapsed) / START_AGE));
  window.MyceliumHero = { INTRO_MS, firstPulseAt: INTRO_S + 2.5 * INTRO_SCALE, introProgress: () => (window.__myceliumScene === scene && !scene.disposed ? scene.introProgress() : 1) };
  const onHidden = () => {
    // A tab hidden mid-intro returns to the established state, not a replay.
    if (document.hidden && introRunning()) intro.skipAt = scene.clock.elapsed;
  };
  document.addEventListener("visibilitychange", onHidden);
  const buildOut = {};
  let introRaf = 0,
    frame = 0;
  // Until the compositor attaches, the hero drives its own growth frames.
  function introFrame(now) {
    introRaf = 0;
    if (disposed || scene.ready) return;
    scene.clock.tick(now);
    const g = scene.geometry || buildOut;
    if (g.paths && g.colonies && g.units && !reduce.matches) {
      commitPresented();
      growStep(ageAt(scene.clock.elapsed), g, performance.now() + 8);
      present();
    }
    if (!scene.timings.firstPaint) scene.timings.firstPaint = performance.now() - begun;
    introRaf = requestAnimationFrame(introFrame);
  }
  introRaf = requestAnimationFrame(introFrame);
  (async () => {
    let g = geometryCache.get(L.key);
    if (!g) {
      sliceStart = performance.now();
      g = await run(buildGeometry(L, budget, buildOut));
      scene.timings.geometry = performance.now() - begun;
      if (disposed) return;
      g.field = await run(extendFieldGraph({
        vs: g.vs, ss: g.ss, paths: [], vertex: g.vertex, edge: g.edge, nodes: g.nodes, w: L.W, h: L.Hh, top: L.top, budget,
        // Field hyphae continue from hero strands, never from fringe hairs or knot fibres.
        seedOk: (v) => v.birth > 0 && v.birth < START_AGE - 10 && v.owner >= 0 && g.paths[v.owner]?.unit < 0 && !/hair/.test(g.paths[v.owner].kind),
      }));
      scene.timings.field = performance.now() - begun;
      if (disposed) return;
      await run(assignLife(g));
      if (disposed) return;
      // Paths held back for the life assignment resume their static growth.
      g.lifeAssigned = true;
      const queued = new Set([...growth.pending, ...growth.active]);
      // r11.2c (owner: strands popped in whole): a held path resumes growing
      // from where it was held, at its own speed from now (never drawn whole
      // in one step); its forks keep at least its delay, so nothing grows
      // ahead of the part it springs from.
      const Anow = Number.isFinite(growth.age) ? growth.age : 0;
      for (const P of g.paths) {
        P.pre = undefined;
        if (!queued.has(P) && growable(P, g) && P.drawn < rasterLen(P) - 1e-6) {
          const v0 = P.pts[0],
            par = v0.owner >= 0 && v0.owner !== P.id ? g.paths[v0.owner] : null,
            own = Anow - (P.t0 + (P.matShift || 0) + (P.drawn || 0) / P.v);
          P.resumeDelay = Math.max(0, own, par?.resumeDelay || 0);
          growth.pending.push(P);
        }
      }
      // Final colony alphas over the whole static raster (see rebakeStatic):
      // after the scene is ready. Inside the build it held the compositor's
      // attach (and with it the field's load-in) back by ~4 s, so the field
      // appeared nearly whole at 8.5 s of a 9 s intro (owner: abrupt load-in).
      g.needsRebake = true;
      g.routes = await run(buildSignalRoutes(g));
      if (disposed) return;
      await run(routeLists(g));
      scene.timings.routes = performance.now() - begun;
      geometryCache.clear();
      geometryCache.set(L.key, g);
    } else {
      // Palette/theme switch reuses geometry; palette-bound sprites are redrawn lazily.
      resetGrowth(g);
      for (const r of g.routes) {
        for (const b of r.buckets.values()) {
          for (const t of b.images || []) t.image.width = t.image.height = 0;
          b.images = [];
        }
        for (const l of r.residue || []) {
          for (const t of l.images || []) t.image.width = t.image.height = 0;
          l.images = null;
        }
      }
    }
    if (disposed) return;
    scene.geometry = g;
    g.livePool = g.paths.filter((P) => P.unit >= 0 && g.units[P.unit]?.kind === "explorer");
    const bloomOut = {};
    await run(makeBlooms(g, bloomOut));
    scene.blooms = bloomOut.blooms;
    const sched = schedule(g.routes);
    scene.sched = sched;
    scene.activeTrips = (t) => {
      const e = ((t % sched.period) + sched.period) % sched.period;
      return sched.trips.map((trip) => ({ route: trip.route, age: e - trip.start })).filter((x) => x.age >= 0 && x.age <= x.route.arrivalMax + TAIL);
    };
    // Without an intro the established state is grown in bounded slices.
    if (!intro.on || reduce.matches) {
      const A0 = reduce.matches ? 1e5 : ageAt(scene.clock.elapsed);
      await run((function* () {
        while (!growStep(A0, g, performance.now() + 8)) {
          if (!reduce.matches) present(), commitPresented();
          yield "grow";
        }
      })());
      present();
      commitPresented();
    }
    if (disposed) return;
    if (!reduce.matches) await run(makeBreath(g));
    // r11.2c: the text-safe zone for the pulse light (see textSafeMask).
    const safeOut = {};
    if (!reduce.matches) await run(textSafeMask(canvas.closest("section") || canvas.parentElement, L, safeOut));
    if (disposed) return;
    scene.textSafe = safeOut.mask || null;
    await nextTask();
    if (disposed) return;
    const tc = performance.now();
    scene.timings.complete = tc - begun;
    const straightness = () => measurePaths(g.paths.filter((P) => P.len >= 28 && !/hair|fibre/.test(P.kind)).map((P) => ({ kind: P.kind, points: P.pts })));
    let auditCache = null;
    const audit = () =>
      auditCache ??
      (auditCache = auditPaths(
        g.paths.filter((P) => !/hair/.test(P.kind) && P.kind !== "cord-fibre").map((P) => ({ kind: /explorer/.test(P.kind) ? "sparse" : P.kind, points: P.pts }))
          .concat(g.field.strands.filter((p) => !p.hair).map((p) => ({ kind: "field", points: p.points }))),
      ));
    Object.defineProperty(window, "__straightness", { configurable: true, get: straightness });
    Object.defineProperty(window, "__morphologyAudit", { configurable: true, get: audit });
    Object.assign(scene, {
      graph: { vs: g.vs, ss: g.ss, nodes: g.nodes, cords: g.linkCords, explorerAudit: g.explorerAudit, units: g.units, paths: g.paths },
      field: g.field,
      routes: g.routes,
      drawLive(ctx, T, A, box) {
        // The previous present has been composited: drop its live tails.
        commitPresented();
        lifeStamp++;
        const running = introRunning();
        frame++;
        growStep(A, g, performance.now() + 10);
        stepXfade();
        // Presented every frame during the intro, a cross-fade and the 12 s
        // after the intro (late and resumed paths finish growing then; at one
        // present per 4 age units they appeared in 4 s chunks), then every 4.
        const key = running || growth.xfade || A < START_AGE + 12 ? frame : Math.floor(A / 4);
        if (key !== growth.presentKey) {
          growth.presentKey = key;
          present();
        }
        const s = drawLive(ctx, A, g, box, running);
        if (!dips.started && !running && g.lifeAssigned && growth.coresDrawn && A >= START_AGE && scene.timings.rebaked) startDips(g);
        const p = T < -1e5 ? 0 : drawPulses(ctx, T, A, g, sched, box);
        return { ...s, pulses: p };
      },
      // Running frames grow in drawLive; exact (frozen/diagnostic) ages grow
      // here in full, and backward seeks rebuild the raster.
      bakeUntil: (A, batched = false) => {
        if (batched) return false;
        finishXfade(); // diagnostic seeks show the final raster
        growStep(A, g);
        const changed = present();
        commitPresented();
        return changed;
      },
      liveCount: (A) => liveAt(A, g).length + growth.active.length,
      // r11.2: the living periphery, drawn by the compositor's life layer.
      drawLife: (ctx, T, A, x0, x1, y0, y1) => drawLife(ctx, T, A, g, x0, x1, y0, y1),
      /** Bounds (hero coords) of the living parts that moved since the last
       * call (the compositor repaints only the life tiles they touch). */
      lifeChanges: (T, A, all = false) => {
        lifeStamp++;
        const out = [];
        for (const P of g.lifeList) {
          let vis, state, s0;
          if (P.unit >= 0) {
            const c = unitClock(P, g.units, A, T);
            vis = Math.max(0, Math.min(P.len, (c.age - P.t0) * P.v));
            state = c.state;
            s0 = 0;
          } else {
            const y = heroVis(P, A, T);
            vis = y.vis;
            state = y.state;
            s0 = y.follow ? 0 : y.base;
          }
          const last = P.lifeShown;
          if (last && !all && Math.abs(last.vis - vis) < 1 && last.state === state) continue; // as the field (LIFE_STEP)
          const lo = Math.max(0, Math.min(last ? last.vis : s0, vis) - (state === 2 || last?.state === 2 || !last ? 34 : 14)),
            hi = Math.max(last ? last.vis : vis, vis) + 4;
          if (last) {
            last.vis = vis;
            last.state = state;
          } else P.lifeShown = { vis, state };
          if (all) continue;
          let bx0 = Infinity, by0 = Infinity, bx1 = -Infinity, by1 = -Infinity;
          for (let i = 0; i < P.pts.length; i++) {
            if (P.arcs[i] < lo - 2 && (i + 1 >= P.pts.length || P.arcs[i + 1] < lo)) continue;
            if (P.arcs[i] > hi + 2) break;
            const q = P.pts[i];
            if (q.x < bx0) bx0 = q.x;
            if (q.y < by0) by0 = q.y;
            if (q.x > bx1) bx1 = q.x;
            if (q.y > by1) by1 = q.y;
          }
          if (bx0 < Infinity) out.push([bx0 - 6, by0 - 6, bx1 + 6, by1 + 6]);
        }
        return out;
      },
      growthState: () => ({ age: growth.age, active: growth.active.length, pending: growth.pending.length, nextT0: Math.min(...growth.pending.map((P) => P.t0)), lastDone: Math.max(...g.paths.filter((P) => growable(P, g)).map((P) => P.t0 + rasterLen(P) / P.v)), startAge: START_AGE, dips: dips.done }),
      visibleArcs: (list, T) => {
        lifeStamp++;
        // As the compositor: reduced motion shows the established state (A = 1e5).
        const A = reduce.matches ? 1e5 : ageAt(T);
        return list.map((P) => visibleArc(P, A, T, g));
      },
      lifeStats: () => {
        const own = g.lifeList.filter((P) => P.dyn && !P.dyn.follow);
        return { living: g.lifeList.length, units: g.lifeList.filter((P) => P.unit >= 0).length, tips: own.filter((P) => P.dyn.base > 0).length, wholeHairs: own.filter((P) => !P.dyn.base).length, followers: g.lifeList.filter((P) => P.dyn?.follow).length };
      },
      freezeBreath,
      pulseEnvelope: envelope,
      pulseTail: TAIL,
      unitClock: (P, A, T) => unitClock(P, g.units, A, T),
    });
    // r11.2b: the probes below run in their own task (completeTask was
    // 30-35 ms at 1920 with everything in one).
    const parts = (scene.timings.completeParts = []);
    let tp = tc;
    const part = async () => {
      parts.push(+(performance.now() - tp).toFixed(1));
      await nextTask();
      tp = performance.now();
      return disposed;
    };
    if (await part()) return;
    /** r11.2 load-in probe: share of each colony's mat ink (radiating colony
     * hyphae, cotton, mat hyphae and their hairs; drawn length x alpha) that
     * is in the static raster now, plus the core/felt-fill build-up. */
    const matPaths = g.colonies.map(() => []);
    for (const P of g.paths) {
      if (!(P.matShift > 0) || !growable(P, g)) continue;
      const v = P.pts[0],
        ci = g.colonies.findIndex((c) => Math.hypot(v.x - c.node.x, v.y - c.node.y) < c.R0 * 1.2);
      if (ci >= 0) matPaths[ci].push(P);
    }
    window.__colonyMatProbe = () =>
      g.colonies.map((c, i) => {
        let have = 0,
          all = 0;
        for (const P of matPaths[i]) {
          const L = rasterLen(P),
            a = P.a[P.a.length >> 1] || 0.1;
          all += L * a;
          have += Math.min(L, P.drawn || 0) * a;
        }
        return { ink: all ? have / all : 1, cores: growth.coresDrawn ? 1 : matGrow(c, growth.age), paths: matPaths[i].length };
      });
    if (await part()) return;
    window.__growthGeometry = { nodes: g.nodes.map((n) => ({ x: n.x, y: n.y, id: n.source })), cords: g.linkCords.map((c) => ({ from: c.a.source, to: c.b.source, length: c.length })), segments: g.ss.length };
    let rootPaths = null;
    Object.defineProperty(window, "__rootPaths", {
      configurable: true,
      get: () => (rootPaths ??= g.paths.filter((P) => P.pts.length >= 9 && !/hair/.test(P.kind)).slice(0, 4000).map((P) => ({ kind: P.kind, points: P.pts.map((v) => [v.x, v.y]) }))),
      // Another renderer (staging style toggle) may assign a plain value.
      set: (v) => Object.defineProperty(window, "__rootPaths", { value: v, writable: true, configurable: true, enumerable: true }),
    });
    window.__fusionEvents = g.paths.filter((P) => P.fused).slice(0, 2000).map((P) => ({ x: P.pts.at(-1).x, y: P.pts.at(-1).y, birth: P.done, degree: P.pts.at(-1).edges.length }));
    window.__dendriticProbe = (T) => {
      const A = START_AGE + T;
      // r11.2: living tips die back tip-first in their retreat phase (owner);
      // outside it a drawn extent never decreases between frames.
      const sample = (a, t) => {
        lifeStamp++;
        return g.paths.map((P) => {
          if (P.unit >= 0) {
            const c = unitClock(P, g.units, a, t);
            return [Math.min(P.len, Math.max(0, (c.age - P.t0) * P.v)), c.state];
          }
          if (P.dyn) {
            const y = heroVis(P, a, t);
            return [y.vis, y.state];
          }
          return [Math.min(P.len, Math.max(0, (a - P.t0) * P.v)), 1];
        });
      };
      const now = sample(A, T),
        next = sample(A + 1 / 60, T + 1 / 60);
      let activeTips = 0,
        regressingTips = 0,
        retreatingTips = 0;
      g.paths.forEach((P, i) => {
        const [ea, sa] = now[i],
          [eb, sb] = next[i];
        if (ea > 0 && ea < P.len && eb > ea) activeTips++;
        if (eb < ea - 1e-6) {
          if (sa === 2 || sb === 2) retreatingTips++;
          else regressingTips++;
        }
      });
      const fused = g.paths.filter((P, i) => P.fused && now[i][0] >= P.len - 1e-6);
      return { activeTips, regressingTips, retreatingTips, fusions: fused.length, connectedSplits: fused.filter((P) => P.pts.at(-1).edges.length >= 3).length };
    };
    if (await part()) return;
    window.__cordAudit = g.cords.map((C) => ({ id: C.id, link: C.link, fibres: C.fibres, w0: C.width0, w1: C.width1, length: C.length, brush: C.brush }));
    // r11 item 3: fuzz along persistent strands (hairs whose base lies on the strand).
    window.__fuzzAudit = () => {
      const hairs = new Map();
      for (const P of g.paths) {
        if (!/hair/.test(P.kind) || P.kind === "knot-hair") continue;
        const o = P.pts[0].owner;
        if (o >= 0) hairs.set(o, (hairs.get(o) || 0) + 1);
      }
      const out = {};
      for (const P of g.paths) {
        if (!/^(explorer|web|brush|mat|colony)$/.test(P.kind) || P.len < 60) continue;
        (out[P.kind] ??= []).push(((hairs.get(P.id) || 0) * 100) / P.len);
      }
      return Object.fromEntries(Object.entries(out).map(([k, v]) => {
        v.sort((a, b) => a - b);
        return [k, { strands: v.length, bare: v.filter((x) => x === 0).length, p10: v[Math.floor(v.length * 0.1)], p50: v[v.length >> 1] }];
      }));
    };
    // r11 item 7: hyphal knots (positions, radii, host colony distance).
    window.__knotAudit = () =>
      g.knots.map((K) => {
        const c = g.colonies.reduce((a, b) => (Math.hypot(b.node.x - K.x, b.node.y - K.y) < Math.hypot(a.node.x - K.x, a.node.y - K.y) ? b : a));
        return { x: K.x, y: K.y, r: K.r, el: K.el, primordium: K.primordium, fibres: K.fibres, lobes: K.lobes.length, colonyDistance: Math.hypot(c.node.x - K.x, c.node.y - K.y) / c.R0, onGraph: g.vs[K.v]?.edges.length >= 2, textSafe: g.protect(K.x, K.y) };
      });
    // r11 item 4: residue alpha per layer of a route at a trip age.
    scene.residueProbe = (routeIndex, age) => {
      const r = g.routes[routeIndex];
      return { end: residueEnd(r), arrivalMax: r.arrivalMax, layers: r.residue.map((l) => ({ arrival: l.arrival, far: l.far, alpha: residueAlpha(r, l, age), tiles: l.items.size })) };
    };
    // r11 item 2: colonies are always the densest mats. Local density = mean
    // linear luminance of the baked raster in 24x24 CSS px windows; a colony's
    // core window is compared with every window along every cord beyond the
    // colony mats (and peak brightness, p98, with the same windows).
    if (await part()) return;
    window.__colonyDensity = () => {
      const W = canvas.width,
        H = canvas.height,
        data = sx.getImageData(0, 0, W, H).data,
        lin = new Float32Array(256);
      for (let i = 0; i < 256; i++) {
        const v = i / 255;
        lin[i] = v <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4;
      }
      const win = (x, y, half = 12) => {
        const x0 = Math.max(0, Math.round((x - half) * d)),
          x1 = Math.min(W, Math.round((x + half) * d)),
          y0 = Math.max(0, Math.round((y - half) * d)),
          y1 = Math.min(H, Math.round((y + half) * d)),
          vals = [];
        for (let yy = y0; yy < y1; yy++)
          for (let xx = x0; xx < x1; xx++) {
            const k = (yy * W + xx) * 4;
            // getImageData is un-premultiplied: the composite over black is rgb x alpha.
            vals.push((0.2126 * lin[data[k]] + 0.7152 * lin[data[k + 1]] + 0.0722 * lin[data[k + 2]]) * (data[k + 3] / 255));
          }
        if (!vals.length) return { mean: 0, p98: 0 };
        const mean = vals.reduce((a, b) => a + b, 0) / vals.length;
        vals.sort((a, b) => a - b);
        return { mean, p98: vals[Math.floor(vals.length * 0.98)] };
      };
      const colonies = g.colonies.map((c) => {
        // The densest 24 px window within 0.25 R0 of the colony centre.
        let best = { mean: 0, p98: 0 };
        for (let a = 0; a < 6.28; a += 0.785)
          for (const f of [0, 0.12, 0.24]) {
            const w = win(c.node.x + Math.cos(a) * f * c.R0, c.node.y + Math.sin(a) * f * c.R0);
            if (w.mean > best.mean) best = w;
            if (!f) break;
          }
        return { colony: c.i, R0: c.R0, coreWidth: 0.5 * c.R0, core: best, visible: c.node.x > 12 && c.node.x < L.W - 12 && c.node.y > 12 && c.node.y < L.H - 12 };
      });
      const cords = g.cords.map((C) => {
        let max = { mean: 0, p98: 0 },
          at = null;
        for (let s = 0; s < C.length; s += 8) {
          const v = C.spine.pts[Math.min(C.spine.pts.length - 1, Math.round((s / C.length) * (C.spine.pts.length - 1)))];
          // Within 0.85 R0 a cord is still emerging from (part of) the mat.
          if (g.colonies.some((c) => Math.hypot(v.x - c.node.x, v.y - c.node.y) < 0.85 * c.R0)) continue;
          if (v.x < 0 || v.x > L.W || v.y < 0 || v.y > L.H) continue;
          const w = win(v.x, v.y);
          if (w.mean > max.mean) max = { ...w, p98: Math.max(max.p98, w.p98) };
          else max.p98 = Math.max(max.p98, w.p98);
          if (w.mean >= max.mean) at = [v.x, v.y];
        }
        return { cord: C.id, link: C.link, from: C.a.source, w0: C.width0, max, at };
      });
      return { colonies, cords };
    };
    // Growth receipts by scene age: coherent colony fronts and cord accretion.
    window.__growthAudit = (A) => {
      const colonies = g.colonies.map((c) => {
        const sectors = new Array(12).fill(0);
        for (const P of g.paths) {
          if (P.kind !== "colony" || P.pts[0].source !== c.i || P.sub) continue; // main tuft front
          const e = (A - P.t0) * P.v;
          if (e <= 0) continue;
          for (let i = 0; i < P.pts.length && P.arcs[i] <= e; i += 3) {
            const q = P.pts[i],
              r = Math.hypot(q.x - c.node.x, q.y - c.node.y),
              k = Math.floor(((Math.atan2(q.y - c.node.y, q.x - c.node.x) + Math.PI) / (2 * Math.PI)) * 12) % 12;
            if (r > sectors[k]) sectors[k] = r;
          }
        }
        const live = sectors.filter((r) => r > 0),
          mean = live.reduce((a, b) => a + b, 0) / Math.max(1, live.length),
          sd = Math.sqrt(live.reduce((a, b) => a + (b - mean) ** 2, 0) / Math.max(1, live.length));
        return { colony: c.i, meanFront: mean, frontCV: mean ? sd / mean : 0, sectors: live.length };
      });
      const cords = g.cords.filter((C) => C.link).map((C) => {
        const base = C.length * 0.35; // beyond the mat where fibres emerge
        let fibres = 0;
        for (const P of g.paths) if (P.group === C.id && P.s0 <= base && (A - P.t0) * P.v + P.s0 >= base) fibres++;
        const tipReached = Math.min(C.length, Math.max(0, (A - C.spine.t0) * C.spine.v));
        return { id: C.id, fibresAtBase: fibres, frontFraction: tipReached / C.length };
      });
      return { age: A, colonies, cords };
    };
    window.__growthProbe = (T) => {
      const A = START_AGE + T;
      return { age: A, live: liveAt(A, g).length, growing: growth.active.length, units: g.units.length };
    };
    window.__darkGapProbe = () => {
      const w = L.W,
        h = L.Hh;
      const eligible = g.ss.filter((s) => !s.field && s.cord < 0 && s.birth <= START_AGE).filter((s) => {
        const x = (s.a.x + s.b.x) / 2,
          y = (s.a.y + s.b.y) / 2;
        return x > w * 0.5 && x < w && y > 20 && y < h - 20 && g.nodes.every((n) => Math.hypot(x - n.x, y - n.y) > 90) && g.linkCords.every((c) => c.poly.every((v) => Math.hypot(x - v.x, y - v.y) > 32));
      });
      return { segments: eligible.length, length: eligible.reduce((n, s) => n + s.len, 0), width: w, height: h, nodeClearance: 90, cordVertexClearance: 32 };
    };
    if (await part()) return;
    window.__graphAudit = {
      vertices: g.vs.length, segments: g.ss.length, isolated: g.vs.filter((v) => !v.edges.length).length, cap: CAP,
      routes: g.routes.map((r) => ({ from: r.from, to: r.to, duration: r.duration, activated: r.count, highway: r.highway, fine: r.fine, buckets: r.buckets.size, maxFine: r.maxFine, arrivalMin: r.arrivalMin, arrivalMax: r.arrivalMax })),
    };
    window.__pulseTrips = sched.trips.map((t) => ({ from: t.route.from, to: t.route.to, start: t.start, arrival: t.start + t.route.duration, end: t.start + t.route.arrivalMax + TAIL }));
    // The compositor attaches in its own task(s).
    parts.push(+(performance.now() - tp).toFixed(1));
    scene.timings.completeTask = Math.max(...parts);
    await nextTask();
    if (disposed) return;
    const tr = performance.now();
    Object.assign(canvas.dataset, {
      segments: g.ss.length, fusions: g.paths.filter((P) => P.fused).length, nodes: g.nodes.length, cords: g.linkCords.length, minWidth: 0.16, maxWidth: 12,
      palette: state.glow, renderMs: scene.timings.complete.toFixed(1), geometryMs: (scene.timings.geometry ?? 0).toFixed(1), graphVertices: g.vs.length, segmentCap: CAP, routes: g.routes.length,
      paths: g.paths.length, units: g.units.length, built: "1", startAge: START_AGE,
    });
    scene.built = true;
    scene.ready = true;
    for (const cb of scene.listeners.splice(0)) cb(scene);
    scene.timings.readyTask = performance.now() - tr; // includes the compositor's first attach stage
    if (g.needsRebake) {
      g.needsRebake = false;
      await nextTask();
      if (disposed) return;
      await run(g.monotone());
      if (disposed) return;
      await run(rebakeStatic(g));
    }
  })();
  return () => {
    disposed = true;
    job = null;
    cancelAnimationFrame(introRaf);
    document.removeEventListener("visibilitychange", onHidden);
    channel.port1.onmessage = null;
    breathBox?.remove();
    for (const r of scene.routes || []) for (const b of r.buckets.values()) for (const t of b.images || []) t.image.width = t.image.height = 0;
    for (const layer of residueCache.keys()) for (const t of layer.images || []) t.image.width = t.image.height = 0;
    for (const layer of residueCache.keys()) layer.images = null;
    if (window.__myceliumScene === scene) scene.disposed = true;
  };
}
