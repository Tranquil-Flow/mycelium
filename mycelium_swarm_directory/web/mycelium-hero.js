// SPDX-License-Identifier: AGPL-3.0-only
/** R9 hero: bounded segment graph, multi-hop arrival bands, full-DPR light.
 * Geometry is built once per size/palette. Only fractional tips and active bands
 * animate. The returned disposer owns all RAF, observer and raster resources.
 */
import { rng, wander, bridgeRoot, measurePaths } from "./mycelium-geometry.js";
export function createHero(canvas, { stage, state, reduce }) {
  let pulseCleanup = () => {};
  function drawWeb(c) {
    const rect = c.getBoundingClientRect(),
      key = [
        rect.width,
        rect.height,
        devicePixelRatio,
        state.glow,
        state.theme,
      ].join("|");
    if (c.__paintKey === key || !rect.width || !rect.height) return;
    c.__paintKey = key;
    c.parentElement
      .querySelectorAll(".old-fibres,.living-tips")
      .forEach((e) => e.remove());
    const begun = performance.now(),
      w = rect.width,
      h = rect.height,
      d = Math.min(devicePixelRatio || 1, 2);
    c.width = Math.round(w * d);
    c.height = Math.round(h * d);
    const R = rng("mycelium-r6-cotton-23"),
      css = getComputedStyle(stage),
      light = state.theme === "light",
      core = css.getPropertyValue("--core").trim(),
      glow = css.getPropertyValue("--glow").trim();
    const mobile = w < 600,
      vs = [],
      ss = [],
      cords = [],
      tips = [],
      paths = [],
      CAP = 350000;
    let branchSerial = 0;
    function vertex(x, y, source) {
      const v = { x, y, source, id: vs.length, edges: [] };
      vs.push(v);
      return v;
    }
    const nodes = (
      mobile
        ? [
            [0.18, 0.68],
            [0.72, 0.73],
            [0.4, 0.92],
            [0.97, 0.95],
            [0.92, 0.48],
          ]
        : [
            [0.57, 0.25],
            [0.76, 0.43],
            [0.93, 0.2],
            [0.92, 0.8],
            [0.61, 0.83],
            [0.31, 0.91],
            [0.05, 0.37],
          ]
    ).map(([a, b], id) => vertex(a * w, b * h, id));
    function edge(
      a,
      b,
      width,
      alpha,
      tone = 1,
      cord = -1,
      birth = 0,
      old = false,
    ) {
      if (ss.length >= CAP) return null;
      const s = {
        id: ss.length,
        a,
        b,
        len: Math.hypot(b.x - a.x, b.y - a.y),
        width,
        alpha,
        tone,
        cord,
        birth,
        old,
        source: a.source,
      };
      ss.push(s);
      a.edges.push(s.id);
      b.edges.push(s.id);
      return s;
    }
    function grow(
      p,
      angle,
      len,
      width,
      alpha,
      depth = 0,
      tone = 1,
      old = false,
      birth = 0,
      branch = null,
    ) {
      branch = branch ?? branchSerial++;
      const points = wander(p, angle, len, R),
        poly = [p],
        fork1 = 0.2 + R() * 0.22,
        fork2 = 0.55 + R() * 0.25;
      let a = p;
      for (let j = 1; j < points.length; j++) {
        const point = points[j],
          b = vertex(point.x, point.y, p.source),
          seg = edge(
            a,
            b,
            Math.max(0.15, width * (1 - (j / points.length) * 0.62)),
            alpha * (1 - (j / points.length) * 0.62),
            tone,
            -1,
            birth ? birth + (j - 1) * 0.65 : 0,
            old,
          );
        if (seg) seg.branch = branch;
        poly.push(b);
        if (
          depth > 0 &&
          (j === Math.floor(points.length * fork1) ||
            j === Math.floor(points.length * fork2))
        )
          grow(
            b,
            Math.atan2(b.y - a.y, b.x - a.x) +
              (R() > 0.5 ? 1 : -1) * (0.3 + R() * 0.6),
            len * (0.28 + R() * 0.3),
            width * (0.48 + R() * 0.2),
            alpha * 0.75,
            depth - 1,
            tone,
            old,
            birth ? birth + j * 0.65 + 1.2 : 0,
            branch,
          );
        a = b;
      }
      paths.push({
        kind: birth ? "growth" : len < 18 ? "cotton" : "hypha",
        points: poly,
        birth,
        branch,
        width,
        alpha,
        tone,
      });
      const q = poly.at(-2) || p;
      tips.push({ v: a, angle: Math.atan2(a.y - q.y, a.x - q.x) });
      return a;
    }
    // Rhizomorphs first: loose parallel fibres, varying bundle breadth, tapered ends.
    const links = [
      [0, 1],
      [1, 2],
      [1, 3],
      [3, 4],
      [4, 5],
      [5, 6],
    ].filter(([a, b]) => nodes[a] && nodes[b]);
    for (const [ia, ib] of links) {
      const a = nodes[ia],
        b = nodes[ib],
        len = Math.hypot(b.x - a.x, b.y - a.y),
        nx = -(b.y - a.y) / len,
        ny = (b.x - a.x) / len,
        id = cords.length,
        root = bridgeRoot(a, b, R),
        N = root.length - 1,
        poly = [a];
      let dist = 0;
      for (let j = 1; j <= N; j++) {
        const p = root[j],
          q = j === N ? b : vertex(p.x, p.y, ia);
        dist += Math.hypot(q.x - poly.at(-1).x, q.y - poly.at(-1).y);
        q.routeDist = dist;
        poly.push(q);
      }
      const cord = { id, a, b, poly, length: dist };
      cords.push(cord);
      paths.push({ kind: "cord-centre", points: poly });
      // Bundled strands separate, cross and rejoin at irregularly placed tie points.
      for (let f = -5; f <= 5; f++) {
        let prev = a,
          braid = 0,
          target = 0,
          run = 0;
        const strand = [a];
        for (let j = 1; j < poly.length; j++) {
          if (run-- <= 0) {
            run = 3 + Math.floor(R() * 8);
            target = (R() - 0.5) * 0.48;
          }
          braid += (target - braid) * 0.28;
          const t = j / N,
            spread = 0.28 + 0.72 * Math.pow(4 * t * (1 - t), 0.5),
            p = poly[j],
            off = f * 0.34 * spread + braid;
          let q =
            f === 0
              ? p
              : j === N
                ? b
                : vertex(p.x + nx * off, p.y + ny * off, ia);
          edge(
            prev,
            q,
            f === 0 ? 0.85 : 0.22,
            f === 0 ? 0.38 : 0.3,
            f % 3 === 0 ? 2 : 1,
            id,
          );
          if (f !== 0 && j % 6 === 0) edge(q, p, 0.16, 0.15, 1, id);
          prev = q;
          strand.push(q);
        }
        paths.push({ kind: "cord-strand", points: strand });
      }
      for (let j = 5; j < N - 4; j += 5) {
        const p = poly[j],
          q = poly[j + 1],
          ang = Math.atan2(q.y - p.y, q.x - p.x),
          side = R() > 0.5 ? 1 : -1;
        grow(
          p,
          ang + side * (0.35 + R() * 0.8),
          28 + R() * 88,
          0.45 + R() * 0.55,
          0.58,
          1 + Math.floor(R() * 3),
        );
        for (let f = 0; f < 25; f++)
          grow(
            p,
            ang + (R() > 0.5 ? 1 : -1) * (0.4 + R() * 1.6),
            3 + R() * 13,
            0.15 + R() * 0.07,
            0.23 + R() * 0.18,
          );
      }
    }
    // Colonies have several interleaved origins on the cord, not central spokes.
    const oldRoots = [];
    nodes.forEach((n, i) => {
      const near = [];
      for (const cord of cords)
        if (cord.a === n || cord.b === n) {
          const pp = cord.a === n ? cord.poly : [...cord.poly].reverse();
          near.push(...pp.slice(0, 24));
        }
      if (!near.length) near.push(n);
      const roots = near.slice(),
        flow = R() * 6.28,
        radius = (mobile ? 95 : 143) * (0.9 + R() * 0.2);
      for (let k = 0; k < 65; k++) {
        const p = near[Math.floor(R() * near.length)],
          a = flow + (R() - 0.5) * (i % 2 ? 3.8 : 5.4);
        roots.push(grow(p, a, 12 + R() * 59, 0.3, 0.48, 2));
      }
      for (let f = 0; f < 520; f++) {
        const p = roots[Math.floor(R() * roots.length)],
          a = R() < 0.38 ? flow + (R() - 0.5) * 3.5 : R() * 6.28,
          old = f % 13 === 0;
        grow(
          p,
          a,
          radius * (0.12 + R() * (i % 2 ? 0.77 : 0.49)),
          0.19 + R() * 0.26,
          0.44 + R() * 0.32,
          f % 7 === 0 ? 1 : 0,
          f % 4 === 0 ? 2 : 1,
          old,
        );
        if (old) oldRoots.push(p);
      }
    });
    // Fine hair attaches to real interior vertices: high local cotton density only.
    const stems = vs.filter((v) => v.edges.length > 1);
    for (let j = 0; j < 44000; j++) {
      const p = stems[Math.floor(R() * stems.length)];
      grow(
        p,
        R() * 6.28,
        2 + R() * 9,
        0.15 + R() * 0.06,
        0.3 + R() * 0.24,
        0,
        j % 5 === 0 ? 2 : 1,
      );
    }
    // Thin cross mesh fuses actual endpoints inside overlapping colonies.
    const grid = new Map(),
      cell = 9;
    for (const v of vs) {
      const k = `${Math.floor(v.x / cell)},${Math.floor(v.y / cell)}`;
      if (!grid.has(k)) grid.set(k, []);
      grid.get(k).push(v);
    }
    let fusions = 0;
    const seedTips = tips.slice();
    for (let j = 0; j < 2300; j++) {
      const seed = seedTips[Math.floor(R() * seedTips.length)],
        p = seed.v,
        points = wander(p, R() * 6.28, 42, R),
        poly = [p];
      let a = p;
      for (let k = 1; k < points.length; k++) {
        const { x: xx, y: yy } = points[k],
          list =
            grid.get(`${Math.floor(xx / cell)},${Math.floor(yy / cell)}`) || [];
        let q =
          k > 4
            ? list.find(
                (v) =>
                  v !== a && v !== p && Math.hypot(v.x - xx, v.y - yy) < 1.1,
              )
            : null;
        if (q) {
          const test = [...poly, q],
            audit = measurePaths([{ kind: "m", points: test }]).m;
          if (!audit.windows.above097) {
            edge(a, q, 0.16, 0.17, 0);
            poly.push(q);
            fusions++;
            break;
          }
          q = null;
        }
        q = vertex(xx, yy, p.source);
        edge(a, q, 0.16, 0.18, 0);
        poly.push(q);
        a = q;
      }
      paths.push({ kind: "mesh", points: poly });
    }
    // Recycled living fronts: extension, hold, retraction, rest. Fixed geometry/memory.
    // Per-branch phase/rate variation changes each cycle; the branch bank stays bounded.
    const fronts = tips.filter(
      (t) =>
        Math.hypot(t.v.x - nodes[t.v.source].x, t.v.y - nodes[t.v.source].y) >
        (mobile ? 70 : 105),
    );
    for (let j = 0; j < 60 && fronts.length; j++) {
      const t = fronts[Math.floor(R() * fronts.length)];
      for (let k = 0; k < 4; k++)
        grow(
          t.v,
          t.angle + (k - 1.5) * 0.18,
          33 + R() * 35,
          0.38,
          0.83,
          1,
          1,
          false,
          3 + R() * 42,
        );
    }
    // R9: sparse explorer roots attached to real highway vertices. No extra nodes.
    // Long declining-density strands and fine forks cross selected dark interstices.
    const outerR = rng("r9-sparse-explorers-8");
    for (const cord of cords) {
      for (let k = 0; k < 8; k++) {
        const at = 3 + Math.floor(outerR() * Math.max(1, cord.poly.length - 7)),
          p = cord.poly[at],
          next = cord.poly[Math.min(at + 1, cord.poly.length - 1)],
          side = k % 2 ? 1 : -1;
        const angle =
            Math.atan2(next.y - p.y, next.x - p.x) +
            side * (0.72 + outerR() * 1.05),
          length = (mobile ? 180 : 380) + outerR() * (mobile ? 160 : 280);
        const first = paths.length,
          firstSeg = ss.length,
          tip = grow(p, angle, length, 0.34, 0.28, 1, 1),
          prev = tips.at(-1),
          heading = prev?.angle ?? angle;
        for (let j = first; j < paths.length; j++) paths[j].kind = "explorer";
        for (let j = firstSeg; j < ss.length; j++) ss[j].explorer = true;
        // Each outer root also carries a longer live front, not a periodic whole-mat refresh.
        grow(
          tip,
          heading + (outerR() - 0.5) * 0.65,
          85 + outerR() * 90,
          0.34,
          0.62,
          1,
          1,
          false,
          3 + outerR() * 42,
        );
      }
    }
    // Owner delta: a separate sparse cohort preserves the R9 geometry prefix.
    // Fine forks cross gaps without adding colonies or a uniform luminous mat.
    const gapR = rng("site-r9-dark-gap-explorers");
    for (const cord of cords)
      for (let k = 0; k < 7; k++) {
        const at = 3 + Math.floor(gapR() * Math.max(1, cord.poly.length - 7));
        const p = cord.poly[at],
          next = cord.poly[Math.min(at + 1, cord.poly.length - 1)];
        const angle =
          Math.atan2(next.y - p.y, next.x - p.x) +
          (k % 2 ? 1 : -1) * (0.85 + gapR() * 0.9);
        const first = paths.length,
          firstSeg = ss.length;
        const tip = grow(
          p,
          angle,
          (mobile ? 190 : 400) + gapR() * (mobile ? 150 : 250),
          0.3,
          0.25,
          1,
          1,
        );
        for (let j = first; j < paths.length; j++)
          paths[j].kind = "gap-explorer";
        for (let j = firstSeg; j < ss.length; j++) ss[j].explorer = true;
        grow(
          tip,
          tips.at(-1).angle,
          70 + gapR() * 95,
          0.28,
          0.52,
          1,
          1,
          false,
          3 + gapR() * 42,
        );
      }
    // Static geometry is rasterised ONCE. No periodic whole-mat redraw or geometry rebuild.
    const ctx = c.getContext("2d"),
      oldLayer = document.createElement("canvas"),
      growth = document.createElement("canvas");
    for (const [layer, cls] of [
      [oldLayer, "old-fibres"],
      [growth, "living-tips"],
    ]) {
      layer.className = "art-canvas " + cls;
      layer.setAttribute("aria-hidden", "true");
      layer.width = c.width;
      layer.height = c.height;
      c.after(layer);
      layer.getContext("2d").scale(d, d);
    }
    ctx.scale(d, d);
    ctx.lineCap = "round";
    ctx.globalCompositeOperation = light ? "source-over" : "lighter";
    function stroke(ctx, s, fraction = 1) {
      ctx.strokeStyle = s.tone === 2 ? core : glow;
      ctx.globalAlpha = s.alpha * (light ? 0.68 : 1);
      ctx.lineWidth = s.width;
      ctx.beginPath();
      ctx.moveTo(s.a.x, s.a.y);
      ctx.lineTo(
        s.a.x + (s.b.x - s.a.x) * fraction,
        s.a.y + (s.b.y - s.a.y) * fraction,
      );
      ctx.stroke();
    }
    const oldx = oldLayer.getContext("2d");
    oldx.lineCap = "round";
    oldx.globalCompositeOperation = light ? "source-over" : "lighter";
    const growing = [];
    for (const seg of ss) {
      if (seg.birth) growing.push(seg);
      else stroke(seg.old ? oldx : ctx, seg);
    }
    const live = paths
      .filter((p) => p.kind === "growth")
      .map((p) => {
        let length = 0;
        const lengths = [0],
          path = new Path2D();
        path.moveTo(p.points[0].x, p.points[0].y);
        for (let i = 1; i < p.points.length; i++) {
          length += Math.hypot(
            p.points[i].x - p.points[i - 1].x,
            p.points[i].y - p.points[i - 1].y,
          );
          lengths.push(length);
          path.lineTo(p.points[i].x, p.points[i].y);
        }
        return { ...p, path, length, lengths };
      });
    const branchStart = new Map();
    for (const p of live)
      branchStart.set(
        p.branch,
        Math.min(branchStart.get(p.branch) ?? Infinity, p.birth),
      );
    function smooth(v) {
      v = Math.max(0, Math.min(1, v));
      return v * v * (3 - 2 * v);
    }
    const branchClocks = new Map();
    function life(branch, birth, seconds) {
      let cfg = branchClocks.get(branch);
      if (!cfg) {
        const mix = (n) => {
          n = Math.imul(n ^ (n >>> 16), 0x45d9f3b);
          n = Math.imul(n ^ (n >>> 16), 0x45d9f3b);
          return ((n ^ (n >>> 16)) >>> 0) / 4294967296;
        };
        cfg = { period: 75 + mix(branch) * 35, phaseSeed: mix(branch + 7919) };
        branchClocks.set(branch, cfg);
      }
      const { period, phaseSeed } = cfg,
        phase =
          (((seconds * 4.212 + phaseSeed * period) % period) + period) % period;
      // All roots are uniformly phase-distributed; 1.2 x R8's 3.51 clock.
      const local = phase - 12 - (birth - (branchStart.get(branch) ?? birth)),
        out = smooth((period - 14 - phase) / 25),
        q = Math.max(0, Math.min(local, period - 12 - phase)),
        eased = q < 2 ? (q * q) / 4 : q - 1,
        extent = (eased / 0.65) * 3.5;
      return { extent, alpha: smooth(local / 2.5) * out, phase, local };
    }
    function growthFraction(seg, seconds) {
      const v = life(
        seg.branch,
        branchStart.get(seg.branch) ?? seg.birth,
        seconds,
      );
      return (
        Math.min(
          1,
          Math.max(
            0,
            (v.local -
              (seg.birth - (branchStart.get(seg.branch) ?? seg.birth))) /
              0.65,
          ),
        ) * v.alpha
      );
    }
    // Full-DPR irregular colony silhouettes for arrival bloom, never a blurred low-res stamp.
    const blooms = nodes.map((n) => {
      const im = document.createElement("canvas");
      im.width = Math.ceil(340 * d);
      im.height = Math.ceil(340 * d);
      const bx = im.getContext("2d");
      bx.scale(d, d);
      bx.translate(170 - n.x, 170 - n.y);
      bx.globalCompositeOperation = light ? "source-over" : "lighter";
      for (const seg of ss)
        if (
          !seg.birth &&
          seg.cord < 0 &&
          seg.source === n.source &&
          Math.hypot(seg.a.x - n.x, seg.a.y - n.y) < 158
        )
          stroke(bx, seg);
      return { image: im, x: n.x - 170, y: n.y - 170, node: n };
    });
    const composeCosts = [];
    let growthCount = 0;
    function updateMat(seconds, force = false) {
      const start = performance.now(),
        t = reduce.matches ? 48 : Math.max(0, seconds),
        gx = growth.getContext("2d");
      gx.clearRect(0, 0, w, h);
      gx.lineCap = "round";
      gx.lineJoin = "round";
      gx.globalCompositeOperation = light ? "source-over" : "lighter";
      growthCount = 0;
      let active = 0,
        maxStep = 0;
      for (const p of live) {
        const v = life(p.branch, p.birth, t);
        if (v.alpha <= 0) continue;
        const extent = Math.min(p.length, v.extent),
          u = Math.min(1, extent / p.length);
        if (extent <= 0) continue;
        gx.globalAlpha = v.alpha * p.alpha * (light ? 0.68 : 1);
        gx.lineWidth = Math.max(0.15, p.width * 0.66);
        gx.strokeStyle = p.tone === 2 ? core : glow;
        if (u === 1) gx.stroke(p.path);
        else {
          active++;
          gx.beginPath();
          gx.moveTo(p.points[0].x, p.points[0].y);
          for (let j = 1; j < p.points.length; j++) {
            const a = p.points[j - 1],
              b = p.points[j],
              f = Math.min(
                1,
                (extent - p.lengths[j - 1]) / (p.lengths[j] - p.lengths[j - 1]),
              );
            if (f <= 0) break;
            gx.lineTo(a.x + (b.x - a.x) * f, a.y + (b.y - a.y) * f);
            if (f < 1) break;
          }
          gx.stroke();
        }
        growthCount += Math.ceil((p.points.length - 1) * u);
        maxStep = Math.max(maxStep, ((3.5 / 0.65) * 4.212) / 60);
      }
      // Breathing and old-fibre fade are compositor opacity, not costly bitmap recopy.
      c.style.opacity = reduce.matches
        ? ".88"
        : String(0.85 + 0.13 * Math.sin((t * 2 * Math.PI) / 14.4 - 0.8));
      const oldAlpha = reduce.matches
        ? 0.58
        : 0.54 + 0.42 * Math.cos((t * 1.56 * 2 * Math.PI) / 103);
      oldLayer.style.opacity = String(oldAlpha);
      Object.assign(c.dataset, {
        growthVisible: growthCount,
        growthTime: t.toFixed(3),
        oldAlpha: oldAlpha.toFixed(4),
        growthCycle: Math.floor((t * 4.212) / 173),
        activeTips: active,
        maxTipStep: maxStep,
      });
      composeCosts.push(performance.now() - start);
      if (composeCosts.length > 4000) composeCosts.shift();
    }
    const graph = {
      vs,
      ss,
      nodes,
      cords,
      growing,
      updateMat,
      composeCosts,
      core,
      glow,
      light,
      blooms,
      growthFraction,
    };
    window.__liveProbe = (t) =>
      live.map((p) => {
        const v = life(p.branch, p.birth, t);
        return {
          extent: Math.min(p.length, v.extent),
          length: p.length,
          alpha: v.alpha,
          branch: p.branch,
          x: p.points[0].x,
          y: p.points[0].y,
        };
      });
    window.__liveGeometry = live.map((p) => ({
      branch: p.branch,
      length: p.length,
      birth: p.birth,
    }));
    window.__rootPaths = paths
      .filter((p) => p.points.length >= 9)
      .map((p) => ({ kind: p.kind, points: p.points.map((v) => [v.x, v.y]) }));
    window.__darkGapProbe = () => {
      const eligible = ss
        .filter((s) => !s.birth && s.cord < 0)
        .filter((s) => {
          const x = (s.a.x + s.b.x) / 2,
            y = (s.a.y + s.b.y) / 2;
          return (
            x > w * 0.5 &&
            x < w &&
            y > 20 &&
            y < h - 20 &&
            nodes.every((n) => Math.hypot(x - n.x, y - n.y) > 90) &&
            cords.every((c) =>
              c.poly.every((v) => Math.hypot(x - v.x, y - v.y) > 32),
            )
          );
        });
      return {
        segments: eligible.length,
        length: eligible.reduce((n, s) => n + s.len, 0),
        width: w,
        height: h,
        nodeClearance: 90,
        cordVertexClearance: 32,
      };
    };
    const routes = buildSignalRoutes(graph);
    Object.assign(c.dataset, {
      segments: ss.length,
      fusions,
      nodes: nodes.length,
      cords: cords.length,
      minWidth: 0.15,
      maxWidth: 3.8,
      palette: state.glow,
      renderMs: (performance.now() - begun).toFixed(1),
      sporeNodes: 0,
      graphVertices: vs.length,
      segmentCap: CAP,
      growthSegments: growing.length,
      routes: routes.length,
    });
    window.__growthGeometry = {
      nodes: nodes.map((n) => ({ x: n.x, y: n.y, id: n.source })),
      cords: cords.map((c) => ({
        from: c.a.source,
        to: c.b.source,
        length: c.length,
      })),
      segments: ss.length,
    };
    let straightnessCache = null;
    Object.defineProperty(window, "__straightness", {
      configurable: true,
      get: () => straightnessCache ?? (straightnessCache = measurePaths(paths)),
    });
    window.__graphAudit = {
      vertices: vs.length,
      segments: ss.length,
      isolated: vs.filter((v) => !v.edges.length).length,
      routes: routes.map((r) => ({
        from: r.from,
        to: r.to,
        duration: r.duration,
        activated: r.count,
        highway: r.highway,
        fine: r.fine,
        buckets: r.buckets.size,
        maxFine: r.maxFine,
        arrivalMin: r.arrivalMin,
        arrivalMax: r.arrivalMax,
      })),
      growthSegments: growing.length,
      cap: CAP,
    };
    startPulses(c, graph, routes, w, h, css, light);
    window.__matCosts = composeCosts;
  }
  // Multi-source Dijkstra seeded in route order on a specific node-to-node cord.
  // Cost is transit time; a separate fine-distance accumulator controls attenuation.
  function buildSignalRoutes(g) {
    const { vs, ss, cords } = g,
      routes = [],
      FAST = 38,
      SLOW = 22,
      LAMBDA = 300,
      CUTOFF = 650,
      STEP = 0.3;
    const resident = new Map(),
      cache = {
        bytes: 0,
        peakBytes: 0,
        buckets: 0,
        limit: 32,
        misses: 0,
        evictions: 0,
      };
    window.__signalCacheStats = cache;
    class Heap {
      constructor() {
        this.a = [];
      }
      push(v) {
        let a = this.a,
          i = a.length;
        a.push(v);
        while (i) {
          let p = (i - 1) >> 1;
          if (a[p][0] <= v[0]) break;
          a[i] = a[p];
          i = p;
        }
        a[i] = v;
      }
      pop() {
        let a = this.a,
          top = a[0],
          last = a.pop();
        if (a.length) {
          let i = 0;
          while (i * 2 + 1 < a.length) {
            let k = i * 2 + 1;
            if (k + 1 < a.length && a[k + 1][0] < a[k][0]) k++;
            if (a[k][0] >= last[0]) break;
            a[i] = a[k];
            i = k;
          }
          a[i] = last;
        }
        return top;
      }
    }
    for (const reverse of [false, true]) {
      const cord = reverse ? cords.at(-1) : cords[0];
      const at = new Float32Array(vs.length).fill(Infinity),
        fine = new Float32Array(vs.length).fill(Infinity),
        heap = new Heap(),
        poly = reverse ? [...cord.poly].reverse() : cord.poly;
      let distance = 0;
      for (let j = 0; j < poly.length; j++) {
        if (j)
          distance += Math.hypot(
            poly[j].x - poly[j - 1].x,
            poly[j].y - poly[j - 1].y,
          );
        const v = poly[j];
        at[v.id] = distance / FAST;
        fine[v.id] = 0;
        heap.push([at[v.id], v.id]);
      }
      while (heap.a.length) {
        const [time, id] = heap.pop();
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
          heap.push([at[q.id], q.id]);
        }
      }
      const buckets = new Map(),
        list = [];
      let count = 0,
        highwayCount = 0,
        fineCount = 0,
        maxFine = 0;
      for (const s of ss) {
        const highway = s.cord >= 0,
          a = at[s.a.id] <= at[s.b.id] ? s.a : s.b,
          dd = fine[a.id] + (highway ? 0 : s.len / 2);
        if (!Number.isFinite(at[a.id]) || dd > CUTOFF) continue;
        const arrival = at[a.id] + s.len / (highway ? FAST : SLOW) / 2,
          amp = highway ? 1 : Math.exp(-dd / LAMBDA);
        if (amp < 0.05) continue;
        const index = Math.floor(arrival / STEP),
          tileX = Math.floor((s.a.x + s.b.x) / 2 / 64),
          tileY = Math.floor((s.a.y + s.b.y) / 2 / 64),
          level = highway ? 5 : Math.min(4, Math.floor(amp * 5)),
          type = highway ? "cord" : s.explorer ? "explorer" : "fine",
          key = `${tileX},${tileY}|${level}|${s.birth ? Math.ceil(s.birth) : 0}|${s.explorer ? "outer" : ""}`;
        let bucket = buckets.get(index);
        if (!bucket) {
          bucket = new Map();
          bucket.bounds = [Infinity, Infinity, -Infinity, -Infinity];
          buckets.set(index, bucket);
        }
        const bb = bucket.bounds;
        bb[0] = Math.min(bb[0], s.a.x, s.b.x);
        bb[1] = Math.min(bb[1], s.a.y, s.b.y);
        bb[2] = Math.max(bb[2], s.a.x, s.b.x);
        bb[3] = Math.max(bb[3], s.a.y, s.b.y);
        let item = bucket.get(key);
        if (!item) {
          item = {
            tile: `${tileX},${tileY}`,
            bounds: [Infinity, Infinity, -Infinity, -Infinity],
            path: new Path2D(),
            amp: highway ? 1 : (level + 0.5) / 5,
            type,
            birth: s.birth,
            end: null,
          };
          bucket.set(key, item);
        }
        item.bounds[0] = Math.min(item.bounds[0], s.a.x, s.b.x);
        item.bounds[1] = Math.min(item.bounds[1], s.a.y, s.b.y);
        item.bounds[2] = Math.max(item.bounds[2], s.a.x, s.b.x);
        item.bounds[3] = Math.max(item.bounds[3], s.a.y, s.b.y);
        if (item.end !== s.a) item.path.moveTo(s.a.x, s.a.y);
        item.path.lineTo(s.b.x, s.b.y);
        item.end = s.b;
        count++;
        if (highway) highwayCount++;
        else fineCount++;
        maxFine = Math.max(maxFine, dd);
        list.push({ segment: s.id, arrival, fine: dd, amp, highway });
      }
      // R8 smooth: same full-DPR strokes, bounded resident time-band cache.
      // Keep immutable paths, not both entire forward/reverse raster timelines.
      for (const [i, b] of buckets) {
        const d = Math.min(devicePixelRatio || 1, 2),
          groups = new Map();
        b.future = [];
        b.images = [];
        b.image = { width: 0, height: 0 };
        b.bytes = 0;
        for (const item of b.values()) {
          if (item.birth) continue;
          if (!groups.has(item.tile)) groups.set(item.tile, []);
          groups.get(item.tile).push(item);
        }
        b.ensureImages = () => {
          if (resident.has(b)) {
            resident.delete(b);
            resident.set(b, true);
            return;
          }
          while (resident.size >= 32) {
            const old = resident.keys().next().value;
            resident.delete(old);
            cache.bytes -= old.bytes;
            for (const t of old.images) {
              t.image.width = 0;
              t.image.height = 0;
            }
            old.images = [];
            old.bytes = 0;
            cache.evictions++;
          }
          for (const items of groups.values()) {
            const l =
                Math.floor(Math.min(...items.map((p) => p.bounds[0]))) - 2,
              t = Math.floor(Math.min(...items.map((p) => p.bounds[1]))) - 2,
              r = Math.ceil(Math.max(...items.map((p) => p.bounds[2]))) + 2,
              bt = Math.ceil(Math.max(...items.map((p) => p.bounds[3]))) + 2;
            const image = document.createElement("canvas");
            image.width = Math.ceil((r - l) * d);
            image.height = Math.ceil((bt - t) * d);
            const x = image.getContext("2d");
            x.scale(d, d);
            x.translate(-l, -t);
            x.lineCap = "round";
            x.lineJoin = "round";
            x.globalCompositeOperation = g.light ? "source-over" : "lighter";
            for (const item of items) {
              const main = item.type === "cord";
              x.globalAlpha = Math.min(
                1,
                item.amp *
                  (g.light ? 0.36 : 0.95) *
                  (item.type === "explorer" ? 1.2 : 1),
              );
              x.strokeStyle = main ? g.core : g.glow;
              x.lineWidth = main ? 1.15 : item.type === "explorer" ? 0.8 : 0.52;
              x.stroke(item.path);
            }
            b.images.push({ image, x: l, y: t });
            b.bytes += image.width * image.height * 4;
          }
          resident.set(b, true);
          cache.bytes += b.bytes;
          cache.peakBytes = Math.max(cache.peakBytes, cache.bytes);
          cache.buckets = resident.size;
          cache.misses++;
        };
      }
      list.sort((a, b) => a.arrival - b.arrival);
      const destination = reverse ? g.nodes[0] : g.nodes.at(-1),
        networkDuration = at[destination.id];
      routes.push({
        from: poly[0].source,
        to: destination.source,
        cord: cord.id,
        reverse,
        landings: g.nodes
          .map((n) => ({ node: n.source, time: at[n.id] }))
          .filter((n) => Number.isFinite(n.time)),
        duration: networkDuration,
        buckets,
        count,
        highway: highwayCount,
        fine: fineCount,
        maxFine,
        arrivalMin: list[0]?.arrival,
        arrivalMax: list.at(-1)?.arrival,
        list,
        step: STEP,
      });
    }
    return routes;
  }
  function startPulses(base, graph, routes, w, h, css, light) {
    pulseCleanup();
    let c = base.parentElement.querySelector(".pulse-canvas");
    if (!c) {
      c = document.createElement("canvas");
      c.className = "pulse-canvas";
      c.setAttribute("aria-hidden", "true");
      base.after(c);
    }
    const d = Math.min(devicePixelRatio || 1, 2); // Actual full-device-pixel pulse geometry.
    c.width = Math.round(w * d);
    c.height = Math.round(h * d);
    const x = c.getContext("2d");
    x.scale(d, d);
    let raf = 0,
      visible = true,
      disposed = false,
      clock = 0,
      last = 0,
      frozen = null,
      io = null;
    const params = new URLSearchParams(location.search);
    if (params.has("t") && Number.isFinite(+params.get("t")))
      frozen = +params.get("t");
    const solo = params.has("solo");
    const core = css.getPropertyValue("--core").trim(),
      glow = css.getPropertyValue("--glow").trim(),
      R = rng("r6-breath-trips");
    // A seeded sequence of received/forwarded messages. Each next trip starts at
    // the preceding destination, after arrival + a variable processing delay.
    const trips = [];
    let start = 0.25,
      from = 0,
      previous = -1;
    for (let i = 0; i < 90; i++) {
      let choices = routes.filter((r) => r.from === from && r.to !== previous);
      if (!choices.length) choices = routes.filter((r) => r.from === from);
      const route = choices[Math.floor(R() * choices.length)];
      if (!route) break;
      trips.push({ route, start });
      start += Math.max(14.4, route.arrivalMax + 5) + R() * 1.1;
      previous = from;
      from = route.to;
    }
    const period = start + 10;
    const tileBytes = routes.reduce(
      (n, r) =>
        n +
        [...r.buckets.values()].reduce(
          (a, b) =>
            a +
            b.images.reduce(
              (n, t) => n + t.image.width * t.image.height * 4,
              0,
            ),
          0,
        ),
      0,
    );
    window.__pulseMetrics = {
      costs: [],
      frames: 0,
      maxConcurrent: 0,
      maxRipples: 0,
      paused: false,
      reason: "",
      activeBuckets: 0,
    };
    window.__pulseTrips = trips.map((t) => ({
      from: t.route.from,
      to: t.route.to,
      start: t.start,
      arrival: t.start + t.route.duration,
      end: t.start + t.route.arrivalMax + 4.2,
    }));
    function draw(seconds) {
      const begin = performance.now();
      x.clearRect(0, 0, w, h);
      let active = 0,
        drawn = 0;
      if (!reduce.matches) {
        x.globalCompositeOperation = light ? "source-over" : "lighter";
        x.lineCap = "butt";
        const t = ((seconds % period) + period) % period;
        const candidates = (solo ? trips.slice(0, 1) : trips)
          .filter((p) => t >= p.start && t < p.start + p.route.arrivalMax + 4.2)
          .slice(-1);
        for (const trip of candidates) {
          active++;
          const age = t - trip.start,
            r = trip.route,
            lo = Math.max(0, Math.floor((age - 4.2) / r.step)),
            hi = Math.floor(age / r.step);
          for (let i = lo; i <= hi; i++) {
            const bucket = r.buckets.get(i);
            if (!bucket) continue;
            const dt = age - (i + 0.5) * r.step;
            if (dt < 0 || dt > 4.2) continue;
            const envelope =
              dt < 1.05
                ? Math.pow(Math.sin(((dt / 1.05) * Math.PI) / 2), 2)
                : Math.pow(Math.cos((((dt - 1.05) / 3.15) * Math.PI) / 2), 2);
            if (envelope < 0.015) continue;
            bucket.ensureImages();
            const distanceFade = Math.exp((-i * r.step * 38) / 1800);
            x.globalAlpha = envelope * distanceFade;
            for (const tile of bucket.images)
              x.drawImage(
                tile.image,
                tile.x,
                tile.y,
                tile.image.width / d,
                tile.image.height / d,
              );
            drawn++;
            // Live tips retain their own slow growth; only already-mature graph edges glow.
          }
          for (const landing of r.landings) {
            const landed = age - landing.time;
            if (landed >= 0 && landed < 6.2) {
              const bloom = graph.blooms[landing.node],
                u = landed / 6.2,
                gain = Math.pow(Math.sin(Math.PI * u), 2),
                scale = 1 + 0.018 * gain;
              x.save();
              x.translate(bloom.node.x, bloom.node.y);
              x.scale(scale, scale);
              x.globalAlpha = gain * (light ? 0.32 : 0.72);
              x.drawImage(bloom.image, -170, -170, 340, 340);
              x.restore();
              drawn++;
            }
          }
        }
      }
      const m = window.__pulseMetrics;
      m.frames++;
      m.maxConcurrent = Math.max(m.maxConcurrent, active);
      m.activeBuckets = drawn;
      m.tileBytes = window.__signalCacheStats.bytes;
      m.costs.push(performance.now() - begin);
      if (m.costs.length > 1200) m.costs.shift();
      Object.assign(c.dataset, {
        active,
        ripples: 0,
        time: seconds.toFixed(3),
        buckets: drawn,
      });
    }
    function tick(now) {
      raf = 0;
      if (disposed) return;
      if (last) clock += (now - last) / 1000;
      last = now;
      graph.updateMat(clock);
      draw(clock);
      schedule();
    }
    function schedule() {
      const m = window.__pulseMetrics,
        moving =
          !disposed &&
          !reduce.matches &&
          !document.hidden &&
          visible &&
          frozen === null;
      m.paused = !moving;
      m.reason = reduce.matches
        ? "reduced-motion"
        : document.hidden
          ? "hidden"
          : !visible
            ? "offscreen"
            : frozen !== null
              ? "frozen"
              : "running";
      if (moving && !raf) raf = requestAnimationFrame(tick);
      if (!moving) {
        cancelAnimationFrame(raf);
        raf = 0;
        last = 0;
        if (reduce.matches) {
          x.clearRect(0, 0, w, h);
          graph.updateMat(600);
        }
      }
    }
    const changed = () => {
      if (reduce.matches) graph.updateMat(600, true);
      schedule();
    };
    document.addEventListener("visibilitychange", changed);
    reduce.addEventListener("change", changed);
    if (typeof IntersectionObserver !== "undefined") {
      try {
        io = new IntersectionObserver((entries) => {
          visible = entries[0].isIntersecting;
          schedule();
        });
        io.observe(base.parentElement);
      } catch {}
    }
    window.__setPulseTime = (t) => {
      frozen = t;
      schedule();
      graph.updateMat(t);
      draw(t);
    };
    window.__growthRootProbe = (t) => {
      const roots = new Map();
      for (const s of graph.growing)
        if (!roots.has(s.branch) || s.birth < roots.get(s.branch).birth)
          roots.set(s.branch, s);
      return [...roots.values()].map((s) => graph.growthFraction(s, t));
    };
    window.__drawPulseOnly = draw;
    window.__setGrowthTime = (t) => graph.updateMat(t, true);
    window.__growthFractionProbe = (t) =>
      graph.growing.map((s) => graph.growthFraction(s, t));
    window.__reachProbe = () =>
      routes.map((r) => {
        const fine = r.list.filter(
          (v) => !v.highway && !graph.ss[v.segment].birth,
        );
        return {
          from: r.from,
          maxFine: fine.reduce((m, v) => Math.max(m, v.fine), 0),
          bands: [0, 245, 490, 600].map((cut) => {
            const a = fine.filter((v) => v.fine >= cut),
              visible = a.filter((v) => {
                const s = graph.ss[v.segment],
                  mx = (s.a.x + s.b.x) / 2,
                  my = (s.a.y + s.b.y) / 2;
                return mx > w * 0.5 && mx < w && my > 20 && my < h - 20;
              });
            return {
              cut,
              segments: a.length,
              unobscured_right_half: visible.length,
              explorers: a.filter((v) => graph.ss[v.segment].explorer).length,
            };
          }),
        };
      });
    window.__signalProbe = () =>
      routes.map((r) => ({
        from: r.from,
        to: r.to,
        landings: r.landings,
        segments: r.list.filter((s) => s.highway || s.segment % 997 === 0),
      }));
    pulseCleanup = () => {
      disposed = true;
      for (const r of routes)
        for (const b of r.buckets.values()) {
          for (const t of b.images) {
            t.image.width = 0;
            t.image.height = 0;
          }
        }
      for (const b of graph.blooms) {
        b.image.width = 0;
        b.image.height = 0;
      }
      cancelAnimationFrame(raf);
      io?.disconnect();
      document.removeEventListener("visibilitychange", changed);
      reduce.removeEventListener("change", changed);
    };
    graph.updateMat(frozen ?? 0, true);
    if (frozen !== null) draw(frozen);
    schedule();
  }
  drawWeb(canvas);
  return () => {
    pulseCleanup();
    delete canvas.__paintKey;
  };
}
