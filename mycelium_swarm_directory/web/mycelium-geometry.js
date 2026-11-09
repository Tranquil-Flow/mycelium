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
/** Shared r11 load-in duration (design/r11-coordination.md): ONE value for hero,
 * field and margins. Every intro timing is designed at INTRO_BASE_MS (6 s) and
 * multiplied by INTRO_SCALE, so a longer intro is the same shape, slower.
 *
 * PRODUCTION_INTRO_MS is the production load-in: 9 s (owner's pick, x1.5, r11.2).
 * It is the single place to change (6000 | 9000 | 12000; see R11-2-READY).
 *
 * STAGING ONLY (r11.2 speed comparison): when the server-injected staging
 * controls exist (the Electric toggle [data-bg-choice] or the intro selector
 * [data-intro-choice], both only on next.mycelium.now with staging samples),
 * the duration is `?intro=6|9|12` (seconds), else the choice stored under
 * STAGING_INTRO_KEY, else STAGING_INTRO_DEFAULT_MS (9 s, the owner's lean).
 * Production never injects either control, so it never reads the parameter
 * or the key. */
const INTRO_BASE_MS = 6000,
  PRODUCTION_INTRO_MS = 9000,
  STAGING_INTRO_CHOICES = Object.freeze([6000, 9000, 12000]),
  STAGING_INTRO_DEFAULT_MS = 9000,
  STAGING_INTRO_KEY = "mycelium-staging-intro-v1";
function introMs(doc = typeof document === "undefined" ? null : document, loc = typeof location === "undefined" ? null : location, store = null) {
  if (!doc || !loc || !doc.querySelector("[data-bg-choice], [data-intro-choice]")) return PRODUCTION_INTRO_MS;
  const v = new URLSearchParams(loc.search).get("intro");
  if (v !== null && /^(6|9|12)$/.test(v)) return +v * 1000;
  try {
    const s = +(store || localStorage).getItem(STAGING_INTRO_KEY);
    if (STAGING_INTRO_CHOICES.includes(s)) return s;
  } catch {}
  return STAGING_INTRO_DEFAULT_MS;
}
const INTRO_MS = introMs(),
  INTRO_SCALE = INTRO_MS / INTRO_BASE_MS;
/** r11.2 passive life (owner: strands must visibly grow and fade back; "if a
 * human is paused at a point on the page, should be able to visibly see it is
 * growing/fading"). One cycle of a living tip: it regrows (g s), holds (h s),
 * dies back tip-first (r s), then rests (the remainder of the period). Grow
 * and retreat move at a near-constant speed (a trapezoid profile, 15% ease at
 * each end), so a tip travels at 8-20 px/s while active, never a slow creep.
 * Returns the extent fraction f (0..1) and the state (0 grow, 1 hold,
 * 2 retreat, 3 rest). Hero and field share it. */
const LIFE_CYCLE = Object.freeze({ G: 0.38, H: 0.24, R: 0.28, V_GROW: [7, 10], V_BACK: [7, 9], HOLD: [0.16, 0.12], REST_MIN: 2.5, EASE: 0.15 });
function trap(u, a = LIFE_CYCLE.EASE) {
  const v = 1 / (1 - a);
  if (u <= 0) return 0;
  if (u >= 1) return 1;
  if (u < a) return (v * u * u) / (2 * a);
  if (u <= 1 - a) return v * (u - a / 2);
  return 1 - (v * (1 - u) * (1 - u)) / (2 * a);
}
/** Per-strand timing from three hashes: average speeds 7-17 (grow) and 7-16
 * (retreat) px/s, so the peak is <= 20 px/s; period at least `period0`. */
function lifeTiming(len, period0, h1, h2, h3) {
  const { V_GROW, V_BACK, HOLD, REST_MIN } = LIFE_CYCLE,
    g = len / (V_GROW[0] + V_GROW[1] * h1),
    r = len / (V_BACK[0] + V_BACK[1] * h2),
    hold = (HOLD[0] + HOLD[1] * h3) * period0;
  return { period: Math.max(period0, g + hold + r + REST_MIN), g, h: hold, r };
}
function tipCycle(tau, period, g = LIFE_CYCLE.G * period, h = LIFE_CYCLE.H * period, r = LIFE_CYCLE.R * period) {
  const u = ((tau % period) + period) % period;
  if (u < g) return { f: trap(u / g), state: 0 };
  if (u < g + h) return { f: 1, state: 1 };
  if (u < g + h + r) return { f: 1 - trap((u - g - h) / r), state: 2 };
  return { f: 0, state: 3 };
}
/** r11.4 frame pacer (owner, after r11.3: "stutter is better but not
 * completely smooth"). Every background loop of the page (the scene build,
 * the post-ready passes, the field's attach stages, band rasters, blooms) runs
 * its slices here, one job per task, and a slice ends before the next vsync,
 * so build work never stacks onto an animation frame. Before, each loop
 * posted its own 4-9 ms MessageChannel tasks back to back: a slice landing on
 * a vsync next to a heavy intro frame cost one to four vsyncs (rAF gaps of
 * 50-230 ms at 1920 DPR2).
 *
 * - The pacer keeps a requestAnimationFrame running while it has work, and
 *   learns the display's frame period (60/120 Hz).
 * - A slice runs until MARGIN ms before the next 60 Hz vsync, at most MAX ms;
 *   with less than MIN ms left it waits for the frame (its task runs right
 *   after the frame's rendering).
 * - Throughput (the build's timeline must not slip: the field attaches when
 *   it is done): while background work gets less than its share of the main
 *   thread (SHARE[prio] of the last WIN ms), the animations redraw on every
 *   2nd (60 Hz display) or every 2nd-4th (120 Hz) vsync instead (see skip):
 *   a steady 30-60 Hz, never a dropped run of frames. A fast host keeps 60 Hz
 *   (or more) throughout; a 120 Hz display redraws at 60 Hz while work is
 *   pending. Starved work (below half its share: frames heavier than the
 *   vsync period, e.g. software raster) runs plain MAX ms slices, as before.
 * - Hidden tabs, or no frame for 4 periods: unpaced MAX ms slices (a tab
 *   hidden mid-build still finishes), with a timer fallback.
 * Jobs: { run(deadline) -> true when finished, prio (0 first), name }. */
const pacer = (() => {
  const MARGIN = 2.5,
    MIN = 2,
    MAX = 8,
    SHARE = [0.55, 0.35],
    WIN = 250,
    HZ60 = 1000 / 60;
  const hasWindow = typeof requestAnimationFrame === "function" && typeof MessageChannel === "function";
  let last = -Infinity,
    period = HZ60,
    raf = 0,
    timer = 0,
    posted = false,
    turn = 0,
    every = 1, // animations redraw on every `every`-th vsync
    judged = 0,
    ch = null;
  const jobs = [],
    spans = [], // [start, end] of the slices inside the last WIN ms
    stats = { slices: 0, waits: 0, floor: 0, unpaced: 0, maxSlice: 0, maxAt: "", every: [], log: [] };
  const now = () => performance.now();
  const target = () => SHARE[Math.min(SHARE.length - 1, Math.min(...jobs.map((j) => j.prio)))];
  function frame(t) {
    raf = 0;
    const d = t - last;
    // A period from undropped intervals only (60 or 120 Hz alike).
    if (d > 5 && d < period * 1.4) period = Math.min(20, Math.max(6, period + (d - period) * 0.2));
    last = t;
    // The redraw governor, judged every WIN ms: one vsync step at a time.
    const lo = Math.max(1, Math.round(HZ60 / period)),
      hi = Math.max(lo, Math.round((2 * HZ60) / period));
    if (!jobs.length) every = 1;
    else if (every < lo) every = lo;
    if (jobs.length && t - judged >= WIN) {
      judged = t;
      const sh = share(t),
        want = target();
      const was = every;
      if (sh < want) every = Math.min(hi, Math.max(lo, every + 1));
      else if (sh > want + 0.1) every = Math.max(lo, every - 1);
      else every = Math.max(lo, every);
      if (every !== was && stats.every.length < 64) stats.every.push([Math.round(t), every, +sh.toFixed(2)]);
    }
    if (jobs.length) {
      post();
      raf = requestAnimationFrame(frame);
    }
  }
  function post() {
    if (posted) return;
    posted = true;
    ch.port2.postMessage(0);
  }
  function kick() {
    if (!ch) {
      ch = new MessageChannel();
      ch.port1.onmessage = slice;
    }
    post();
    if (hasWindow && !raf) raf = requestAnimationFrame(frame);
  }
  function share(t) {
    while (spans.length && spans[0][1] < t - WIN) spans.shift();
    let s = 0;
    for (const [a, b] of spans) s += b - Math.max(a, t - WIN);
    return s / WIN;
  }
  function wait() {
    stats.waits++;
    clearTimeout(timer);
    // No frame within 4 periods (throttled or hidden): go on unpaced.
    timer = setTimeout(() => {
      timer = 0;
      if (jobs.length) post();
    }, 4 * Math.max(period, 50) + 2);
  }
  function deadlineAt(t) {
    if (!hasWindow || document.hidden || t - last > Math.max(period, 50) * 4) {
      stats.unpaced++;
      return t + MAX;
    }
    // Paced on a 60 Hz grid at most: on a 120 Hz display a slice may take
    // the odd (skipped) 120 Hz frame.
    const next = last + Math.max(period, HZ60),
      sh = share(t);
    // Starved (frames heavier than the vsync period, e.g. software raster):
    // plain MAX ms slices, as before the pacer, until the share recovers.
    if (sh < target() / 2) {
      stats.floor++;
      return t + MAX;
    }
    // A frame is due (its task is queued ahead of ours): yield to it.
    if (t > next - MARGIN) return -1;
    if (next - MARGIN - t >= MIN) return Math.min(t + MAX, next - MARGIN);
    return -1;
  }
  function slice() {
    posted = false;
    if (!jobs.length) return;
    const t = now(),
      deadline = deadlineAt(t);
    if (deadline < 0) return wait();
    // Highest priority first; round-robin among equals.
    let best = -1;
    for (let k = 0; k < jobs.length; k++) {
      const i = (k + turn) % jobs.length;
      if (best < 0 || jobs[i].prio < jobs[best].prio) best = i;
    }
    turn = best + 1;
    const job = jobs[best];
    let done = true;
    try {
      done = job.run(deadline) !== false;
    } finally {
      const e = now();
      if (done) jobs.splice(jobs.indexOf(job), 1);
      spans.push([t, e]);
      stats.slices++;
      if (e - t > stats.maxSlice) (stats.maxSlice = e - t), (stats.maxAt = job.name || "");
      if (e - t > 12 && stats.log.length < 64) stats.log.push([Math.round(t), +(e - t).toFixed(1), job.name || "", +(deadline - t).toFixed(1)]);
      if (jobs.length) post();
      else every = 1;
    }
  }
  return {
    stats,
    /** Adds a job; its run(deadline) is called in later tasks until it returns true. */
    add(job) {
      jobs.push(job);
      kick();
      return job;
    },
    remove(job) {
      const i = jobs.indexOf(job);
      if (i >= 0) jobs.splice(i, 1);
    },
    /** True when an animation frame `sinceDrawn` ms after the last drawn one
     * should not redraw (see the governor above); false whenever no
     * background work is pending. */
    skip(sinceDrawn) {
      return jobs.length > 0 && sinceDrawn < every * period - 3;
    },
    /** Resolves, with the slice's deadline, at the start of a paced slice
     * (a fresh task with frame time left). */
    slot(prio = 0, name = "slot") {
      return new Promise((resolve) => this.add({ prio, name, run: (deadline) => (resolve(deadline), true) }));
    },
  };
})();
if (typeof window !== "undefined") window.__myceliumPacer = pacer.stats;
export {
  pacer,
  rng, gauss, hash01, smooth, setup, regionGain, persistWalk, steerWalk, wander, dendriteWalk, arcLengths, resample, measurePaths, auditPaths, rootLaterals, sceneClock,
  INTRO_BASE_MS, PRODUCTION_INTRO_MS, STAGING_INTRO_CHOICES, STAGING_INTRO_DEFAULT_MS, STAGING_INTRO_KEY, INTRO_MS, INTRO_SCALE, introMs, LIFE_CYCLE, tipCycle, lifeTiming,
};
