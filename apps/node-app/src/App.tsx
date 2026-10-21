// SPDX-License-Identifier: AGPL-3.0-only
import { useCallback, useEffect, useRef, useState } from 'react';
import { deviceRef, errorText, type EvidenceExport, type NodeApi, type NodeStatus, type ServiceStatus, tauriApi } from './api';

type Tab = 'status' | 'serving' | 'settings';

export function App({ api = tauriApi }: { api?: NodeApi }) {
  const [status, setStatus] = useState<NodeStatus | null>(null);
  const [service, setService] = useState<ServiceStatus | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [tab, setTab] = useState<Tab>('status');
  const [manual, setManual] = useState('');
  const [exported, setExported] = useState<EvidenceExport | null>(null);
  const statusRef = useRef<NodeStatus | null>(null);
  statusRef.current = status;

  useEffect(() => {
    let off: (() => void) | undefined;
    let offService: (() => void) | undefined;
    let alive = true;
    api.onStatus((s) => alive && setStatus(s)).then((u) => (off = u));
    // The serving service also changes without a tap here (restored after a
    // restart, stopped from the notification): follow it, never a stale snapshot.
    api
      .onService((s) => alive && setService(s))
      .then((u) => (offService = u))
      .catch(() => undefined);
    api.status().then((s) => alive && setStatus(s)).catch(() => undefined);
    api.servingStatus().then((s) => alive && setService(s)).catch(() => undefined);
    return () => {
      alive = false;
      off?.();
      offService?.();
    };
  }, [api]);

  const run = useCallback(
    async (label: string, action: () => Promise<unknown>) => {
      setBusy(label);
      setError(null);
      try {
        await action();
      } catch (e) {
        setError(errorText(e));
      } finally {
        setBusy(null);
      }
    },
    [],
  );

  const pairAndJoin = useCallback(
    (qr: string) =>
      run('Pairing…', async () => {
        const ref = deviceRef(statusRef.current?.endpoint_id ?? '');
        setStatus(await api.pair(qr.trim(), ref));
        setBusy('Joining…');
        setStatus(await api.join());
      }),
    [api, run],
  );

  useEffect(() => {
    let off: (() => void) | undefined;
    api
      .onDeepLink((url) => {
        if (url.startsWith('mycelium:')) void pairAndJoin(url);
      })
      .then((u) => (off = u))
      .catch(() => undefined);
    return () => off?.();
  }, [api, pairAndJoin]);

  const phase = status?.phase ?? 'starting';
  const needsPairing = phase === 'unpaired' || phase === 'pairing';

  return (
    <main className="app">
      <header>
        <h1>Mycelium Node</h1>
        <span className={`pill pill-${phase}`} data-testid="phase">
          {phase}
        </span>
      </header>

      {error && (
        <p role="alert" className="error">
          {error}
        </p>
      )}
      {busy && <p className="busy">{busy}</p>}

      {/* Tabs stay reachable in every phase: a failed or pending pairing must
          never hide Settings (evidence export) or the serving controls. */}
      <nav className="tabs">
        {(['status', 'serving', 'settings'] as Tab[]).map((t) => (
          <button key={t} aria-pressed={tab === t} onClick={() => setTab(t)}>
            {t}
          </button>
        ))}
      </nav>
      {tab === 'status' &&
        (needsPairing ? (
          <section className="card" aria-label="onboarding">
            <h2>Join a swarm</h2>
            <p>Scan the invite QR code shown by the swarm owner. That is all.</p>
            <button
              className="primary"
              disabled={!!busy || !status}
              onClick={() =>
                run('Scanning…', async () => {
                  const qr = await api.scanQr();
                  if (qr) await pairAndJoin(qr);
                })
              }
            >
              Scan invite QR
            </button>
            <details>
              <summary>Paste an invite code instead</summary>
              <textarea
                aria-label="invite code"
                value={manual}
                onChange={(e) => setManual(e.target.value)}
                rows={3}
              />
              <button disabled={!!busy || !manual.startsWith('mycelium:')} onClick={() => pairAndJoin(manual)}>
                Pair
              </button>
            </details>
          </section>
        ) : (
          status && <StatusCard status={status} onJoin={() => run('Joining…', async () => setStatus(await api.join()))} busy={!!busy} />
        ))}
      {tab === 'serving' && (
        <ServingCard
          service={service}
          status={status}
          busy={!!busy}
          onStart={() => run('Starting…', async () => setService(await api.servingStart()))}
          onStop={() =>
            run('Stopping…', async () => {
              setStatus(await api.drain());
              setService(await api.servingStop());
            })
          }
        />
      )}
      {tab === 'settings' && status && (
        <SettingsCard
          status={status}
          busy={!!busy}
          exported={exported}
          onExport={() => run('Exporting…', async () => setExported(await api.exportEvidence()))}
        />
      )}
    </main>
  );
}

function ago(ts: number | null): string {
  if (!ts) return '—';
  const s = Math.max(0, Math.round(Date.now() / 1000 - ts));
  return `${s}s ago`;
}

/** What the membership really is right now (the seed-signed lease decides). */
function membershipText(status: NodeStatus): string {
  if (status.phase === 'revoked') return 'revoked by the swarm owner';
  if (status.phase === 'membership_lost') return 'ended by the seed';
  if (!status.member_generation) return '—';
  if (status.lease_fresh) return `generation ${status.member_generation} · lease held`;
  return `generation ${status.member_generation} · lease lost, reconnecting…`;
}

function StatusCard({ status, onJoin, busy }: { status: NodeStatus; onJoin: () => void; busy: boolean }) {
  return (
    <section className="card" aria-label="status">
      <dl>
        <dt>Swarm</dt>
        <dd>{status.swarm_id ?? 'not joined'}</dd>
        <dt>Membership</dt>
        <dd>{membershipText(status)}</dd>
        <dt>Heartbeats</dt>
        <dd>
          {status.heartbeats_sent} (last {ago(status.last_heartbeat_at)})
        </dd>
        <dt>Network</dt>
        <dd>{status.relay_online ? 'iroh online' : 'connecting…'}</dd>
        <dt>Route ready</dt>
        <dd>{status.route_ready ? 'yes' : 'no (placement is the planner’s decision)'}</dd>
        <dt>Route assignment</dt>
        <dd>
          {status.route_assignment ? `${status.route_assignment} (issued by the swarm owner)` : 'none'}
          {status.route_assignment_error && ` · ${status.route_assignment_error}`}
        </dd>
        <dt>Stage</dt>
        <dd>{status.stage_loaded ? status.stage_loaded.slice(0, 19) : 'none assigned'}</dd>
        <dt>Frames</dt>
        <dd>
          {status.frames_executed}/{status.frames_received}
          {status.last_execute_ms != null && ` · ${status.last_execute_ms.toFixed(1)} ms`}
        </dd>
      </dl>
      {status.last_error && <p className="error">{status.last_error}</p>}
      {status.phase === 'paired' && (
        <button className="primary" disabled={busy} onClick={onJoin}>
          Join now
        </button>
      )}
    </section>
  );
}

function ServingCard({
  service,
  status,
  busy,
  onStart,
  onStop,
}: {
  service: ServiceStatus | null;
  status: NodeStatus | null;
  busy: boolean;
  onStart: () => void;
  onStop: () => void;
}) {
  const running = service?.running ?? false;
  // The OS service running is not serving: the swarm only sees this node while
  // its lease is held.
  const held = status?.lease_fresh ?? false;
  return (
    <section className="card" aria-label="serving">
      <p>
        {!running
          ? 'Not serving.'
          : !held
            ? status?.phase === 'revoked' || status?.phase === 'membership_lost'
              ? 'Not serving: this node is no longer a swarm member.'
              : 'Not serving right now: reconnecting to the swarm…'
            : service?.mode === 'foreground_only'
              ? 'Serving while this app is open.'
              : 'Serving in the background. Stop any time from the notification.'}
      </p>
      <button className={running ? '' : 'primary'} disabled={busy || !status?.member_generation} onClick={running ? onStop : onStart}>
        {running ? 'Stop serving' : 'Start serving'}
      </button>
      {!status?.member_generation && <p className="hint">Join a swarm first.</p>}
    </section>
  );
}

function SettingsCard({
  status,
  busy,
  exported,
  onExport,
}: {
  status: NodeStatus;
  busy: boolean;
  exported: EvidenceExport | null;
  onExport: () => void;
}) {
  return (
    <section className="card" aria-label="settings">
      <dl>
        <dt>Peer class</dt>
        <dd>{status.peer_class}</dd>
        <dt>Runtime</dt>
        <dd>{status.runtime_backend}</dd>
        <dt>Node id</dt>
        <dd className="mono">{status.endpoint_id.slice(0, 16)}…</dd>
      </dl>
      <button disabled={busy} onClick={onExport}>
        Export evidence
      </button>
      {exported && (
        <p className="hint">
          {exported.observations} observations {exported.shared ? 'shared' : 'saved'}.
          {exported.downloads ? ` Saved to Downloads/${exported.downloads}.` : ''}
        </p>
      )}
      <p className="hint">Uninstalling the app removes the node identity from this device.</p>
    </section>
  );
}
