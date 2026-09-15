import {describe,it,expect} from 'vitest';
import fixture from '../../test/a14Conformance.json';
import {decodeProductSnapshot} from '../productEvidence/contracts';
import {effectiveFreshness} from './contracts';
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
