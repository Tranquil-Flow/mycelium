// SPDX-License-Identifier: AGPL-3.0-only
/** R10 shared geometry: seeded persistent (correlated-turn) hyphal walks, the
 * two-sided visible-scale morphology audit, and the shared scene clock.
 * Pure functions only (no DOM) except `setup`/`sceneClock`; runnable in Node. */
function rng(seed) {
  let h = 2166136261;
  for (const c of String(seed)) {
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
// Approximately standard normal (Irwin-Hall of four uniforms).
function gauss(R) {
  return (R() + R() + R() + R() - 2) * 1.7320508;
}
// Independent integer hash in [0,1): phases and periods never share a seed.
function hash01(n) {
  n = Math.imul(n ^ (n >>> 16), 0x45d9f3b);
  n = Math.imul(n ^ (n >>> 16), 0x45d9f3b);
  return ((n ^ (n >>> 16)) >>> 0) / 4294967296;
}
function smooth(v) {
  v = v < 0 ? 0 : v > 1 ? 1 : v;
  return v * v * (3 - 2 * v);
}
function setup(c) {
  const rect = c.getBoundingClientRect(),
    d = Math.min(devicePixelRatio || 1, 2),
    w = rect.width,
    h = rect.height;
  c.width = Math.round(w * d);
  c.height = Math.round(h * d);
  const off = document.createElement("canvas");
  off.width = c.width;
  off.height = c.height;
  const x = off.getContext("2d");
  x.scale(d, d);
  return { x, w, h, off, d };
}
/**
 * Correlated-turn walk (D1). The turning rate itself is an Ornstein-Uhlenbeck
 * process, so the heading persists and bends smoothly over 50-200 px. The
 * curvature amplitude drifts along the strand (aperiodic regional variation),
 * and a visible-scale guard bends any run whose 150/250/400 px chord/arc ratio
 * would exceed `straightMax`. Kinks are left to callers (forks).
 *  opts: step, k (max curvature rad/px), run (mean run px), jit, restore,
 *        pull(x,y,meanHeading,arc)->dMean, straightMax
 */
/** Slow regional persistence field (0.35-1.65): curvature varies by region. */
function regionGain(x, y) {
  return Math.max(0.35, Math.min(1.65, 1 + 0.45 * Math.sin(x / 410 + 1.3) * Math.sin(y / 530 + 0.7) + 0.2 * Math.sin((x + y) / 260 + 2.1)));
}
function persistWalk(p, heading, len, R, opts = {}) {
  const ds0 = opts.step ?? 2.5,
    n = Math.max(2, Math.ceil(len / ds0)),
    ds = len / n,
    // Per-strand curvature scale and run length: varied by strand and region.
    floor = 0.45,
    pull = opts.pull;
  let kmax = (opts.k ?? 0.016) * (0.7 + R() * 0.6),
    run = (opts.run ?? 60) * (0.7 + R() * 0.6),
    jit = opts.jit ?? 0.03,
    restore = opts.restore ?? 0.008,
    straightMax = opts.straightMax ?? 0.982;
  const guards = [100, 150, 250, 400].map((a) => Math.round(a / ds)),
    // The first point keeps its identity: a walk from a graph vertex stays connected.
    points = [p];
  // Varied strands (sparse vocabulary): per-strand style and regional persistence,
  // so the field is not a population of similar gentle arcs.
  const events = [],
    kBase = kmax;
  if (opts.vary) {
    const reg = regionGain(p.x, p.y),
      u = R();
    kmax *= 0.3 + 0.7 * reg;
    straightMax = Math.min(straightMax, 0.972);
    run /= Math.sqrt(0.3 + 0.7 * reg);
    if (u < 0.16) {
      // long, nearly (never ruler-) straight runs
      kmax *= 0.75;
      run *= 1.6;
      jit *= 0.75;
      straightMax = 0.975;
    } else if (u < 0.6) {
      // one or two strong bends on an otherwise calm strand
      kmax *= 0.65;
      const nb = R() < 0.55 ? 1 : 2;
      for (let b = 0; b < nb; b++) {
        const span = 25 + R() * 30;
        events.push({ at: len * (0.12 + 0.76 * R()), span, k: ((R() < 0.5 ? -1 : 1) * (0.55 + R() * 0.6)) / span });
      }
    } else if (u < 0.8) {
      // restless strands: tighter, shorter curvature runs
      kmax *= 1.7;
      run *= 0.55;
    } else if (u < 0.88) {
      // an occasional kink (a lost branch scar)
      kmax *= 0.85;
      events.push({ at: len * (0.2 + 0.6 * R()), span: 0, kink: (R() < 0.5 ? -1 : 1) * (0.22 + R() * 0.28) });
    }
  }
  let mean = heading,
    h = heading,
    sign = R() < 0.5 ? -1 : 1,
    kt = sign * kmax * (floor + (1 - floor) * R()),
    k = kt * R(),
    next = Math.max(4, (-Math.log(1 - R()) * run) / ds),
    cool = 0,
    gain = 1,
    gainTarget = 1;
  for (let i = 1; i <= n; i++) {
    // Regional curvature gain wanders slowly and aperiodically.
    if (R() < ds / 160) gainTarget = 0.5 + R();
    gain += (gainTarget - gain) * 0.03;
    // Curvature runs of exponential length (mean ~60 px), not per-step targets.
    if (i >= next) {
      if (R() < 0.5) sign = -sign;
      kt = sign * kmax * gain * (floor + (1 - floor) * R());
      next = i + Math.max(6, (-Math.log(1 - R()) * run) / ds);
    }
    k += (kt - k) * 0.15;
    let extra = 0;
    for (const e of events) {
      const s = i * ds;
      if (e.span && s >= e.at && s < e.at + e.span) extra += e.k;
      else if (!e.span && !e.done && s >= e.at) {
        h += e.kink;
        e.done = true;
        cool = i + 30;
      }
    }
    if (pull) mean += pull(points[i - 1].x, points[i - 1].y, mean, i * ds);
    const dev = Math.atan2(Math.sin(h - mean), Math.cos(h - mean));
    h += (k + extra - dev * restore) * ds + gauss(R) * jit;
    const a = points[i - 1];
    points.push({ x: a.x + Math.cos(h) * ds, y: a.y + Math.sin(h) * ds });
    // Visible-scale anti-ruler guard: an O(1) chord test per window length;
    // a straight run starts a new curvature run back towards the mean.
    if (i >= cool)
      for (const w of guards) {
        if (i < w) break;
        const b = points[i - w];
        if (Math.hypot(points[i].x - b.x, points[i].y - b.y) / (w * ds) > straightMax) {
          sign = dev > 0 ? -1 : 1;
          kt = sign * Math.max(kmax, kBase * 0.8) * (0.8 + 0.4 * R());
          next = i + Math.max(16, run / ds);
          cool = i + 30;
          break;
        }
      }
  }
  if (opts.vary) unruler(points, ds);
  return points;
}
/**
 * r11: deterministic last resort against ruler-straight windows in the sparse
 * vocabulary (a rare calm strand can slip past the guard during a cool-down).
 * A window whose chord/arc ratio still exceeds the visible-scale ceiling gets a
 * smooth sine bow (a few px over 150-400 px) on the side it already curves to.
 * Consumes no randomness; the first point (a graph vertex) never moves.
 */
function unruler(points, ds) {
  const n = points.length - 1;
  let arc = null;
  const prefix = () => {
    arc = new Float64Array(n + 1);
    for (let k = 1; k <= n; k++) arc[k] = arc[k - 1] + Math.hypot(points[k].x - points[k - 1].x, points[k].y - points[k - 1].y);
  };
  for (const [span, ceiling] of [[150, 0.9965], [250, 0.9955], [400, 0.995]]) {
    const w = Math.round(span / ds);
    if (n < w + 2) continue;
    if (!arc) prefix();
    for (let i = w + 1; i <= n; i++) {
      const a = points[i - w],
        b = points[i],
        chord = Math.hypot(b.x - a.x, b.y - a.y);
      if (chord / (arc[i] - arc[i - w]) <= ceiling) continue;
      // Sagitta for the target ratio (circular arc approx.): 1 - 8/3 (s/L)^2.
      const sag = chord * Math.sqrt(((1 - (ceiling - 0.003)) * 3) / 8),
        nx = -(b.y - a.y) / chord,
        ny = (b.x - a.x) / chord,
        m = points[i - (w >> 1)],
        side = (m.x - a.x) * nx + (m.y - a.y) * ny >= 0 ? 1 : -1;
      for (let j = i - w + 1; j < i; j++) {
        const f = Math.sin((Math.PI * (j - (i - w))) / w) * sag * side;
        points[j] = { x: points[j].x + nx * f, y: points[j].y + ny * f };
      }
      prefix();
    }
  }
}
/** Steered walk that ends near `target`, still correlated-turn (cords). */
function steerWalk(p, target, R, opts = {}) {
  const len0 = Math.hypot(target.x - p.x, target.y - p.y),
    k = opts.steer ?? 0.035;
  let heading = Math.atan2(target.y - p.y, target.x - p.x) + gauss(R) * (opts.launch ?? 0.25);
  const pull = (x, y, h, arc) => {
    const want = Math.atan2(target.y - y, target.x - x),
      d = Math.atan2(Math.sin(want - h), Math.cos(want - h)),
      remaining = Math.hypot(target.x - x, target.y - y);
    // Steering firms up as the tip nears the target; far away it is a whisper.
    return d * k * (0.35 + 0.65 * smooth(1 - remaining / Math.max(1, len0)));
  };
  const points = persistWalk(p, heading, len0 * (opts.excess ?? 1.06), R, { ...opts, pull });
  // Trim at closest approach so the cord really reaches the target region.
  let best = 0,
    bd = Infinity;
  for (let i = 0; i < points.length; i++) {
    const dd = Math.hypot(points[i].x - target.x, points[i].y - target.y);
    if (dd < bd) {
      bd = dd;
      best = i;
    }
  }
  return points.slice(0, Math.max(2, best + 1));
}
// Backwards-compatible names: every generator now shares the persistent walk.
function wander(p, angle, len, R) {
  return persistWalk(p, angle, len, R);
}
function dendriteWalk(p, angle, len, R) {
  return persistWalk(p, angle, len, R);
}
function arcLengths(p) {
  const arc = [0];
  for (let i = 1; i < p.length; i++)
    arc.push(arc[i - 1] + Math.hypot(p[i].x - p[i - 1].x, p[i].y - p[i - 1].y));
  return arc;
}
function quantiles(v) {
  v.sort((a, b) => a - b);
  const q = (f) => v[Math.min(v.length - 1, Math.floor(v.length * f))] ?? null;
  return { count: v.length, min: v[0] ?? null, p05: q(0.05), p50: q(0.5), p95: q(0.95), p99: q(0.99), max: v.at(-1) ?? null };
}
/** Legacy 28 px chord audit, kept for comparison with R9 receipts. */
function measurePaths(paths) {
  const groups = {};
  for (const { kind, points: p } of paths) {
    const g = (groups[kind] ??= { whole: [], windows: [], longPaths: 0 });
    const arc = arcLengths(p),
      length = arc.at(-1);
    if (length < 28) continue;
    g.longPaths++;
    g.whole.push(Math.hypot(p.at(-1).x - p[0].x, p.at(-1).y - p[0].y) / length);
    for (let a = 0, b = 1; a < p.length - 1; a++) {
      while (b < p.length && arc[b] - arc[a] < 28 - 1e-6) b++;
      for (let c = b; c < p.length; c += Math.max(1, (c - b) >> 2))
        g.windows.push(Math.hypot(p[c].x - p[a].x, p[c].y - p[a].y) / (arc[c] - arc[a]));
    }
  }
  const summary = (v) => ({ ...quantiles(v), above097: v.filter((x) => x > 0.97).length });
  return Object.fromEntries(
    Object.entries(groups).map(([k, g]) => [k, { longPaths: g.longPaths, whole: summary(g.whole), windows: summary(g.windows) }]),
  );
}
// Equal-arc resampling for scale-correct turning/curvature measurements.
function resample(p, spacing) {
  const arc = arcLengths(p),
    out = [p[0]];
  for (let s = spacing, i = 1; s <= arc.at(-1); s += spacing) {
    while (i < p.length - 1 && arc[i] < s) i++;
    const f = (s - arc[i - 1]) / Math.max(1e-9, arc[i] - arc[i - 1]);
    out.push({ x: p[i - 1].x + (p[i].x - p[i - 1].x) * f, y: p[i - 1].y + (p[i].y - p[i - 1].y) * f });
  }
  return out;
}
/**
 * Two-sided visible-scale audit (replaces the 28 px straightness guard).
 *  - ruler side: chord/arc over every >=150 px window (150/250/400 px, every 10 px)
 *  - scribble side: absolute turning per 100 px (5 px resampling, so raster
 *    jitter is not mistaken for curvature) and U-turns (>150 deg net heading
 *    change within 45 px of arc) per 1000 px
 *  - periodicity: share of the strongest bin in the signed-curvature spectrum
 */
function auditPaths(paths, opts = {}) {
  const minLength = opts.minLength ?? 150,
    groups = {};
  for (const { kind, points } of paths) {
    const g = (groups[kind] ??= { paths: 0, length: 0, windows: [], turning: [], uturns: 0, spectral: [], strandTurn: [], bendContrast: [] });
    if (points.length < 2) continue;
    const q = resample(points, 5),
      length = (q.length - 1) * 5;
    if (length < minLength) continue;
    g.paths++;
    g.length += length;
    for (const span of [150, 250, 400]) {
      const k = span / 5,
        list = span === 150 ? g.windows : (g["windows" + span] ??= []);
      for (let a = 0; a + k < q.length; a += 2)
        list.push(Math.hypot(q[a + k].x - q[a].x, q[a + k].y - q[a].y) / span);
    }
    const head = [];
    for (let i = 1; i < q.length; i++) head.push(Math.atan2(q[i].y - q[i - 1].y, q[i].x - q[i - 1].x));
    const dh = [];
    for (let i = 1; i < head.length; i++) dh.push(Math.atan2(Math.sin(head[i] - head[i - 1]), Math.cos(head[i] - head[i - 1])));
    for (let a = 0; a + 20 <= dh.length; a += 4) {
      let s = 0;
      for (let i = a; i < a + 20; i++) s += Math.abs(dh[i]);
      g.turning.push(s);
    }
    if (length >= 150) {
      // Curvature variety: per-strand mean turning (rad/100 px) and bend
      // contrast (strongest 50 px window over the strand's median window).
      const tot = dh.reduce((s, v) => s + Math.abs(v), 0);
      g.strandTurn.push((tot * 100) / length);
      const wins = [];
      for (let a = 0; a + 10 <= dh.length; a += 2) {
        let s = 0;
        for (let i = a; i < a + 10; i++) s += dh[i];
        wins.push(Math.abs(s));
      }
      wins.sort((x, y) => x - y);
      g.bendContrast.push(wins.at(-1) / Math.max(0.02, wins[wins.length >> 1]));
    }
    for (let a = 0; a + 9 <= dh.length; a++) {
      let s = 0;
      for (let i = a; i < a + 9; i++) s += dh[i];
      if (Math.abs(s) > 2.62) {
        g.uturns++;
        a += 9;
      }
    }
    if (dh.length >= 48) {
      // Periodicity: the strongest autocorrelation of signed curvature beyond
      // its first zero crossing. A sine wave scores ~1; aperiodic bends ~0-0.3.
      const n = dh.length,
        mean = dh.reduce((s, v) => s + v, 0) / n,
        c = dh.map((v) => v - mean),
        c0 = c.reduce((s, v) => s + v * v, 0);
      if (c0 > 0) {
        let crossed = false,
          peak = 0;
        for (let lag = 1; lag <= Math.min(60, n >> 1); lag++) {
          let s = 0;
          for (let j = 0; j + lag < n; j++) s += c[j] * c[j + lag];
          const ac = (s / c0) * (n / (n - lag));
          if (ac < 0) crossed = true;
          else if (crossed) peak = Math.max(peak, ac);
        }
        g.spectral.push(peak);
      }
    }
  }
  return Object.fromEntries(
    Object.entries(groups).map(([k, g]) => {
      const w = quantiles(g.windows);
      return [
        k,
        {
          paths: g.paths,
          length: Math.round(g.length),
          window150: { ...w, above097: g.windows.filter((v) => v > 0.97).length, above099: g.windows.filter((v) => v > 0.99).length },
          window250: quantiles(g.windows250 ?? []),
          window400: quantiles(g.windows400 ?? []),
          turningPer100px: quantiles(g.turning),
          uturnsPer1000px: g.length ? (g.uturns * 1000) / g.length : 0,
          periodicity: quantiles(g.spectral),
          strandTurn: quantiles(g.strandTurn),
          strandTurnCV: (() => {
            const v = g.strandTurn,
              m = v.reduce((a, b) => a + b, 0) / Math.max(1, v.length);
            return m ? Math.sqrt(v.reduce((a, b) => a + (b - m) ** 2, 0) / Math.max(1, v.length)) / m : 0;
          })(),
          bendContrast: quantiles(g.bendContrast),
        },
      ];
    }),
  );
}
// Attach fine laterals to existing roots at Poisson positions, on random sides
// (the old k%2 alternation read as herringbone).
function rootLaterals(roots, attach, R) {
  let lateralCount = 0,
    minLateralsPer100px = Infinity;
  for (const p of roots) {
    const arc = arcLengths(p.points),
      length = arc.at(-1);
    p.laterals = 0;
    if (!length) continue;
    const count = Math.max(2, Math.ceil(length / 18));
    const at = Array.from({ length: count }, () => length * (0.04 + R() * 0.9)).sort((a, b) => a - b);
    for (const target of at) {
      let i = 1;
      while (i < arc.length - 1 && arc[i] < target) i++;
      const a = p.points[i - 1],
        b = p.points[i],
        heading = Math.atan2(b.y - a.y, b.x - a.x);
      attach(p, b, heading + (R() < 0.5 ? 1 : -1) * (0.3 + R() * 0.32), 5 + R() * 16 * (0.4 + R()), i);
      p.laterals++;
    }
    if (R() < 0.6) {
      const b = p.points.at(-1),
        a = p.points.at(-2),
        heading = Math.atan2(b.y - a.y, b.x - a.x);
      for (const side of [-1, 1]) {
        attach(p, b, heading + side * (0.26 + R() * 0.3), 6 + R() * 12, p.points.length - 1);
        p.laterals++;
      }
    }
    lateralCount += p.laterals;
    minLateralsPer100px = Math.min(minLateralsPer100px, (p.laterals * 100) / length);
  }
  return {
    mainStrands: roots.length,
    lateralCount,
    loneThreads: roots.filter((p) => !p.laterals).length,
    minLateralsPer100px: Number.isFinite(minLateralsPer100px) ? minLateralsPer100px : 0,
  };
}
function sceneClock() {
  let elapsed = 0,
    last = null;
  return {
    tick(now) {
      if (typeof document !== "undefined" && document.hidden) {
        last = null;
        return elapsed;
      }
      if (last !== null) elapsed += Math.min(0.25, Math.max(0, now - last) / 1000);
      last = now;
      return elapsed;
    },
    pause() {
      last = null;
    },
    get elapsed() {
      return elapsed;
    },
  };
}
export { rng, gauss, hash01, smooth, setup, regionGain, persistWalk, steerWalk, wander, dendriteWalk, arcLengths, resample, measurePaths, auditPaths, rootLaterals, sceneClock };
