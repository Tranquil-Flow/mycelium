// SPDX-License-Identifier: AGPL-3.0-only
import type { RouteNode } from './contracts';
import { GLOBE_FRAME, cellSlot, globeCells, globeRegions, hemispheres, regionNames, regionShortNames, type GlobeRegion } from './globe';

const peers = (n: number) => `${n} ${n === 1 ? 'peer' : 'peers'}`;

/**
 * Optional, non-normative A14b globe: a synchronized view of the coarse region
 * codes the projection's nodes already publish. It owns no fact, renders counts
 * per coarse region only (no peer identity, no coordinate, no display-cell id),
 * and selection here only marks peers in the normative peer list.
 */
export function RegionGlobe({nodes, selected, onSelect}: {readonly nodes: readonly RouteNode[]; readonly selected: GlobeRegion | null; readonly onSelect: (region: GlobeRegion | null) => void}) {
  const cells = globeCells(nodes);
  const toggle = (region: GlobeRegion) => onSelect(selected === region ? null : region);
  return <section className="region-globe" aria-label="Schematic region globe">
    <h3>Schematic region globe</h3>
    <p>Optional, non-normative view of the coarse regions the peer list already publishes. A cell is schematic, never a position; a declared region is not verified location. Peers with an unknown region stay off-globe.</p>
    <div className="region-globe-pair">
      {hemispheres.map(hemisphere => <div key={hemisphere} className="region-globe-frame">
        <svg viewBox={`0 0 ${GLOBE_FRAME.size} ${GLOBE_FRAME.size}`} aria-hidden="true" focusable="false">
          <path className="region-globe-sphere" d={GLOBE_FRAME[hemisphere].sphere} />
          <path className="region-globe-graticule" d={GLOBE_FRAME[hemisphere].graticule} />
          <path className="region-globe-land" d={GLOBE_FRAME[hemisphere].land} />
          <path className="region-globe-rim" d={GLOBE_FRAME[hemisphere].sphere} />
        </svg>
        {globeRegions.filter(region => cellSlot(region).hemisphere === hemisphere).map(region => {
          const {x, y} = cellSlot(region);
          const count = cells.onGlobe[region];
          return <button key={region} type="button" className="region-globe-cell" data-globe-region={region} data-occupied={count > 0}
            style={{left: `${x}%`, top: `${y}%`}} aria-pressed={selected === region}
            aria-label={`${regionNames[region]}: ${peers(count)}`} onClick={() => toggle(region)}>
            <span aria-hidden="true"><small>{regionShortNames[region]}</small><b data-globe-count>{count}</b></span>
          </button>;
        })}
      </div>)}
    </div>
    <table className="region-globe-table" aria-label="Globe regions (text alternative)">
      <caption>Text alternative: peers per coarse region, as published</caption>
      <thead><tr><th scope="col">Region</th><th scope="col">Peers</th><th scope="col">Peer list</th></tr></thead>
      <tbody>
        {globeRegions.map(region => <tr key={region} data-globe-row={region}>
          <th scope="row">{regionNames[region]}</th><td>{cells.onGlobe[region]}</td>
          <td><button type="button" className="region-globe-mark" aria-pressed={selected === region} aria-label={`${selected === region ? 'Unmark' : 'Mark'} ${regionNames[region]}`} onClick={() => toggle(region)}>{selected === region ? 'Unmark' : 'Mark'}</button></td>
        </tr>)}
        <tr data-globe-row="unknown"><th scope="row">{regionNames.unknown} (off-globe)</th><td>{cells.offGlobe}</td><td>Listed</td></tr>
      </tbody>
    </table>
    <p role="status">{selected === null ? 'No region marked.' : `${regionNames[selected]} marked: ${peers(cells.onGlobe[selected])} highlighted in the peer list.`}</p>
  </section>;
}
