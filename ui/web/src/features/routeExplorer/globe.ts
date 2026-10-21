// SPDX-License-Identifier: AGPL-3.0-only
import { geoGraticule10, geoOrthographic, geoPath } from 'd3-geo';
import { feature } from 'topojson-client';
import type { GeometryCollection, Topology } from 'topojson-specification';
import landTopology from 'world-atlas/land-110m.json';
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

// Fixed product anchors, keyed by display cell: a rough continent centre on one
// of two fixed orthographic hemispheres ([degrees east, degrees north]). They are
// layout constants owned by the product, so Asia sits over Asia; they never depend
// on any peer, evidence record or projection, and only the derived screen slot
// (percent of the hemisphere frame) reaches the DOM.
export type Hemisphere = 'west' | 'east';
export const hemispheres: readonly Hemisphere[] = ['west', 'east'];
const CELL_ANCHORS: Readonly<Record<string, readonly [Hemisphere, number, number]>> = Object.freeze({
  cell_polar: ['west', -40, 76],
  cell_americas_north: ['west', -100, 42],
  cell_americas_south: ['west', -60, -14],
  cell_europe: ['east', 18, 50],
  cell_africa: ['east', 20, 4],
  cell_asia: ['east', 95, 42],
  cell_oceania: ['east', 132, -24],
});
const GLOBE_SIZE = 240;
const ROTATIONS: Readonly<Record<Hemisphere, [number, number]>> = Object.freeze({west: [80, -12], east: [-65, -12]});
const projectionFor = (hemisphere: Hemisphere) =>
  geoOrthographic().scale(GLOBE_SIZE / 2 - 4).translate([GLOBE_SIZE / 2, GLOBE_SIZE / 2]).rotate(ROTATIONS[hemisphere]).clipAngle(90).precision(0.5);

export interface CellSlot { readonly hemisphere: Hemisphere; readonly x: number; readonly y: number }
const SLOTS: Readonly<Record<string, CellSlot>> = Object.freeze(Object.fromEntries(Object.entries(CELL_ANCHORS).map(([cell, [hemisphere, east, north]]) => {
  const [px, py] = projectionFor(hemisphere)([east, north]) ?? [GLOBE_SIZE / 2, GLOBE_SIZE / 2];
  // Keep a whole cell inside its hemisphere frame at phone width.
  const slot = (v: number) => Math.min(86, Math.max(14, Math.round(v / GLOBE_SIZE * 1000) / 10));
  return [cell, Object.freeze({hemisphere, x: slot(px), y: slot(py)})];
})));
export function cellSlot(region: GlobeRegion): CellSlot {
  return SLOTS[DISPLAY_CELLS[region]];
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

// Decorative frame only: two fixed orthographic hemispheres with a graticule and
// low-resolution land outlines (Natural Earth 1:110m via world-atlas, bundled
// statically, never fetched at runtime). The same paths are drawn for every
// input; nothing about a peer reaches this geometry.
const land = feature(landTopology as unknown as Topology<{land: GeometryCollection}>, landTopology.objects.land as unknown as GeometryCollection);
export interface HemisphereFrame { readonly sphere: string; readonly graticule: string; readonly land: string }
const frameFor = (hemisphere: Hemisphere): HemisphereFrame => {
  const path = geoPath(projectionFor(hemisphere)).digits(1);
  return Object.freeze({sphere: path({type: 'Sphere'}) ?? '', graticule: path(geoGraticule10()) ?? '', land: path(land) ?? ''});
};
export const GLOBE_FRAME = Object.freeze({
  size: GLOBE_SIZE,
  west: frameFor('west'),
  east: frameFor('east'),
});
