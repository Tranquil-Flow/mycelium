// SPDX-License-Identifier: AGPL-3.0-only
import { useEffect, useMemo, useState } from 'react';
import { useProductEvidence } from '../productEvidence/ProductEvidenceContext';
import { dimensions, effectiveFreshness, type Dimension, type RouteExplorer, type RouteSegment } from './contracts';
import { RegionGlobe } from './RegionGlobe';
import type { GlobeRegion } from './globe';
import './routeExplorer.css';
const labels: Record<Dimension, string> = {edge_kind:'Edge kind',phase:'Phase',path_status:'Path status',role:'Path role',transport:'Transport',freshness:'Freshness'};
const text = (v: unknown) => v === null ? 'unknown' : String(v).replaceAll('_', ' ');
const PAGE = 32;

export function RouteExplorerWorkspace() {
  const evidence = useProductEvidence();
  const [paused, setPaused] = useState<RouteExplorer | null>(null);
  const [lowPower, setLowPower] = useState(false);
  const [now, setNow] = useState(Date.now);
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), lowPower ? 30_000 : 1_000);
    return () => clearInterval(timer);
  }, [lowPower]);
  const current = evidence.visible?.snapshot.route_explorer;
  const projection = paused ?? current;
  return <section className="panel route-explorer" aria-label="Route explorer">
    <h2>Route explorer</h2>
    <p>Read-only route evidence. No geography, inferred traffic, or serving-readiness claim.</p>
    <div className="route-controls">
      <button disabled={!current} aria-pressed={paused !== null} onClick={() => setPaused(paused === null ? current ?? null : null)}>{paused === null ? 'Pause route view' : 'Resume route view'}</button>
      <label><input type="checkbox" checked={lowPower} onChange={e => setLowPower(e.target.checked)} /> Low power (static, bounded refresh)</label>
    </div>
    <p role="status">{paused !== null ? 'Paused historical view · ' : ''}{evidence.latest?.status ?? 'unavailable'} · {evidence.latest?.freshness ?? 'unknown'}. Static graph; motion is never required.</p>
    {projection === undefined ? <p role="status">Route projection unavailable. No route facts inferred from older publications.</p> :
      <RouteExplorerView projection={projection} connected={paused === null && evidence.latest?.status === 'connected' && evidence.latest?.freshness === 'current'} now={now} />}
  </section>;
}

export function RouteExplorerView({projection, connected, now}: {readonly projection: RouteExplorer; readonly connected: boolean; readonly now: number}) {
  const [filters, setFilters] = useState<Partial<Record<Dimension | 'provenance' | 'history', string>>>({});
  const [selected, setSelected] = useState<string | null>(null);
  const [page, setPage] = useState(0);
  const [peerPage, setPeerPage] = useState(0);
  const [globe, setGlobe] = useState(false);
  const [markedRegion, setMarkedRegion] = useState<GlobeRegion | null>(null);
  const safePeerPage = Math.min(peerPage, Math.max(0, Math.ceil(projection.nodes.length / 64) - 1));
  const nodes = useMemo(() => new Map(projection.nodes.map(n => [n.id, n])), [projection.nodes]);
  const peerName = (id: string | null) => id === null ? 'Unknown source' : `${nodes.get(id)?.kind === 'member' ? 'Member' : 'Endpoint'} ${id.slice(7, 19)}`;
  const regionLabel = (id: string | null) => {
    const node = id === null ? undefined : nodes.get(id);
    return node ? `${text(node.region_source)} · ${text(node.region)}` : 'unavailable · unknown';
  };
  const records = projection.segments;
  const filtered = records.filter(s => Object.entries(filters).every(([k, v]) => !v || (k === 'freshness' ? effectiveFreshness(s, now, connected) : k === 'history' ? (s.history ? 'historical' : 'latest') : s[k as keyof RouteSegment]) === v));
  const safePage = Math.min(page, Math.max(0, Math.ceil(filtered.length / PAGE) - 1));
  const visible = filtered.slice(safePage * PAGE, (safePage + 1) * PAGE);
  const detail = records.find(s => s.id === selected);
  const chooseFilter = (key: keyof typeof filters, value: string) => { setFilters(f => ({...f, [key]:value})); setPage(0); };
  const select = (s: RouteSegment) => setSelected(s.id);
  const summary = (s: RouteSegment) => `${peerName(s.source)} → ${peerName(s.destination)} · ${text(s.path_status)} · ${s.transport} · ${text(s.phase)} · ${effectiveFreshness(s, now, connected)} · Path ${text(s.binding.path_generation)} · connection ${text(s.connection_generation)} · ${s.provenance}`;
  return <div className="route-explorer" data-route-generation={projection.generation}>
    <p className="route-boundary">{projection.source_mode === 'fixture' ? 'Local conformance input — not physical route evidence.' : projection.source_mode === 'replay' ? 'Sealed replay — not live route evidence.' : projection.source_mode === 'degraded' ? 'Degraded authority — current evidence unavailable.' : 'Live public source — rendering does not qualify a physical route.'}</p>
    <p>Generation {projection.generation} · {projection.nodes.length} peers · {records.length} retained segments. Unknown regions are valid. Optional replica/recovery/speculation overlays unavailable.</p>
    <p>A transport endpoint is not a membership identity. Unknown source, phase, request and track bindings stay unknown. Selected logical links are not observed traffic.</p>
    <fieldset className="route-filters"><legend>Independent route filters</legend>
      {(Object.keys(dimensions) as Dimension[]).map(k => <label key={k}>{labels[k]}<select aria-label={labels[k]} value={filters[k] ?? ''} onChange={e => chooseFilter(k, e.target.value)}><option value="">All</option>{dimensions[k].map(v => <option key={v} value={v}>{text(v)}</option>)}</select></label>)}
      <label>Provenance<select aria-label="Provenance" value={filters.provenance ?? ''} onChange={e => chooseFilter('provenance',e.target.value)}><option value="">All</option>{['live','fixture','replay','degraded'].map(v => <option key={v}>{v}</option>)}</select></label>
      <label>Timeline<select aria-label="Timeline" value={filters.history ?? ''} onChange={e => chooseFilter('history',e.target.value)}><option value="">All</option><option value="latest">Latest</option><option value="historical">Historical</option></select></label>
      <button onClick={() => {setFilters({});setPage(0);}}>Clear filters</button>
    </fieldset>
    <p role="status">{filtered.length} matching segments · showing {visible.length}. {detail && !filtered.some(s => s.id === detail.id) ? 'Selected segment is outside the filter; detail retained.' : selected !== null && !detail ? 'Selected segment is outside retained authority history.' : ''}</p>
    <div className="route-controls" aria-label="Segment pages"><button disabled={safePage === 0} onClick={() => setPage(safePage - 1)}>Previous segments</button><span>Page {safePage + 1} of {Math.max(1,Math.ceil(filtered.length/PAGE))}</span><button disabled={(safePage+1)*PAGE >= filtered.length} onClick={() => setPage(safePage+1)}>Next segments</button></div>
    <div className="route-table-scroll" tabIndex={0} role="region" aria-label="Scrollable route timeline">
      <table aria-label="Route timeline"><caption>Normative route timeline — arrows indicate direction, never measured traffic</caption>
        <thead><tr>{['Inspect','Direction','Path / edge / phase / role','Transport','Generation / provenance','Freshness / history','Metrics','Bindings / evidence'].map(h => <th scope="col" key={h}>{h}</th>)}</tr></thead>
        <tbody>{visible.map(s => { const index = records.indexOf(s)+1; return <tr key={s.id} data-route-segment={s.id} data-selected={selected === s.id}>
          <th scope="row"><button aria-label={`Inspect segment ${index}`} aria-pressed={selected === s.id} onClick={() => select(s)}>Segment {index}</button></th>
          <td>{peerName(s.source)} → {peerName(s.destination)}</td>
          <td>{text(s.path_status)} / {text(s.edge_kind)} / {text(s.phase)} / {text(s.role)}</td>
          <td>{s.transport}</td><td>Path {text(s.binding.path_generation)} · connection {text(s.connection_generation)} · {s.provenance}</td>
          <td>{effectiveFreshness(s,now,connected)} · {s.history ? 'historical' : 'latest retained'}<br />Issued {text(s.issued_at)} · expires {text(s.expires_at)} (Unix ms)</td>
          <td><details><summary>RTT {text(s.metrics.rtt_ms)} ms · all metrics</summary><dl>{Object.entries(s.metrics).map(([k,v]) => <div key={k}><dt>{text(k)}</dt><dd>{text(v)}</dd></div>)}</dl></details></td>
          <td><details><summary>Complete bindings and provenance</summary><dl>{Object.entries(s.binding).map(([k,v]) => <div key={k}><dt>{text(k)}</dt><dd>{text(v)}</dd></div>)}<dt>Authority digest</dt><dd>{s.authority_digest}</dd><dt>Segment identity</dt><dd>{s.id}</dd><dt>Source region</dt><dd>{regionLabel(s.source)}</dd><dt>Destination region</dt><dd>{regionLabel(s.destination)}</dd></dl></details></td>
        </tr>; })}</tbody>
      </table>
    </div>
    {visible.length === 0 ? <p>No matching route segments. Missing evidence is not a zero measurement.</p> : null}
    <section aria-label="Logical route graph"><h3>Logical route graph</h3><p>Static directed segments; same filter, page and selection as the table. No spatial or geographic inference.</p>
      <ol className="route-graph">{visible.map(s => <li key={s.id} data-graph-segment={s.id}><button aria-label={`Select segment ${records.indexOf(s)+1}: ${summary(s)}`} aria-pressed={selected === s.id} onClick={() => select(s)}><span className="route-vertex">{peerName(s.source)}</span><span className="route-arrow" aria-hidden="true"> → </span><span className="route-vertex">{peerName(s.destination)}</span><small>{text(s.path_status)} · {s.transport} · {text(s.phase)} · {effectiveFreshness(s,now,connected)} · {s.history ? 'historical' : 'latest retained'}<br />Path {text(s.binding.path_generation)} · connection {text(s.connection_generation)} · {s.provenance}</small></button></li>)}</ol>
    </section>
    <section aria-label="Selected route detail"><h3>Selected route detail</h3>{detail ? <><p>{summary(detail)}</p>
      <p>{text(detail.edge_kind)} · {text(detail.role)} · {detail.history ? 'historical' : 'latest retained'} · Issued {text(detail.issued_at)} · expires {text(detail.expires_at)} (Unix ms)</p>
      <dl>{Object.entries(detail.metrics).map(([k,v]) => <div key={k}><dt>{text(k)}</dt><dd>{text(v)}</dd></div>)}
        {Object.entries(detail.binding).map(([k,v]) => <div key={k}><dt>{text(k)}</dt><dd>{text(v)}</dd></div>)}
        <dt>Source region</dt><dd>{regionLabel(detail.source)}</dd><dt>Destination region</dt><dd>{regionLabel(detail.destination)}</dd><dt>Source authority</dt><dd>{detail.authority_digest}</dd><dt>Segment identity</dt><dd>{detail.id}</dd></dl>
    </> : <p>No retained segment selected. Selection only changes this view.</p>}</section>
    <section aria-label="Peer regions and unknown-region peers"><h3>Peer regions and unknown-region peers</h3><p>All {projection.nodes.length} public peers remain inspectable, including unknown-region peers, regardless of route filters. Endpoint and member namespaces are intentionally separate. A declared region does not verify physical location; a reviewed relay region locates only its relay.</p>
      <ul className="route-peers">{projection.nodes.slice(safePeerPage*64,(safePeerPage+1)*64).map(n => <li key={n.id} {...(markedRegion !== null && n.region === markedRegion ? {'data-region-marked': true} : {})}><details><summary>{peerName(n.id)} · {text(n.region)}{markedRegion !== null && n.region === markedRegion ? ' · marked on globe' : ''}</summary><p>{n.id}</p><p>Region source {text(n.region_source)} · {text(n.region)}. No location inferred from transport or network identity.</p></details></li>)}</ul>
      <div className="route-controls"><button disabled={safePeerPage===0} onClick={() => setPeerPage(safePeerPage-1)}>Previous peers</button><span>Peer page {safePeerPage+1}</span><button disabled={(safePeerPage+1)*64>=projection.nodes.length} onClick={() => setPeerPage(safePeerPage+1)}>Next peers</button></div>
    </section>
    <section aria-label="Optional region globe" className="route-globe-toggle"><label><input type="checkbox" checked={globe} onChange={e => { setGlobe(e.target.checked); setMarkedRegion(null); }} /> Show schematic region globe (optional; the peer list stays normative)</label>
      {globe ? <RegionGlobe nodes={projection.nodes} selected={markedRegion} onSelect={setMarkedRegion} /> : null}
    </section>
    <details><summary>Projection provenance and retention</summary><p>Source {projection.source_digest}</p><p>Projection {projection.digest}</p><p>Up to 4096 peers and segments; upstream activation history retains up to 64 observations. Retention is bounded, not a complete lifetime traffic log. Digests bind public bytes; they are not signatures or qualifications. No raw workload export.</p></details>
  </div>;
}
