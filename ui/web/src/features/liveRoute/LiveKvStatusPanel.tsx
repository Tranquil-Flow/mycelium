import { useEffect, useMemo, useState } from 'react';
import {
  HttpLiveRouteStatusClient,
  type LiveRouteStatus,
  type LiveRouteStatusClient,
} from './routeStatus';
import styles from './LiveRouteWorkspace.module.css';

export interface LiveKvStatusPanelProps {
  readonly view: 'inference' | 'nodes';
  readonly freshness: 'current' | 'stale' | 'fixture' | 'replay';
  readonly client?: LiveRouteStatusClient;
}

function bytes(value: number): string {
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KiB`;
  return `${(value / (1024 * 1024)).toFixed(1)} MiB`;
}

export function LiveKvStatusPanel({ view, freshness, client }: LiveKvStatusPanelProps) {
  const defaultClient = useMemo(() => new HttpLiveRouteStatusClient(), []);
  const source = client ?? defaultClient;
  const [status, setStatus] = useState<LiveRouteStatus | null>(null);
  const [error, setError] = useState<string | null>(null);

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
        setStatus(null);
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

  if (status === null) {
    return (
      <section className={styles.panel} aria-label="Distributed decode status">
        <h2>Distributed decode status</h2>
        <p role={error === null ? 'status' : 'alert'}>
          {error === null ? 'Loading live placement state…' : `Unavailable · ${error}`}
        </p>
      </section>
    );
  }

  const unsafe = freshness !== 'current' || !status.route_alive || status.simulated || status.counters.fatal !== null;
  const activePeers = status.peers.filter((peer) => peer.release_state === 'active').length;
  const decoding = status.peers.some((peer) => peer.release_state === 'active' && peer.decode_operation_count > 0);
  const phase = activePeers === 0 ? 'released / idle' : decoding ? 'incremental decode' : 'prefill / first token';
  const positions = status.peers
    .filter((peer) => peer.current_position !== null)
    .map((peer) => `${peer.node_id}: ${peer.current_position}`)
    .join(', ');
  return (
    <section className={styles.panel} aria-label="Distributed decode status">
      <div className={styles.panelTitlebar}>
        <div>
          <p className={styles.eyebrow}>M23 stage-local KV</p>
          <h2>Distributed decode status</h2>
        </div>
        <strong className={styles.evidenceBadge}>
          {unsafe ? 'Not qualified for new work' : `${status.decode_mode} live`}
        </strong>
      </div>
      <dl className={styles.measurements}>
        <div><dt>Model</dt><dd>{status.model_id}</dd></div>
        <div><dt>Decode mode</dt><dd>{status.decode_mode}</dd></div>
        <div><dt>Phase</dt><dd>{phase}</dd></div>
        <div><dt>Current positions</dt><dd>{positions || 'Released'}</dd></div>
        <div><dt>Placement activity</dt><dd>{activePeers} active / {status.peers.length} peers</dd></div>
        <div><dt>Qualification</dt><dd>{freshness}</dd></div>
      </dl>
      {view === 'inference' ? (
        <p role="status">
          {activePeers > 0
            ? 'Prefill or incremental decode is active; per-stage KV positions update once per second.'
            : 'No inference is active. The most recent request has released its stage-local KV state.'}
        </p>
      ) : (
        <div className={styles.tableWrap}>
          <table>
            <thead><tr><th>Peer</th><th>Backend / layers</th><th>Architecture</th><th>Supported modes</th><th>KV state</th><th>Decode work</th></tr></thead>
            <tbody>
              {status.peers.map((peer) => (
                <tr key={peer.node_id}>
                  <th scope="row">{peer.node_id}</th>
                  <td>{peer.placements.map((placement) => `${placement.runtime_backend} [${placement.start_layer},${placement.end_layer_exclusive})`).join(', ') || 'Unreported'}</td>
                  <td>{peer.architecture ?? 'Unknown'}</td>
                  <td>{peer.supported_decode_modes.join(', ') || 'None qualified'}</td>
                  <td>{peer.active_kv_state_count} active · {bytes(peer.active_kv_bytes)} · peak {bytes(peer.peak_kv_bytes)} · {peer.release_state}{peer.last_release_reason === null ? '' : ` (${peer.last_release_reason})`}</td>
                  <td>{peer.decode_input_token_count} input tokens / {peer.decode_operation_count} operations · position {peer.current_position ?? 'released'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {status.counters.fatal === null ? null : <p role="alert">Route failed closed · {status.counters.fatal}</p>}
    </section>
  );
}
