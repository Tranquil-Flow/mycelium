import { useMemo, useState } from 'react';
import type { M14TopologyEdge, M14TopologyProjection } from './routeStatus';
import styles from './LiveRouteWorkspace.module.css';

type TopologyView = 'ring' | 'scc' | 'elastic' | 'map';

function edgeCost(edge: M14TopologyEdge): number {
  return edge.rtt_ms / 2 + edge.jitter_ms;
}

function edgeName(edge: M14TopologyEdge): string {
  return `${edge.src} → ${edge.dst}`;
}

function positions(nodes: readonly string[], view: TopologyView): ReadonlyMap<string, { x: number; y: number }> {
  const entries = nodes.map((node, index) => {
    if (view === 'map') return [node, { x: 90 + index * (360 / Math.max(nodes.length - 1, 1)), y: 160 }] as const;
    if (view === 'elastic') return [node, { x: 80 + index * (380 / Math.max(nodes.length - 1, 1)), y: index % 2 === 0 ? 105 : 205 }] as const;
    const radius = view === 'scc' ? 92 : 122;
    const angle = -Math.PI / 2 + index * Math.PI * 2 / nodes.length;
    return [node, { x: 270 + Math.cos(angle) * radius, y: 160 + Math.sin(angle) * radius }] as const;
  });
  return new Map(entries);
}

export function M14TopologyPanel({ topology, view }: { readonly topology: M14TopologyProjection; readonly view: 'plans' | 'network' | 'nodes' | 'readiness' }) {
  const [layout, setLayout] = useState<TopologyView>('ring');
  const [selectedKey, setSelectedKey] = useState(() => {
    const preferred = topology.edges.find((edge) => edge.logical_role === 'decode_loopback') ?? topology.edges[0];
    return preferred === undefined ? '' : `${preferred.src}\u0000${preferred.dst}`;
  });
  const selected = topology.edges.find((edge) => `${edge.src}\u0000${edge.dst}` === selectedKey) ?? topology.edges[0];
  const opened = topology.decision.opened_order;
  const nodePositions = useMemo(() => positions(opened, layout), [layout, opened]);

  return (
    <section className={styles.panel} aria-label={`${view} measured topology evidence`}>
      <div className={styles.panelTitlebar}>
        <div><p className={styles.eyebrow}>Measured network topology</p><h2>Directed activation path intelligence</h2></div>
        <span className={styles.evidenceBadge}>{topology.measurement_source.replaceAll('_', ' ')}</span>
      </div>
      <p>
        <strong>{topology.decision.globally_exact ? 'Globally exact' : 'Bounded heuristic'}</strong>
        {' · '}{topology.decision.explored_candidates} candidate cycles · selected cost {topology.decision.selected_cost_ms.toFixed(3)} ms
      </p>

      {view === 'network' ? (
        <>
          <div className={styles.layoutTabs} role="group" aria-label="Topology layout">
            {([['ring', 'Ring'], ['scc', 'SCC'], ['elastic', 'Elastic geo'], ['map', 'True map']] as const).map(([key, label]) => (
              <button key={key} type="button" aria-pressed={layout === key} onClick={() => setLayout(key)}>{label}</button>
            ))}
          </div>
          {layout === 'map' ? <p className={styles.unknownGeometry}>Verified coordinates are unavailable; all peers remain in the explicit unknown-location bucket.</p> : null}
          <TopologyDiagram topology={topology} positions={nodePositions} mapMode={layout === 'map'} selectedKey={selectedKey} onSelect={setSelectedKey} />
          {selected === undefined ? null : <EdgeInspector edge={selected} />}
        </>
      ) : null}

      {view === 'plans' ? (
        <>
          <h3>Measured cycle candidates</h3>
          <div className={styles.tableWrap}><table><thead><tr><th>Candidate</th><th>Directed cost</th><th>Result</th><th>Reason</th></tr></thead><tbody>{topology.decision.candidates.map((candidate) => <tr key={candidate.order.join('→')} className={candidate.selected ? styles.selectedRow : undefined}><th scope="row">{candidate.order.join(' → ')} → {candidate.order[0]}</th><td>{candidate.cost_ms.toFixed(3)} ms</td><td>{candidate.selected ? 'Selected' : 'Eligible'}</td><td>{candidate.selected ? 'Minimum measured cost' : (candidate.rejection_reason ?? 'Higher measured cost')}</td></tr>)}</tbody></table></div>
          <p><strong>Opened pipeline:</strong> {opened.join(' → ')}; sampled-token closure {topology.decision.loopback.src} → {topology.decision.loopback.dst}.</p>
          <p><strong>Winning rationale:</strong> {topology.decision.winning_rationale}</p>
          <h3>Nested contiguous layer allocation</h3>
          <div className={styles.pipeline}>{topology.allocation.map((allocation, index) => <article key={allocation.node_id}><span>Stage {index + 1}</span><strong>{allocation.node_id}</strong><small>layers [{allocation.start}, {allocation.end})</small></article>)}</div>
        </>
      ) : null}

      {view === 'nodes' ? (
        <>
          <h3>Physical peers and persistent connections</h3>
          <div className={styles.tableWrap}><table><thead><tr><th>Node</th><th>Allocated layers</th><th>Outbound observations</th><th>Persistent reuse</th></tr></thead><tbody>{topology.allocation.map((allocation) => {
            const outbound = topology.edges.filter((edge) => edge.src === allocation.node_id);
            return <tr key={allocation.node_id}><th scope="row">{allocation.node_id}</th><td>[{allocation.start}, {allocation.end})</td><td>{outbound.length} / {opened.length - 1}</td><td>{outbound.reduce((sum, edge) => sum + edge.frames_sent, 0)} frames / {outbound.reduce((sum, edge) => sum + edge.connections_opened, 0)} connections</td></tr>;
          })}</tbody></table></div>
        </>
      ) : null}

      {view === 'readiness' ? (
        <>
          <h3>Topology acceptance gates</h3>
          <dl className={styles.measurements}>
            <div><dt>Directed matrix</dt><dd>{topology.edges.length} / {opened.length * (opened.length - 1)} edges</dd></div>
            <div><dt>Resolved paths</dt><dd>Complete</dd></div>
            <div><dt>Reusable connections</dt><dd>{topology.edges.every((edge) => edge.frames_sent > edge.connections_opened) ? 'Proven' : 'Not proven'}</dd></div>
            <div><dt>Forward rails</dt><dd>{topology.edges.filter((edge) => edge.logical_role === 'forward').length}</dd></div>
            <div><dt>Physical loopback</dt><dd>{topology.decision.loopback.src} → {topology.decision.loopback.dst}</dd></div>
            <div><dt>Promotion</dt><dd>{topology.promotion?.decision ?? 'Candidate evidence only'}</dd></div>
          </dl>
          {topology.exclusions.length === 0 ? <p>No topology exclusions are attached.</p> : <ul>{topology.exclusions.map((exclusion) => <li key={exclusion}>{exclusion}</li>)}</ul>}
        </>
      ) : null}
    </section>
  );
}

function TopologyDiagram({ topology, positions: layout, mapMode, selectedKey, onSelect }: {
  readonly topology: M14TopologyProjection;
  readonly positions: ReadonlyMap<string, { x: number; y: number }>;
  readonly mapMode: boolean;
  readonly selectedKey: string;
  readonly onSelect: (key: string) => void;
}) {
  return <svg className={styles.topologyDiagram} viewBox="0 0 540 320" role="img" aria-label="Complete measured directed topology">
    <defs><marker id="m14-arrow" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto"><path d="M0,0 L8,4 L0,8 z" /></marker></defs>
    {mapMode ? <rect x="38" y="90" width="464" height="140" rx="18" className={styles.unknownBucket} /> : null}
    {topology.edges.map((edge) => {
      const src = layout.get(edge.src); const dst = layout.get(edge.dst);
      if (src === undefined || dst === undefined) return null;
      const key = `${edge.src}\u0000${edge.dst}`;
      const mx = (src.x + dst.x) / 2; const my = (src.y + dst.y) / 2;
      const dx = dst.x - src.x; const dy = dst.y - src.y;
      const length = Math.max(Math.hypot(dx, dy), 1); const curve = mapMode ? 16 : 13;
      const cx = mx - dy / length * curve; const cy = my + dx / length * curve;
      return <path key={key} d={`M ${src.x} ${src.y} Q ${cx} ${cy} ${dst.x} ${dst.y}`} markerEnd="url(#m14-arrow)" tabIndex={0} role="button" aria-label={`${edgeName(edge)}, ${edge.logical_role}`} onClick={() => onSelect(key)} onKeyDown={(event) => { if (event.key === 'Enter' || event.key === ' ') onSelect(key); }} className={`${styles.topologyEdge} ${styles[edge.logical_role]} ${selectedKey === key ? styles.selectedEdge : ''}`} />;
    })}
    {[...layout].map(([node, point]) => <g key={node}><circle cx={point.x} cy={point.y} r="31" className={styles.topologyNode} /><text x={point.x} y={point.y + 4} textAnchor="middle">{node}</text></g>)}
  </svg>;
}

function EdgeInspector({ edge }: { readonly edge: M14TopologyEdge }) {
  return <article className={styles.edgeInspector} aria-label={`Edge inspector ${edgeName(edge)}`}>
    <h3>{edgeName(edge)} · {edge.logical_role.replaceAll('_', ' ')}</h3>
    <p><code>RTT / 2 + jitter = {edge.rtt_ms.toFixed(3)} / 2 + {edge.jitter_ms.toFixed(3)} = {edgeCost(edge).toFixed(3)} ms</code></p>
    <dl className={styles.measurements}>
      <div><dt>Selected path</dt><dd>{edge.path_class}{edge.relay_region === null ? '' : ` · ${edge.relay_region}`}</dd></div>
      <div><dt>Goodput</dt><dd>{(edge.goodput_Bps * 8 / 1_000_000).toFixed(2)} Mbps</dd></div>
      <div><dt>Samples / loss</dt><dd>{edge.sample_count} / {(edge.loss_ratio * 100).toFixed(2)}%</dd></div>
      <div><dt>Connection reuse</dt><dd>{edge.frames_sent} frames / {edge.connections_opened} opened</dd></div>
      <div><dt>Generation</dt><dd>{edge.connection_generation}</dd></div>
      <div><dt>Fresh until</dt><dd>{new Date(edge.fresh_until_unix_ms).toLocaleString()}</dd></div>
    </dl>
    <small>Measured terms: selected-path RTT, jitter, goodput, loss, frame and connection counters. Observation {edge.observation_digest}.</small>
  </article>;
}
