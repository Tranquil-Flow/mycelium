import { useEffect, useMemo, useState } from 'react';
import {
  HttpLiveRouteStatusClient,
  type LiveRouteStatus,
  type LiveRouteStatusClient,
} from './routeStatus';
import styles from './LiveRouteWorkspace.module.css';
import { A5ReplicaTrackPanel, type A5ReplicaTrackView } from './A5ReplicaTrackPanel';

export type ConcurrencyWorkspace =
  | 'inference'
  | 'lab'
  | 'network'
  | 'nodes'
  | 'plans'
  | 'readiness'
  | 'incidents'
  | 'settings';

function readiness(status: LiveRouteStatus) {
  const qualification = status.concurrency_liveness_qualification;
  return [
    ['Cooperative interruption', qualification.cooperative_interruption_proven],
    ['Exact request cleanup', qualification.request_scoped_cleanup_proven],
    ['Publisher generation fencing', qualification.publisher_generation_fencing_proven],
    ['Scoped liveness', qualification.scoped_liveness_proven],
    ['Shared-process termination avoided', !qualification.shared_process_termination_used],
  ] as const;
}

export function ConcurrencyLivenessProjection({
  status,
  view,
  nowUnixMs = Date.now(),
}: {
  readonly status: LiveRouteStatus;
  readonly view: ConcurrencyWorkspace;
  readonly nowUnixMs?: number;
}) {
  const qualification = status.concurrency_liveness_qualification;
  const candidatePeers = status.peers.filter(
    (peer) => peer.interruptibility?.cooperative_bound_candidate === true,
  );
  const replicaView: A5ReplicaTrackView = view === 'readiness' || view === 'settings'
    ? 'qualification'
    : view === 'incidents'
      ? 'loss'
      : 'tracks';
  return (
    <>
    <A5ReplicaTrackPanel
      qualifications={status.replica_track_qualification}
      lossPlacementIds={status.replica_loss_placement_ids}
      nowUnixMs={nowUnixMs}
      view={replicaView}
    />
    <section className={styles.panel} aria-label="Concurrent execution and scoped liveness">
      <div className={styles.panelTitlebar}>
        <div>
          <p className={styles.eyebrow}>Concurrent execution</p>
          <h2>Request isolation and route liveness</h2>
        </div>
        <span className={styles.evidenceBadge}>
          {qualification.eligible ? 'Physically qualified' : 'Qualification pending'}
        </span>
      </div>
      <p>
        One request can be interrupted without terminating the shared node. Interruption,
        exact cleanup, backend release, and terminal publication share one {qualification.cancellation_and_cleanup_bound_ms.toLocaleString()} ms limit.
      </p>

      {view === 'inference' ? (
        <p role="status">
          Maximum qualified concurrency: {qualification.maximum_concurrent_requests}. New requests remain unavailable for this capability until every proof below is physically sealed.
        </p>
      ) : null}

      {view === 'lab' || view === 'nodes' ? (
        <div className={styles.tableWrap}><table>
          <thead><tr><th>Node</th><th>Backend</th><th>Decode mode</th><th>Cancellation unit</th><th>Longest observed unit</th><th>Candidate</th><th>Active KV</th></tr></thead>
          <tbody>{status.peers.map((peer) => <tr key={peer.node_id}>
            <th scope="row">{peer.node_id}</th>
            <td>{peer.interruptibility?.runtime_backend ?? 'Unknown'}</td>
            <td>{peer.interruptibility?.decode_mode ?? peer.decode_mode ?? 'Unknown'}</td>
            <td>{peer.interruptibility?.work_unit === 'transformer_layer' ? 'Transformer layer' : 'Unproven'}</td>
            <td>{peer.interruptibility?.maximum_observed_work_unit_ms === null || peer.interruptibility === null ? 'Not measured' : `${peer.interruptibility.maximum_observed_work_unit_ms.toLocaleString()} ms (${peer.interruptibility.observed_work_unit_count.toLocaleString()} samples)`}</td>
            <td>{peer.interruptibility?.cooperative_bound_candidate ? 'Eligible for physical proof' : peer.interruptibility?.backend_candidate ? 'Awaiting measured work' : 'Ineligible'}</td>
            <td>{peer.active_kv_state_count}</td>
          </tr>)}</tbody>
        </table></div>
      ) : null}

      {view === 'network' ? (
        <div className={styles.tableWrap}><table>
          <thead><tr><th>Directed subject</th><th>Kind</th><th>Generation</th><th>State</th><th>Observation</th><th>Misses</th></tr></thead>
          <tbody>{status.liveness.subjects.map((subject) => <tr key={`${subject.kind}:${subject.subject_id}`}>
            <th scope="row">{subject.subject_id}</th><td>{subject.kind}</td><td>{subject.membership_generation}</td><td>{subject.state}</td><td>{subject.last_source.replaceAll('_', ' ')}</td><td>{subject.consecutive_misses}</td>
          </tr>)}</tbody>
        </table>{status.liveness.subjects.length === 0 ? <p>No directed liveness subjects have been observed yet.</p> : null}</div>
      ) : null}

      {view === 'plans' ? (
        <p>
          Immutable current paths retain exact request, attempt, path digest, topology, command,
          cancellation, and publisher generations. Affected requests terminate explicitly;
          successor replay and recovery are deferred to separately qualified capabilities.
        </p>
      ) : null}

      {view === 'readiness' || view === 'settings' ? (
        <dl className={styles.measurements}>
          {readiness(status).map(([label, proven]) => <div key={label}><dt>{label}</dt><dd>{proven ? 'Proven' : 'Not yet proven'}</dd></div>)}
          <div><dt>Concurrent requests</dt><dd>{qualification.maximum_concurrent_requests}</dd></div>
          <div><dt>Total cancellation limit</dt><dd>{qualification.cancellation_and_cleanup_bound_ms.toLocaleString()} ms</dd></div>
        </dl>
      ) : null}

      {view === 'settings' ? (
        <p>These are qualified generation bounds. Any future policy edit applies only to newly admitted request generations and cannot mutate an active path.</p>
      ) : null}

      {view === 'incidents' ? (
        status.liveness.incidents.length === 0
          ? <p>No scoped liveness incidents are retained.</p>
          : <ul>{status.liveness.incidents.map((incident) => <li key={incident.sequence}>
              {incident.source.replaceAll('_', ' ')} · {incident.scope} · {incident.subject_id} · {incident.outcome}
              {incident.detection_latency_ms === null ? '' : ` · ${incident.detection_latency_ms} ms detection`}
              {incident.affected_track_ids.length === 0 ? '' : ` · ${incident.affected_track_ids.length} affected request track(s)`}
            </li>)}</ul>
      ) : null}

      {view === 'lab' ? <p>{candidatePeers.length} of {status.peers.length} physical peers advertise a bounded-work-unit backend candidate. Advertising is not qualification.</p> : null}
      {status.liveness.deployment_fatal_reason === null ? null : <p role="alert">Deployment fatal: {status.liveness.deployment_fatal_reason}</p>}
    </section>
    </>
  );
}

export function ConcurrencyLivenessSource({
  view,
  client,
}: {
  readonly view: ConcurrencyWorkspace;
  readonly client?: LiveRouteStatusClient;
}) {
  const defaultClient = useMemo(() => new HttpLiveRouteStatusClient(), []);
  const source = client ?? defaultClient;
  const [status, setStatus] = useState<LiveRouteStatus | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let mounted = true;
    let inFlight = false;
    setStatus(null);
    setError(null);
    const load = () => {
      if (inFlight) return;
      inFlight = true;
      void source.load().then((value) => {
        if (!mounted) return;
        setStatus(value);
        setError(null);
      }).catch((reason) => {
        if (!mounted) return;
        setStatus(null);
        setError(reason instanceof Error ? reason.message : 'concurrency_liveness_unavailable');
      }).finally(() => {
        inFlight = false;
      });
    };
    load();
    const timer = window.setInterval(load, 1_000);
    return () => {
      mounted = false;
      window.clearInterval(timer);
    };
  }, [source]);

  if (status !== null) return <ConcurrencyLivenessProjection status={status} view={view} />;
  return <section className={styles.panel} aria-label="Concurrent execution and scoped liveness"><h2>Request isolation and route liveness</h2><p role={error === null ? 'status' : 'alert'}>{error ?? 'Loading concurrency and liveness evidence…'}</p></section>;
}
