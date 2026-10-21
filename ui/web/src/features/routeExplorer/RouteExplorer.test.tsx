// SPDX-License-Identifier: AGPL-3.0-only
import { fireEvent, render, screen, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { RouteExplorerView } from './RouteExplorer';
import type { RouteExplorer } from './contracts';
const h = (x: string) => `sha256:${x.repeat(64)}`;
const projection: RouteExplorer = {
  protocol: 'mycelium.route_explorer.v1', generation: 1, source_mode: 'fixture', source_digest: h('a'), digest: h('b'), overlays: 'unavailable',
  nodes: [{id:h('c'),kind:'endpoint',region:'unknown',region_source:'unavailable'}],
  segments: [{id:h('d'),source:null,destination:h('c'),binding:{deployment:null,request:null,attempt:null,track:null,path_generation:null},connection_generation:1,issued_at:1,expires_at:2,metrics:{rtt_ms:4,warm_rtt_ms:null,jitter_ms:null,goodput_bytes_per_second:null,loss_ratio:null,sample_count:1,measured_zero:false},authority_digest:h('e'),provenance:'fixture',history:false,edge_kind:'transport_observation',phase:'unknown',path_status:'observed',role:'unknown',transport:'direct',freshness:'current'}],
};
describe('normative route explorer', () => {
  it('keeps unknown bindings, fixture provenance and table/graph selection synchronized', () => {
    render(<RouteExplorerView projection={projection} connected={true} now={1} />);
    expect(screen.getByText(/Local conformance input/)).toBeVisible();
    const table = screen.getByRole('table', {name:'Route timeline'});
    const graph = screen.getByRole('region', {name:'Logical route graph'});
    fireEvent.click(within(table).getByRole('button', {name:'Inspect segment 1'}));
    expect(within(graph).getByRole('button', {name:/Select segment 1/})).toHaveAttribute('aria-pressed','true');
    expect(screen.getByRole('region', {name:'Peer regions and unknown-region peers'})).toHaveTextContent('Endpoint');
  });
  it('filters both presentations, without hiding unknown peers', () => {
    render(<RouteExplorerView projection={projection} connected={true} now={1} />);
    fireEvent.change(screen.getByLabelText('Transport'), {target:{value:'relay'}});
    expect(screen.queryByRole('button', {name:'Inspect segment 1'})).not.toBeInTheDocument();
    expect(screen.queryByRole('button', {name:/Select segment 1/})).not.toBeInTheDocument();
    expect(screen.getByRole('region', {name:'Peer regions and unknown-region peers'})).toHaveTextContent('Endpoint');
  });
  it('expires evidence even without another authority publication', () => {
    render(<RouteExplorerView projection={projection} connected={true} now={3} />);
    expect(screen.getByRole('table', {name:'Route timeline'})).toHaveTextContent('stale');
  });
});

it('retains complete selected bindings and measurements outside the active filter', () => {
  render(<RouteExplorerView projection={projection} connected now={1} />);
  fireEvent.click(screen.getByRole('button', {name:'Inspect segment 1'}));
  fireEvent.change(screen.getByLabelText('Transport'), {target:{value:'relay'}});
  const detail = screen.getByRole('region', {name:'Selected route detail'});
  expect(detail).toHaveTextContent('rtt ms');
  expect(detail).toHaveTextContent('4');
  expect(detail).toHaveTextContent('request');
  expect(detail).toHaveTextContent('unknown');
  expect(detail).toHaveTextContent('fixture');
  expect(detail).toHaveTextContent('connection 1');
});

it('uses only published node region/source labels and never assigns the endpoint a member region', () => {
  const endpoint = {...projection.nodes[0]};
  const member = {id:h('f'), kind:'member' as const, region:'europe' as const, region_source:'owner_declared' as const};
  const withRegion = {...projection, nodes:[endpoint,member], segments:[{...projection.segments[0],source:member.id}]};
  render(<RouteExplorerView projection={withRegion} connected now={1} />);
  expect(screen.getByRole('table', {name:'Route timeline'})).toHaveTextContent('owner declared');
  const peers = screen.getByRole('region', {name:'Peer regions and unknown-region peers'});
  expect(peers).toHaveTextContent('europe');
  expect(peers).toHaveTextContent('owner declared');
  expect(peers).toHaveTextContent('unavailable');
  expect(peers).toHaveTextContent('unknown');
});

it('clamps peer pagination when a new generation shrinks the roster', () => {
  const many = {...projection, nodes:Array.from({length:65},(_,i)=>({...projection.nodes[0],id:`sha256:${i.toString(16).padStart(64,'0')}`}))};
  const {rerender} = render(<RouteExplorerView projection={many} connected now={1} />);
  fireEvent.click(screen.getByRole('button',{name:'Next peers'}));
  rerender(<RouteExplorerView projection={projection} connected now={1} />);
  expect(screen.getByRole('region',{name:'Peer regions and unknown-region peers'})).toHaveTextContent('Endpoint');
  expect(screen.getByRole('region',{name:'Peer regions and unknown-region peers'})).toHaveTextContent('Peer page 1');
});

it('exposes path and connection generations in the graph as well as the table', () => {
  render(<RouteExplorerView projection={projection} connected now={1} />);
  expect(screen.getByRole('region',{name:'Logical route graph'})).toHaveTextContent('Path unknown');
  expect(screen.getByRole('region',{name:'Logical route graph'})).toHaveTextContent('connection 1');
});
