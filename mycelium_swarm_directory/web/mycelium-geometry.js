// SPDX-License-Identifier: AGPL-3.0-only
/** Seeded root walks and swarm-only gill identities shared by the renderers. */
function rng(seed) {
  let h = 2166136261;
  for (const c of seed) {
    h ^= c.charCodeAt(0);
    h = Math.imul(h, 16777619);
  }
  return () => {
    h ^= h << 13;
    h ^= h >>> 17;
    h ^= h << 5;
    return (h >>> 0) / 4294967296;
  };
}
function setup(c) {
  const rect = c.getBoundingClientRect(),
    d = Math.min(devicePixelRatio || 1, 2),
    w = rect.width,
    h = rect.height;
  c.width = Math.round(w * d);
  c.height = Math.round(h * d);
  let off = document.createElement("canvas");
  off.width = c.width;
  off.height = c.height;
  let x = off.getContext("2d");
  x.scale(d, d);
  return { x, w, h, off, d };
}
// Seeded identity geometry for swarm cards only; never used by the backdrop.
function printAt(x, s, cx, cy, r, css, light) {
  const R = rng(s.id + "|" + s.owner),
    phase = R() * 6.28,
    lobes = 2 + Math.floor(R() * 6),
    hole = 0.08 + R() * 0.14,
    n = 180,
    ellipse = 0.72 + R() * 0.26,
    rotation = R() * 6.28;
  const mid = css.getPropertyValue("--strand").trim(),
    core = css.getPropertyValue("--core").trim();
  x.save();
  x.translate(cx, cy);
  x.rotate(rotation);
  x.scale(1, ellipse);
  if (!light) {
    const g = x.createRadialGradient(0, 0, 1, 0, 0, r * 1.5);
    g.addColorStop(0, core + "22");
    g.addColorStop(0.5, mid + "13");
    g.addColorStop(1, mid + "00");
    x.fillStyle = g;
    x.fillRect(-r * 1.5, -r * 1.5, r * 3, r * 3);
  }
  for (let i = 0; i < n; i++) {
    const a = (i / n) * Math.PI * 2,
      edge =
        r *
        (0.8 + 0.14 * Math.sin(a * lobes + phase) + 0.035 * Math.sin(a * 17)),
      start = r * hole;
    const grad = x.createLinearGradient(
      Math.cos(a) * start,
      Math.sin(a) * start,
      Math.cos(a) * edge,
      Math.sin(a) * edge,
    );
    grad.addColorStop(0, core);
    grad.addColorStop(0.35, css.getPropertyValue("--glow").trim());
    grad.addColorStop(1, mid);
    x.strokeStyle = grad;
    x.lineWidth = 0.25 + R() * 0.3;
    x.globalAlpha = 0.4 + R() * 0.5;
    x.beginPath();
    for (let j = 0; j <= 16; j++) {
      let t = j / 16,
        rr = start + (edge - start) * t,
        aa = a + 0.04 * Math.sin(t * 4 + phase) * (1 - t);
      if (!j) x.moveTo(Math.cos(aa) * rr, Math.sin(aa) * rr);
      else x.lineTo(Math.cos(aa) * rr, Math.sin(aa) * rr);
    }
    x.stroke();
    // Selected gills continue outwards as hyphae, not an enclosing icon outline.
    if (i % 6 === 0) {
      x.globalAlpha = light ? 0.34 : 0.45;
      x.strokeStyle = mid;
      x.beginPath();
      x.moveTo(Math.cos(a) * edge, Math.sin(a) * edge);
      x.quadraticCurveTo(
        Math.cos(a + 0.04) * r * 1.32,
        Math.sin(a + 0.04) * r * 1.32,
        Math.cos(a + 0.1) * r * 1.7,
        Math.sin(a + 0.1) * r * 1.7,
      );
      x.stroke();
    }
  }
  x.restore();
}
function rootWalk(p, angle, len, R) {
  const n = Math.max(2, Math.ceil(len / 3.5)),
    step = len / n,
    spread = 0.16 + R() * 0.72,
    persistence = 0.12 + R() * 0.65;
  let points = [p],
    heading = angle,
    drift = 0,
    run = 0,
    target = 0;
  for (let j = 0; j < n; j++) {
    if (run-- <= 0) {
      run = 1 + Math.floor(R() * 4);
      target = (R() - 0.5) * spread * 2.2;
      if (R() < 0.16) target += (R() > 0.5 ? 1 : -1) * (0.45 + R() * 0.55);
    }
    drift = drift * persistence + target * (1 - persistence);
    heading += drift + (R() - 0.5) * 0.18;
    // Keep long roots directed, without any periodic steering.
    let delta = Math.atan2(
      Math.sin(heading - angle),
      Math.cos(heading - angle),
    );
    heading -= delta * 0.16;
    let q;
    for (let trial = 0; trial < 24; trial++) {
      const a = points.at(-1);
      q = {
        x: a.x + Math.cos(heading) * step,
        y: a.y + Math.sin(heading) * step,
      };
      let bad = false;
      for (let k = 0; k < points.length; k++) {
        const arc = (points.length - k) * step;
        if (
          arc >= 28 - 1e-6 &&
          Math.hypot(q.x - points[k].x, q.y - points[k].y) / arc > 0.958
        ) {
          bad = true;
          break;
        }
      }
      if (!bad) break;
      heading += (R() > 0.5 ? 1 : -1) * (0.35 + R() * 0.85);
    }
    points.push(q);
  }
  return points;
}
function wander(p, angle, len, R) {
  return rootWalk(p, angle, len, R);
}
function bridgeRoot(a, b, R) {
  const len = Math.hypot(b.x - a.x, b.y - a.y),
    angle = Math.atan2(b.y - a.y, b.x - a.x),
    nx = -Math.sin(angle),
    ny = Math.cos(angle),
    n = Math.ceil(len / 3.5);
  // Condition an aperiodic lateral random walk on both endpoints; reject straight windows.
  for (let attempt = 0; attempt < 120; attempt++) {
    const offset = [0];
    let velocity = 0,
      target = 0,
      run = 0;
    for (let j = 1; j <= n; j++) {
      if (run-- <= 0) {
        run = 1 + Math.floor(R() * 4);
        target = (R() - 0.5) * (3 + attempt * 0.08);
      }
      velocity = 0.25 * velocity + 0.75 * target;
      offset.push(offset.at(-1) + velocity * 3.5);
    }
    const pp = offset.map((o, j) => ({
      x: a.x + ((b.x - a.x) * j) / n + nx * (o - (offset[n] * j) / n),
      y: a.y + ((b.y - a.y) * j) / n + ny * (o - (offset[n] * j) / n),
    }));
    pp[0] = a;
    pp[n] = b;
    // Local repair uses irregular normal displacements, not periodic wiggles.
    for (let sweep = 0; sweep < 50; sweep++) {
      let bad = 0;
      for (let i = 0; i < pp.length - 1; i++) {
        let arc = 0;
        for (let j = i + 1; j < pp.length; j++) {
          arc += Math.hypot(pp[j].x - pp[j - 1].x, pp[j].y - pp[j - 1].y);
          if (
            arc >= 24 &&
            Math.hypot(pp[j].x - pp[i].x, pp[j].y - pp[i].y) / arc > 0.945
          ) {
            const k = i + 1 + Math.floor(R() * (j - i - 1));
            const push = (R() > 0.5 ? 1 : -1) * (2 + R() * 3);
            pp[k].x += nx * push;
            pp[k].y += ny * push;
            bad++;
            break;
          }
        }
      }
      if (!bad) return pp;
    }
  }
  throw new Error("root bridge guard exhausted");
}
function measurePaths(paths) {
  const groups = {};
  for (const { kind, points: p } of paths) {
    const g = (groups[kind] ??= { whole: [], windows: [], longPaths: 0 });
    let length = 0;
    for (let j = 1; j < p.length; j++)
      length += Math.hypot(p[j].x - p[j - 1].x, p[j].y - p[j - 1].y);
    if (length < 28) continue;
    g.longPaths++;
    g.whole.push(Math.hypot(p.at(-1).x - p[0].x, p.at(-1).y - p[0].y) / length);
    // Every starting sample; all contiguous windows with at least 28px arc length.
    for (let a = 0; a < p.length - 1; a++) {
      let arc = 0;
      for (let b = a + 1; b < p.length; b++) {
        arc += Math.hypot(p[b].x - p[b - 1].x, p[b].y - p[b - 1].y);
        if (arc >= 28 - 1e-6)
          g.windows.push(Math.hypot(p[b].x - p[a].x, p[b].y - p[a].y) / arc);
      }
    }
  }
  function summary(v) {
    v.sort((a, b) => a - b);
    return {
      count: v.length,
      min: v[0] ?? null,
      p50: v[Math.floor(v.length * 0.5)] ?? null,
      p95: v[Math.floor(v.length * 0.95)] ?? null,
      max: v.at(-1) ?? null,
      above097: v.filter((x) => x > 0.97).length,
    };
  }
  return Object.fromEntries(
    Object.entries(groups).map(([k, g]) => [
      k,
      {
        longPaths: g.longPaths,
        whole: summary(g.whole),
        windows: summary(g.windows),
      },
    ]),
  );
}
export { rng, setup, printAt, rootWalk, wander, bridgeRoot, measurePaths };
