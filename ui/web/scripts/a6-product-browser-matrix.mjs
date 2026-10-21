#!/usr/bin/env node
// SPDX-License-Identifier: AGPL-3.0-only
// A6 matrix: transport fixture and a factory-gated live composition are distinct.
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { createInterface } from 'node:readline';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import { validateFaultControl, sendControlledFault, runSingleEngine } from './a6-product-browser-e2e.mjs';
const root = fileURLToPath(new URL('../../../', import.meta.url));
const engines = Object.freeze(['chromium','firefox','webkit']);
const workspaces = Object.freeze(['lab','network','plans','incidents','readiness','nodes','settings','inference']);
const cases = Object.freeze(['ordinary_browser_source_failure_recovery','no_successor']);
const scenarios = Object.freeze(['positive_source_failure_recovery','truthful_no_successor_abort','privacy_canaries','reconnect_before_cutover','reconnect_after_cutover','reload_during_recovery','back_forward','workspace_switching','clean_second_session','no_stale_source','no_duplicate_terminal','console_page_cross_origin_errors']);
const sources = ['ui/web/scripts/a6-product-browser-e2e.mjs','ui/web/scripts/a6-product-browser-matrix.mjs','ui/web/scripts/a6-serve-receiver-factory.mjs','mycelium_a6_direct_socket.py','mycelium_a6_http_fault_control.py','mycelium_a6_fault_control.py','scripts/run_a6_recovery_gate.py'];
const fail = () => { throw new Error('matrix_receipt_invalid'); };
export function sourceBinding() {
  const hash=createHash('sha256');
  for(const name of sources){ hash.update(name+'\0'); hash.update(readFileSync(path.join(root,name))); hash.update('\0'); }
  return 'sha256:'+hash.digest('hex');
}
function clean(value) {
  const serialized=JSON.stringify(value);
  if (!serialized || /A6-BROWSER-PRIVATE-|encoded_prompt_token_ids|committed_prefix_tokens|checkpoint_path|FLEET GRANTED TO A6/.test(serialized)
      || /"(?:private_canaries|prompt|output|grant|authorization|cookie|checkpoint_path)"\s*:/.test(serialized)) fail();
}
function bound(value) { return typeof value==='string' && /^sha256:[0-9a-f]{64}$/.test(value); }
export function aggregate(receipts, expected) {
  if (!Array.isArray(receipts) || receipts.length!==3 || !bound(expected?.source_binding)
      || !bound(expected?.candidate_binding)) fail();
  const seen=new Set(), lifetimes=new Set();
  for(const r of receipts){
    clean(r);
    if (!r || typeof r!=='object' || !engines.includes(r.engine) || seen.has(r.engine)
      || r.protocol!=='mycelium.a6_product_browser_observation.v1' || r.mode!=='live'
      || r.live_evidence!==true || r.fixture_or_unit_evidence!==false || r.passed!==true
      || r.aggregation_pending!==true || r.qualification_claim===true || r.promotion_authorized===true
      || r.source_binding!==expected.source_binding || r.candidate_binding!==expected.candidate_binding
      || typeof r.receiver_lifetime_id!=='string' || !/^[0-9a-f]{64}$/.test(r.receiver_lifetime_id)
      || lifetimes.has(r.receiver_lifetime_id) || !Array.isArray(r.engines) || r.engines.length!==1
 || r.engines[0].engine!==r.engine || r.engines[0].positive?.scenario!=='positive'
 || r.engines[0].negative?.scenario!=='negative'
 || JSON.stringify(r.workspaces)!==JSON.stringify(workspaces)
 || ![r.engines[0].positive,r.engines[0].negative].every(s=>
   s.source_failure_observed===true && s.reload_during_recovery===true
   && s.reconnect_before_cutover===true && s.reconnect_after_cutover===true
   && s.back_forward===true && s.stale_source_after_cutover===false
   && s.duplicate_terminal===false && Number.isSafeInteger(s.terminal_count)
   && JSON.stringify(s.workspaces)===JSON.stringify(workspaces))
 || r.engines[0].positive.recovery_observed!==true
      || JSON.stringify(r.case_ids)!==JSON.stringify(cases)
      || JSON.stringify(r.required_scenarios)!==JSON.stringify(scenarios)
      || !r.scenario_passes || Object.keys(r.scenario_passes).length!==scenarios.length
      || !scenarios.every(s=>r.scenario_passes[s]===true)
      || r.second_session_private_content_visible!==false || r.stale_source_after_cutover!==false
      || r.duplicate_terminal!==false || r.console_error_count!==0 || r.page_error_count!==0
      || r.cross_origin_request_count!==0
      || r.receiver_execution?.lifetime_id!==r.receiver_lifetime_id
      || r.receiver_execution.source_binding!==expected.source_binding
      || JSON.stringify(r.receiver_execution.case_ids)!==JSON.stringify(cases)
      || JSON.stringify(r.receiver_execution.statuses)!=='[200,200]'
      || r.receiver_execution.torn_down!==true) fail();
    seen.add(r.engine); lifetimes.add(r.receiver_lifetime_id);
  }
  if (engines.some(e=>!seen.has(e))) fail();
  return {protocol:'mycelium.a6_product_browser_matrix.v1', mode:'live', passed:true,
    engines:[...engines], independent_receiver_lifetimes:3, source_binding:expected.source_binding,
    candidate_binding:expected.candidate_binding, qualification_claim:false, promotion_authorized:false,
    physical_qualification_pending:true};
}
function lines(child) {
  const reader=createInterface({input:child.stdout, crlfDelay:Infinity});
  const iterator=reader[Symbol.asyncIterator]();
  const next=async()=>{
    let timer;
    const item=await Promise.race([iterator.next(),new Promise((_,reject)=>{
      timer=setTimeout(()=>reject(new Error('fixture_worker_timeout')),10000);
    })]).finally(()=>clearTimeout(timer));
    if(item.done || item.value.length>4096) throw new Error('fixture_worker_incomplete');
    return JSON.parse(item.value);
  };
  return {next,reader};
}
async function fixtureOne(engine,binding) {
  const py=process.env.MYCELIUM_A6_FIXTURE_PYTHON || 'python3.14';
  const child=spawn(py,['-B',path.join(root,'mycelium_a6_direct_socket.py'),'--fixture-worker'],{
    cwd:root,env:{...process.env,PYTHONDONTWRITEBYTECODE:'1',PYTHONPATH:root},stdio:['pipe','pipe','pipe']});
  const {next,reader}=lines(child);
  let closed=false;
  try {
    const ready=await next();
    if(!/^[0-9a-f]{64}$/.test(ready.lifetime_id) || !Number.isInteger(ready.port)
      || ready.port<1 || ready.port>65535) fail();
    const url=`http://127.0.0.1:${ready.port}/a6/controlled-fault`;
    const control=validateFaultControl({grant:'FLEET GRANTED TO A6',productOrigin:'http://127.0.0.1:9',
      positiveUrl:url,negativeUrl:url,positiveCaseId:cases[0],negativeCaseId:cases[1]});
    const used=new Set();
    const statuses=[await sendControlledFault(control.positive,used),await sendControlledFault(control.negative,used)];
    child.stdin.end('close\n');
    const final=await next();
    const exit=await new Promise(resolve=>child.once('close',resolve));
    closed=true;
    if(exit!==0 || final.lifetime_id!==ready.lifetime_id || final.torn_down!==true
      || JSON.stringify(final.case_ids)!==JSON.stringify(cases)
      || JSON.stringify(statuses)!=='[200,200]') fail();
    return {engine,lifetime_id:ready.lifetime_id,source_binding:binding,case_ids:final.case_ids,
      statuses,torn_down:true,fixture_only:true};
  } catch { throw new Error('fixture_receiver_failed'); }
  finally {reader.close(); if(!closed) child.kill();}
}
export async function runFixtureMatrix() {
  const binding=sourceBinding(), receivers=[];
  for(const engine of engines) receivers.push(await fixtureOne(engine,binding));
  if(new Set(receivers.map(r=>r.lifetime_id)).size!==3) fail();
  return {protocol:'mycelium.a6_product_browser_matrix_fixture.v1',mode:'fixture',passed:true,
    live_evidence:false,fixture_or_unit_evidence:true,browser_scenarios_executed:false,
    qualification_claim:false,promotion_authorized:false,physical_devices:0,
    claim_boundary:'Three real independent direct-loopback receiver processes and one-use case pairs; synthetic seams, no browser or physical request qualification.',
    engines:[...engines],source_binding:binding,receivers};
}
// Owner injects an explicit *real* receiver runtime factory. Without one, no live execution.
// Factory.start must construct receiver/listener and return
// {lifetime_id, origin, positiveUrl, negativeUrl, maximumNewTokens, close()}.
// close() must return a receiver-derived post-teardown execution witness, never a caller ID.
export async function runLiveMatrix({factory,candidate_binding}={}) {
  if (!factory || typeof factory.start!=='function') throw new Error('receiver_runtime_factory_required');
  const source_binding=sourceBinding();
  if (!bound(candidate_binding)) fail();
  const receipts=[];
  for(const engine of engines) {
    const runtime=await factory.start(engine);
    let receipt, witness;
    try {
      if(!runtime || typeof runtime.close!=='function' || !/^[0-9a-f]{64}$/.test(runtime.lifetime_id)) fail();
      const faultControl=validateFaultControl({grant:process.env.MYCELIUM_A6_FLEET_GRANT,
        productOrigin:runtime.origin,positiveUrl:runtime.positiveUrl,negativeUrl:runtime.negativeUrl,
        positiveCaseId:cases[0],negativeCaseId:cases[1]});
      receipt=await runSingleEngine({engine,origin:runtime.origin,faultControl,
        maximumNewTokens:runtime.maximumNewTokens});
    } finally { witness=await runtime?.close?.(); }
    if(sourceBinding()!==source_binding) fail();
    receipts.push({...receipt, receiver_lifetime_id:runtime.lifetime_id,
      source_binding,candidate_binding, receiver_execution:witness});
  }
  return aggregate(receipts,{source_binding,candidate_binding});
}
if (process.argv[1] && path.resolve(process.argv[1])===fileURLToPath(import.meta.url)) {
  const args=process.argv.slice(2);
  if(args.length===1 && args[0]==='--fixture') {
    runFixtureMatrix().then(r=>process.stdout.write(JSON.stringify(r,null,2)+'\n')).catch(()=>{
      process.stderr.write('fixture_receiver_failed\n');process.exitCode=1;});
  } else {
    process.stderr.write(process.env.MYCELIUM_A6_FLEET_GRANT==='FLEET GRANTED TO A6'
      ? 'receiver_runtime_factory_required\n':'explicit_fleet_grant_required\n');
    process.exitCode=1;
  }
}
