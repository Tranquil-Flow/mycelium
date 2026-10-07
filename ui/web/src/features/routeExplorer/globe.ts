import { geoGraticule10, geoOrthographic, geoPath } from 'd3-geo';
import { regionCodes, type RegionCode, type RouteNode } from './contracts';

// A14b display cells (spec 2026-08-18-mycelium-a14b-coarse-region-globe.md §7).
// The region-to-cell mapping is a closed product constant mirrored from
// `mycelium_product_spine.coarse_region.DISPLAY_CELLS`; `unknown` has no cell and
// stays off-globe. A cell is schematic product geometry, never a position: it is
// not part of any contract payload and is never rendered into the DOM.
export type GlobeRegion = Exclude<RegionCode, 'unknown'>;
export const DISPLAY_CELLS: Readonly<Record<GlobeRegion, string>> = Object.freeze({
  africa: 'cell_africa',
  americas_north: 'cell_americas_north',
  americas_south: 'cell_americas_south',
  asia: 'cell_asia',
  europe: 'cell_europe',
  oceania: 'cell_oceania',
  polar: 'cell_polar',
});
export const globeRegions = regionCodes.filter((code): code is GlobeRegion => code !== 'unknown');

// Fixed schematic slots, in percent of the globe frame, keyed by display cell.
// These are screen-layout constants owned by the product, not coordinates of
// anything: they never depend on any peer, evidence record or projection.
const CELL_SLOTS: Readonly<Record<string, readonly [number, number]>> = Object.freeze({
  cell_polar: [50, 12],
  cell_americas_north: [22, 34],
  cell_americas_south: [30, 70],
  cell_europe: [52, 33],
  cell_africa: [51, 63],
  cell_asia: [77, 40],
  cell_oceania: [76, 75],
});
export function cellSlot(region: GlobeRegion): readonly [number, number] {
  return CELL_SLOTS[DISPLAY_CELLS[region]];
}

export const regionNames: Readonly<Record<RegionCode, string>> = Object.freeze({
  africa: 'Africa',
  americas_north: 'North America',
  americas_south: 'South America',
  asia: 'Asia',
  europe: 'Europe',
  oceania: 'Oceania',
  polar: 'Polar',
  unknown: 'Unknown region',
});

// Short visible labels for the schematic cells; the accessible name uses regionNames.
export const regionShortNames: Readonly<Record<GlobeRegion, string>> = Object.freeze({
  africa: 'Africa',
  americas_north: 'N. Amer.',
  americas_south: 'S. Amer.',
  asia: 'Asia',
  europe: 'Europe',
  oceania: 'Oceania',
  polar: 'Polar',
});

export interface GlobeCells {
  readonly onGlobe: Readonly<Record<GlobeRegion, number>>;
  readonly offGlobe: number;
}

/** Pure count of published peers per coarse region code; reads only node.region. */
export function globeCells(nodes: readonly RouteNode[]): GlobeCells {
  const onGlobe = Object.fromEntries(globeRegions.map(code => [code, 0])) as Record<GlobeRegion, number>;
  let offGlobe = 0;
  for (const node of nodes) {
    if (node.region === 'unknown' || !Object.hasOwn(DISPLAY_CELLS, node.region)) offGlobe += 1;
    else onGlobe[node.region as GlobeRegion] += 1;
  }
  return { onGlobe, offGlobe };
}

// Decorative frame only: a fixed orthographic sphere and graticule. The same two
// paths are drawn for every input; nothing about a peer reaches this geometry.
const GLOBE_SIZE = 240;
const projection = geoOrthographic().scale(GLOBE_SIZE / 2 - 6).translate([GLOBE_SIZE / 2, GLOBE_SIZE / 2]).rotate([-10, -18]).precision(0.5);
const path = geoPath(projection);
const round = (d: string | null) => (d ?? '').replace(/\d+\.\d+/g, n => Number(n).toFixed(1));
export const GLOBE_FRAME = Object.freeze({
  size: GLOBE_SIZE,
  sphere: round(path({ type: 'Sphere' })),
  graticule: round(path(geoGraticule10())),
});
