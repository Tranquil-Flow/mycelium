// SPDX-License-Identifier: AGPL-3.0-only
// STAGING ONLY (next.mycelium.now style toggle): the r9 "electric" renderer, byte-for-byte
// from tag style/electric-r9 (326fad85) except module names. Never served in production.
/** One page-space scene. Hero and understory share vertices, edges and pulse
 * arrival routes; only this viewport canvas is visible. The vertical field is a
 * bounded periodic graph, not a document-height bitmap. No scroll parallax. */
import {rng, dendriteWalk, measurePaths, rootLaterals, sceneClock} from './mycelium-geometry-electric.js';

function contentBounds(offset=0) {
  const main=document.querySelector('main'),box=main.getBoundingClientRect(),style=getComputedStyle(main);
  let left=box.left+parseFloat(style.paddingLeft||0),right=box.right-parseFloat(style.paddingRight||0);
  const rects=[...main.querySelectorAll('.card,.sect,.panel,.hero-copy')].map(e=>e.getBoundingClientRect()).filter(r=>r.width&&r.height);
  for(const r of rects){left=Math.min(left,r.left);right=Math.max(right,r.right);}
  return {left,right,offset,rects:rects.length,enabled:innerWidth>=1100&&Math.min(left,innerWidth-right)>=60};
}
export function extendFieldGraph({vs,ss,paths,vertex,edge,nodes,w,h}) {
  const R=rng('r9b-connected-field'), height=Math.max(600,innerHeight*2), top=h-150;
  const offset=document.getElementById('hyphae')?.getBoundingClientRect().left||0;
  const layout=contentBounds(offset),left=-offset;w=innerWidth;
  const baseCount=Math.round(w*height/10000), count=Math.round(baseCount*1.2);
  const strands=[],fusions=[], all=[];
  const add=(points,root=null,birth=0,width=.42,alpha=.48)=>{
    const poly=[points[0]], segments=[];
    let a=points[0];
    for(let i=1;i<points.length;i++) {
      const q=points[i],b=q.edges?q:vertex(q.x,q.y,a.source);
      const s=edge(a,b,width,alpha,1,-1,birth?birth+(i-1)*.5:0);
      if(s){s.field=true;segments.push(s);}
      poly.push(b);a=b;
    }
    const p={points:poly,segments,width,alpha,birth,phase:root?.phase??R()*120,
      period:root?.period??(80+R()*60),static:!birth};
    for(let i=0;i<segments.length;i++){segments[i].fieldPath=p;segments[i].fieldIndex=i;}
    strands.push(p);paths.push({kind:'field',points:poly});all.push(...poly);return p;
  };
  // First lay the mature field: sparse persistent headings, not wandering snakes.
  for(let i=0;i<count;i++) {
    const start=vertex(left+R()*w,top+R()*height,0);
    const points=dendriteWalk(start,R()*Math.PI*2,95+R()*110,R);
    add(points);
  }
  const mature=strands.slice(), grid=new Map(),cell=12;
  for(const p of mature)for(const q of p.points){
    const k=`${Math.floor(q.x/cell)},${Math.floor(q.y/cell)}`;
    if(!grid.has(k))grid.set(k,[]);grid.get(k).push({q,p});
  }
  // Connections between the hero's actual colonies and the field, not decorative
  // look-alike lines. A spine visits nearby field vertices and thus joins islands.
  const connectors=[];
  for(let lane=0;lane<Math.max(3,Math.round(w/260));lane++) {
    const targetX=left+(lane+.5)*w/Math.max(3,Math.round(w/260));
    let a=nodes.length?nodes.reduce((a,b)=>Math.hypot(b.x-targetX,b.y-h)<Math.hypot(a.x-targetX,a.y-h)?b:a):mature[lane%mature.length].points[0];
    for(let y=top+30;y<top+height;y+=100) {
      const candidates=all.filter(q=>Math.abs(q.y-y)<60);
      if(!candidates.length)continue;
      const b=candidates.reduce((a,b)=>Math.abs(b.x-targetX)<Math.abs(a.x-targetX)?b:a);
      if(a===b)continue;
      const length=Math.hypot(b.x-a.x,b.y-a.y),n=Math.max(2,Math.ceil(length/3.5)),points=[a];
      // Endpoint-conditioned, small nonperiodic turns. Endpoints retain identity.
      let off=0;const offsets=[0];for(let i=1;i<n;i++){off+=(R()-.5)*7;offsets.push(off);}offsets.push(0);
      const nx=-(b.y-a.y)/(length||1),ny=(b.x-a.x)/(length||1);
      for(let i=1;i<n;i++)points.push({x:a.x+(b.x-a.x)*i/n+nx*offsets[i],y:a.y+(b.y-a.y)*i/n+ny*offsets[i]});
      points.push(b);connectors.push(add(points,null,0,.38,.4));a=b;
    }
  }
  function branch(parent,start,heading,length,birth,width,canFuse=true) {
    let points=dendriteWalk(start,heading,length,R),hit=null;
    if(canFuse)for(let j=8;j<points.length;j++) {
      const q=points[j],near=grid.get(`${Math.floor(q.x/cell)},${Math.floor(q.y/cell)}`)||[];
      const target=near.find(v=>v.p!==parent&&Math.hypot(v.q.x-q.x,v.q.y-q.y)<5);
      if(target){points=points.slice(0,j).concat([target.q]);hit=target.q;break;}
    }
    const p=add(points,parent,birth,width,.5);
    if(canFuse) {
      const at=Math.floor(points.length*(.35+R()*.2)),a=p.points[at-1],b=p.points[at];
      branch(parent,b,Math.atan2(b.y-a.y,b.x-a.x)+(R()<.5?-.6:.6),22+R()*28,birth+at*.5,width*.65,false);
    }
    if(hit) {
      const a=p.points.at(-2),heading=Math.atan2(hit.y-a.y,hit.x-a.x),arrival=birth+(points.length-1)*.5;
      fusions.push({point:hit,parent:p,birth:arrival});
      for(const side of [-1,1])branch(parent,hit,heading+side*.5,24+R()*36,arrival+1,width*.7,false);
    }
  }
  for(const parent of mature) {
    const at=Math.floor(parent.points.length*(.3+R()*.4)),a=parent.points[at-1],b=parent.points[at];
    branch(parent,b,Math.atan2(b.y-a.y,b.x-a.x)+(R()<.5?-.6:.6),45+R()*60,1,.3);
  }
  // Minor peripheral colonies, connected through real vertices. Never swarm icons.
  const marginRoutes=[],marginKnots=[],marginDensityRatio=.4;
  const join=(a,b,width=.9)=>{
    const raw=dendriteWalk(a,Math.atan2(b.y-a.y,b.x-a.x),Math.hypot(b.x-a.x,b.y-a.y),R),last=raw.at(-1);
    const points=raw.map((q,i)=>({x:q.x+(b.x-last.x)*i/(raw.length-1),y:q.y+(b.y-last.y)*i/(raw.length-1)}));
    points[0]=a;points[points.length-1]=b;const p=add(points,null,0,width,.7);p.margin=true;return p;
  };
  if(layout.enabled)for(const side of [-1,1]){
    const px=side<0?layout.left*.42:layout.right+(innerWidth-layout.right)*.58;
    const knots=[];
    for(let y=top+135;y<top+height;y+=460){
      const centre=vertex(px-offset+(R()-.5)*18,y+(R()-.5)*75,0);knots.push(centre);marginKnots.push(centre);
      for(let j=0;j<Math.round(40*marginDensityRatio);j++){
        const p=add(dendriteWalk(centre,j*Math.PI*2/16+(R()-.5)*.2,15+R()*30,R),null,0,.22+R()*.18,.7);p.margin=true;
      }
    }
    const first=knots[0];
    const source=nodes.length?nodes.reduce((a,b)=>Math.hypot(b.x-first.x,b.y-first.y)<Math.hypot(a.x-first.x,a.y-first.y)?b:a):all.reduce((a,b)=>Math.hypot(b.x-first.x,b.y-first.y)<Math.hypot(a.x-first.x,a.y-first.y)?b:a);
    const route=[join(source,first,1.1)];
    for(let j=1;j<knots.length;j++)route.push(join(knots[j-1],knots[j],1.05));
    marginRoutes.push({side,paths:route,knots,source});
  }
  const mainRoots=strands.slice();
  // Poisson-like attachment positions, long acute laterals and two finer orders.
  // The old equal-arc comb (one 4–15px tick every 18px) read as stripes.
  const webR=rng('r9-irregular-root-web'),orders={1:0,2:0,3:0},gaps=[];
  let webFusions=0,lateralCount=0,minLateralsPer100px=Infinity;
  const webGrid=new Map(),webCell=8;
  function indexWeb(p){for(const q of p.points){const key=`${Math.floor(q.x/webCell)},${Math.floor(q.y/webCell)}`;
    if(!webGrid.has(key))webGrid.set(key,[]);webGrid.get(key).push({q,p});}}
  for(const p of mainRoots)indexWeb(p);
  function fineWeb(parent,order){
    const arc=[0];for(let i=1;i<parent.points.length;i++)arc.push(arc.at(-1)+Math.hypot(parent.points[i].x-parent.points[i-1].x,parent.points[i].y-parent.points[i-1].y));
    const length=arc.at(-1);if(!length)return;
    const count=order===1?Math.max(2,Math.ceil(length/18)):(length>18?2:1);
    const positions=Array.from({length:count},()=>length*(.04+webR()*.86)).sort((a,b)=>a-b);
    parent.laterals=0;
    for(let k=0;k<positions.length;k++){
      let at=1;while(at<arc.length-1&&arc[at]<positions[k])at++;
      const start=parent.points[at],prev=parent.points[at-1],heading=Math.atan2(start.y-prev.y,start.x-prev.x);
      const spacing=Math.max(6,((positions[k+1]??length)-(positions[k-1]??0))/2);
      const distance=spacing*(.3+webR()*1.2),side=webR()<.5?-1:1;
      const angle=heading+side*(Math.PI/12+webR()*Math.PI/6),steps=Math.max(2,Math.ceil(distance/3));
      let direction=angle,turn=0,points=[start];
      for(let j=1;j<=steps;j++){
        turn=turn*.65+(webR()-.5)*.16;
        direction+=turn-Math.atan2(Math.sin(direction-heading),Math.cos(direction-heading))*.045;
        const a=points.at(-1),q={x:a.x+Math.cos(direction)*distance/steps,y:a.y+Math.sin(direction)*distance/steps};
        let hit=null,best=2.4;
        if(j>steps*.55)for(let dx=-1;dx<=1;dx++)for(let dy=-1;dy<=1;dy++){
          const near=webGrid.get(`${Math.floor(q.x/webCell)+dx},${Math.floor(q.y/webCell)+dy}`)||[];
          for(const candidate of near){if(candidate.p===parent||candidate.p.static!==parent.static)continue;
            const delta=Math.hypot(q.x-candidate.q.x,q.y-candidate.q.y);
            if(delta<best&&Math.hypot(start.x-candidate.q.x,start.y-candidate.q.y)>5){hit=candidate.q;best=delta;}}
        }
        points.push(hit||q);if(hit){webFusions++;break;}
      }
      const child=add(points,parent,parent.birth?parent.birth+at*.5:0,parent.width*(order===1?.55:.66),parent.alpha*(order===1?.6:.8));
      child.hair=true;child.order=order;child.margin=parent.margin;
      child.marginRoot=parent.marginRoot||parent.points[0];child.marginDistance=(parent.marginDistance||0)+arc[at];
      indexWeb(child);orders[order]++;lateralCount++;parent.laterals++;
      if(order<3&&distance>7)fineWeb(child,order+1);
    }
    if(order===1){
      for(let k=1;k<positions.length;k++)gaps.push((positions[k]-positions[k-1])/(length/count));
      minLateralsPer100px=Math.min(minLateralsPer100px,parent.laterals*100/length);
    }
  }
  for(const parent of mainRoots)fineWeb(parent,1);
  const mean=gaps.reduce((n,v)=>n+v,0)/gaps.length;
  const spacingCV=Math.sqrt(gaps.reduce((n,v)=>n+(v-mean)**2,0)/gaps.length)/mean;
  // Gap-sequence periodogram: a regular repeating spacing modulation must not
  // dominate. CV separately rejects the old almost-constant comb spacing.
  const sample=gaps.slice(0,512),power=[];
  for(let k=1;k<=64;k++){let re=0,im=0;for(let j=0;j<sample.length;j++){
    const phase=2*Math.PI*k*j/sample.length;re+=(sample[j]-mean)*Math.cos(phase);im+=(sample[j]-mean)*Math.sin(phase);}
    power.push(re*re+im*im);}
  const rootAudit={mainStrands:mainRoots.length,lateralCount,minLateralsPer100px,loneThreads:mainRoots.filter(p=>!p.laterals).length,
    web:{orders,spacingCV,spectralPeakFraction:Math.max(...power)/power.reduce((n,v)=>n+v,0),fusions:webFusions,spacingSamples:gaps.length}};
  // Periodic projection below the first tile: paired boundary vertices are
  // identical under the page-space tile translation. Finite storage at any height.
  return {strands,fusions,connectors,top,height,width:w,left,layout,baseCount,count,rootAudit,marginKnots,marginRoutes,marginDensityRatio,
    segments:ss.filter(s=>s.field),densityMultiplier:1.2};
}

function life(p,t,reduce) {
  if(p.static||reduce)return {extent:p.points.length-1,alpha:p.alpha};
  const phase=(t*1.2+p.phase)%p.period,local=phase-5-p.birth;
  const retire=Math.max(0,Math.min(1,(phase-(p.period-18))/12));
  return {extent:Math.max(0,Math.min(p.points.length-1,local/.5)),retire,
    alpha:retire>=1?0:p.alpha*Math.max(0,Math.min(1,local/2))};
}
export function createField({stage,state,reduce}) {
  const c=document.getElementById('page-mycelium'),x=c.getContext('2d'),
    tile=document.createElement('canvas'),bx=tile.getContext('2d');
  const hero=document.getElementById('hyphae'),scene=hero?window.__myceliumScene:null;
  const css=getComputedStyle(stage),light=state.theme==='light',glow=css.getPropertyValue('--glow').trim(),core=css.getPropertyValue('--core').trim();
  const width=Math.round(innerWidth),height=Math.round(innerHeight),d=Math.min(devicePixelRatio||1,2);
  c.width=width*d;c.height=height*d;x.scale(d,d);
  let graph=scene?.graph,field=scene?.field;
  if(!field) {
    const vs=[],ss=[],paths=[];
    const vertex=(x,y,source=0)=>{const v={x,y,source,id:vs.length,edges:[]};vs.push(v);return v;};
    const edge=(a,b,width,alpha,tone,cord,birth)=>{const s={id:ss.length,a,b,width,alpha,tone,cord,birth,len:Math.hypot(b.x-a.x,b.y-a.y)};ss.push(s);a.edges.push(s.id);b.edges.push(s.id);return s;};
    field=extendFieldGraph({vs,ss,paths,vertex,edge,nodes:[],w:width,h:150});graph={vs,ss};
  }
  tile.width=field.width;tile.height=Math.ceil(field.height);
  const fieldAlpha=light?.17:.4,sharedClock=scene?.clock||sceneClock();
  const marginWeight=x=>{const l=field.layout,px=x+l.offset;if(!l.enabled)return 0;const distance=px<l.left?l.left-px:px>l.right?px-l.right:0;const q=Math.max(0,Math.min(1,distance/72));return q*q*(3-2*q);};
  const marginGradients=new WeakMap();
  function marginGradient(ctx){
    if(marginGradients.has(ctx))return marginGradients.get(ctx);
    const gradient=ctx.createLinearGradient(-field.layout.offset,0,width-field.layout.offset,0);
    // Smooth spatial attenuation on a connected stroke, not overlapping round
    // caps whose double alpha produced a bead at every ~3.5px graph vertex.
    for(let i=0;i<=128;i++){
      const rgb=glow.startsWith('#')?parseInt(glow.slice(1),16):0;
      gradient.addColorStop(i/128,`rgba(${rgb>>16},${rgb>>8&255},${rgb&255},${marginWeight(i*width/128-field.layout.offset)})`);
    }
    marginGradients.set(ctx,gradient);return gradient;
  }
  function drawPath(ctx,p,extent,retire=0){
    const style=ctx.strokeStyle,join=ctx.lineJoin;
    if(p.margin){ctx.strokeStyle=marginGradient(ctx);ctx.lineJoin='round';}
    tracePath(ctx,p,extent,retire);ctx.strokeStyle=style;ctx.lineJoin=join;
  }
  function tracePath(ctx,p,extent,retire=0) {
    if(retire>0){
      // Fixed laid edges dim from tip to root; the root never slides forwards.
      const alpha=ctx.globalAlpha,front=(1-retire)*(p.points.length-1),solid=Math.max(0,Math.min(extent,front-3));
      drawPath(ctx,p,solid);
      for(let i=Math.floor(solid)+1;i<=Math.min(Math.ceil(extent),Math.ceil(front+3));i++){
        const a=p.points[i-1],b=p.points[i];if(!b)continue;
        ctx.globalAlpha=alpha*Math.max(0,Math.min(1,(front+3-i)/6));
        ctx.beginPath();ctx.moveTo(a.x,a.y);ctx.lineTo(b.x,b.y);ctx.stroke();
      }ctx.globalAlpha=alpha;return;
    }
    ctx.beginPath();ctx.moveTo(p.points[0].x,p.points[0].y);
    for(let i=1;i<=Math.ceil(extent);i++) {
      const a=p.points[i-1],b=p.points[i],f=Math.min(1,extent-i+1);
      if(!b||f<=0)break;ctx.lineTo(a.x+(b.x-a.x)*f,a.y+(b.y-a.y)*f);
    }
    ctx.stroke();
  }
  function rasterStatic(){
  bx.setTransform(1,0,0,1,-field.left,-field.top);bx.clearRect(field.left,field.top,field.width,field.height);bx.lineCap='round';bx.strokeStyle=glow;
  for(const p of field.strands.filter(p=>p.static)) {
    bx.globalAlpha=p.alpha*fieldAlpha;bx.lineWidth=p.width;
    for(const shift of [-field.height,0,field.height]){bx.save();bx.translate(0,shift);drawPath(bx,p,p.points.length-1);bx.restore();}
  }

  }
  rasterStatic();
  const heroInterval=scene?Math.max(14.4,...scene.routes.map(r=>r.arrivalMax+5)):24;
  const marginSchedules=field.marginRoutes.map((r,side)=>{
    const legs=r.paths.map(p=>{let distance=0;const segments=p.segments.map(s=>{const start=distance;distance+=s.len;return {s,arrival:start/80};});return {p,segments,duration:distance/80};});
    const period=Math.max(38,heroInterval/ .9,...legs.map(l=>l.duration+12));
    const landing=scene?.routes[0]?.landings.find(l=>l.node===r.source.source)?.time||0;
    return {...r,legs,period,phase:landing+3+side*9};
  });
  function marginTrips(t){return marginSchedules.flatMap(s=>{
    const elapsed=t-s.phase;if(elapsed<0)return [];
    const cycle=Math.floor(elapsed/s.period),age=elapsed%s.period,leg=s.legs[cycle%s.legs.length];
    return age<leg.duration+5?[{...s,leg,age}]:[];
  });}
  window.__marginPulseProbe=t=>({active:marginTrips(t).map(s=>s.side),periods:marginSchedules.map(s=>s.period),intensity:.75,heroInterval});
  let raf=0,last=0,clock=0,frozen=null,disposed=false;
  window.__understoryMetrics={costs:[],frames:0,paused:false};
  window.__understoryAudit={paths:field.strands.length,branches:field.strands.filter(p=>!p.static).length,
    fusions:field.fusions.length,nodes:field.marginKnots.length,sporeNodes:0,densityMultiplier:1.2,baseCount:field.baseCount,count:field.count,
    sharedGraph:!!scene,connectors:field.connectors.length,canvasCount:2,
    ...field.rootAudit,explorerLoneThreads:scene?.graph.explorerAudit?.loneThreads??0,
    explorerAudit:scene?.graph.explorerAudit,marginKnots:field.marginKnots.length,marginDensityRatio:field.marginDensityRatio,marginPulseIntensity:.75,marginPulseLeak:135,marginPulsePeriods:marginSchedules.map(s=>s.period),marginPulseRateRelative:marginSchedules.map(s=>heroInterval/s.period),protectedRects:field.layout.rects,
    canvasPixels:c.width*c.height,bytes:(c.width*c.height+tile.width*tile.height)*4,
    straightness:measurePaths(field.strands.filter(p=>!p.hair).map(p=>({kind:'understory',points:p.points})))};
  function paint(seconds) {
    if(disposed)return;
    const begin=performance.now(),t=reduce.matches?32:seconds,scroll=window.scrollY;
    const box=hero?.getBoundingClientRect(),ox=box?.left??0,oy=box?box.top:0;
    x.clearRect(0,0,width,height);
    if(scene && oy<height && oy+hero.height/d>0) {
      // Composite the existing full-DPR hero rasters onto the ONE visible scene.
      // Smooth mask has no rectangular cut at the old hero boundary.
      x.save();x.translate(ox,oy);x.globalCompositeOperation='source-over';
      for(const layer of hero.parentElement.querySelectorAll('canvas')) {
        x.globalAlpha=Number(layer.style.opacity||1);x.drawImage(layer,0,0,hero.width/d,hero.height/d);
      }
      x.globalAlpha=1;x.globalCompositeOperation='destination-in';
      const mask=x.createLinearGradient(0,0,hero.width/d,0);
      if(width<600){mask.addColorStop(0,'#fff');mask.addColorStop(1,'#fff');}
      else{mask.addColorStop(0,'#ffffff08');mask.addColorStop(.36,'#ffffff08');mask.addColorStop(.6,'#fff');mask.addColorStop(1,'#fff');}
      x.fillStyle=mask;x.fillRect(0,0,hero.width/d,hero.height/d);
      const vertical=x.createLinearGradient(0,0,0,hero.height/d);
      for(const [at,a] of (width<600?[[0,.03],[.5,.04],[.7,1],[.88,1],[1,0]]:[[0,0],[.12,1],[.78,1],[1,0]]))vertical.addColorStop(at,`rgba(255,255,255,${a})`);
      x.fillStyle=vertical;x.fillRect(0,0,hero.width/d,hero.height/d);
      x.restore();
    }
    // Tile copies are page-anchored. Scrolling never slides roots relative to UI.
    const origin=scene?oy+field.top:-scroll;
    const first=Math.floor((scroll-(scene?oy+scroll+field.top:0))/field.height);
    for(let k=Math.max(scene?0:-Infinity,first-1);k<=first+2;k++) {
      const y=origin+k*field.height;
      if(y>height||y+field.height<0)continue;
      x.drawImage(tile,ox+field.left,y);
      x.save();x.translate(ox,(scene?oy:0)+k*field.height-(scene?0:scroll+field.top));
      x.strokeStyle=glow;x.lineCap='round';
      for(const p of field.strands.filter(p=>!p.static)) {
        const v=life(p,t,reduce.matches);if(v.alpha<=0||v.extent<=0)continue;
        x.globalAlpha=v.alpha*fieldAlpha;x.lineWidth=p.width;drawPath(x,p,v.extent,v.retire);
      }
      for(const f of field.fusions) {
        const v=life({...f.parent,birth:f.birth},t,reduce.matches);if(v.extent<=0)continue;
        x.globalAlpha=v.alpha*fieldAlpha;x.fillStyle=core;x.beginPath();x.arc(f.point.x,f.point.y,.9,0,Math.PI*2);x.fill();
      }
      // One calm node-to-node trip per side, on the same graph. Subsequent
      // legs start from the previous receiving colony, not independent flashes.
      if(!reduce.matches)for(const trip of marginTrips(t)){
        x.strokeStyle=core;x.lineCap='butt';
        for(const item of trip.leg.segments){
          const dt=trip.age-item.arrival;if(dt<0||dt>4.2)continue;
          const env=dt<1.05?Math.sin(dt/1.05*Math.PI/2)**2:Math.cos((dt-1.05)/3.15*Math.PI/2)**2;
          const s=item.s;x.globalAlpha=env*.75*(light?.2:.65)*marginWeight((s.a.x+s.b.x)/2);
          x.lineWidth=Math.max(.45,s.width*1.6);x.beginPath();x.moveTo(s.a.x,s.a.y);x.lineTo(s.b.x,s.b.y);x.stroke();
        }
        const node=trip.leg.p.points.at(-1),age=trip.age-trip.leg.duration;
        if(age>=0&&age<5){
          const env=Math.sin(age/5*Math.PI)**2;
          x.globalAlpha=env*.75*(light?.2:.65)*marginWeight(node.x);x.fillStyle=core;
          x.beginPath();x.arc(node.x,node.y,1.4,0,Math.PI*2);x.fill();
          // Short peripheral leak follows attached roots and their lateral hairs.
          for(const p of field.strands){if(!p.margin||(p.marginRoot||p.points[0])!==node)continue;
            let distance=p.marginDistance||0;
            for(const s of p.segments){const dt=age-distance/35;distance+=s.len;if(distance>135||dt<0||dt>3)continue;
              x.globalAlpha=Math.sin(dt/3*Math.PI)**2*.75*(light?.2:.65)*Math.exp(-distance/67.5)*marginWeight(s.a.x);
              x.lineWidth=Math.max(.25,s.width*1.5);x.beginPath();x.moveTo(s.a.x,s.a.y);x.lineTo(s.b.x,s.b.y);x.stroke();}
          }
        }
      }
      x.restore();
    }
    if(scene) {
      // The same connector edges start in hero colonies; draw their above-tile
      // portions too. Both ends are real vertices in the pulse route graph.
      x.save();x.translate(ox,oy);x.strokeStyle=glow;x.lineCap='round';
      x.save();x.beginPath();x.rect(-1000,-10000,width+2000,10000+field.top);x.clip();
      for(const p of field.connectors){x.globalAlpha=fieldAlpha*p.alpha;x.lineWidth=p.width;drawPath(x,p,p.points.length-1);}
      x.restore();
      // Full-DPR pulse leakage along actual graph arrival times, not a second
      // independent animation. New tips glow only after they have been laid.
      if(!reduce.matches)for(const trip of scene.activeTrips(t)) {
        for(const item of trip.route.fieldList) {
          const s=graph.ss[item.segment],dt=trip.age-item.arrival;
          if(dt<0||dt>4.2)continue;
          const env=dt<1.05?Math.sin(dt/1.05*Math.PI/2)**2:Math.cos((dt-1.05)/3.15*Math.PI/2)**2;
          const p=s.fieldPath,v=p?life(p,t,false):null;
          if(p&&!p.static&&(v.extent<(s.fieldIndex||0)+1||v.alpha<=0))continue;
          x.globalAlpha=env*item.amp*(light?.2:.65)*(p?.margin?marginWeight((s.a.x+s.b.x)/2)*.5:1);x.strokeStyle=core;x.lineWidth=Math.max(.5,s.width*1.8);
          x.beginPath();x.moveTo(s.a.x,s.a.y);x.lineTo(s.b.x,s.b.y);x.stroke();
        }
      }
      x.restore();

    }
    Object.assign(c.dataset,{time:t.toFixed(2),paths:field.strands.length,nodes:field.marginKnots.length,sporeNodes:0,canvasPixels:c.width*c.height});
    const m=window.__understoryMetrics;m.frames++;m.costs.push(performance.now()-begin);if(m.costs.length>1200)m.costs.shift();
  }
  function tick(now){raf=0;clock=sharedClock.tick(now);last=now;paint(clock);schedule();}
  function schedule(){const moving=!disposed&&!reduce.matches&&!document.hidden&&frozen===null;window.__understoryMetrics.paused=!moving;if(moving&&!raf)raf=requestAnimationFrame(tick);else if(!moving){cancelAnimationFrame(raf);raf=0;last=0;}}
  const changed=()=>{if(document.hidden||reduce.matches)sharedClock.pause();paint(frozen??clock);schedule();},scrolled=()=>paint(frozen??clock);
  window.__setUnderstoryTime=t=>{frozen=t;schedule();paint(t);};window.__paintUnderstory=paint;
  const layoutObserver=new MutationObserver(()=>{const previous=field.layout;field.layout=contentBounds(previous.offset);window.__understoryAudit.protectedRects=field.layout.rects;if(previous.left!==field.layout.left||previous.right!==field.layout.right){marginGradients.delete(bx);marginGradients.delete(x);rasterStatic();}});
  layoutObserver.observe(document.querySelector('main'),{subtree:true,childList:true,attributes:true,attributeFilter:['hidden']});
  paint(0);schedule();window.addEventListener('scroll',scrolled,{passive:true});document.addEventListener('visibilitychange',changed);reduce.addEventListener('change',changed);
  return ()=>{disposed=true;layoutObserver.disconnect();cancelAnimationFrame(raf);window.removeEventListener('scroll',scrolled);document.removeEventListener('visibilitychange',changed);reduce.removeEventListener('change',changed);tile.width=tile.height=0;};
}
