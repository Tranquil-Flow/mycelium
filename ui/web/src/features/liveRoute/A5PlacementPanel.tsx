import type { LiveRouteStatus } from './routeStatus';
import styles from './LiveRouteWorkspace.module.css';

/** Observation-only: membership and node activity never establish placement work. */
export function A5PlacementPanel({ status, nowUnixMs }: {
  readonly status: LiveRouteStatus;
  readonly nowUnixMs: number;
}) {
  const placements = status.peers.flatMap((peer) => peer.placements.map((placement) => ({ peer, placement })));
  return <section className={styles.panel} aria-label="Placement work and qualification"
    data-deployment-id={status.deployment_id} data-topology-version={status.topology_version}>
    <h2>Placement work and qualification</h2>
    <p>Observed cumulative operations and active KV belong to the named placement. They are not per-request frame evidence. Missing observations remain unknown.</p>
    <div className={styles.tableWrap}><table>
      <thead><tr><th>Placement</th><th>Node</th><th>Stage</th><th>Layer range (end exclusive)</th><th>Replica group</th><th>Artifact proof</th><th>Load proof</th><th>Qualification</th><th>Prefill operations</th><th>Decode operations</th><th>Active KV states</th><th>Active KV bytes</th><th>Health</th></tr></thead>
      <tbody>{placements.map(({ peer, placement }) => {
        const observation = peer.placement_counters?.[placement.placement_id];
        const qualifications = status.replica_track_qualification.filter((item) => item.placement_id === placement.placement_id);
        const lost = status.replica_loss_placement_ids.includes(placement.placement_id);
        return <tr key={`${peer.node_id}:${placement.placement_id}`} data-placement-id={placement.placement_id}>
          <th scope="row">{placement.placement_id}</th><td>{peer.node_id}</td><td>{placement.stage_id}</td>
          <td>{placement.start_layer}–{placement.end_layer_exclusive}</td>
          <td>{qualifications.length ? [...new Set(qualifications.map((item) => item.replica_group_id))].join(', ') : 'Unknown'}</td>
          <td>{qualifications.length ? [...new Set(qualifications.map((item) => item.artifact_verification_digest))].join(', ') : 'Unknown'}</td>
          <td>{qualifications.length ? [...new Set(qualifications.map((item) => item.load_proof_digest))].join(', ') : 'Unknown'}</td>
          <td>{qualifications.length ? qualifications.map((item) => <div key={item.qualification_id}>
            {item.qualification_id} · generation {item.qualifier_generation} · {item.expires_at_unix_ms <= nowUnixMs ? 'Expired' : item.placement_ids.some((id) => status.replica_loss_placement_ids.includes(id)) ? 'Lost' : !status.route_alive ? 'Unavailable' : item.route_ready ? 'Qualified' : 'Rejected'}
          </div>) : 'Unknown'}</td>
          <td data-counter="prefill_operation_count">{observation?.prefill_operation_count ?? 'Unknown'}</td>
          <td data-counter="decode_operation_count">{observation?.decode_operation_count ?? 'Unknown'}</td>
          <td data-counter="active_state_count">{observation?.active_state_count ?? 'Unknown'}</td>
          <td data-counter="active_kv_bytes">{observation?.active_kv_bytes ?? 'Unknown'}</td>
          <td>{lost ? 'Placement lost' : !status.route_alive ? 'Route unavailable' : !peer.data_plane_health_observed ? 'Unknown' : peer.transport_fatal ? 'Node transport failed' : peer.transport_running === true ? 'Node transport running' : 'Node transport unavailable'}</td>
        </tr>;
      })}</tbody>
    </table></div>
    {placements.length === 0 ? <p>No placement observations available.</p> : null}
  </section>;
}
