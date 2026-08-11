import type { M18ReplicaPlan, M18ReplicaRuntime } from './m18Replication';
import styles from './LiveRouteWorkspace.module.css';

export type M18ReplicationView = 'plans' | 'network' | 'nodes' | 'readiness' | 'incidents' | 'inference';

function short(value: string): string {
  return value.startsWith('sha256:') ? `${value.slice(0, 15)}…` : value;
}

export function M18ReplicationPanel({ plan, runtime, view }: { readonly plan: M18ReplicaPlan; readonly runtime: M18ReplicaRuntime | null; readonly view: M18ReplicationView }) {
  const accepted = plan.candidate_decisions.filter((item) => item.accepted);
  const rejected = plan.candidate_decisions.filter((item) => !item.accepted);
  const latestRequests = runtime?.requests.slice(-8) ?? [];
  return <section className={styles.panel} aria-label={`M18 ${view} replicated throughput evidence`}>
    <div className={styles.panelTitlebar}><div><p className={styles.eyebrow}>M18 · replicated throughput</p><h2>{view === 'plans' ? 'Capability-aware replica plan' : view === 'network' ? 'Complete legal request tracks' : view === 'nodes' ? 'Replica placements by node' : view === 'readiness' ? 'Replica qualification lifecycle' : view === 'incidents' ? 'Replica degradation evidence' : 'Immutable request-track attribution'}</h2></div><span className={styles.evidenceBadge}>data parallel</span></div>
    <p>Requests are distributed across complete tracks. A single request is never tensor-split across replicas, and its KV remains pinned to its admitted track.</p>
    {view === 'plans' ? <>
      <dl className={styles.measurements}>
        <div><dt>Primary capacity</dt><dd>{plan.flow.primary_capacity_rps.toFixed(3)} req/s</dd></div>
        <div><dt>Replicated prediction</dt><dd>{plan.flow.replicated_capacity_rps.toFixed(3)} req/s</dd></div>
        <div><dt>Predicted gain</dt><dd>{plan.flow.predicted_gain_rps.toFixed(3)} req/s</dd></div>
        <div><dt>Measured gain</dt><dd>{runtime?.throughput === null || runtime?.throughput === undefined ? 'not attached' : `${(runtime.throughput.gain_fraction * 100).toFixed(1)}%`}</dd></div>
        <div><dt>Accepted candidates</dt><dd>{accepted.length}</dd></div>
        <div><dt>Rejected candidates</dt><dd>{rejected.length}</dd></div>
        <div><dt>Planner evidence</dt><dd>generation {plan.evidence.generation} · {short(plan.evidence.evidence_digest)}</dd></div>
      </dl>
      <div className={styles.tableWrap}><table><thead><tr><th>Candidate</th><th>Group / node</th><th>Decision</th><th>Robust gain / required</th><th>Failure domain</th></tr></thead><tbody>{plan.candidate_decisions.map((item, index) => <tr key={`${item.placement_id}-${index}`}><th scope="row">{item.placement_id}</th><td>{item.replica_group_id} · {item.node_id}</td><td>{item.accepted ? 'accepted' : item.reason.replaceAll('_', ' ')}</td><td>{item.robust_gain_rps.toFixed(3)} / {item.minimum_required_gain_rps.toFixed(3)} req/s</td><td>{item.failure_domain_warning?.replaceAll('_', ' ') ?? item.failure_domain}</td></tr>)}</tbody></table></div>
    </> : null}
    {view === 'network' ? <div className={styles.tableWrap}><table><thead><tr><th>Track</th><th>Ordered placements</th><th>Traffic</th><th>Cost</th></tr></thead><tbody>{plan.tracks.map((track) => <tr key={track.track_id}><th scope="row">{track.planner_track_id}<br/><small>{short(track.track_id)}</small></th><td>{track.placement_ids.join(' → ')}</td><td>{(track.traffic_fraction * 100).toFixed(1)}%</td><td>{track.cost_ms.toFixed(2)} ms</td></tr>)}</tbody></table></div> : null}
    {view === 'nodes' ? <div className={styles.tableWrap}><table><thead><tr><th>Node</th><th>Placement</th><th>Group / layers</th><th>Role</th><th>Capacity</th></tr></thead><tbody>{plan.placements.map((placement) => <tr key={placement.placement_id}><th scope="row">{placement.node_id}</th><td>{placement.placement_id}</td><td>{placement.replica_group_id} · [{placement.layer_range.start}, {placement.layer_range.end})</td><td>{placement.primary ? 'primary' : 'replica'}</td><td>{placement.service_capacity_rps.toFixed(3)} req/s</td></tr>)}</tbody></table></div> : null}
    {view === 'readiness' ? <>
      {runtime === null ? <p><strong>Planner intent only.</strong> No Router/Qualifier replica runtime has been attached, so the replica plan is not promoted.</p> : <div className={styles.tableWrap}><table><thead><tr><th>Qualified track</th><th>Placements</th><th>Admission</th><th>Active requests</th><th>Qualification</th></tr></thead><tbody>{runtime.qualified_tracks.map((track) => <tr key={track.track_id}><th scope="row">{short(track.track_id)}</th><td>{track.placement_ids.join(' → ')}</td><td>{track.admission_state}</td><td>{track.active_request_count}</td><td>{track.qualification_id}</td></tr>)}</tbody></table></div>}
      <p>Planner route-ready claim: <strong>no</strong>. Runtime promotion requires independent physical qualification and measured material gain.</p>
      {runtime?.throughput ? <p><strong>Physical throughput:</strong> {runtime.throughput.baseline_throughput_rps.toFixed(3)} → {runtime.throughput.replicated_throughput_rps.toFixed(3)} req/s across {runtime.throughput.replicated_request_count} flow-weighted requests; gate {runtime.throughput.passed ? 'passed' : 'failed'}.</p> : null}
    </> : null}
    {view === 'incidents' ? runtime === null || runtime.incidents.length === 0 ? <p>No replica runtime incident is attached. This does not turn Planner intent into a qualified route.</p> : <ol>{runtime.incidents.map((incident) => <li key={incident.incident_id}><strong>{incident.kind.replaceAll('_', ' ')}</strong> · {short(incident.track_id)} · {incident.reason}; recovery claimed: no</li>)}</ol> : null}
    {view === 'inference' ? latestRequests.length === 0 ? <p>No retained M18 request-track binding yet.</p> : <div className={styles.tableWrap}><table><thead><tr><th>Request</th><th>Track</th><th>Immutable placement sequence</th><th>Phase</th><th>KV</th></tr></thead><tbody>{latestRequests.map((request) => <tr key={request.request_id}><th scope="row">{request.request_id}</th><td>{short(request.track_id)}</td><td>{request.placement_ids.join(' → ')}</td><td>{request.terminal_state ?? request.phase}</td><td>track pinned</td></tr>)}</tbody></table></div> : null}
    <p><small>{plan.claim_boundary}</small></p>
  </section>;
}
