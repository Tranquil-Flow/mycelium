import { useEffect, useMemo, useState } from 'react';
import type { ObservatoryAdapterQualification } from '../../data/observatoryEventProjection';
import {
  HttpLiveRouteStatusClient,
  type LiveRouteStatus,
  type LiveRouteStatusClient,
} from './routeStatus';
import styles from './LiveRouteWorkspace.module.css';
import { M13PlacementPanel } from './M13PlacementPanel';
import { M14TopologyPanel } from './M14TopologyPanel';
import { M15WorkloadPanel } from './M15WorkloadPanel';
import {
  HttpM15ComparisonClient,
  type M15ComparisonClient,
  type M15PlanComparison,
} from './m15Comparison';
import { M16RuntimePanel } from './M16RuntimePanel';
import { HttpM16RuntimeClient, type M16RuntimeClient, type M16RuntimeStatus } from './m16Runtime';
import { M17ModelOperationPanel } from './M17ModelOperationPanel';
import {
  HttpM17ModelOperationClient,
  type M17ModelOperation,
  type M17ModelOperationClient,
} from './m17ModelOperation';
import { M18ReplicationSourcePanel } from './M18ReplicationSourcePanel';
import { M19RecoverySourcePanel } from './M19RecoverySourcePanel';
import { M20SpeculationSourcePanel } from './M20SpeculationSourcePanel';
import { M21HeterogeneousSourcePanel } from './M21HeterogeneousSourcePanel';
import { M22ReleaseSourcePanel } from './M22ReleaseSourcePanel';
import { M23KvSourcePanel } from './M23KvSourcePanel';

export interface LiveRouteWorkspaceProps {
  readonly view: 'network' | 'plans' | 'readiness' | 'incidents';
  readonly qualification: ObservatoryAdapterQualification | null;
  readonly freshness: 'current' | 'stale';
  readonly client?: LiveRouteStatusClient;
  readonly workloadClient?: M15ComparisonClient;
  readonly runtimeClient?: M16RuntimeClient;
  readonly modelOperationClient?: M17ModelOperationClient;
}

function metric(value: number | null, suffix = ' ms'): string {
  return value === null ? 'Unknown' : `${value.toFixed(1)}${suffix}`;
}

function bytes(value: number): string {
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KiB`;
  return `${(value / (1024 * 1024)).toFixed(1)} MiB`;
}

export function LiveRouteWorkspace({ view, qualification, freshness, client, workloadClient, runtimeClient, modelOperationClient }: LiveRouteWorkspaceProps) {
  const defaultClient = useMemo(() => new HttpLiveRouteStatusClient(), []);
  const defaultWorkloadClient = useMemo(() => new HttpM15ComparisonClient(), []);
  const defaultRuntimeClient = useMemo(() => new HttpM16RuntimeClient(), []);
  const defaultModelOperationClient = useMemo(() => new HttpM17ModelOperationClient(), []);
  const source = client ?? defaultClient;
  const workloadSource = workloadClient ?? defaultWorkloadClient;
  const runtimeSource = runtimeClient ?? defaultRuntimeClient;
  const modelOperationSource = modelOperationClient ?? defaultModelOperationClient;
  const [status, setStatus] = useState<LiveRouteStatus | null>(null);
  const [workloadComparison, setWorkloadComparison] = useState<M15PlanComparison | null>(null);
  const [workloadUnavailable, setWorkloadUnavailable] = useState(false);
  const [runtime, setRuntime] = useState<M16RuntimeStatus | null>(null);
  const [runtimeError, setRuntimeError] = useState<string | null>(null);
  const [modelOperation, setModelOperation] = useState<M17ModelOperation | null>(null);
  const [error, setError] = useState<string | null>(null);

  const refresh = async () => {
    try {
      setStatus(await source.load());
      setError(null);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'route_status_unavailable');
    }
  };

  useEffect(() => {
    let mounted = true;
    const load = async () => {
      try {
        const value = await source.load();
        if (!mounted) return;
        setStatus(value);
        setError(null);
      } catch (reason) {
        if (!mounted) return;
        setError(reason instanceof Error ? reason.message : 'route_status_unavailable');
      }
    };
    void load();
    const timer = window.setInterval(() => void load(), 1_000);
    return () => {
      mounted = false;
      window.clearInterval(timer);
    };
  }, [source]);

  useEffect(() => {
    if (view !== 'plans') return;
    void workloadSource.load().then((comparison) => {
      setWorkloadComparison(comparison);
      setWorkloadUnavailable(false);
    }).catch(() => {
      setWorkloadComparison(null);
      setWorkloadUnavailable(true);
    });
  }, [view, workloadSource]);

  useEffect(() => {
    let mounted = true;
    let controller: AbortController | null = null;
    const load = () => {
      if (controller !== null) return;
      const requestController = new AbortController();
      controller = requestController;
      void runtimeSource.load(requestController.signal).then((value) => {
        if (!mounted) return;
        setRuntime(value);
        setRuntimeError(null);
      }).catch((reason) => {
        if (!mounted || requestController.signal.aborted) return;
        setRuntime(null);
        setRuntimeError(reason instanceof Error ? reason.message : 'm16_runtime_unavailable');
      }).finally(() => {
        if (controller === requestController) controller = null;
      });
    };
    load();
    const timer = window.setInterval(load, 1_000);
    return () => {
      mounted = false;
      controller?.abort();
      window.clearInterval(timer);
    };
  }, [runtimeSource]);

  useEffect(() => {
    if (view !== 'plans' && view !== 'readiness' && view !== 'incidents') return;
    const controller = new AbortController();
    void modelOperationSource.load(controller.signal)
      .then(setModelOperation)
      .catch(() => setModelOperation(null));
    return () => controller.abort();
  }, [modelOperationSource, view]);

  if (status === null) {
    return (
      <section className={styles.workspace} aria-label={`Live ${view} evidence`}>
        <p className={styles.eyebrow}>Qualifier-bound live route</p>
        <h1>{view === 'readiness' ? 'Readiness' : view === 'network' ? 'Network' : view === 'incidents' ? 'Incidents' : 'Plans'}</h1>
        <p role={error === null ? 'status' : 'alert'}>{error ?? 'Loading physical route evidence…'}</p>
        <button type="button" onClick={() => void refresh()}>Refresh</button>
      </section>
    );
  }

  const latest = status.recent_inferences.at(-1) ?? null;
  const title = view === 'readiness' ? 'Readiness' : view === 'network' ? 'Network' : view === 'incidents' ? 'Incidents' : 'Plans';
  return (
    <section className={styles.workspace} aria-label={`Live ${view} evidence`}>
      <header className={styles.header}>
        <div><p className={styles.eyebrow}>Qualifier-bound live route</p><h1>{title}</h1></div>
        <div className={styles.actions}>
          <strong>{status.route_alive && !status.simulated ? 'Physical route live' : 'Route unavailable'}</strong>
          <button type="button" onClick={() => void refresh()}>Refresh evidence</button>
        </div>
      </header>

      <dl className={styles.summary}>
        <div><dt>Model</dt><dd>{status.model_id}</dd></div>
        <div><dt>Deployment</dt><dd>{status.deployment_id}</dd></div>
        <div><dt>Topology</dt><dd>v{status.topology_version} · {status.peers.length} peers</dd></div>
        <div><dt>Decode</dt><dd>{status.decode_mode}</dd></div>
        <div><dt>Evidence</dt><dd>{freshness}</dd></div>
        <div><dt>Route identity</dt><dd>{status.route_identity_digest ?? 'Unavailable'}</dd></div>
      </dl>
      {runtime === null && runtimeError !== null ? (
        <p role="alert">M16 runtime evidence unavailable: {runtimeError}</p>
      ) : null}

      {view === 'network' ? (
        <>
          <M22ReleaseSourcePanel view="network" hideUnavailable />
          <M21HeterogeneousSourcePanel view="network" hideUnavailable />
          <M19RecoverySourcePanel view="network" hideUnavailable />
          <M18ReplicationSourcePanel view="network" hideUnavailable />
          {runtime === null ? null : <M16RuntimePanel runtime={runtime} view="network" />}
          {status.topology === null ? null : <M14TopologyPanel topology={status.topology} view="network" />}
          {status.placement === null ? null : <M13PlacementPanel placement={status.placement} view="network" />}
          <section className={styles.panel} aria-labelledby="live-pipeline-title">
            <h2 id="live-pipeline-title">Ordered physical pipeline</h2>
            <div className={styles.pipeline}>
              {status.stages.map((stage, index) => (
                <article key={stage.placement_id}>
                  <span>Stage {index + 1}</span>
                  <strong>{stage.node_id}</strong>
                  <small>layers [{stage.start_layer}, {stage.end_layer_exclusive}) · {stage.runtime_backend}</small>
                </article>
              ))}
            </div>
          </section>
          <PeerTable status={status} />
        </>
      ) : null}

      {view === 'plans' ? <M22ReleaseSourcePanel view="plans" hideUnavailable /> : null}
      {view === 'plans' ? <M23KvSourcePanel view="plans" hideUnavailable /> : null}
      {view === 'plans' ? (
        <><M21HeterogeneousSourcePanel view="plans" hideUnavailable /><M20SpeculationSourcePanel view="plans" hideUnavailable /><M19RecoverySourcePanel view="plans" hideUnavailable /><M18ReplicationSourcePanel view="plans" hideUnavailable />{modelOperation === null ? null : <M17ModelOperationPanel operation={modelOperation} view="plans" />}{runtime === null ? null : <M16RuntimePanel runtime={runtime} view="plans" />}{workloadComparison === null ? (workloadUnavailable ? <section className={styles.panel}><h2>Workload-aware comparison unavailable</h2><p>M15 policy evidence is not attached to this deployment. Existing physical measurements remain valid.</p></section> : null) : <M15WorkloadPanel comparison={workloadComparison} />}{status.topology === null ? null : <M14TopologyPanel topology={status.topology} view="plans" />}{status.placement === null ? null : <M13PlacementPanel placement={status.placement} view="plans" />}<section className={styles.panel} aria-labelledby="live-plan-title">
          <h2 id="live-plan-title">Qualified deployment measurement</h2>
          <p>This is observed physical execution, not a modeled alternative.</p>
          <dl className={styles.measurements}>
            <div><dt>Context</dt><dd>{latest?.context_tokens ?? 'Unknown'} tokens</dd></div>
            <div><dt>Output</dt><dd>{latest?.output_tokens ?? 'Unknown'} tokens</dd></div>
            <div><dt>Prefill</dt><dd>{metric(latest?.prefill_ms ?? null)}</dd></div>
            <div><dt>TTFT</dt><dd>{metric(latest?.ttft_ms ?? null)}</dd></div>
            <div><dt>TPOT</dt><dd>{metric(latest?.tpot_ms ?? null)}</dd></div>
            <div><dt>Total</dt><dd>{metric(latest?.total_ms ?? null)}</dd></div>
          </dl>
        </section></>
      ) : null}

      {view === 'readiness' ? (
        <>
          <M22ReleaseSourcePanel view="readiness" hideUnavailable />
          <M23KvSourcePanel view="readiness" hideUnavailable />
          <M21HeterogeneousSourcePanel view="readiness" hideUnavailable />
          <M20SpeculationSourcePanel view="readiness" hideUnavailable />
          <M19RecoverySourcePanel view="readiness" hideUnavailable />
          <M18ReplicationSourcePanel view="readiness" hideUnavailable />
          {modelOperation === null ? null : <M17ModelOperationPanel operation={modelOperation} view="readiness" />}
          {runtime === null ? null : <M16RuntimePanel runtime={runtime} view="readiness" />}
          {status.topology === null ? null : <M14TopologyPanel topology={status.topology} view="readiness" />}
          {status.placement === null ? null : <M13PlacementPanel placement={status.placement} view="readiness" />}
          <section className={styles.panel} aria-labelledby="live-readiness-title">
            <h2 id="live-readiness-title">Physical qualification</h2>
            <dl className={styles.measurements}>
              <div><dt>Qualifier decision</dt><dd>{qualification?.route_ready ? 'Accepted' : 'Not accepted'}</dd></div>
              <div><dt>Evidence class</dt><dd>{qualification?.evidence_class ?? 'Unavailable'}</dd></div>
              <div><dt>Stage-load proofs</dt><dd>{qualification?.binding.stage_load_proof_digests.length ?? 0}</dd></div>
              <div><dt>Fatal route state</dt><dd>{status.counters.fatal ?? 'None'}</dd></div>
              <div><dt>Frames sent / received</dt><dd>{status.counters.frames_sent} / {status.counters.frames_received}</dd></div>
              <div><dt>Applied operations</dt><dd>{status.counters.applied_operation_count}</dd></div>
            </dl>
          </section>
          <PeerTable status={status} />
        </>
      ) : null}

      {view === 'incidents' ? <M22ReleaseSourcePanel view="incidents" hideUnavailable /> : null}
      {view === 'incidents' ? <M23KvSourcePanel view="incidents" hideUnavailable /> : null}
      {view === 'incidents' ? (
        <><M19RecoverySourcePanel view="incidents" hideUnavailable /><M18ReplicationSourcePanel view="incidents" hideUnavailable />{modelOperation === null ? null : <M17ModelOperationPanel operation={modelOperation} view="incidents" />}{runtime === null ? null : <M16RuntimePanel runtime={runtime} view="incidents" />}<section className={styles.panel} aria-labelledby="live-incidents-title">
          <h2 id="live-incidents-title">Physical route incident log</h2>
          {status.counters.fatal === null && status.incidents.length === 0 ? (
            <p>No active physical route incident. All projected peers remain on the qualified topology.</p>
          ) : status.counters.fatal !== null ? (
            <article role="alert">
              <strong>Physical route failed closed</strong>
              <p>Public failure code: <code>{status.counters.fatal}</code></p>
              <p>New work remains blocked until the complete topology is rebuilt and requalified.</p>
            </article>
          ) : null}
          {status.incidents.length > 0 ? (
            <ol aria-label="Observed physical route incidents">
              {status.incidents.map((incident) => (
                <li key={incident.incident_id}>
                  <strong>{incident.state.replaceAll('_', ' ')}</strong>
                  <span> · {incident.deployment_id}</span>
                  <p>
                    {incident.reason}
                    {incident.request_id === null ? '' : ` · request ${incident.request_id}`}
                  </p>
                </li>
              ))}
            </ol>
          ) : null}
        </section></>
      ) : null}
    </section>
  );
}

function PeerTable({ status }: { readonly status: LiveRouteStatus }) {
  const latest = status.recent_inferences.at(-1);
  return (
    <section className={styles.panel} aria-labelledby="live-peer-title">
      <h2 id="live-peer-title">Per-host execution evidence</h2>
      <div className={styles.tableWrap}>
        <table>
          <thead><tr><th>Peer</th><th>Backend / layers</th><th>Total sent</th><th>Total received</th><th>Operations</th><th>Latest Δ sent / received / ops</th><th>Incremental KV</th></tr></thead>
          <tbody>
            {status.peers.map((peer) => {
              const delta = latest?.peer_counter_deltas.find((item) => item.node_id === peer.node_id);
              const ranges = peer.placements.map((placement) => `${placement.runtime_backend} [${placement.start_layer},${placement.end_layer_exclusive})`).join(', ');
              return (
                <tr key={peer.node_id}>
                  <th scope="row">{peer.node_id}</th>
                  <td>{ranges}</td>
                  <td>{peer.frames_sent}</td>
                  <td>{peer.frames_received}</td>
                  <td>{peer.applied_operation_count}</td>
                  <td>{delta ? `${delta.frames_sent} / ${delta.frames_received} / ${delta.applied_operation_count}` : 'Unknown'}</td>
                  <td>
                    {peer.decode_mode ?? 'Unknown'} · {peer.active_kv_state_count} active · {bytes(peer.active_kv_bytes)}
                    <small>
                      {peer.architecture ?? 'unknown architecture'} · position {peer.current_position ?? 'released'} · peak {bytes(peer.peak_kv_bytes)} · {peer.release_state}{peer.last_release_reason === null ? '' : ` (${peer.last_release_reason})`}
                      {' · '}decode work {peer.decode_input_token_count} input tokens / {peer.decode_operation_count} operations · {bytes(peer.activation_output_bytes)} activations
                    </small>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </section>
  );
}
