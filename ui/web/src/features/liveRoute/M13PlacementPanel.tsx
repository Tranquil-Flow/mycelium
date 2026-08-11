import type { M13PlacementProjection } from './routeStatus';
import styles from './LiveRouteWorkspace.module.css';

export function M13PlacementPanel({ placement, view }: { readonly placement: M13PlacementProjection; readonly view: 'plans' | 'network' | 'nodes' | 'readiness' }) {
  return (
    <section className={styles.panel} aria-label={`${view} placement evidence`}>
      <h2>Evidence-driven placement</h2>
      <p><strong>{placement.placement_provenance}</strong> · signed snapshot generation {placement.snapshot_generation} · authority generation {placement.authority_generation}</p>
      <dl className={styles.measurements}>
        <div><dt>Snapshot</dt><dd>{placement.snapshot_digest}</dd></div>
        <div><dt>Evidence bundle</dt><dd>{placement.evidence_bundle_digest}</dd></div>
        <div><dt>Decode / quantization</dt><dd>{placement.decode_mode} · {placement.quantization}</dd></div>
        <div><dt>Valid until</dt><dd>{new Date(placement.valid_until_unix_ms).toLocaleString()}</dd></div>
      </dl>

      {view === 'plans' ? (
        <>
          <h3>DP allocation and calibrated inputs</h3>
          <PlacementTable placement={placement} capacity />
          {placement.ab_deltas.length === 0 ? <p>No physical A/B delta is attached.</p> : <ul>{placement.ab_deltas.map((delta) => <li key={`${delta.kind}-${delta.candidate_snapshot_digest}`}><strong>{delta.kind}</strong> · {delta.changed_input}; allocation {formatAllocation(delta.allocation_before)} → {formatAllocation(delta.allocation_after)}</li>)}</ul>}
          {placement.promotion === null ? <p>Candidate promotion evidence is not attached.</p> : <p><strong>Candidate {placement.promotion.decision}</strong> · {placement.promotion.sample_size} canaries · {placement.promotion.reasons.join(', ') || 'all gates passed'}</p>}
        </>
      ) : null}

      {view === 'network' ? (
        <>
          <h3>Selected order and measured directed edges</h3>
          <PlacementTable placement={placement} />
          <div className={styles.tableWrap}><table><thead><tr><th>Edge</th><th>RTT p95</th><th>Jitter</th><th>Goodput</th></tr></thead><tbody>{placement.links.map((link) => <tr key={`${link.src}-${link.dst}`}><th scope="row">{link.src} → {link.dst}</th><td>{link.rtt_ms.toFixed(1)} ms</td><td>{link.jitter_ms.toFixed(1)} ms</td><td>{(link.bandwidth_Bps * 8 / 1_000_000).toFixed(1)} Mbps</td></tr>)}</tbody></table></div>
        </>
      ) : null}

      {view === 'nodes' ? (
        <><h3>Calibrated capacity and assignment objects</h3><PlacementTable placement={placement} capacity /></>
      ) : null}

      {view === 'readiness' ? (
        <>
          <h3>Assignment and load gates</h3>
          <div className={styles.tableWrap}><table><thead><tr><th>Node</th><th>Profile</th><th>Assignment</th><th>Objects</th><th>Load proof</th><th>Ready</th></tr></thead><tbody>{placement.nodes.map((node) => <tr key={node.node_id}><th scope="row">{node.node_id}</th><td>{node.profile_digest}</td><td>{node.assignment_id}</td><td>{node.assigned_object_count}</td><td>{node.load_proof_digest ?? 'Missing'}</td><td>{node.ready ? 'yes' : 'no'}</td></tr>)}</tbody></table></div>
        </>
      ) : null}

      {placement.exclusions.length > 0 ? <><h3>Excluded candidates</h3><ul>{placement.exclusions.map((item) => <li key={item.node_id}>{item.node_id}: {item.reasons.join(', ')}</li>)}</ul></> : null}
    </section>
  );
}

function formatAllocation(allocation: readonly { readonly node_id: string; readonly start: number; readonly end: number }[]): string {
  return allocation.map((item) => `${item.node_id} [${item.start},${item.end})`).join(' · ');
}

function PlacementTable({ placement, capacity = false }: { readonly placement: M13PlacementProjection; readonly capacity?: boolean }) {
  return <div className={styles.tableWrap}><table><thead><tr><th>Node</th><th>Layers</th><th>Backend</th>{capacity ? <><th>Fast / total</th><th>Prefill / decode</th><th>Profile</th></> : null}</tr></thead><tbody>{placement.nodes.map((node) => <tr key={node.node_id}><th scope="row">{node.node_id}</th><td>[{node.start_layer}, {node.end_layer_exclusive})</td><td>{node.backend} · {node.decode_mode}</td>{capacity ? <><td>{node.fast_allocatable_bytes.toLocaleString()} / {node.total_allocatable_bytes.toLocaleString()}</td><td>{node.prefill_ms_per_layer_token} / {node.decode_ms_per_layer_token} ms/layer-token</td><td>{node.profile_digest}</td></> : null}</tr>)}</tbody></table></div>;
}
