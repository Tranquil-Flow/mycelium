// SPDX-License-Identifier: AGPL-3.0-only
import {describe,it,expect} from 'vitest';
import fixture from '../../test/a14Conformance.json';
import evidenceFixture from '../../../../../contracts/compatibility-fixtures/coarse-region-evidence-v1.json';
import {decodeProductSnapshot} from '../productEvidence/contracts';
import {decodeCoarseRegionEvidence, effectiveFreshness, regionCodes} from './contracts';
describe('registered coarse-region evidence Python/TypeScript parity',()=>{
 it('decodes the generated Python evidence record with frozen vocabulary',()=>{
  expect(decodeCoarseRegionEvidence([evidenceFixture])[0]).toEqual(evidenceFixture);
  expect(regionCodes).toEqual(['africa','americas_north','americas_south','asia','europe','oceania','polar','unknown']);
  expect(evidenceFixture.region).toBe('unknown');
 });
 for(const change of [
  {hostname:'private.example'}, {region:'us-east-1'}, {subject_ref:'192.0.2.1'},
  {source_kind:'latency_distance_class'}, {issued_at_unix_ms:true}, {expires_at_unix_ms:-1},
 ]) it(`rejects invalid evidence ${Object.keys(change)[0]}`,()=>{
  expect(()=>decodeCoarseRegionEvidence([{...evidenceFixture,...change}])).toThrow();
 });
});
describe('Python producer to browser decoder',()=>{
 it('accepts production-emitted conformance with no namespace or traffic inference',()=>{
  const s=decodeProductSnapshot(fixture);
  expect(s.route_explorer?.nodes).toHaveLength(3);
  expect(s.route_explorer?.segments.map(v=>v.transport)).toEqual(['unknown','direct','relay']);
  expect(s.route_explorer?.source_mode).toBe('fixture');
 });
 for(const field of ['latitude','hostname','private_path','prompt','token_ids']) for(const level of ['root','node','segment','metrics','binding']) {
  it(`rejects ${level}.${field}`,()=>{
   const s=structuredClone(fixture);
   const r=s.route_explorer;
   const target=level==='root'?r:level==='node'?r.nodes[0]:level==='segment'?r.segments[0]:level==='metrics'?r.segments[0].metrics:r.segments[0].binding;
   Object.assign(target,{[field]:'PRIVATE-CANARY'});
   expect(()=>decodeProductSnapshot(s)).toThrow();
  });
 }
 it('rejects substituted generation and provenance',()=>{
  const s=structuredClone(fixture);s.route_explorer.generation++;
  expect(()=>decodeProductSnapshot(s)).toThrow();
  s.route_explorer.generation--;s.route_explorer.source_mode='live';
  expect(()=>decodeProductSnapshot(s)).toThrow();
 });
 it('keeps future observations unknown rather than current',()=>{
  const s=decodeProductSnapshot(fixture).route_explorer!.segments[1];
  expect(effectiveFreshness(s,0,true)).toBe('unknown');
 });
});
