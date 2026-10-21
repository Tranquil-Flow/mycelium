// SPDX-License-Identifier: AGPL-3.0-only
import assert from 'node:assert/strict';
import test from 'node:test';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { aggregate, runFixtureMatrix, sourceBinding } from './a6-product-browser-matrix.mjs';
const script = fileURLToPath(new URL('./a6-product-browser-matrix.mjs', import.meta.url));
const engines = ['chromium', 'firefox', 'webkit'];
const cases = ['ordinary_browser_source_failure_recovery', 'no_successor'];
const scenarios = ['positive_source_failure_recovery','truthful_no_successor_abort','privacy_canaries','reconnect_before_cutover','reconnect_after_cutover','reload_during_recovery','back_forward','workspace_switching','clean_second_session','no_stale_source','no_duplicate_terminal','console_page_cross_origin_errors'];
const workspaces = ['lab','network','plans','incidents','readiness','nodes','settings','inference'];
const binding = 'sha256:'+'a'.repeat(64);
function scenario(name) { return {scenario:name,source_failure_observed:true,recovery_observed:name==='positive',
  reload_during_recovery:true,reconnect_before_cutover:true,reconnect_after_cutover:true,
  back_forward:true,stale_source_after_cutover:false,duplicate_terminal:false,
  terminal_count:1,workspaces}; }
function receipt(engine, index) {
  return { protocol:'mycelium.a6_product_browser_observation.v1', mode:'live', live_evidence:true,
    fixture_or_unit_evidence:false, passed:true, aggregation_pending:true,
    engine, engines:[{engine,positive:scenario('positive'),negative:scenario('negative')}],workspaces,
    receiver_lifetime_id:index.toString(16).padStart(64,'0'), source_binding:binding, candidate_binding:binding,
    case_ids:cases, required_scenarios:scenarios, scenario_passes:Object.fromEntries(scenarios.map(s=>[s,true])),
    second_session_private_content_visible:false, stale_source_after_cutover:false,
    duplicate_terminal:false, console_error_count:0, page_error_count:0, cross_origin_request_count:0,
    receiver_execution:{lifetime_id:index.toString(16).padStart(64,'0'), source_binding:binding, case_ids:cases, statuses:[200,200], torn_down:true}
  };
}
const group = () => engines.map(receipt);
test('three independent receiver-bound single-engine receipts aggregate only as a set', () => {
  const result = aggregate(group(), {source_binding:binding,candidate_binding:binding});
  assert.equal(result.passed,true);
  assert.deepEqual(result.engines,engines);
  assert.equal(result.qualification_claim,false);
});
test('fail closed on duplicate lifetime, engine, mismatched source/candidate, case, scenario and child integrity', () => {
  const mutations = [
    a=>a[1].receiver_lifetime_id=a[0].receiver_lifetime_id,
    a=>a[1].receiver_execution.lifetime_id=a[0].receiver_lifetime_id,
    a=>a[1].engine='chromium',
    a=>a[1].source_binding='sha256:'+'b'.repeat(64),
    a=>a[1].candidate_binding='sha256:'+'b'.repeat(64),
    a=>a[1].case_ids=['no_successor','no_successor'],
    a=>delete a[1].scenario_passes.back_forward,
    a=>a[1].scenario_passes.privacy_canaries=false,
    a=>a[1].passed=false,
    a=>a[1].receiver_execution.statuses=[200,409],
    a=>a[1].receiver_execution.torn_down=false,
    a=>a[1].engines[0].engine='webkit',
    a=>a[1].engines[0].positive.reconnect_before_cutover=false,
    a=>a[1].workspaces=['inference'],
    a=>a[1].fixture_or_unit_evidence=true,
    a=>a[1].private_canaries=['A6-BROWSER-PRIVATE-PROMPT-123'],
    a=>a[1].private_canaries=['benign-looking raw prompt'],
    a=>a[1].engines[0].positive.grant='hidden',
    a=>a[1].receiver_execution.case_ids=['no_successor','ordinary_browser_source_failure_recovery'],
  ];
  for (const mutate of mutations) {
    const a=group(); mutate(a);
    assert.throws(()=>aggregate(a,{source_binding:binding,candidate_binding:binding}), /matrix_/, String(mutate));
  }
  for(const a of [group().slice(0,2), [...group(), receipt('webkit',4)]])
    assert.throws(()=>aggregate(a,{source_binding:binding,candidate_binding:binding}), /matrix_/);
  assert.throws(()=>aggregate(group(),{source_binding:binding,candidate_binding:'sha256:'+'b'.repeat(64)}), /matrix_/);
});
test('no live factory refuses without grant or with grant before contact', () => {
  for(const env of [{}, {MYCELIUM_A6_FLEET_GRANT:'FLEET GRANTED TO A6'}]) {
    const p=spawnSync(process.execPath,[script,'--live'],{encoding:'utf8',env:{...process.env,...env}});
    assert.equal(p.status,1);
    assert.match(p.stderr,/explicit_fleet_grant_required|receiver_runtime_factory_required/);
    assert.doesNotMatch(p.stderr,/FLEET GRANTED|ECONNREFUSED/);
  }
});
test('real direct-loopback local fixture runs one-use pair across three distinct receiver constructions',async()=>{
  const report=await runFixtureMatrix();
  assert.equal(report.mode,'fixture');
  assert.equal(report.live_evidence,false);
  assert.equal(report.qualification_claim,false);
  assert.equal(report.passed,true);
  assert.equal(report.browser_scenarios_executed,false);
  assert.deepEqual(report.engines,engines);
  assert.equal(new Set(report.receivers.map(r=>r.lifetime_id)).size,3);
  for(const r of report.receivers){assert.deepEqual(r.case_ids,cases);assert.deepEqual(r.statuses,[200,200]);assert.equal(r.torn_down,true);assert.equal(r.source_binding,sourceBinding());}
  assert.doesNotMatch(JSON.stringify(report),/FLEET GRANTED|A6-BROWSER-PRIVATE|checkpoint_path/);
});
