// SPDX-License-Identifier: AGPL-3.0-only
/** Procedural spore deposits, seeded only by public swarm ID.
 * Local photo studies: Amanita, Conocybe, oyster, Panaeolus, Psathyrella,
 * Psilocybe, Volvariella and a bolete. No reference images ship with this module.
 * Natural pigment is independent of the site's glow and colour scheme.
 */
const TAU = Math.PI * 2;
const PIGMENTS = [
  ["white", "#eeeadd", "#777c70"],
  ["cream", "#ddd0a0", "#72766b"],
  ["pink", "#ad7874", "#e9e1d5"],
  ["rust", "#90502d", "#e8decc"],
  ["brown", "#60412e", "#e5dfd0"],
  ["purple-brown", "#483342", "#e7e0d5"],
  ["black", "#28272a", "#e8e3d7"],
];
const cache = new Map();
const MAX_BYTES = 8 * 1024 * 1024,
  MAX_ENTRIES = 128;
let bytes = 0,
  renders = 0,
  hits = 0;
function hash(value) {
  let h = 2166136261;
  for (const ch of String(value)) h = Math.imul(h ^ ch.charCodeAt(0), 16777619);
  return h >>> 0;
}
function random(seed) {
  return () => {
    seed += 0x6d2b79f5;
    let v = Math.imul(seed ^ (seed >>> 15), seed | 1);
    v ^= v + Math.imul(v ^ (v >>> 7), v | 61);
    return ((v ^ (v >>> 14)) >>> 0) / 4294967296;
  };
}
function rgba(hex, alpha) {
  const n = parseInt(hex.slice(1), 16);
  return `rgba(${n >> 16},${(n >> 8) & 255},${n & 255},${alpha})`;
}
export function sporeSpec(id) {
  const seed = hash("spore-deposit-v1|" + id),
    R = random(seed);
  const pigment = PIGMENTS[seed % PIGMENTS.length];
  return {
    seed,
    type: (seed >>> 15) % 7 === 0 ? "pores" : "gills",
    colour: pigment[0],
    ink: pigment[1],
    paper: pigment[2],
    gills: 64 + Math.floor(R() * 43),
  };
}
function deposit(spec, pixels) {
  // Supersample small cards, then keep only the final DPR-sized raster in cache.
  const work = document.createElement("canvas");
  work.width = work.height = Math.max(160, pixels);
  const x = work.getContext("2d");
  x.scale(work.width / 100, work.height / 100);
  const R = random(spec.seed),
    phase = R() * TAU,
    tilt = R() * TAU;
  const ex = 0.87 + R() * 0.12,
    ey = 0.87 + R() * 0.12;
  const cx = 48 + R() * 4,
    cy = 48 + R() * 4;
  const edge = (a) =>
    43 * (1 + 0.035 * Math.sin(a * 3 + phase) + 0.025 * Math.sin(a * 7 + tilt));
  const strength = (a) =>
    Math.max(
      0.15,
      0.68 + 0.23 * Math.sin(a * 2 + phase) + 0.16 * Math.cos(a * 5 + tilt),
    );
  const point = (a, r) => [
    cx + Math.cos(a) * r * ex,
    cy + Math.sin(a) * r * ey,
  ];
  const paper = x.createRadialGradient(50, 50, 44, 50, 50, 49);
  paper.addColorStop(0, spec.paper);
  paper.addColorStop(0.7, spec.paper);
  paper.addColorStop(1, rgba(spec.paper, 0));
  x.fillStyle = paper;
  x.fillRect(0, 0, 100, 100);
  // Paper texture remains much quieter than the deposited powder.
  for (let i = 0; i < 1000; i++) {
    const a = R() * TAU,
      r = Math.sqrt(R()) * 47;
    x.fillStyle = `rgba(30,25,20,${R() * 0.035})`;
    x.fillRect(50 + Math.cos(a) * r, 50 + Math.sin(a) * r, 0.25, 0.25);
  }
  const ink = x.createRadialGradient(cx, cy, 0, cx, cy, 46);
  for (const [at, alpha] of [
    [0, 0],
    [0.13, 0],
    [0.22, 0.95],
    [0.55, 0.95],
    [0.8, 0.65],
    [1, 0],
  ])
    ink.addColorStop(at, rgba(spec.ink, alpha));
  x.strokeStyle = ink;
  x.lineCap = "round";
  if (spec.type === "gills") {
    // Long gills have uneven angular spacing. Short lamellulae join between them.
    const angles = Array.from(
      { length: spec.gills },
      (_, i) => ((i + 0.18 + R() * 0.64) / spec.gills) * TAU,
    );
    for (let i = 0; i < angles.length; i++) {
      const a = angles[i],
        next = i + 1 < angles.length ? angles[i + 1] : angles[0] + TAU;
      const rays = [
        [a, 5 + R() * 4],
        [a + (next - a) * (0.27 + R() * 0.12), 17 + R() * 10],
        [a + (next - a) * (0.64 + R() * 0.12), 26 + R() * 7],
      ];
      for (const [angle, start] of rays) {
        const end = edge(angle) * (0.91 + R() * 0.09),
          bend = (R() - 0.5) * 0.035;
        const p0 = point(angle, start),
          p1 = point(angle + bend, start + (end - start) * 0.55),
          p2 = point(angle, end);
        x.beginPath();
        x.moveTo(...p0);
        x.quadraticCurveTo(...p1, ...p2);
        x.globalAlpha = strength(angle) * 0.18;
        x.lineWidth = 0.65 + R() * 0.55;
        x.stroke();
        x.globalAlpha = strength(angle) * (0.55 + R() * 0.4);
        x.lineWidth = 0.15 + R() * 0.23;
        x.stroke();
        // Loose grains on and beside each gill, rather than uniform circular rings.
        for (let j = 0; j < 23; j++) {
          const t = R(),
            rr = start + (end - start) * t;
          const aa = angle + bend * 2 * t * (1 - t) + (R() - 0.5) * 0.022;
          const [px, py] = point(aa, rr),
            grain = 0.12 + R() * 0.3;
          x.globalAlpha =
            strength(angle) * (1 - Math.pow(rr / 46, 3)) * (0.18 + R() * 0.6);
          x.fillStyle = spec.ink;
          x.fillRect(px, py, grain, grain * (0.6 + R()));
        }
      }
    }
  } else {
    // Bolete-type prints: fine irregular pores, with grain breaking up the grid.
    for (let row = -28; row <= 28; row++)
      for (let col = -28; col <= 28; col++) {
        const dx = (col + (row % 2) * 0.5) * 1.55 + (R() - 0.5) * 0.5,
          dy = row * 1.35 + (R() - 0.5) * 0.5;
        const rr = Math.hypot(dx, dy),
          a = Math.atan2(dy, dx);
        if (rr < 8 || rr > edge(a) || R() < 0.06) continue;
        x.globalAlpha =
          strength(a) *
          Math.pow(Math.max(0, 1 - rr / 47), 0.3) *
          (0.2 + R() * 0.5);
        x.strokeStyle = spec.ink;
        x.lineWidth = 0.15 + R() * 0.2;
        x.beginPath();
        x.ellipse(
          cx + dx * ex,
          cy + dy * ey,
          0.34 + R() * 0.22,
          0.35 + R() * 0.22,
          0,
          0,
          TAU,
        );
        x.stroke();
        x.fillStyle = spec.ink;
        x.fillRect(cx + dx * ex + 0.35, cy + dy * ey, 0.22, 0.22);
      }
  }
  // Occasional displaced powder, with soft edges rather than uniform spots.
  for (let i = 0; i < 5; i++) {
    const [px, py] = point(R() * TAU, 19 + R() * 20),
      radius = 1.7 + R() * 3.7;
    const smudge = x.createRadialGradient(px, py, 0, px, py, radius);
    smudge.addColorStop(0, rgba(spec.ink, 0.12 + R() * 0.1));
    smudge.addColorStop(1, rgba(spec.ink, 0));
    x.globalAlpha = 1;
    x.fillStyle = smudge;
    x.fillRect(px - radius, py - radius, radius * 2, radius * 2);
  }
  // An off-centre pale stem impression; not a perfect geometric cut-out.
  x.globalAlpha = 1;
  x.fillStyle =
    spec.colour === "white" || spec.colour === "cream" ? "#d6d2bd" : spec.paper;
  x.beginPath();
  for (let i = 0; i <= 48; i++) {
    const a = (i / 48) * TAU,
      rr =
        6.5 *
        (1 + 0.12 * Math.sin(a * 3 + phase) + 0.07 * Math.sin(a * 5 + tilt));
    const px = cx + Math.cos(a) * rr * 0.83,
      py = cy + Math.sin(a) * rr * 1.12;
    if (!i) x.moveTo(px, py);
    else x.lineTo(px, py);
  }
  x.closePath();
  x.fill();
  const result = document.createElement("canvas");
  result.width = result.height = pixels;
  const out = result.getContext("2d");
  out.imageSmoothingQuality = "high";
  out.drawImage(work, 0, 0, pixels, pixels);
  work.width = work.height = 0;
  return result;
}
export function drawSpore(
  canvas,
  id,
  {
    size = canvas.getBoundingClientRect().width || 40,
    dpr = devicePixelRatio,
  } = {},
) {
  const pixels = Math.max(
    16,
    Math.round(Math.min(128, size) * Math.min(3, Math.max(1, dpr))),
  );
  const key = String(id) + "|" + pixels;
  let bitmap = cache.get(key);
  if (bitmap) {
    hits++;
    cache.delete(key);
    cache.set(key, bitmap);
  } else {
    bitmap = deposit(sporeSpec(id), pixels);
    cache.set(key, bitmap);
    bytes += pixels * pixels * 4;
    renders++;
    while (bytes > MAX_BYTES || cache.size > MAX_ENTRIES) {
      const [old, c] = cache.entries().next().value;
      cache.delete(old);
      bytes -= c.width * c.height * 4;
      c.width = c.height = 0;
    }
  }
  canvas.width = canvas.height = pixels;
  canvas.getContext("2d").drawImage(bitmap, 0, 0);
}
export function sporeCacheStats() {
  return {
    entries: cache.size,
    bytes,
    renders,
    hits,
    maxBytes: MAX_BYTES,
    maxEntries: MAX_ENTRIES,
  };
}
