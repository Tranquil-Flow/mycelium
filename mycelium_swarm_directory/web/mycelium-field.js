// SPDX-License-Identifier: AGPL-3.0-only
/** Viewport-bounded, node-free understory; independent of the hero's visibility. */
import { rng, wander, measurePaths } from "./mycelium-geometry.js";
export function createField({ stage, state, reduce }) {
  const $ = (id) => document.getElementById(id);
  let understoryCleanup = () => {};
  function startUnderstory() {
    understoryCleanup();
    const c = $("page-mycelium"),
      x = c.getContext("2d"),
      tile = document.createElement("canvas"),
      bx = tile.getContext("2d"),
      css = getComputedStyle(stage),
      light = state.theme === "light",
      R = rng("r7-sparse-understory"),
      paths = [];
    let width = 0,
      height = 0,
      raf = 0,
      last = 0,
      clock = 0,
      lastPaint = -1,
      disposed = false,
      frozen = new URLSearchParams(location.search).has("t")
        ? Number(new URLSearchParams(location.search).get("t"))
        : null;
    const density = 1;
    function size() {
      const r = c.getBoundingClientRect();
      width = Math.round(r.width);
      height = Math.round(r.height);
      c.width = tile.width = Math.ceil(width * density);
      c.height = tile.height = Math.ceil(height * density);
      x.setTransform(density, 0, 0, density, 0, 0);
      bx.setTransform(density, 0, 0, density, 0, 0);
      lastPaint = -1;
      paths.length = 0;
      if (!width || !height) return;
      const count = Math.round((width * height) / 10000);
      for (let i = 0; i < count; i++) {
        const p = { x: R() * width, y: R() * height },
          points = wander(p, R() * 6.28, 110 + R() * 170, R);
        paths.push({
          points,
          phase: R() * 120,
          period: 80 + R() * 60,
          width: 0.42 + R() * 0.18,
          alpha: 0.48 + R() * 0.24,
        });
        if (i % 2 === 0) {
          const root = points[Math.floor(points.length * 0.5)];
          paths.push({
            points: wander(root, R() * 6.28, 38 + R() * 85, R),
            phase: R() * 120,
            period: 90 + R() * 60,
            width: 0.3,
            alpha: 0.48,
          });
        }
      }
      for (const p of paths) {
        p.path = new Path2D();
        p.points.forEach((q, i) =>
          i ? p.path.lineTo(q.x, q.y) : p.path.moveTo(q.x, q.y),
        );
        p.minY = Math.min(...p.points.map((p) => p.y));
        p.maxY = Math.max(...p.points.map((p) => p.y));
      }
      window.__understoryAudit = {
        paths: paths.length,
        nodes: 0,
        canvasPixels: c.width * c.height,
        bytes: c.width * c.height * 8,
        canvasCount: 2,
        density,
        straightness: measurePaths(
          paths.map((p) => ({ kind: "understory", points: p.points })),
        ),
      };
    }
    function paint(seconds, force = false) {
      const begin = performance.now(),
        t = reduce.matches ? 32 : seconds,
        bucket = t,
        y = window.scrollY;
      if (bucket === lastPaint && !force && y === paint.y) return;
      paint.y = y;
      x.clearRect(0, 0, width, height);
      if (!width || !height) return;
      // Fractional tips and alpha advance each animation frame; scroll still reuses a tile at unchanged time.
      if (bucket !== lastPaint) {
        lastPaint = bucket;
        bx.clearRect(0, 0, width, height);
        bx.strokeStyle = css.getPropertyValue("--glow").trim();
        bx.lineCap = "round";
        for (const p of paths) {
          const phase = ((t + p.phase) % p.period) / p.period,
            extent = reduce.matches
              ? 0.85
              : Math.min(1, 0.18 + phase * 3, (1 - phase) * 4 + 0.18),
            alpha = p.alpha * (0.64 + 0.36 * Math.sin(phase * Math.PI));
          bx.globalAlpha = alpha;
          bx.lineWidth = p.width;
          const n = Math.max(2, (p.points.length - 1) * extent);
          for (const wrap of [-height, 0, height]) {
            if (p.minY + wrap > height + 2 || p.maxY + wrap < -2) continue;
            if (n >= p.points.length - 1) {
              bx.save();
              bx.translate(0, wrap);
              bx.stroke(p.path);
              bx.restore();
              continue;
            }
            bx.beginPath();
            for (let j = 0; j <= Math.floor(n); j++) {
              const q = p.points[j];
              if (j) bx.lineTo(q.x, q.y + wrap);
              else bx.moveTo(q.x, q.y + wrap);
            }
            const a = p.points[Math.floor(n)],
              b = p.points[Math.min(p.points.length - 1, Math.ceil(n))],
              f = n % 1;
            bx.lineTo(a.x + (b.x - a.x) * f, a.y + (b.y - a.y) * f + wrap);
            bx.stroke();
          }
        }
      }
      const box = stage.getBoundingClientRect(),
        hero = document.querySelector(".hero-layout")?.getBoundingClientRect();
      x.save();
      x.beginPath();
      x.rect(
        box.left,
        Math.max(0, box.top, hero ? hero.bottom - 130 : 0),
        box.width,
        Math.max(
          0,
          Math.min(height, box.bottom) -
            Math.max(0, box.top, hero ? hero.bottom - 130 : 0),
        ),
      );
      x.clip();
      const offset = reduce.matches ? 0 : -((y * 0.22) % height);
      x.drawImage(tile, 0, offset, width, height);
      x.drawImage(tile, 0, offset + height, width, height);
      x.restore();
      Object.assign(c.dataset, {
        time: t.toFixed(2),
        paths: paths.length,
        nodes: 0,
        canvasPixels: c.width * c.height,
      });
      const m = window.__understoryMetrics;
      m.frames++;
      m.costs.push(performance.now() - begin);
      if (m.costs.length > 1200) m.costs.shift();
    }
    function tick(now) {
      raf = 0;
      if (disposed) return;
      if (last) clock += (now - last) / 1000;
      last = now;
      paint(clock);
      schedule();
    }
    function schedule() {
      const moving =
        !disposed && !reduce.matches && !document.hidden && frozen === null;
      window.__understoryMetrics.paused = !moving;
      if (moving && !raf) raf = requestAnimationFrame(tick);
      else if (!moving) {
        cancelAnimationFrame(raf);
        raf = 0;
        last = 0;
      }
    }
    const changed = () => {
        paint(frozen ?? clock, true);
        schedule();
      },
      scrolled = () => {
        paint(frozen ?? clock, true);
      },
      resized = () => {
        size();
        changed();
      };
    window.__understoryMetrics = { costs: [], frames: 0, paused: false };
    window.__setUnderstoryTime = (t) => {
      frozen = t;
      schedule();
      paint(t, true);
    };
    window.__paintUnderstory = paint;
    size();
    paint(frozen ?? 0, true);
    schedule();
    window.addEventListener("scroll", scrolled, { passive: true });
    window.addEventListener("resize", resized);
    document.addEventListener("visibilitychange", changed);
    reduce.addEventListener("change", changed);
    let ro = null;
    if (typeof ResizeObserver !== "undefined") {
      ro = new ResizeObserver(() => {
        const r = c.getBoundingClientRect();
        if (Math.round(r.width) !== width || Math.round(r.height) !== height)
          resized();
      });
      ro.observe(c);
    }
    understoryCleanup = () => {
      disposed = true;
      ro?.disconnect();
      cancelAnimationFrame(raf);
      window.removeEventListener("scroll", scrolled);
      window.removeEventListener("resize", resized);
      document.removeEventListener("visibilitychange", changed);
      reduce.removeEventListener("change", changed);
    };
  }
  startUnderstory();
  return () => understoryCleanup();
}
