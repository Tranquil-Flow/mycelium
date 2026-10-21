// SPDX-License-Identifier: AGPL-3.0-only
import { fireEvent, render, screen, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import fixture from '../../test/a14bRegionConformance.json';
import { decodeProductSnapshot } from '../productEvidence/contracts';
import { RouteExplorerView } from './RouteExplorer';
import { DISPLAY_CELLS, GLOBE_FRAME, cellSlot, globeCells, globeRegions } from './globe';
import { regionCodes, type RouteExplorer, type RouteNode } from './contracts';

// A14b §7: the globe is an optional, synchronized, non-normative view over the
// node regions the A14a projection already publishes. These cases run over the
// backend-published conformance bytes (scripts/generate_a14b_region_conformance.py).
const published = (): RouteExplorer => decodeProductSnapshot(structuredClone(fixture)).route_explorer!;
const h = (x: string) => `sha256:${x.repeat(64)}`;
const node = (id: string, region: RouteNode['region'], region_source: RouteNode['region_source'] = region === 'unknown' ? 'unavailable' : 'owner_declared'): RouteNode =>
  ({id, kind: 'member', region, region_source});
const globeToggle = () => screen.getByRole('checkbox', {name: /Show schematic region globe/});
// Everything the A14a view renders, i.e. the explorer minus the optional globe section.
const a14aFacts = (root: HTMLElement) => {
  const clone = root.cloneNode(true) as HTMLElement;
  clone.querySelector('[aria-label="Optional region globe"]')?.remove();
  return clone.innerHTML;
};

describe('A14b display cells', () => {
  it('mirror the closed product constant: one schematic cell per non-unknown code, no digit', () => {
    expect(Object.keys(DISPLAY_CELLS).sort()).toEqual(regionCodes.filter(c => c !== 'unknown').sort());
    for (const [code, cell] of Object.entries(DISPLAY_CELLS)) expect(cell).toBe(`cell_${code}`);
    expect(JSON.stringify(DISPLAY_CELLS)).not.toMatch(/\d/);
    expect(Object.hasOwn(DISPLAY_CELLS, 'unknown')).toBe(false);
  });

  it('places each cell at a fixed slot that no input can move, and keeps unknown off-globe', () => {
    const slots = globeRegions.map(cellSlot);
    expect(new Set(slots.map(s => `${s.hemisphere}:${s.x}:${s.y}`)).size).toBe(globeRegions.length);
    for (const slot of slots) expect(Math.hypot(slot.x - 50, slot.y - 50)).toBeLessThan(48);
    const counts = globeCells([node(h('1'), 'europe'), node(h('2'), 'unknown'), node(h('3'), 'unknown', 'latency_distance_class')]);
    expect(counts.onGlobe.europe).toBe(1);
    expect(counts.offGlobe).toBe(2);
    expect(Object.values(counts.onGlobe).reduce((a, b) => a + b, 0) + counts.offGlobe).toBe(3);
    expect(globeRegions.map(cellSlot)).toEqual(slots);
  });

  it('anchors each cell over its continent on a fixed hemisphere', () => {
    const at = (region: Parameters<typeof cellSlot>[0]) => cellSlot(region);
    expect(at('americas_north').hemisphere).toBe('west');
    expect(at('americas_south').hemisphere).toBe('west');
    for (const region of ['europe', 'africa', 'asia', 'oceania'] as const) expect(at(region).hemisphere).toBe('east');
    expect(at('americas_south').y).toBeGreaterThan(at('americas_north').y);
    expect(at('africa').y).toBeGreaterThan(at('europe').y);
    expect(at('asia').x).toBeGreaterThan(at('europe').x);
    expect(at('oceania').y).toBeGreaterThan(at('asia').y);
    expect(at('polar').y).toBeLessThan(at('americas_north').y);
  });

  it('draws the same decorative frame, land outlines included, for every input', () => {
    for (const hemisphere of ['west', 'east'] as const) {
      expect(GLOBE_FRAME[hemisphere].sphere.length).toBeGreaterThan(0);
      expect(GLOBE_FRAME[hemisphere].graticule.length).toBeGreaterThan(0);
      expect(GLOBE_FRAME[hemisphere].land.length).toBeGreaterThan(1000);
    }
    expect(GLOBE_FRAME.west.land).not.toBe(GLOBE_FRAME.east.land);
    const frame = (projection: RouteExplorer) => {
      const {container, unmount} = render(<RouteExplorerView projection={projection} connected now={1} />);
      fireEvent.click(globeToggle());
      const svgs = [...container.querySelectorAll('[aria-label="Schematic region globe"] svg')].map(svg => svg.outerHTML);
      unmount();
      return svgs;
    };
    const allUnknown: RouteExplorer = {...published(), regions: undefined, nodes: published().nodes.map(n => ({...n, region: 'unknown' as const, region_source: 'unavailable' as const}))};
    const drawn = frame(published());
    expect(drawn).toHaveLength(2);
    expect(frame(allUnknown)).toEqual(drawn);
  });
});

describe('A14b optional globe in the route explorer', () => {
  it('is off by default, so the A14a view renders exactly as before', () => {
    const {container} = render(<RouteExplorerView projection={published()} connected now={1} />);
    expect(globeToggle()).not.toBeChecked();
    expect(screen.queryByRole('region', {name: 'Schematic region globe'})).not.toBeInTheDocument();
    expect(screen.getByRole('table', {name: 'Route timeline'})).toBeVisible();
    expect(screen.getByRole('region', {name: 'Peer regions and unknown-region peers'})).toBeVisible();
    expect(container.querySelector('[data-globe-region]')).toBeNull();
  });

  it('loses no A14a fact when enabled, and enabling then disabling restores the identical view', () => {
    const {container} = render(<RouteExplorerView projection={published()} connected now={1} />);
    const root = container.firstElementChild as HTMLElement;
    const before = a14aFacts(root);
    fireEvent.click(globeToggle());
    expect(screen.getByRole('region', {name: 'Schematic region globe'})).toBeVisible();
    expect(a14aFacts(root)).toBe(before);
    fireEvent.click(globeToggle());
    expect(screen.queryByRole('region', {name: 'Schematic region globe'})).not.toBeInTheDocument();
    expect(a14aFacts(root)).toBe(before);
  });

  it('shows exactly the published region counts, with unknown peers off-globe', () => {
    const projection = published();
    render(<RouteExplorerView projection={projection} connected now={1} />);
    fireEvent.click(globeToggle());
    const globe = screen.getByRole('region', {name: 'Schematic region globe'});
    for (const region of globeRegions) {
      const expected = projection.nodes.filter(n => n.region === region).length;
      expect(globe.querySelector(`button[data-globe-region="${region}"] [data-globe-count]`)?.textContent).toBe(String(expected));
      expect(within(globe).getByRole('table').querySelector(`[data-globe-row="${region}"] td`)?.textContent).toBe(String(expected));
    }
    const unknown = projection.nodes.filter(n => n.region === 'unknown').length;
    expect(unknown).toBeGreaterThan(0);
    expect(globe.querySelector('[data-globe-row="unknown"] td')?.textContent).toBe(String(unknown));
    expect(globe.querySelector('[data-globe-region="unknown"]')).toBeNull();
  });

  it('renders no identity, coordinate, cell id or provider name', () => {
    const projection = published();
    const {container} = render(<RouteExplorerView projection={projection} connected now={1} />);
    fireEvent.click(globeToggle());
    const globe = container.querySelector('[aria-label="Schematic region globe"]') as HTMLElement;
    const html = globe.outerHTML;
    for (const n of projection.nodes) {
      expect(html).not.toContain(n.id);
      expect(html).not.toContain(n.id.slice(7, 19));
    }
    expect(html).not.toMatch(/cell_|latitude|longitude|geohash|endpoint_id|relay_url|ip_address|us-east/i);
    expect(html).not.toMatch(/\b(?:\d{1,3}\.){3}\d{1,3}\b/);
    for (const text of globe.querySelectorAll('*')) {
      for (const attr of text.getAttributeNames()) expect(attr).not.toMatch(/lat|lon|coord|cell/i);
    }
  });

  it('keeps globe cells and the text alternative synchronized with the peer list (keyboard-operable buttons)', () => {
    const projection = published();
    render(<RouteExplorerView projection={projection} connected now={1} />);
    fireEvent.click(globeToggle());
    const globe = screen.getByRole('region', {name: 'Schematic region globe'});
    const peers = screen.getByRole('region', {name: 'Peer regions and unknown-region peers'});
    const europe = within(globe).getByRole('button', {name: /^Europe: \d+ peers?$/});
    expect(europe.tagName).toBe('BUTTON');
    fireEvent.click(europe);
    expect(europe).toHaveAttribute('aria-pressed', 'true');
    expect(within(globe).getByRole('button', {name: 'Unmark Europe'})).toHaveAttribute('aria-pressed', 'true');
    const marked = peers.querySelectorAll('[data-region-marked]');
    expect(marked).toHaveLength(projection.nodes.filter(n => n.region === 'europe').length);
    expect(peers).toHaveTextContent('marked on globe');
    fireEvent.click(within(globe).getByRole('button', {name: 'Unmark Europe'}));
    expect(peers.querySelectorAll('[data-region-marked]')).toHaveLength(0);
    expect(europe).toHaveAttribute('aria-pressed', 'false');
  });

  it('shows an A14a-era projection with every peer off-globe', () => {
    const projection: RouteExplorer = {...published(), regions: undefined, nodes: published().nodes.map(n => ({...n, region: 'unknown' as const, region_source: 'unavailable' as const}))};
    render(<RouteExplorerView projection={projection} connected now={1} />);
    fireEvent.click(globeToggle());
    const globe = screen.getByRole('region', {name: 'Schematic region globe'});
    for (const region of globeRegions) expect(globe.querySelector(`button[data-globe-region="${region}"] [data-globe-count]`)?.textContent).toBe('0');
    expect(globe.querySelector('[data-globe-row="unknown"] td')?.textContent).toBe(String(projection.nodes.length));
  });

  it('clears the marked region when the globe is disabled', () => {
    render(<RouteExplorerView projection={published()} connected now={1} />);
    fireEvent.click(globeToggle());
    fireEvent.click(screen.getByRole('button', {name: /^Asia: /}));
    expect(screen.getByRole('region', {name: 'Peer regions and unknown-region peers'}).querySelectorAll('[data-region-marked]').length).toBeGreaterThan(0);
    fireEvent.click(globeToggle());
    expect(screen.getByRole('region', {name: 'Peer regions and unknown-region peers'}).querySelectorAll('[data-region-marked]')).toHaveLength(0);
  });
});
