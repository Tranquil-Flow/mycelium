// SPDX-License-Identifier: AGPL-3.0-only
/** R10 page compositor and below-fold field. One page-space mycelium: the hero
 * graph and this field share vertices, edges and pulse routes. The field uses
 * the sparse (ref-8) vocabulary: long fine hyphae holding their heading along a
 * low-frequency flow away from the hero, the margin colonies and off-screen
 * sources, branching acutely into 2nd/3rd orders that run alongside the parent
 * and occasionally fuse into elongated cells. Density falls with distance from
 * sources and towards text. Two fixed viewport canvases: a static composite
 * (redrawn only on scroll or bake) and a live layer (fronts, pulses, blooms).
 */
import { rng, gauss, smooth, persistWalk, measurePaths, auditPaths, sceneClock } from "./mycelium-geometry.js";

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
  return { left, right, offset, rects: rects.length, enabled: innerWidth >= 1100 && Math.min(left, innerWidth - right) >= 60 };
}

/** Generator: lays the field into the shared graph; returns the field record. */
export function* extendFieldGraph({ vs, ss, paths, vertex, edge, nodes = [], w, h, top: pageTop = 0, budget = () => false, seedOk = () => true }) {
  const R = rng("r10-sparse-field"),
    W = document.documentElement.clientWidth || innerWidth,
    height = Math.max(1800, Math.round(innerHeight * 2.4)),
    top = h - 150,
    layout = contentBounds(0),
    left = 0;
  w = W;
  const baseCount = Math.round((w * height) / 26000),
    count = Math.round(baseCount * 1.2);
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
  const sparseWalk = { k: 0.013, run: 60, jit: 0.038, pull: pullFlow };
  // Sources: hero continuations, virtual edge sources, margin colonies.
  const seeds = [];
  const heroBand = nodes.length ? vs.filter((v) => v.y > top + 6 && v.y < h + 200 && v.edges.length > 1 && seedOk(v)) : [];
  for (let i = 0; i < Math.round(count * 0.3) && heroBand.length; i++) seeds.push({ v: heroBand[Math.floor(R() * heroBand.length)], hero: true });
  for (let i = 0; seeds.length < count && i < count * 6; i++) {
    const s = virtual[Math.floor(R() * virtual.length)],
      x = R() < 0.5 ? (R() < 0.5 ? R() * Math.max(80, layout.left) : w - R() * Math.max(80, w - layout.right)) : R() * w,
      y = top + 40 + R() * (height - 80),
      dd = Math.hypot(x - s.x, y - s.y);
    // Density by distance from sources and columns: margins carry more.
    if (R() > (0.35 + 0.65 * marginWeight(x)) * (0.5 + 0.5 / (1 + dd / 900))) continue;
    seeds.push({ x, y });
  }
  const mainRoots = [];
  for (const seed of seeds) {
    const start = seed.v ?? vertex(seed.x, seed.y, 0),
      heading = flow(start.x, start.y) + gauss(R) * 0.2,
      len = 200 + R() * 300,
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
      mainRoots.push(child);
      s += 50 + -Math.log(1 - R()) * 80;
    }
    budget() && (yield "field");
  }
  // Living branches: tips that grow, hold and dim in place (never retract).
  const live = [];
  for (const parent of mainRoots.filter(() => R() < 0.22)) {
    const at = Math.floor(parent.points.length * (0.4 + R() * 0.4)),
      a = parent.points[Math.max(0, at - 1)],
      b = parent.points[at];
    if (!b) continue;
    const hh = Math.atan2(b.y - a.y, b.x - a.x) + (R() < 0.5 ? -1 : 1) * (0.3 + R() * 0.25);
    const branch = add(persistWalk(b, hh, 60 + R() * 90, R, sparseWalk), { width: 0.4, alpha: parent.alpha * 0.9, birth: 1 + R() * 3 });
    live.push(branch);
    // A live tip meeting another strand fuses there (seamless, no bead).
    const end = branch.points.at(-1),
      near = (grid.get(gk(end.x, end.y)) || []).find((c) => c.p !== branch && c.p !== parent);
    if (near) fusions.push({ point: end, parent: branch, birth: branch.birth + branch.points.length * 0.5 });
  }
  if (!fusions.length && live.length) fusions.push({ point: live[0].points.at(-1), parent: live[0], birth: live[0].birth });
  yield "field-live";

  // Minor margin colonies: small asymmetric tufts dissolving into ref-8 hyphae.
  const marginRoutes = [],
    marginKnots = [],
    marginDensityRatio = 0.4;
  const join = (a, b, width = 0.9) => {
    const raw = persistWalk(a, Math.atan2(b.y - a.y, b.x - a.x), Math.hypot(b.x - a.x, b.y - a.y), R, { k: 0.016, run: 60, jit: 0.025 }),
      last = raw.at(-1),
      pts = raw.map((q, i) => ({ x: q.x + ((b.x - last.x) * i) / (raw.length - 1), y: q.y + ((b.y - last.y) * i) / (raw.length - 1) }));
    pts[0] = a;
    pts[pts.length - 1] = b;
    return add(pts, { width, alpha: 0.95, margin: true });
  };
  if (layout.enabled)
    for (const side of [-1, 1]) {
      const px = side < 0 ? layout.left * 0.45 : layout.right + (w - layout.right) * 0.55,
        knots = [];
      for (let y = top + 120 + R() * 120; y < top + height - 60; y += 380 + R() * 360) {
        const centre = vertex(px + gauss(R) * 14, y + gauss(R) * 30, 0);
        knots.push(centre);
        marginKnots.push(centre);
        // Fan biased along the margin, heavy-tailed lengths: never a 16-ray asterisk.
        const fan = Math.PI / 2 + gauss(R) * 0.4 + (R() < 0.5 ? Math.PI : 0);
        for (let j = 0; j < Math.round(150 * marginDensityRatio); j++) {
          const hh = R() < 0.65 ? fan + gauss(R) * 0.9 : R() * 6.28,
            o0 = R() * 6.28,
            r0 = Math.abs(gauss(R)) * 4,
            ln = 14 + 70 * Math.pow(R(), 1.5) * (0.6 + 0.4 * Math.cos(hh - fan));
          const p = add([centre, ...persistWalk({ x: centre.x + Math.cos(o0) * r0, y: centre.y + Math.sin(o0) * r0 }, hh, ln, R, { k: 0.014, run: 40, jit: 0.03, step: 2 })], { width: 0.24 + R() * 0.16, alpha: 0.62, margin: true });
          p.hair = true;
        }
        // ...dissolving into a few long sparse hyphae.
        for (let j = 0; j < 7; j++) {
          const hh = fan + gauss(R) * 1.2;
          const p = add(persistWalk(centre, hh, 140 + R() * 220, R, { ...sparseWalk, pull: undefined }), { width: 0.45, alpha: 0.75, margin: true });
          mainRoots.push(p);
        }
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
    gaps = [];
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
    if (i % 20 === 0 && budget()) yield "web";
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
  const rootAudit = {
    mainStrands: rooted.length, lateralCount, minLateralsPer100px: Number.isFinite(minLateralsPer100px) ? minLateralsPer100px : 0, loneThreads: rooted.filter((p) => !p.laterals).length,
    web: { orders, spacingCV, spectralPeakFraction: Math.max(...power) / power.reduce((n, v) => n + v, 0), fusions: webFusions, spacingSamples: gaps.length },
  };
  return {
    strands, fusions, connectors: [], top, height, width: w, left, layout, baseCount, count, rootAudit, marginKnots, marginRoutes, marginDensityRatio, pageTop,
    segments: ss.filter((s) => s.field), densityMultiplier: 1.2, flowSources: virtual.length,
  };
}

function life(p, t, reduce) {
  if (p.static || reduce) return { extent: p.points.length - 1, alpha: p.alpha };
  const phase = (t * 1.2 + p.phase) % p.period,
    local = phase - 5 - p.birth;
  // Dims in place over the last 20 s of a period; the laid path never retracts.
  const fade = 1 - smooth((phase - (p.period - 22)) / 20);
  return { extent: Math.max(0, Math.min(p.points.length - 1, local / 0.5)), alpha: p.alpha * Math.max(0, Math.min(1, local / 2)) * fade };
}

export function createField({ stage, state, reduce }) {
  const c = document.getElementById("page-mycelium"),
    x = c.getContext("2d");
  const live = document.createElement("canvas");
  live.className = "page-mycelium page-mycelium-live";
  live.setAttribute("aria-hidden", "true");
  c.after(live);
  const lx = live.getContext("2d");
  const tile = document.createElement("canvas"),
    bx = tile.getContext("2d");
  const hero = document.getElementById("hyphae"),
    css = getComputedStyle(stage),
    light = state.theme === "light",
    glow = css.getPropertyValue("--glow").trim(),
    core = css.getPropertyValue("--core").trim();
  const width = Math.round(document.documentElement.clientWidth || innerWidth),
    height = Math.round(innerHeight),
    d = Math.min(devicePixelRatio || 1, 2);
  for (const k of [c, live]) {
    k.width = width * d;
    k.height = height * d;
  }
  x.setTransform(d, 0, 0, d, 0, 0);
  lx.setTransform(d, 0, 0, d, 0, 0);
  const fieldAlpha = light ? 0.24 : 0.62,
    lightMul = light ? 0.62 : 1;
  let scene = hero ? window.__myceliumScene : null,
    field = null,
    graph = null,
    disposed = false,
    raf = 0,
    frozen = null,
    clock = scene?.clock || sceneClock(),
    lastScroll = NaN,
    lastVersion = -1,
    tileVersion = 0,
    drawnTile = -1,
    dirty = null,
    liveStrands = [],
    marginSchedules = [],
    heroInterval = 24;
  const metrics = (window.__understoryMetrics = { costs: [], frames: 0, paused: false, staticRedraws: 0, liveCosts: [] });
  const params = new URLSearchParams(location.search);
  if (params.has("t") && Number.isFinite(+params.get("t"))) frozen = +params.get("t");

  const marginWeight = (px) => {
    const l = field.layout;
    if (!l.enabled) return 0;
    const dd = px < l.left ? l.left - px : px > l.right ? px - l.right : 0,
      q = Math.max(0, Math.min(1, dd / 72));
    return q * q * (3 - 2 * q);
  };
  // Chunked strokes: per-chunk column/margin weight (a strand entering a text
  // column dims there, not by its start point) and a distal taper.
  const columnWeight = (px) => 0.32 + 0.68 * marginWeight(px);
  function drawStrand(ctx, p, extent, alpha) {
    const pts = p.points,
      n = pts.length - 1,
      end = Math.min(n, extent),
      CH = 10;
    ctx.lineCap = p.width < 0.55 ? "butt" : "round";
    for (let i = 0; i < end; i += CH) {
      const j = Math.min(i + CH, Math.ceil(end)),
        mid = pts[Math.min(n, (i + j) >> 1)],
        w = p.margin ? marginWeight(mid.x) : columnWeight(mid.x);
      if (w <= 0.01) continue;
      const distal = (i + j) / 2 > n * 0.5;
      ctx.globalAlpha = alpha * w * lightMul * (distal ? p.taper : 1);
      ctx.lineWidth = p.width * (distal ? 0.7 : 1);
      ctx.beginPath();
      ctx.moveTo(pts[i].x, pts[i].y);
      for (let k = i + 1; k <= j; k++) {
        const f = Math.min(1, end - k + 1);
        if (f <= 0) break;
        ctx.lineTo(pts[k - 1].x + (pts[k].x - pts[k - 1].x) * f, pts[k - 1].y + (pts[k].y - pts[k - 1].y) * f);
      }
      ctx.stroke();
    }
  }
  // The static tile is rasterised in bounded slices (no long task); it is
  // presented as it fills, like the hero.
  let rasterJob = 0;
  function rasterStatic() {
    tile.width = field.width;
    tile.height = Math.ceil(field.height);
    bx.setTransform(1, 0, 0, 1, -field.left, -field.top);
    bx.clearRect(field.left, field.top, field.width, field.height);
    bx.strokeStyle = glow;
    bx.globalCompositeOperation = light ? "source-over" : "lighter";
    const list = field.strands.filter((p) => p.static),
      job = ++rasterJob;
    let i = 0;
    const ch = new MessageChannel();
    ch.port1.onmessage = () => {
      if (disposed || job !== rasterJob) return;
      const t0 = performance.now();
      while (i < list.length && performance.now() - t0 < 8) {
        const p = list[i++];
        for (const shift of [-field.height, 0, field.height]) {
          if (p.box[3] + shift < field.top || p.box[1] + shift > field.top + field.height) continue;
          bx.save();
          bx.translate(0, shift);
          drawStrand(bx, p, p.points.length - 1, p.alpha * fieldAlpha);
          bx.restore();
        }
      }
      tileVersion++;
      if (i < list.length) ch.port2.postMessage(0);
      else c.dataset.tile = "1";
    };
    ch.port2.postMessage(0);
  }
  function attach() {
    field = scene ? scene.field : field;
    graph = scene ? scene.graph : graph;
    for (const p of field.strands) {
      let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
      for (const q of p.points) {
        if (q.x < x0) x0 = q.x;
        if (q.y < y0) y0 = q.y;
        if (q.x > x1) x1 = q.x;
        if (q.y > y1) y1 = q.y;
      }
      p.box = [x0 - 3, y0 - 3, x1 + 3, y1 + 3];
    }
    liveStrands = field.strands.filter((p) => !p.static);
    rasterStatic();
    heroInterval = scene?.sched ? Math.max(16, ...scene.routes.map((r) => r.arrivalMax + scene.pulseTail + 2)) : 24;
    marginSchedules = field.marginRoutes.map((r, side) => {
      const legs = r.paths.map((p) => {
        let distance = 0;
        const segments = p.segments.map((s) => {
          const start = distance;
          distance += s.len;
          return { s, arrival: start / 80 };
        });
        return { p, segments, duration: distance / 80 };
      });
      const period = Math.max(heroInterval / 0.9, ...legs.map((l) => l.duration + 8));
      const landing = scene?.routes?.[0]?.landings.find((l) => l.node === r.source.source)?.time || 0;
      return { ...r, legs, period, phase: landing + 3 + side * 9, chosen: new Map() };
    });
    window.__understoryAudit = {
      paths: field.strands.length, branches: field.strands.filter((p) => !p.static).length, fusions: field.fusions.length, nodes: field.marginKnots.length, sporeNodes: 0,
      densityMultiplier: 1.2, baseCount: field.baseCount, count: field.count, sharedGraph: !!scene, connectors: 0, canvasCount: 2, flowSources: field.flowSources,
      ...field.rootAudit, explorerLoneThreads: scene?.graph.explorerAudit?.loneThreads ?? 0, explorerAudit: scene?.graph.explorerAudit,
      marginKnots: field.marginKnots.length, marginDensityRatio: field.marginDensityRatio, marginPulseIntensity: 0.75, marginPulseLeak: 135,
      marginPulsePeriods: marginSchedules.map((s) => s.period), marginPulseRateRelative: marginSchedules.map((s) => heroInterval / s.period), protectedRects: field.layout.rects,
      canvasPixels: c.width * c.height, bytes: (2 * c.width * c.height + tile.width * tile.height) * 4,

    };
    // Heavy audits are computed lazily on first read, never during start-up.
    let straightness = null,
      morphology = null;
    Object.defineProperty(window.__understoryAudit, "straightness", { enumerable: true, get: () => (straightness ??= measurePaths(field.strands.filter((p) => !p.hair).map((p) => ({ kind: "understory", points: p.points })))) });
    Object.defineProperty(window.__understoryAudit, "morphology", { enumerable: true, get: () => (morphology ??= auditPaths(field.strands.filter((p) => !p.hair).map((p) => ({ kind: "understory", points: p.points }))).understory) });
    window.__marginPulseProbe = (t) => {
      const trips = marginTrips(t),
        base = (scene ? scene.hero.top : 0) + Math.max(0, Math.floor((scrollY - (scene ? scene.hero.top : 0) - field.top) / field.height)) * field.height;
      return {
        active: trips.map((s) => s.side), periods: marginSchedules.map((s) => s.period), intensity: 0.75, heroInterval,
        legs: trips.map((s) => ({ side: s.side, y0: base + Math.min(s.leg.p.points[0].y, s.leg.p.points.at(-1).y), y1: base + Math.max(s.leg.p.points[0].y, s.leg.p.points.at(-1).y) })),
      };
    };
    window.__setUnderstoryTime = (t) => {
      frozen = t;
      schedule();
      paint(t, true);
    };
    if (scene) {
      window.__setPulseTime = (t) => {
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
    window.__paintUnderstory = (t) => paint(t, true);
    lastVersion = -1;
    paint(frozen ?? clock.elapsed, true);
    c.dataset.ready = "1";
    schedule();
  }
  function marginTrips(t) {
    const viewTop = scrollY,
      viewBottom = scrollY + innerHeight;
    return marginSchedules.flatMap((s) => {
      const elapsed = t - s.phase;
      if (elapsed < 0 || !s.legs.length) return [];
      const cycle = Math.floor(elapsed / s.period),
        age = elapsed % s.period;
      // Pick the leg crossing the current viewport when the trip starts.
      let leg = s.chosen.get(cycle);
      if (!leg) {
        const pageY = (q) => (scene ? scene.hero.top : 0) + q.y;
        const k = Math.floor((viewTop - (scene ? scene.hero.top : 0) - field.top) / field.height);
        const shift = (scene ? 0 : 0) + Math.max(0, k) * field.height;
        leg = s.legs
          .map((l) => ({ l, score: Math.abs((pageY(l.p.points[0]) + pageY(l.p.points.at(-1))) / 2 + shift - (viewTop + viewBottom) / 2) }))
          .sort((a, b) => a.score - b.score)[0].l;
        s.chosen.set(cycle, leg);
        if (s.chosen.size > 8) s.chosen.delete(s.chosen.keys().next().value);
      }
      return age < leg.duration + 5 ? [{ ...s, leg, age }] : [];
    });
  }
  const envelope = (dt) => (dt < 1.4 ? Math.sin(((dt / 1.4) * Math.PI) / 2) ** 2 : Math.cos((((dt - 1.4) / 4.2) * Math.PI) / 2) ** 2) * 0.7;

  function paint(T, force = false) {
    if (disposed || !field) return;
    const begin = performance.now(),
      reduced = reduce.matches,
      t = reduced ? 32 : T,
      A = scene ? (reduced ? 1e5 : scene.startAge + t) : 0,
      scroll = scrollY,
      heroTop = scene ? scene.hero.top : 0,
      oy = heroTop - scroll; // page-space origin of scene coordinates
    // Bakes are batched to 4 s while running (one static redraw per batch at most).
    if (scene?.bakeUntil) frozen !== null || reduced ? scene.bakeUntil(A) : scene.bakeUntil(Math.floor(A / 4) * 4, true);
    const version = (scene ? scene.hero.version : 0) * 1000 + tileVersion;
    // ---- static composite: only on scroll or newly baked material ----
    if (force || scroll !== lastScroll || version !== lastVersion) {
      x.clearRect(0, 0, width, height);
      x.globalCompositeOperation = "source-over";
      x.globalAlpha = 1;
      if (scene && oy < height && oy + scene.hero.H > 0) x.drawImage(scene.hero.canvas, 0, oy, scene.hero.W, scene.hero.H);
      x.globalCompositeOperation = light ? "source-over" : "lighter";
      const origin = oy + field.top,
        first = Math.floor(-origin / field.height);
      for (let k = Math.max(scene ? 0 : -1e9, first - 1); k <= first + 1; k++) {
        const y = origin + k * field.height;
        if (y > height || y + field.height < 0) continue;
        x.drawImage(tile, field.left, y);
      }
      x.globalCompositeOperation = "source-over";
      lastScroll = scroll;
      lastVersion = version;
      metrics.staticRedraws++;
    }
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
      lx.save();
      lx.translate(0, oy);
      const hb = [Infinity, Infinity, -Infinity, -Infinity];
      liveStats = scene.drawLive(lx, scene.pulsesOff || reduced ? -1e6 : t, A, hb);
      lx.restore();
      if (hb[0] < Infinity) add(hb[0], hb[1] + oy, hb[2], hb[3] + oy);
    }
    const origin = oy + field.top,
      first = Math.floor(-origin / field.height);
    for (let k = Math.max(scene ? 0 : -1e9, first - 1); k <= first + 1; k++) {
      const y = origin + k * field.height;
      if (y > height || y + field.height < 0) continue;
      lx.save();
      lx.translate(0, y - field.top);
      lx.strokeStyle = glow;
      const shift = y - field.top;
      for (const p of liveStrands) {
        if (p.box[3] + shift < 0 || p.box[1] + shift > height) continue;
        const v = life(p, t, reduced);
        if (v.alpha <= 0 || v.extent <= 0) continue;
        drawStrand(lx, p, v.extent, v.alpha * fieldAlpha);
        add(p.box[0], p.box[1] + shift, p.box[2], p.box[3] + shift);
      }
      if (!reduced)
        for (const trip of marginTrips(t)) {
          lx.strokeStyle = core;
          lx.lineCap = "butt";
          const gain = 0.75 * (light ? 0.34 : 0.85);
          for (const item of trip.leg.segments) {
            const dt = trip.age - item.arrival;
            if (dt < 0 || dt > 5.6) continue;
            const s = item.s,
              mw = marginWeight((s.a.x + s.b.x) / 2),
              env = envelope(dt);
            lx.globalAlpha = env * gain * mw * 0.3;
            lx.lineWidth = 4;
            lx.beginPath();
            lx.moveTo(s.a.x, s.a.y);
            lx.lineTo(s.b.x, s.b.y);
            lx.stroke();
            lx.globalAlpha = env * gain * mw;
            lx.lineWidth = 1.1;
            lx.stroke();
          }
          add(trip.leg.p.box[0] - 140, trip.leg.p.box[1] + shift - 140, trip.leg.p.box[2] + 140, trip.leg.p.box[3] + shift + 140);
          const node = trip.leg.p.points.at(-1),
            age = trip.age - trip.leg.duration;
          if (age >= 0 && age < 5) {
            // Short peripheral leak along the receiving tuft's own hyphae.
            for (const p of field.strands) {
              if (!p.margin || (p.marginRoot || p.points[0]) !== node) continue;
              let distance = p.marginDistance || 0;
              for (const s of p.segments) {
                const dt = age - distance / 35;
                distance += s.len;
                if (distance > 135 || dt < 0 || dt > 3) continue;
                lx.globalAlpha = Math.sin((dt / 3) * Math.PI) ** 2 * gain * Math.exp(-distance / 67.5) * marginWeight(s.a.x);
                lx.lineWidth = Math.max(0.3, s.width * 1.5);
                lx.beginPath();
                lx.moveTo(s.a.x, s.a.y);
                lx.lineTo(s.b.x, s.b.y);
                lx.stroke();
              }
            }
          }
        }
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
          if (p && !p.static) {
            const v = life(p, t, false);
            if (v.extent < (s.fieldIndex || 0) + 1 || v.alpha <= 0) continue;
          }
          lx.globalAlpha = envelope(dt) * item.amp * (light ? 0.24 : 0.75);
          add(Math.min(s.a.x, s.b.x) - 3, Math.min(s.a.y, s.b.y) + oy - 3, Math.max(s.a.x, s.b.x) + 3, Math.max(s.a.y, s.b.y) + oy + 3);
          lx.lineWidth = Math.max(0.5, s.width * 1.8);
          lx.beginPath();
          lx.moveTo(s.a.x, s.a.y);
          lx.lineTo(s.b.x, s.b.y);
          lx.stroke();
        }
      lx.restore();
    }
    lx.globalAlpha = 1;
    dirty = box[0] < Infinity ? box : null;
    const cost = performance.now() - begin;
    metrics.frames++;
    metrics.costs.push(cost);
    metrics.liveCosts.push(performance.now() - lb);
    if (metrics.costs.length > 1200) {
      metrics.costs.shift();
      metrics.liveCosts.shift();
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
    Object.assign(c.dataset, { time: t.toFixed(3), paths: field.strands.length, nodes: field.marginKnots.length, sporeNodes: 0, canvasPixels: c.width * c.height });
  }
  function tick(now) {
    raf = 0;
    const T = clock.tick(now);
    paint(T);
    schedule();
  }
  function schedule() {
    const moving = !disposed && !!field && !reduce.matches && !document.hidden && frozen === null;
    metrics.paused = !moving;
    const pm = window.__pulseMetrics;
    if (pm) {
      pm.paused = !moving;
      pm.reason = reduce.matches ? "reduced-motion" : document.hidden ? "hidden" : frozen !== null ? "frozen" : !field ? "building" : "running";
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
  // Progressive presentation while the scene builds: blit the static raster as it fills.
  let buildRaf = 0;
  function buildTick(now) {
    buildRaf = 0;
    if (disposed || field) return;
    if (scene) {
      const T = clock.tick(now),
        oy = scene.hero.top - scrollY;
      if (scene.hero.version !== lastVersion || scrollY !== lastScroll) {
        x.clearRect(0, 0, width, height);
        if (!reduce.matches) x.drawImage(scene.hero.canvas, 0, oy, scene.hero.W, scene.hero.H);
        lastVersion = scene.hero.version;
        lastScroll = scrollY;
      }
      void T;
    }
    buildRaf = requestAnimationFrame(buildTick);
  }
  const changed = () => {
    if (document.hidden || reduce.matches) clock.pause();
    if (field) paint(frozen ?? clock.elapsed, true);
    schedule();
  };
  const scrolled = () => {
    if (!field) return;
    if (frozen !== null || reduce.matches) paint(frozen ?? clock.elapsed);
    else schedule();
  };
  if (scene) {
    buildRaf = requestAnimationFrame(buildTick);
    scene.onReady(() => {
      if (disposed) return;
      cancelAnimationFrame(buildRaf);
      attach();
    });
  } else {
    // Swarms app pages: a standalone field graph, also built in slices.
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
    let start = performance.now();
    const budget = () => (performance.now() - start > 8 ? ((start = performance.now()), true) : false);
    const gen = extendFieldGraph({ vs, ss, paths, vertex, edge, nodes: [], w: width, h: 150, budget });
    const ch = new MessageChannel();
    ch.port1.onmessage = () => {
      if (disposed) return;
      const t0 = performance.now();
      let r;
      do r = gen.next();
      while (!r.done && performance.now() - t0 < 9);
      if (!r.done) return ch.port2.postMessage(0);
      field = r.value;
      graph = { vs, ss };
      attach();
    };
    ch.port2.postMessage(0);
  }
  const layoutObserver = new MutationObserver(() => {
    if (!field) return;
    const previous = field.layout;
    field.layout = contentBounds(previous.offset);
    if (window.__understoryAudit) window.__understoryAudit.protectedRects = field.layout.rects;
    if (previous.left !== field.layout.left || previous.right !== field.layout.right) rasterStatic();
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
    tile.width = tile.height = 0;
    live.width = live.height = 0;
    live.remove();
  };
}
