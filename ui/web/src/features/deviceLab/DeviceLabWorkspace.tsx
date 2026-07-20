import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type FormEvent,
} from 'react';
import {
  HttpDeviceLabClient,
  type DeviceLabClient,
  type DeviceLabInferenceRecord,
  type DeviceLabInvite,
  type DeviceLabStatus,
} from './deviceLabClient';
import styles from './DeviceLabWorkspace.module.css';

export interface DeviceLabWorkspaceProps {
  readonly operatorToken: string | null;
  readonly client?: DeviceLabClient;
  readonly createRequestId?: () => string;
  readonly pollIntervalMs?: number;
}

function errorCode(reason: unknown): string {
  if (reason instanceof Error && reason.message.length > 0) return reason.message;
  return 'device_lab_action_failed';
}

function scientific(value: number): string {
  return Number.isFinite(value) ? value.toExponential(3) : 'unavailable';
}

function shortDigest(value: string): string {
  return value.length > 24 ? `${value.slice(0, 15)}…${value.slice(-7)}` : value;
}

function localTime(unixSeconds: number): string {
  return Number.isFinite(unixSeconds)
    ? new Date(unixSeconds * 1_000).toLocaleTimeString()
    : 'unknown';
}

function defaultRequestId(): string {
  const suffix = globalThis.crypto?.randomUUID?.()
    ?? `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
  return `request-${suffix}`;
}

export function DeviceLabWorkspace({
  operatorToken,
  client,
  createRequestId = defaultRequestId,
  pollIntervalMs = 3_000,
}: DeviceLabWorkspaceProps) {
  const labClient = useMemo(
    () => operatorToken === null ? null : (client ?? new HttpDeviceLabClient(operatorToken)),
    [client, operatorToken],
  );
  const lifecycleController = useRef<AbortController | null>(null);
  const actionControllers = useRef(new Set<AbortController>());
  const activeRequestRef = useRef<string | null>(null);
  const cancelledRequestIds = useRef(new Set<string>());
  const [status, setStatus] = useState<DeviceLabStatus | null>(null);
  const [loading, setLoading] = useState(operatorToken !== null);
  const [statusError, setStatusError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [invites, setInvites] = useState<readonly DeviceLabInvite[]>([]);
  const [inviteCount, setInviteCount] = useState(2);
  const [prompt, setPrompt] = useState('moonlit swarm');
  const [maxNewTokens, setMaxNewTokens] = useState(2);
  const [requiredPeers, setRequiredPeers] = useState(2);
  const [activeRequest, setActiveRequest] = useState<string | null>(null);
  const [busyAction, setBusyAction] = useState<'invite' | 'infer' | 'cancel' | null>(null);
  const [selectedEvidence, setSelectedEvidence] = useState<DeviceLabInferenceRecord | null>(null);

  const acceptStatus = useCallback((next: DeviceLabStatus) => {
    setStatus(next);
    setStatusError(null);
    setSelectedEvidence((current) => current ?? next.recent_requests.at(-1) ?? null);
  }, []);

  const refresh = useCallback(async (signal: AbortSignal) => {
    if (labClient === null) return;
    try {
      acceptStatus(await labClient.status(signal));
    } catch (reason) {
      if (signal.aborted) return;
      setStatusError(errorCode(reason));
    } finally {
      if (!signal.aborted) setLoading(false);
    }
  }, [acceptStatus, labClient]);

  useEffect(() => {
    if (labClient === null) {
      setLoading(false);
      return;
    }
    const controller = new AbortController();
    lifecycleController.current = controller;
    void refresh(controller.signal);
    const timer = window.setInterval(() => void refresh(controller.signal), pollIntervalMs);
    return () => {
      window.clearInterval(timer);
      controller.abort();
      if (lifecycleController.current === controller) lifecycleController.current = null;
      for (const action of actionControllers.current) action.abort();
      actionControllers.current.clear();
    };
  }, [labClient, pollIntervalMs, refresh]);

  const withAction = async <T,>(operation: (signal: AbortSignal) => Promise<T>): Promise<T> => {
    const controller = new AbortController();
    actionControllers.current.add(controller);
    try {
      return await operation(controller.signal);
    } finally {
      actionControllers.current.delete(controller);
    }
  };

  const createInvites = async () => {
    if (labClient === null) return;
    setBusyAction('invite');
    setActionError(null);
    setNotice(null);
    try {
      const created = await Promise.all(
        Array.from({ length: inviteCount }, () => withAction((signal) => labClient.createInvite(300, signal))),
      );
      setInvites((current) => [...current, ...created]);
      setNotice(`${created.length} one-use device link${created.length === 1 ? '' : 's'} created in memory.`);
    } catch (reason) {
      setActionError(errorCode(reason));
    } finally {
      setBusyAction(null);
    }
  };

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (
      labClient === null
      || activeRequestRef.current !== null
      || prompt.length === 0
      || requiredPeers > maxNewTokens
    ) return;
    const requestId = createRequestId();
    activeRequestRef.current = requestId;
    setActiveRequest(requestId);
    setBusyAction('infer');
    setActionError(null);
    setNotice(null);
    try {
      const record = await withAction((signal) => labClient.infer({
        prompt,
        max_new_tokens: maxNewTokens,
        required_distinct_peers: requiredPeers,
        request_id: requestId,
      }, signal));
      if (
        cancelledRequestIds.current.has(requestId)
        || activeRequestRef.current !== requestId
      ) return;
      setSelectedEvidence(record);
      setNotice(
        `Completed with ${record.observed_distinct_peers}/${record.required_distinct_peers} exact peer sessions.`,
      );
      const signal = lifecycleController.current?.signal;
      if (signal !== undefined) void refresh(signal);
    } catch (reason) {
      if (cancelledRequestIds.current.has(requestId)) {
        // Keep the accepted cancellation notice terminal for this request.
      } else if (errorCode(reason) === 'request_cancelled') {
        setNotice('Request cancelled safely; joined workers remain available.');
      } else {
        setActionError(errorCode(reason));
      }
    } finally {
      cancelledRequestIds.current.delete(requestId);
      if (activeRequestRef.current === requestId) {
        activeRequestRef.current = null;
        setActiveRequest(null);
        setBusyAction(null);
      }
    }
  };

  const cancel = async () => {
    if (labClient === null || activeRequestRef.current === null) return;
    const requestId = activeRequestRef.current;
    setBusyAction('cancel');
    setActionError(null);
    try {
      const cancelled = await withAction((signal) => labClient.cancel(requestId, signal));
      if (cancelled) {
        cancelledRequestIds.current.add(requestId);
        if (activeRequestRef.current === requestId) {
          activeRequestRef.current = null;
          setActiveRequest(null);
          setBusyAction(null);
        }
      } else if (activeRequestRef.current === requestId) {
        setBusyAction('infer');
      }
      setNotice(
        cancelled
          ? 'Cancellation accepted; no new browser-stage compute can start for this request.'
          : 'Request finished before cancellation reached active work.',
      );
    } catch (reason) {
      setActionError(errorCode(reason));
      setBusyAction(activeRequestRef.current === requestId ? 'infer' : null);
    }
  };

  const manualRefresh = () => {
    const signal = lifecycleController.current?.signal;
    if (signal !== undefined) void refresh(signal);
  };

  const copyInvite = async (invite: DeviceLabInvite, index: number) => {
    try {
      await navigator.clipboard.writeText(invite.url);
      setNotice(`Device ${index + 1} link copied by explicit action.`);
    } catch {
      setActionError('clipboard_write_failed');
    }
  };

  const downloadEvidence = () => {
    if (selectedEvidence === null) return;
    const blob = new Blob([`${JSON.stringify(selectedEvidence, null, 2)}\n`], {
      type: 'application/json',
    });
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = `mycelium-local-evidence-${selectedEvidence.request_id.replace(/[^A-Za-z0-9._-]+/g, '-')}.json`;
    link.click();
    window.setTimeout(() => URL.revokeObjectURL(url), 1_000);
    setNotice('Unsigned local evidence JSON downloaded locally.');
  };

  const minimumPeerSessionsMet = status !== null && status.ready_peer_count >= requiredPeers;
  const runDisabled =
    labClient === null
    || activeRequest !== null
    || prompt.length === 0
    || requiredPeers > maxNewTokens
    || !minimumPeerSessionsMet;

  return (
    <section className={styles.workspace} aria-labelledby="device-lab-title">
      <header className={styles.hero}>
        <div className={styles.heroCopy}>
          <p className={styles.eyebrow}>Real browser-session testing · bounded local evidence</p>
          <h1 id="device-lab-title">Device Lab</h1>
          <p>
            Enroll authenticated browser sessions, require a minimum number of distinct peer sessions, and
            inspect each completed request's exact cohort without promoting production readiness.
            Physical-device identity remains unproven.
          </p>
        </div>
        <div className={styles.truth}>
          <strong>route_ready=false</strong>
          <span>Qualifier-owned authority remains unchanged</span>
        </div>
      </header>

      {operatorToken === null ? (
        <div className={styles.alert} role="alert">
          <strong>Operator capability missing</strong>
          <p>Open the one-time operator URL emitted by the local device-lab server.</p>
        </div>
      ) : null}
      {loading ? <div role="status">Loading live device status…</div> : null}
      {statusError !== null ? (
        <div className={styles.alert} role="alert" aria-label="Status refresh error">
          <strong>{statusError}</strong>
          <p>Last verified status remains visible; no fresh device state was inferred.</p>
        </div>
      ) : null}
      {actionError !== null ? (
        <div className={styles.alert} role="alert" aria-label="Device Lab action error">
          <strong>{actionError}</strong>
          <p>The failed action created no readiness claim.</p>
        </div>
      ) : null}
      {notice !== null ? (
        <p className={styles.notice} role="status" aria-label="Device Lab notice">{notice}</p>
      ) : null}

      {status !== null ? (
        <div className={styles.statusGrid} aria-label="Live Device Lab status">
          <div className={styles.stat}><span>Browser sessions</span><strong>{status.peer_count} joined</strong><small>Authenticated sessions; device identity unproven</small></div>
          <div className={styles.stat}><span>Work eligible</span><strong>{status.ready_peer_count} ready</strong><small>Current bounded sessions</small></div>
          <div className={styles.stat}><span>Distinct-peer minimum</span><strong>Minimum {requiredPeers}</strong><small>{minimumPeerSessionsMet ? `Minimum ${requiredPeers} distinct peer sessions met` : `Need ${requiredPeers - status.ready_peer_count} more ready`}</small></div>
          <div className={styles.stat}><span>Local history</span><strong>{status.completed_request_count} complete</strong><small>{status.active_request_count} active · {status.pending_job_count} pending</small></div>
        </div>
      ) : null}

      <div className={styles.grid}>
        <section className={styles.panel} aria-labelledby="device-links-title">
          <div className={styles.panelHeader}>
            <div><p className={styles.eyebrow}>One-use enrollment</p><h2 id="device-links-title">Connect devices</h2></div>
            <button type="button" onClick={manualRefresh} disabled={labClient === null || loading}>Refresh live status</button>
          </div>
          <p>Each link exchanges once for one expiring browser-worker session. Keep links private.</p>
          <div className={styles.inviteControls}>
            <label className={styles.form}>
              <span>Invite count</span>
              <input
                aria-label="Invite count"
                type="number"
                min="1"
                max="6"
                value={inviteCount}
                onChange={(event) => setInviteCount(Math.max(1, Math.min(6, Number(event.currentTarget.value) || 1)))}
                disabled={labClient === null}
              />
            </label>
            <button
              type="button"
              className={styles.primary}
              onClick={() => void createInvites()}
              disabled={labClient === null || busyAction !== null}
            >
              Create {inviteCount} one-use link{inviteCount === 1 ? '' : 's'}
            </button>
          </div>
          {invites.length > 0 ? (
            <div className={styles.invites} role="region" aria-label="Created device links">
              {invites.map((invite, index) => (
                <div className={styles.invite} key={`${invite.url}:${index}`}>
                  <div><strong>Device {index + 1} · one use</strong><div className={styles.digest} data-invite-url={invite.url}>{invite.url}</div><small>Expires {localTime(invite.expires_at)}</small></div>
                  <button type="button" onClick={() => void copyInvite(invite, index)}>Copy device {index + 1} link</button>
                </div>
              ))}
            </div>
          ) : null}
        </section>

        <section className={styles.panel} aria-labelledby="run-title">
          <div className={styles.panelHeader}>
            <div><p className={styles.eyebrow}>Minimum distinct-peer exercise</p><h2 id="run-title">Run local evidence</h2></div>
          </div>
          <form className={styles.form} onSubmit={(event) => void submit(event)}>
            <label>
              <span>Prompt seed</span>
              <textarea aria-label="Prompt seed" maxLength={4_096} value={prompt} onChange={(event) => setPrompt(event.currentTarget.value)} disabled={labClient === null || activeRequest !== null} />
            </label>
            <div className={styles.formRow}>
              <label><span>Maximum fixture tokens</span><input aria-label="Maximum fixture tokens" type="number" min="1" max="8" value={maxNewTokens} onChange={(event) => setMaxNewTokens(Math.max(1, Math.min(8, Number(event.currentTarget.value) || 1)))} disabled={activeRequest !== null} /></label>
              <label><span>Minimum distinct peer sessions</span><input aria-label="Minimum distinct peer sessions" type="number" min="1" max="6" value={requiredPeers} onChange={(event) => setRequiredPeers(Math.max(1, Math.min(6, Number(event.currentTarget.value) || 1)))} disabled={activeRequest !== null} /></label>
            </div>
            {requiredPeers > maxNewTokens ? <p className={styles.alert}>Distinct-peer minimum cannot exceed fixture-token count.</p> : null}
            {activeRequest !== null ? <p className={styles.active}>Active {activeRequest}</p> : null}
            <div className={styles.actions}>
              <button type="submit" className={styles.primary} disabled={runDisabled}>Run local evidence request</button>
              <button type="button" className={styles.danger} onClick={() => void cancel()} disabled={activeRequest === null || busyAction === 'cancel'}>Cancel active request</button>
            </div>
          </form>
        </section>
      </div>

      {status !== null ? (
        <section className={styles.panel} aria-labelledby="inventory-title">
          <div className={styles.panelHeader}><div><p className={styles.eyebrow}>Live session projection</p><h2 id="inventory-title">Browser workers</h2></div><code className={styles.digest}>{shortDigest(status.stage_pack_digest)}</code></div>
          <div className={styles.inventory}>
            <table aria-label="Live browser worker inventory">
              <thead><tr><th>Peer session</th><th>State</th><th>Assigned layer</th><th>Completed jobs</th><th>Evidence class</th></tr></thead>
              <tbody>{status.peers.map((peer) => <tr key={peer.peer_id}><td className={styles.digest}>{peer.peer_id}</td><td>{peer.state}</td><td>{peer.assigned_layer.start_layer}–{peer.assigned_layer.end_layer_exclusive}</td><td>{peer.completed_jobs}</td><td>Synthetic matrix exercise; not model inference</td></tr>)}</tbody>
            </table>
            {status.peers.length === 0 ? <p className={styles.empty}>No browser workers joined yet.</p> : null}
          </div>
        </section>
      ) : null}

      {selectedEvidence !== null ? (
        <section className={styles.panel} aria-labelledby="evidence-title">
          <div className={styles.panelHeader}>
            <div><p className={styles.eyebrow}>Unsigned source-bound local evidence</p><h2 id="evidence-title">Request evidence</h2><code className={styles.digest}>{selectedEvidence.request_id}</code></div>
            <button type="button" onClick={downloadEvidence}>Download local evidence JSON</button>
          </div>
          <pre className={styles.output}>{selectedEvidence.generated_labels.join(' ')}</pre>
          <dl className={styles.metrics}>
            <div><dt>Exact peer sessions</dt><dd>{selectedEvidence.observed_distinct_peers} / {selectedEvidence.required_distinct_peers} exact peer sessions</dd></div>
            <div><dt>Maximum stage error</dt><dd>{scientific(selectedEvidence.max_intermediate_error)}</dd></div>
            <div><dt>Maximum fixture-score error</dt><dd>{scientific(selectedEvidence.max_logit_error)}</dd></div>
          </dl>
          <h3>Fixture-token journey</h3>
          <div className={styles.journey}>
            <div className={styles.stage}><strong>Local deterministic input transform</strong><span>Synthetic token embedding and prefix fixture</span></div>
            <div className={styles.stage}><strong>Bounded browser matrix exercise</strong><span>One synthetic matrix transform per token; not model inference</span></div>
            <div className={styles.stage}><strong>Local deterministic output scoring</strong><span>Fixture-only completion and label selection</span></div>
          </div>
          <div className={styles.tableWrap}>
            <table aria-label="Per-fixture-token browser-stage evidence">
              <caption>Per-fixture-token browser-stage evidence</caption>
              <thead><tr><th>Fixture token</th><th>Browser peer</th><th>Selected label</th><th>Stage error</th><th>Fixture-score error</th><th>Route</th></tr></thead>
              <tbody>{selectedEvidence.token_records.map((token) => <tr data-token-evidence key={token.browser_job_id}><td>{token.token_index}</td><td className={styles.digest}>{token.browser_peer_id}</td><td>{token.selected_label}</td><td>{scientific(token.intermediate_error)}</td><td>{scientific(token.logit_error)}</td><td>route_ready=false</td></tr>)}</tbody>
            </table>
          </div>
        </section>
      ) : null}

      {status !== null ? (
        <section className={styles.panel} aria-labelledby="history-title">
          <div className={styles.panelHeader}><div><p className={styles.eyebrow}>Bounded local history</p><h2 id="history-title">Recent requests</h2></div></div>
          <div className={styles.tableWrap}>
            <table aria-label="Recent local requests">
              <thead><tr><th>Request</th><th>Completed</th><th>Fixture tokens</th><th>Exact peers</th><th>Claim</th><th>Inspect</th></tr></thead>
              <tbody>{status.recent_requests.map((record) => {
                const selected = selectedEvidence?.request_id === record.request_id;
                return <tr key={record.request_id}><td className={styles.digest}>{record.request_id}</td><td>{localTime(record.completed_at)}</td><td>{record.generated_tokens.length}</td><td>{record.observed_distinct_peers}/{record.required_distinct_peers}</td><td>Local only</td><td><button type="button" aria-label={`Inspect ${record.request_id}`} aria-pressed={selected} disabled={selected} onClick={() => setSelectedEvidence(record)}>{selected ? 'Selected' : 'Inspect'}</button></td></tr>;
              })}</tbody>
            </table>
            {status.recent_requests.length === 0 ? <p className={styles.empty}>No completed local requests.</p> : null}
          </div>
        </section>
      ) : null}

      <footer className={styles.boundary}>
        <strong>Local evidence only · route_ready=false</strong>
        <p>
          A synthetic browser stage is bounded matrix work—never model inference.
          This workspace does not mutate Router state or establish physical model-stage qualification.
        </p>
      </footer>
    </section>
  );
}

export default DeviceLabWorkspace;
