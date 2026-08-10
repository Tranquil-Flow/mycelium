import { useEffect, useMemo, useState, type FormEvent } from 'react';
import type {
  ProductSwarmStatus,
  SwarmInviteResponse,
} from '../../app/contracts';
import { HttpSwarmClient, type SwarmClient } from './SwarmClient';
import { inventoryRows, type InventorySort } from './inventory';
import styles from './SwarmWorkspace.module.css';

export interface SwarmWorkspaceProps {
  readonly client?: SwarmClient;
  readonly initialStatus?: ProductSwarmStatus;
  readonly now?: () => number;
  readonly concealNetworkIdentity?: boolean;
  readonly readOnly?: boolean;
  readonly readOnlyReason?: string;
}

function errorMessage(error: unknown): string {
  return error instanceof Error && error.message.length > 0 ? error.message : 'swarm_request_failed';
}

function InviteCard({ invite, now }: { invite: SwarmInviteResponse; now: number }) {
  const seconds = Math.max(0, Math.ceil((invite.expires_at_unix_ms - now) / 1_000));
  const qrPayload = `mycelium://join?code=${encodeURIComponent(invite.invite_code)}`;
  const title = invite.capability === 'native_inference_node'
    ? 'Active native-node invite'
    : 'Active browser-probe invite';
  return (
    <section className={styles.invite} role="region" aria-label={title}>
      <div>
        <strong>{title}</strong>
        <p>Single use · expires in {seconds}s · revocable coordinator enrollment</p>
      </div>
      <code>{invite.invite_code}</code>
      <div className={styles.actions}>
        <button type="button" onClick={() => void navigator.clipboard.writeText(invite.invite_code)}>
          Copy invite code
        </button>
        <button type="button" onClick={() => void navigator.clipboard.writeText(qrPayload)}>
          Copy QR payload
        </button>
      </div>
    </section>
  );
}

export function SwarmWorkspace({
  client,
  initialStatus,
  now = Date.now,
  concealNetworkIdentity = false,
  readOnly = false,
  readOnlyReason = 'Enrollment and membership changes are unavailable in offline evidence mode.',
}: SwarmWorkspaceProps) {
  const defaultClient = useMemo(() => new HttpSwarmClient({ now }), [now]);
  const swarmClient = client ?? defaultClient;
  const [status, setStatus] = useState<ProductSwarmStatus | null>(initialStatus ?? null);
  const [loading, setLoading] = useState(initialStatus === undefined);
  const [error, setError] = useState<string | null>(null);
  const [search, setSearch] = useState('');
  const [sort, setSort] = useState<InventorySort>('identity');
  const [invite, setInvite] = useState<SwarmInviteResponse | null>(null);
  const [inviteCode, setInviteCode] = useState('');
  const [deviceName, setDeviceName] = useState('');
  const [notice, setNotice] = useState<string | null>(null);
  const [confirmLeave, setConfirmLeave] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const refresh = async (): Promise<void> => {
    try {
      const value = await swarmClient.status();
      setStatus(value);
      setError(null);
    } catch (reason) {
      setStatus(null);
      setError(errorMessage(reason));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    if (initialStatus === undefined) void refresh();
    // Initial status ownership is deliberate: do not fetch over an injected product snapshot.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const rows = useMemo(
    () => status === null ? [] : inventoryRows(status, search, sort, now(), concealNetworkIdentity),
    [concealNetworkIdentity, now, search, sort, status],
  );

  const createInvite = async (capability: 'native_inference_node' | 'synthetic_browser_probe') => {
    setBusy(true);
    setError(null);
    try {
      setInvite(await swarmClient.createInvite(capability, 300));
    } catch (reason) {
      setError(errorMessage(reason));
    } finally {
      setBusy(false);
    }
  };

  const join = async (event: FormEvent) => {
    event.preventDefault();
    if (inviteCode.length < 16 || deviceName.length > 128) return;
    setBusy(true);
    setError(null);
    try {
      const joined = await swarmClient.join(inviteCode, deviceName);
      setInviteCode('');
      setNotice(`Joined as ${joined.member_id}`);
      await refresh();
    } catch (reason) {
      setError(errorMessage(reason));
    } finally {
      setBusy(false);
    }
  };

  const leave = async (memberId: string) => {
    setBusy(true);
    setError(null);
    try {
      const result = await swarmClient.leave(memberId);
      if (!result.left) throw new Error('leave_not_confirmed');
      setNotice(`${memberId} left its device session`);
      setConfirmLeave(null);
      if (status !== null) {
        setStatus({
          ...status,
          native_nodes: status.native_nodes.filter((node) => node.member_id !== memberId),
        });
      }
    } catch (reason) {
      setError(errorMessage(reason));
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className={styles.workspace} aria-labelledby="swarm-title">
      <header className={styles.hero}>
        <div>
          <p className={styles.eyebrow}>Device sessions and bounded enrollment</p>
          <h2 id="swarm-title">Nodes and swarm</h2>
          <p>Native model-stage nodes remain distinct from synthetic browser probes.</p>
        </div>
        <div className={styles.truth}>
          <strong>route_ready=false by default</strong>
          <span>Route readiness is qualifier-owned; this screen cannot promote it.</span>
        </div>
      </header>

      {error !== null ? (
        <div className={styles.error} role="alert">
          <strong>{error}</strong>
          <span>No unverified device status is introduced; the last verified projection may remain visible.</span>
        </div>
      ) : null}
      {notice !== null ? <p className={styles.notice} role="status">{notice}</p> : null}

      <div className={styles.grid}>
        <section className={styles.panel} aria-labelledby="inventory-title">
          <div className={styles.panelHeading}>
            <div>
              <p className={styles.eyebrow}>Approved inventory projection</p>
              <h2 id="inventory-title">Devices</h2>
            </div>
            <button type="button" onClick={() => void refresh()} disabled={loading || busy}>
              Refresh status
            </button>
          </div>
          <div className={styles.filters}>
            <label>
              <span>Search devices</span>
              <input
                type="search"
                value={search}
                onChange={(event) => setSearch(event.currentTarget.value)}
                aria-label="Search devices"
              />
            </label>
            <label>
              <span>Sort inventory</span>
              <select value={sort} onChange={(event) => setSort(event.currentTarget.value as InventorySort)}>
                <option value="identity">Identity</option>
                <option value="capability">Capability</option>
                <option value="state">State</option>
                <option value="connectivity">Connectivity</option>
                <option value="expiry">Expiry</option>
              </select>
            </label>
          </div>

          {loading ? <p role="status">Loading verified device status…</p> : null}
          {!loading && status !== null ? (
            <div className={styles.tableWrap}>
              <table>
                <thead><tr><th>Identity</th><th>Capability</th><th>State</th><th>Connectivity</th><th>Endpoint</th><th>Expiry</th><th>Action</th></tr></thead>
                <tbody>
                  {rows.map((row) => (
                    <tr key={`${row.kind}:${row.id}`}>
                      <th scope="row">{row.id}</th>
                      <td>{row.capabilityLabel}</td>
                      <td>{row.state}</td>
                      <td>{row.connectivity}</td>
                      <td>{row.endpointLabel}</td>
                      <td>{row.expiryLabel}</td>
                      <td>
                        {row.kind === 'native_node' ? (
                          <button type="button" onClick={() => setConfirmLeave(row.id)} disabled={readOnly}>
                            Leave {row.id}
                          </button>
                        ) : 'Probe only'}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {rows.length === 0 ? <p>No matching approved inventory.</p> : null}
            </div>
          ) : null}

          {confirmLeave !== null ? (
            <div className={styles.confirm} role="group" aria-label={`Confirm leave ${confirmLeave}`}>
              <p>Leaving closes only this product device session. It does not mutate Router state.</p>
              <button type="button" onClick={() => void leave(confirmLeave)} disabled={busy || readOnly}>Confirm leave</button>
              <button type="button" onClick={() => setConfirmLeave(null)} disabled={busy}>Keep session</button>
            </div>
          ) : null}
        </section>

        <aside className={styles.panel} aria-labelledby="enroll-title">
          <p className={styles.eyebrow}>Coordinator enrollment</p>
          <h2 id="enroll-title">Invite or join</h2>
          <p>Invites are bounded, single-use enrollment material. They are never stored by this page.</p>
          {readOnly ? <p role="status">{readOnlyReason}</p> : null}
          <button type="button" onClick={() => void createInvite('native_inference_node')} disabled={busy || readOnly}>
            Create native-node invite
          </button>
          <form className={styles.join} onSubmit={(event) => void join(event)}>
            <label>
              <span>Invite code</span>
              <input
                aria-label="Invite code"
                autoComplete="off"
                spellCheck={false}
                value={inviteCode}
                onChange={(event) => setInviteCode(event.currentTarget.value)}
                maxLength={2_048}
                disabled={readOnly}
              />
            </label>
            <label>
              <span>Device name</span>
              <input
                aria-label="Device name"
                value={deviceName}
                onChange={(event) => setDeviceName(event.currentTarget.value)}
                maxLength={128}
                disabled={readOnly}
              />
            </label>
            <button type="submit" disabled={busy || readOnly || inviteCode.length < 16}>Join swarm</button>
          </form>
        </aside>
      </div>

      {invite !== null ? <InviteCard invite={invite} now={now()} /> : null}

      <details className={styles.developer}>
        <summary>Developer/probe browser workers</summary>
        <div>
          <p>Synthetic matrix probe only. Browser work cannot set model, stage, inference, or route readiness.</p>
          <p><code>route_ready=false</code> remains literal for every browser worker.</p>
          <button type="button" onClick={() => void createInvite('synthetic_browser_probe')} disabled={busy || readOnly}>
            Create browser-probe invite
          </button>
        </div>
      </details>

      <footer className={styles.boundary}>
        Route readiness is qualifier-owned. Enrollment does not establish physical qualification and does not mutate Router state.
      </footer>
    </section>
  );
}

export default SwarmWorkspace;
