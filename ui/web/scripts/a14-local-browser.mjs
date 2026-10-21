// SPDX-License-Identifier: AGPL-3.0-only
import { chromium, expect } from '@playwright/test';
import { writeFile, mkdir } from 'node:fs/promises';
import { spawn } from 'node:child_process';
import { createInterface } from 'node:readline';
import { once } from 'node:events';
import assert from 'node:assert/strict';
import path from 'node:path';
const out = process.env.A14_EVIDENCE;
const expectedPeers = Number(process.env.A14_CONFORMANCE_MEMBERS ?? 2) + 1;
assert(out, 'A14_EVIDENCE required');
await mkdir(out,{recursive:true});
const env={...process.env,PYTHONDONTWRITEBYTECODE:'1'};
for(const key of ['PYTHONPATH','PYTHONHOME','VIRTUAL_ENV']) delete env[key];
const server=spawn('/opt/homebrew/bin/python3.14',['tests/a14_acceptance/serve_browser.py'],{cwd:path.resolve('../..'),env,stdio:['ignore','pipe','pipe']});
let serverErrors='';server.stderr.on('data',b=>serverErrors+=b.toString());
let browser;
const checks=[], failures=[], requests=[];
let page;
try {
 const lines=createInterface({input:server.stdout});
 const [line]=await Promise.race([once(lines,'line'),new Promise((_,reject)=>setTimeout(()=>reject(Error('server_start_timeout')),15000).unref())]);
 const {origin}=JSON.parse(line); assert(/^http:\/\/127\.0\.0\.1:\d+$/.test(origin));
 assert.equal((await fetch(origin)).status,200);
 browser=await chromium.launch({executablePath:process.env.A14_CHROMIUM ?? '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',headless:true,args:['--disable-background-networking']});
 const context=await browser.newContext({viewport:{width:1440,height:1100},reducedMotion:'reduce',serviceWorkers:'block'});
 async function instrument(p) {
  p.on('pageerror',e=>failures.push(e.message));
  p.on('request',r=>requests.push({method:r.method(),path:new URL(r.url()).pathname,same_origin:new URL(r.url()).origin===origin}));
  await p.route('**/*',route=>new URL(route.request().url()).origin===origin ? route.continue() : route.abort());
 }
 page=await context.newPage();await instrument(page);
 await page.goto(origin+'/#network');
 const explorer=page.getByRole('region',{name:'Route explorer',exact:true});
 const table=page.getByRole('table',{name:'Route timeline',exact:true});
 await expect(table).toBeVisible({timeout:15000});
 await expect(explorer).toContainText('Local conformance input — not physical route evidence.');
 await expect(explorer).toContainText('connected');
 assert.equal(await page.locator('[data-route-segment]').count(),3);
 async function equivalent() {
  const a=await page.locator('[data-route-segment]').evaluateAll(es=>es.map(e=>e.getAttribute('data-route-segment')));
  const b=await page.locator('[data-graph-segment]').evaluateAll(es=>es.map(e=>e.getAttribute('data-graph-segment')));
  assert.deepEqual(a,b);return a;
 }
 const initial=await equivalent();checks.push('production_projection_table_graph');
 const snapshot=await page.evaluate(async()=>{const r=await fetch('/api/v1/product/snapshot');return r.json();});
 assert.equal(snapshot.publication.source_mode,'fixture');assert.equal(snapshot.route_explorer.source_mode,'fixture');
 assert.equal(snapshot.route_explorer.segments.length,3);
 assert.deepEqual(snapshot.route_explorer.segments.map(s=>[s.path_status,s.transport,s.provenance,s.connection_generation]),[
  ['selected','unknown','fixture',null],['observed','direct','fixture',1],['observed','relay','fixture',2]]);
 assert.deepEqual(snapshot.route_explorer.segments.map(s=>s.history),[false,true,false]);
 assert(snapshot.route_explorer.segments[0].metrics.rtt_ms===null && snapshot.route_explorer.segments[0].source!==null);
 assert(snapshot.route_explorer.segments.slice(1).every(s=>s.source===null && s.binding.request===null && s.binding.track===null));
 const tableRows=table.locator('tbody tr');
 assert.equal(await tableRows.count(),3);
 for (const [index, className] of ['unknown','direct','relay'].entries()) {
  await expect(tableRows.nth(index)).toContainText(className);
  await expect(tableRows.nth(index)).toContainText('fixture');
 }
 assert(snapshot.readiness.every(r=>r.dimension!=='qualification'||r.state!=='ready'));
 await writeFile(out+'/production-snapshot.json',JSON.stringify(snapshot,null,2));
 assert.equal(snapshot.route_explorer.nodes.length,expectedPeers);
 if(expectedPeers>64) {
  const tray=page.getByRole('region',{name:'Peer regions and unknown-region peers',exact:true});
  assert.equal(await tray.locator('li').count(),64);
  await page.getByRole('button',{name:'Next peers',exact:true}).click();
  await expect(tray).toContainText('Peer page 2');assert.equal(await tray.locator('li').count(),64);
  await page.getByRole('button',{name:'Previous peers',exact:true}).click();
  await expect(tray).toContainText('Peer page 1');checks.push('large_roster_bounded_deterministic_pages');
 }
 const button=page.getByRole('button',{name:'Inspect segment 1',exact:true});
 await button.focus();await page.keyboard.press('Enter');
 await expect(button).toBeFocused();await expect(button).toHaveAttribute('aria-pressed','true');
 const selectedDetail=page.getByRole('region',{name:'Selected route detail',exact:true});
 await expect(selectedDetail).toContainText(snapshot.route_explorer.segments[0].authority_digest);
 await expect(selectedDetail).toContainText('request');
 await expect(selectedDetail).toContainText('unavailable · unknown');
 await expect(page.getByRole('button',{name:/^Select segment 1:/})).toHaveAttribute('aria-pressed','true');
 await page.getByLabel('Transport',{exact:true}).selectOption('relay');assert.equal((await equivalent()).length,1);
 await expect(selectedDetail).toContainText(snapshot.route_explorer.segments[0].authority_digest);
 await page.getByLabel('Path status',{exact:true}).selectOption('selected');assert.equal((await equivalent()).length,0);
 await expect(page.getByRole('region',{name:'Peer regions and unknown-region peers',exact:true})).toContainText(`All ${expectedPeers} public peers`);
 await page.getByRole('button',{name:'Clear filters',exact:true}).click();assert.deepEqual(await equivalent(),initial);
 checks.push('keyboard_focus_selection_independent_filters_unknown_tray');
 await page.getByRole('button',{name:'Pause route view',exact:true}).click();await expect(explorer).toContainText('Paused historical view');
 await page.getByRole('button',{name:'Resume route view',exact:true}).click();
 await page.getByLabel('Low power (static, bounded refresh)',{exact:true}).check();
 assert.equal(await explorer.evaluate(el=>el.getAnimations({subtree:true}).length),0);
 checks.push('pause_resume_reduced_motion_low_power_static');
 for(const route of ['nodes','plans','readiness','incidents','settings','lab','inference','network']) {
  const active=()=>page.locator(`nav a[href="#${route}"][aria-current="page"]`);
  await page.goto(origin+'/#'+route);await expect(active()).toBeVisible();
  await page.reload();await expect(active()).toBeVisible();
  const away=route==='network'?'nodes':'network';
  await page.locator(`nav a[href="#${away}"]`).click();
  await expect(page.locator(`nav a[href="#${away}"][aria-current="page"]`)).toBeVisible();
  await page.goBack();await expect(active()).toBeVisible();
  await page.goForward();await expect(page.locator(`nav a[href="#${away}"][aria-current="page"]`)).toBeVisible();
  await page.goBack();await expect(active()).toBeVisible();
  const visibleText=await page.locator('body').innerText();
  const inputValues=await page.locator('input,textarea').evaluateAll(es=>es.map(e=>e.value).join('\n'));
  for(const canary of ['RAW-ENDPOINT-CANARY','192.0.2.23','node-a','node-b']) assert(!(visibleText+inputValues).includes(canary));
 }
 await expect(table).toBeVisible();assert.deepEqual(await equivalent(),initial);
 await page.reload();await expect(table).toBeVisible();assert.deepEqual(await equivalent(),initial);
 await page.getByRole('link',{name:/^Nodes/}).click();
 await page.goBack();await expect(table).toBeVisible();
 await page.goForward();await expect(table).not.toBeVisible();await page.goBack();await expect(table).toBeVisible();
 checks.push('eight_workspaces_direct_navigation_refresh_back_forward');
 await page.route('**/api/v1/product/events',r=>r.abort());
 await expect(explorer).toContainText('disconnected',{timeout:10000});await expect(table).toContainText('stale');
 assert.deepEqual(await equivalent(),initial);
 await page.unroute('**/api/v1/product/events');
 await expect(page.locator('body')).toHaveAttribute('data-evidence','connected',{timeout:10000});assert.deepEqual(await equivalent(),initial);
 checks.push('disconnect_retains_history_reconnect');
 await page.emulateMedia({forcedColors:'active',reducedMotion:'reduce'});
 await page.evaluate(()=>document.documentElement.style.fontSize='200%');
 await expect(table).toBeVisible();await page.screenshot({path:out+'/high-contrast-200-percent.png',fullPage:true});
 await page.evaluate(()=>document.documentElement.style.fontSize='');await page.emulateMedia({forcedColors:'none',reducedMotion:'reduce'});
 await page.setViewportSize({width:390,height:844});await expect(table).toBeVisible();
 const scroller=page.getByRole('region',{name:'Scrollable route timeline',exact:true});
 await scroller.focus();await page.keyboard.press('ArrowRight');
 await expect.poll(()=>scroller.evaluate(el=>el.scrollLeft)).toBeGreaterThan(0);
 await scroller.evaluate(el=>el.scrollLeft=0);
 const widths=await explorer.evaluate(el=>({width:el.getBoundingClientRect().width,viewport:innerWidth}));
 assert(widths.width<=widths.viewport);
 await page.screenshot({path:out+'/mobile-network.png',fullPage:true});
 checks.push('forced_colors_text_scaling_responsive_scrollable_table');
 await page.setViewportSize({width:1440,height:1100});
 await expect(page.locator('body')).toHaveAttribute('data-evidence','connected',{timeout:10000});
 await writeFile(out+'/browser-dom.txt',await page.locator('body').innerText());
 await writeFile(out+'/browser-ax.txt',await page.locator('body').ariaSnapshot());
 await page.screenshot({path:out+'/browser-network.png',fullPage:true});
 await page.clock.install();await page.clock.setFixedTime(Date.now()+700000);await page.clock.runFor(1100);
 await expect(table).toContainText('stale');checks.push('observation_expiry_without_republication');
 const second=await browser.newContext({serviceWorkers:'block'});const other=await second.newPage();await instrument(other);
 await other.route('**/api/v1/product/snapshot',r=>r.abort());await other.goto(origin+'/#network');
 await expect(other.getByRole('region',{name:'Route explorer',exact:true})).toContainText('Route projection unavailable');
 await other.unroute('**/api/v1/product/snapshot');
 await expect(other.getByRole('table',{name:'Route timeline',exact:true})).toBeVisible({timeout:15000});
 assert.deepEqual(await other.locator('[data-route-segment]').evaluateAll(es=>es.map(e=>e.getAttribute('data-route-segment'))),initial);
 assert.equal(await other.locator('[data-route-segment] button[aria-pressed=true]').count(),0);
 const storage=await other.evaluate(()=>Object.keys(localStorage));assert(storage.every(k=>k==='mycelium.product-ui.preferences.v1'));
 const body=await other.locator('body').innerText();
 for(const canary of ['RAW-ENDPOINT-CANARY','192.0.2.23','node-a','node-b','latitude','longitude']) assert(!body.includes(canary));
 assert(requests.every(r=>r.same_origin&&r.method==='GET'));assert.deepEqual(failures,[]);
 const after=await other.evaluate(async()=>{const r=await fetch('/api/v1/product/snapshot');return r.json();});
 assert.deepEqual(after.route_explorer.segments,snapshot.route_explorer.segments);
 assert(after.route_explorer.generation>=snapshot.route_explorer.generation);
 assert(after.readiness.every(r=>r.dimension!=='qualification'||r.state!=='ready'));
 assert.equal(after.route_explorer.source_mode,'fixture');
 assert.equal(new URL(other.url()).pathname,'/operator');
 checks.push('second_session_initial_retry_privacy_read_only_no_third_party_requests');
 await second.close();await context.close();
 await writeFile(out+'/browser-report.json',JSON.stringify({protocol:'mycelium.a14_local_browser_conformance.v1',physical_devices:0,qualification_claim:false,source_mode:'fixture',browser_version:browser.version(),checks,failures,requests,viewport_checks:widths},null,2));
 console.log(JSON.stringify({checks,failures,physical_devices:0}));
} catch(e) {
 if(page){await writeFile(out+'/browser-failure-dom.txt',await page.locator('body').innerText());await page.screenshot({path:out+'/browser-failure.png',fullPage:true});}
 await writeFile(out+'/browser-failure.json',JSON.stringify({error:String(e),checks,failures,requests,serverErrors},null,2));
 console.error(e);process.exitCode=1;
} finally {
 await browser?.close();
 server.kill('SIGTERM');await once(server,'exit').catch(()=>undefined);
 await writeFile(out+'/server-cleanup.json',JSON.stringify({pid:server.pid,exitCode:server.exitCode,signalCode:server.signalCode,serverErrors}));
}
