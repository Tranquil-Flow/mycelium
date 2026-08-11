import type { M16RuntimeStatus } from './m16Runtime';
import styles from './LiveRouteWorkspace.module.css';

export function M16RuntimePanel({ runtime, view }: { readonly runtime: M16RuntimeStatus; readonly view: 'network' | 'plans' | 'readiness' | 'incidents' | 'nodes' }) {
  if (view === 'incidents') {
    return <section className={styles.panel} aria-label="Admission incidents"><h2>Admission and cleanup incidents</h2>{runtime.incidents.length === 0 ? <p>No admission, backpressure, expiry, or cleanup incident recorded.</p> : <ol>{runtime.incidents.map((incident) => <li key={incident.incident_id}><strong>{incident.kind.replaceAll('_', ' ')}</strong> · {incident.state} · {incident.request_id}</li>)}</ol>}</section>;
  }
  return (
    <section className={styles.panel} aria-label={`${view} runtime control`}>
      <div className={styles.panelTitlebar}><div><p className={styles.eyebrow}>Resource admission and scheduling</p><h2>{view === 'plans' ? 'Bounded workload scheduler' : view === 'network' ? 'Pinned path reservations' : view === 'nodes' ? 'Per-placement resource ledger' : 'Runtime admission readiness'}</h2></div><span className={styles.evidenceBadge}>{runtime.batch_state.mode.replaceAll('_', ' ')}</span></div>
      <dl className={styles.measurements}>
        <div><dt>Queue</dt><dd>{runtime.queue.depth} / {runtime.queue.maximum_items}</dd></div>
        <div><dt>Interactive / batch</dt><dd>{runtime.queue.interactive_depth} / {runtime.queue.batch_depth}</dd></div>
        <div><dt>Active request</dt><dd>{runtime.queue.active_request_id ?? 'None'}</dd></div>
        <div><dt>Topology pin</dt><dd>v{runtime.topology_version}</dd></div>
      </dl>
      <div className={styles.tableWrap}><table><thead><tr><th>Placement</th><th>Node</th><th>Memory free</th><th>KV free</th><th>Workspace free</th><th>Reservations</th></tr></thead><tbody>{runtime.placements.map((placement) => <tr key={placement.placement_id}><th scope="row">{placement.placement_id}</th><td>{placement.node_id}</td><td>{placement.free_memory_bytes.toLocaleString()}</td><td>{placement.free_kv_bytes.toLocaleString()}</td><td>{placement.free_workspace_bytes.toLocaleString()}</td><td>{placement.active_reservations} / {placement.maximum_reservations}</td></tr>)}</tbody></table></div>
      {runtime.requests.length === 0 ? <p>No retained request path yet.</p> : <div className={styles.tableWrap}><table><thead><tr><th>Request</th><th>Phase</th><th>Progressive candidate</th><th>Locked decode path</th><th>Reservations</th></tr></thead><tbody>{runtime.requests.slice(-8).map((request) => <tr key={request.request_id}><th scope="row">{request.request_id}</th><td>{request.phase}</td><td>{request.candidate_placement_ids.join(' → ')}</td><td>{request.placement_ids.join(' → ')} · v{request.topology_version}</td><td>{request.reservation_count}</td></tr>)}</tbody></table></div>}
      <p>{runtime.claim_boundary}</p>
      {runtime.performance_budgets.length === 0 ? <p>Concurrent physical budget evidence pending.</p> : runtime.performance_budgets.map((budget) => <p key={budget.budget_id}><strong>{budget.overall_state.replaceAll('_', ' ')}</strong> · {budget.observed_request_count} observed requests · {budget.dimensions.filter((dimension) => dimension.state === 'approved_exclusion').length} approved exclusions</p>)}
    </section>
  );
}
