// SPDX-License-Identifier: AGPL-3.0-only
/** Imaginary mushroom deposits, seeded only by public swarm ID.
 * Radial anatomy and powder come from local licensed photo studies; the colours
 * and impossible cap structures are fantasy. No images, fonts or network I/O.
 * Pigments never change with the site's theme/hue. These identify swarms, not nodes.
 */
const TAU = Math.PI * 2;
const ARCHETYPES = ["vortex", "lightning", "halo", "petal", "twins", "crystal", "constellation", "aurora"];
// Name, primary deposit, secondary deposit, bright rim, quiet backing.
const PIGMENTS = [
  ["violet", "#bd69ff", "#ff62da", "#e6ccff", "#221033"],
  ["magenta", "#ff30bd", "#ff9665", "#ffd0e7", "#321229"],
  ["teal", "#16efd0", "#72d8ed", "#c3ffdf", "#0d3033"],
  ["gold", "#ffc62d", "#f0d17b", "#fff1b7", "#30220e"],
  ["ember", "#ff691f", "#e9b255", "#ffe7af", "#341a19"],
  ["frost", "#d3f7ff", "#88a8e4", "#f3fcff", "#1b2b43"],
  ["ink-violet", "#39234f", "#725097", "#e6b1ff", "#b9a8ca"],
  ["iridescent", "#20ffd2", "#d96cea", "#f8d79a", "#23203c"],
  ["aurora", "#49ff9c", "#7992ef", "#f6c7ef", "#192e32"],
  ["copper", "#c98b68", "#b4bd8b", "#f5ddbd", "#2b251f"],
];
const cache = new Map();
const MAX_BYTES = 8 * 1024 * 1024, MAX_ENTRIES = 128;
let bytes = 0, renders = 0, hits = 0;
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
  const seed = hash("spore-fantasy-v2|" + id);
  // One quarter are deliberately quieter: earthy copper or inky violet.
  // Keep the group choice separate from the within-group pigment draw.
  const choice = hash("pigment|" + id), group = (choice >>> 16) % 4 === 0 ? [6,9] : [0,1,2,3,4,5,7,8];
  const pigment = PIGMENTS[group[Math.floor(random(choice)() * group.length)]];
  const archetype = ARCHETYPES[hash("anatomy|" + id) % ARCHETYPES.length];
  return {seed, archetype, type: archetype === "crystal" ? "pores" : "gills",
    colour: pigment[0], ink: pigment[1], second: pigment[2], light: pigment[3],
    paper: pigment[4], gills: 42 + (seed >>> 12) % 28};
}
function deposit(spec, pixels) {
  const work = document.createElement("canvas");
  work.width = work.height = Math.max(192, pixels);
  const x = work.getContext("2d");
  x.scale(work.width / 100, work.height / 100);
  const R = random(spec.seed), phase = R() * TAU, tilt = R() * TAU;
  const small = pixels <= 80;
  const kind = spec.archetype, lobes = 4 + Math.floor(R() * 4);
  const cx = 40 + R() * 20, cy = 40 + R() * 20;
  const ex = 0.79 + R() * 0.23, ey = 0.82 + R() * 0.2;
  const twist = (R() < 0.5 ? -1 : 1) * (1.1 + R() * 1.1);
  // Independent asymmetric folds, scallops and one torn sector; never a disc.
  const edge = (a) => 43 * (1 + 0.16 * Math.sin(a + phase) +
    0.09 * Math.cos(a * 3 + tilt) + 0.045 * Math.sin(a * 11 + phase) +
    (kind === "petal" ? 0.21 * Math.cos(a * lobes + tilt) : 0) -
    0.19 * Math.pow(Math.max(0, Math.cos(a - tilt)), 22));
  const strength = (a) => Math.max(0.14, 0.62 + 0.26 * Math.sin(a * 2 + phase) + 0.22 * Math.cos(a * 5 + tilt));
  const point = (a, r) => [cx + Math.cos(a) * r * ex, cy + Math.sin(a) * r * ey];
  const path = (points) => {
    x.beginPath(); points.forEach((p,i) => i ? x.lineTo(...p) : x.moveTo(...p));
  };
  function cap(scale, dx, dy, turn) {
    x.save(); x.translate(dx,dy); x.translate(cx,cy); x.rotate(turn); x.scale(scale,scale); x.translate(-cx,-cy);
    const outline = Array.from({length:181},(_,i) => point(i/180*TAU, edge(i/180*TAU)));
    // A shape-following backing supports both themes without a circular badge.
    path(outline); x.closePath(); x.fillStyle = rgba(spec.paper,0.22); x.fill();
    // A translucent bloom, rather than an opaque coloured cap. The ground shows
    // through; a dark hairline below the pigment keeps light-theme edges vivid.
    x.strokeStyle=rgba(spec.second,.1);x.lineWidth=6;x.stroke();
    x.strokeStyle=rgba("#190e2b",.65);x.lineWidth=1.3;x.stroke();
    x.lineJoin = "round";
    x.strokeStyle = rgba(spec.light,0.45); x.lineWidth = 1.2; x.stroke();
    const ink = x.createLinearGradient(cx-35,cy-28,cx+32,cy+35);
    ink.addColorStop(0,spec.ink); ink.addColorStop(0.48,spec.second);
    ink.addColorStop(0.72,spec.ink); ink.addColorStop(1,spec.light);
    x.lineCap = "round";
    if (kind === "crystal") {
      // Coarse, broken hexagonal pores stay legible at card size.
      for (let row=-12;row<=12;row++) for(let col=-12;col<=12;col++) {
        const dx=(col+(row%2)*0.5)*(small?7:4.3), dy=row*(small?6:3.75);
        const rr=Math.hypot(dx,dy), a=Math.atan2(dy,dx);
        if(rr<9 || rr>edge(a)-2 || R()<0.13) continue;
        const px=cx+dx*ex+(R()-.5), py=cy+dy*ey+(R()-.5), radius=(small?2.3:1.3)+R()*.7;
        path(Array.from({length:7},(_,i)=>[px+Math.cos(i/6*TAU)*radius,py+Math.sin(i/6*TAU)*radius]));
        x.strokeStyle=ink; x.lineWidth=(small?.9:.55)+R()*.4; x.globalAlpha=strength(a); x.stroke();
      }
    } else {
      const ribs=kind==="lightning"?(small?9:17):small?(kind==="vortex"?14:24):spec.gills;
      for(let i=0;i<ribs;i++) {
        const a=(i+R()*.7)/ribs*TAU, end=edge(a)*(0.88+R()*.12);
        const start=6+R()*5;
        const spiral=kind==="vortex" ? twist : kind==="aurora" ? twist*.5 : .07;
        const pts=[];
        for(let j=0;j<=16;j++) {
          const t=j/16, r=start+(end-start)*t;
          const angle=a+spiral*(1-t)*(1-t)+(kind==="lightning" && j>0 ? ((j%4<2?1:-1)*(.06+R()*.1)) : 0);
          pts.push(point(angle,r));
        }
        path(pts);
        x.strokeStyle=rgba("#1b1237",.72);x.globalAlpha=1;x.lineWidth=small?2.4:1.1;x.stroke();
        x.strokeStyle=ink;
        x.globalAlpha=strength(a)*.38; x.lineWidth=small?3:2; x.stroke();
        x.globalAlpha=kind==="halo"?.32:Math.min(1,strength(a)+.25); x.lineWidth=(small?1.05:.6)+R()*.42; x.stroke();
        // Lamellulae / lightning forks diverge from the long radial ribs.
        const branch=pts[8],tip=point(a+.035+R()*.045,end*(.82+R()*.18));
        path([branch,point(a+.055,end*.78),tip]); x.lineWidth=.35+R()*.3; x.stroke();
        if(kind==="lightning") {
          path([pts[5],point(a-.19,end*.52),point(a-.1,end*.65),point(a-.3,end*.88)]); x.lineWidth=small?1.25:.85;x.stroke();
          path([pts[10],point(a+.22,end*.72),point(a+.15,end*.85),point(a+.32,end*.96)]);x.stroke();
        }
        for(let j=0;j<(small?8:27);j++) {
          const t=R(), r=start+(end-start)*t, aa=a+spiral*(1-t)*(1-t)+(R()-.5)*.04;
          const [px,py]=point(aa,r),grain=.16+R()*.46;
          x.fillStyle=R()<.25?spec.light:spec.ink;
          x.globalAlpha=strength(a)*(.2+R()*.65); x.fillRect(px,py,grain,grain);
        }
      }
    }
    if(kind==="halo") {
      for(let ring=0;ring<(small?3:4);ring++) {
        const fraction=.36+ring*(small?.24:.17);
        const pts=Array.from({length:131},(_,i)=>{const a=i/130*TAU;return point(a,edge(a)*fraction+Math.sin(a*7+phase)*.8)});
        path(pts); x.strokeStyle=spec.light; x.globalAlpha=.85; x.lineWidth=small?1.8:1.1; x.stroke();
      }
    }
    if(kind==="constellation") {
      for(let i=0;i<(small?9:18);i++) {
        const a=R()*TAU, r=13+R()*23, [px,py]=point(a,r),rad=(small?1.5:.8)+R()*1.1;
        x.globalAlpha=.9; x.fillStyle=spec.light; x.beginPath(); x.arc(px,py,rad,0,TAU); x.fill();
        if(i%3===0) {path([[px-2.5,py],[px+2.5,py]]);x.strokeStyle=spec.light;x.lineWidth=.5;x.stroke();path([[px,py-2.5],[px,py+2.5]]);x.stroke();}
      }
    }
    // Broken bioluminescent rim, never an unbroken neon outline.
    for(let i=0;i<11;i++) {
      const a=R()*TAU,span=.06+R()*.23;
      path(Array.from({length:10},(_,j)=>{const b=a+j/9*span;return point(b,edge(b)*.98)}));
      x.strokeStyle=spec.light;x.globalAlpha=.3+R()*.35;x.lineWidth=.55;x.stroke();
    }
    // Loose powder drifts and asymmetric smudges, including beyond the cap edge.
    for(let i=0;i<(small?220:950);i++) {
      const a=R()*TAU,r=Math.sqrt(R())*(edge(a)+3);
      if(r<8 || R()>strength(a))continue;
      const [px,py]=point(a,r),s=.15+R()*.55;
      x.globalAlpha=.12+R()*.4;x.fillStyle=R()<.4?spec.second:spec.ink;x.fillRect(px,py,s,s);
    }
    for(let i=0;i<5;i++) {
      const [px,py]=point(R()*TAU,18+R()*22),r=2+R()*4;
      const smudge=x.createRadialGradient(px,py,0,px,py,r);
      smudge.addColorStop(0,rgba(spec.second,.34));smudge.addColorStop(1,rgba(spec.ink,0));
      x.globalAlpha=1;x.fillStyle=smudge;x.fillRect(px-r,py-r,2*r,2*r);
    }
    // Irregular stem gap; occasional invented sigil, not a text/font dependency.
    path(Array.from({length:41},(_,i)=>{const a=i/40*TAU;return point(a,5.8*(1+.16*Math.sin(a*3+phase)))}));
    x.closePath();x.globalAlpha=1;x.fillStyle=spec.paper;x.fill();
    x.strokeStyle=rgba(spec.light,.65);x.lineWidth=.55;x.stroke();
    if(spec.seed%3!==0) {
      path([[cx,cy-3.5],[cx+2.3,cy],[cx,cy+3.5],[cx-2.3,cy],[cx,cy-3.5]]);
      x.strokeStyle=spec.light;x.lineWidth=.6;x.stroke();
      path([[cx-3.2,cy+2],[cx+3.2,cy-2]]);x.stroke();
    }
    x.restore();
  }
  if(kind==="twins") {cap(.61,-15,-13,-.35);cap(.55,21,20,.8);}
  else cap(1,0,0,0);
  const result=document.createElement("canvas");result.width=result.height=pixels;
  const out=result.getContext("2d");out.imageSmoothingQuality="high";out.drawImage(work,0,0,pixels,pixels);
  work.width=work.height=0;return result;
}
export function drawSpore(canvas,id,{size=canvas.getBoundingClientRect().width||40,dpr=devicePixelRatio}={}) {
  const pixels=Math.max(16,Math.round(Math.min(128,size)*Math.min(3,Math.max(1,dpr))));
  const key=String(id)+"|"+pixels;
  let bitmap=cache.get(key);
  if(bitmap) {hits++;cache.delete(key);cache.set(key,bitmap);}
  else {
    bitmap=deposit(sporeSpec(id),pixels);cache.set(key,bitmap);bytes+=pixels*pixels*4;renders++;
    while(bytes>MAX_BYTES || cache.size>MAX_ENTRIES) {
      const [old,c]=cache.entries().next().value;cache.delete(old);bytes-=c.width*c.height*4;c.width=c.height=0;
    }
  }
  canvas.width=canvas.height=pixels;canvas.getContext("2d").drawImage(bitmap,0,0);
}
export function sporeCacheStats() {return {entries:cache.size,bytes,renders,hits,maxBytes:MAX_BYTES,maxEntries:MAX_ENTRIES};}
