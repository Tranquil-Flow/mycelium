import { useEffect, useLayoutEffect, useMemo, useState, type FormEvent, type KeyboardEvent } from 'react';
import {
  MAX_NEW_TOKENS,
  MAX_PROMPT_UTF8_BYTES,
} from '../../app/contracts';
import { ProductInferenceClient, type InferenceClient } from './requestClient';
import {
  createBrowserInferenceTabSessionStore,
  type InferenceTabSessionStore,
} from './sessionStore';
import { useInferenceSession } from './useInferenceSession';
import styles from './InferenceWorkspace.module.css';
import {
  HttpDeploymentRegistryClient,
  type DeploymentRegistryClient,
  type DeploymentRegistryStatus,
} from './deploymentClient';
import {
  HttpM15ComparisonClient,
  type M15ComparisonClient,
  type M15PlanComparison,
} from '../liveRoute/m15Comparison';
import { loadProductSettings } from '../settings/SettingsContext';
import { HttpM16RuntimeClient, type M16RuntimeClient, type M16RuntimeStatus } from '../liveRoute/m16Runtime';
import { ModelCatalogControlSource } from '../models/ModelCatalogControlSource';
import { ArtifactAcquisitionSource } from '../models/ArtifactAcquisitionSource';
import { DEPLOYMENTS_CHANGED_EVENT } from '../liveRoute/deploymentActivation';

const encoder = new TextEncoder();
const DEPLOYMENT_RECONCILE_INTERVAL_MS = 5_000;
const activeModelDisplay = Object.freeze({
  name: import.meta.env.VITE_ACTIVE_MODEL_DISPLAY_NAME?.trim() ?? '',
  deployment_id: import.meta.env.VITE_ACTIVE_MODEL_DEPLOYMENT_ID?.trim() ?? '',
  manifest_digest: import.meta.env.VITE_ACTIVE_MODEL_MANIFEST_DIGEST?.trim() ?? '',
});

export interface InferenceWorkspaceProps {
  readonly client?: InferenceClient;
  readonly now?: () => number;
  readonly externalBlockReason?: string | null;
  readonly sessionStore?: InferenceTabSessionStore | null;
  readonly deploymentClient?: DeploymentRegistryClient | null;
  readonly workloadClient?: M15ComparisonClient | null;
  readonly runtimeClient?: M16RuntimeClient | null;
}

function phaseLabel(phase: ReturnType<typeof useInferenceSession>['phase']): string {
  switch (phase) {
    case 'idle':
      return 'Ready';
    case 'submitting':
      return 'Submitting';
    case 'streaming':
      return 'Streaming';
    case 'interrupted':
      return 'Stream interrupted';
    case 'cancelling':
      return 'Cancellation pending';
    case 'cancel_unconfirmed':
      return 'Cancellation unconfirmed';
    case 'completed':
      return 'Completed';
    case 'cancelled':
      return 'Cancelled';
    case 'failed':
      return 'Failed';
  }
}

function executionLabel(
  evidenceClass: 'physical_qualification' | 'synthetic_test_fixture' | null,
  ready: boolean,
): string {
  if (evidenceClass === 'synthetic_test_fixture') {
    return 'Local / synthetic test evidence — not qualified distributed execution';
  }
  if (evidenceClass === 'physical_qualification' && ready) {
    return 'Qualified distributed execution · qualifier-owned current binding';
  }
  if (evidenceClass === 'physical_qualification') {
    return 'Physical qualification not accepted — distributed execution disabled';
  }
  return 'Execution source unknown — distributed execution disabled';
}

function activityCopy(
  phase: ReturnType<typeof useInferenceSession>['phase'],
  tokenCount: number,
  stageCount: number,
): { readonly title: string; readonly detail: string } | null {
  if (phase === 'submitting') {
    return {
      title: 'Submitting request',
      detail: 'Revalidating the captured model and deployment binding.',
    };
  }
  if (phase === 'streaming' && tokenCount === 0) {
    return {
      title: 'Waiting for first token',
      detail: `Distributed prefill and first-token decode are running across ${stageCount} qualified ${stageCount === 1 ? 'stage' : 'stages'}.`,
    };
  }
  if (phase === 'streaming') {
    return {
      title: 'Generating response',
      detail: 'Decoded tokens are streaming back from the qualified route.',
    };
  }
  if (phase === 'cancelling') {
    return {
      title: 'Stopping generation',
      detail: 'Cancellation is propagating across the qualified route.',
    };
  }
  if (phase === 'cancel_unconfirmed') {
    return {
      title: 'Cancellation unconfirmed',
      detail:
        'The route acknowledged the cancellation, but the terminal frame has not arrived. ' +
        'The request’s final state belongs to the server; a new request can be submitted.',
    };
  }
  return null;
}

function modelDisplayName(
  qualification: ReturnType<typeof useInferenceSession>['qualification'],
): string {
  if (qualification === null) return 'Unknown';
  if (
    activeModelDisplay.name.length > 0 &&
    activeModelDisplay.deployment_id === qualification.binding.deployment_id &&
    activeModelDisplay.manifest_digest === qualification.binding.manifest_digest
  ) {
    return activeModelDisplay.name;
  }
  return qualification.binding.model_id;
}

function historyPreview(value: string, emptyLabel: string): string {
  const compact = value.replace(/\s+/g, ' ').trim();
  if (compact.length === 0) return emptyLabel;
  return compact.length <= 96 ? compact : `${compact.slice(0, 95)}…`;
}

export function InferenceWorkspace({
  client,
  now,
  externalBlockReason = null,
  sessionStore,
  deploymentClient,
  workloadClient,
  runtimeClient,
}: InferenceWorkspaceProps) {
  const defaultClient = useMemo(() => new ProductInferenceClient(), []);
  const defaultSessionStore = useMemo(() => createBrowserInferenceTabSessionStore(), []);
  const defaultDeploymentClient = useMemo(() => new HttpDeploymentRegistryClient(), []);
  const defaultWorkloadClient = useMemo(() => new HttpM15ComparisonClient(), []);
  const defaultRuntimeClient = useMemo(() => new HttpM16RuntimeClient(), []);
  const effectiveDeploymentClient = deploymentClient === undefined
    ? client === undefined ? defaultDeploymentClient : null
    : deploymentClient;
  const effectiveSessionStore = sessionStore === undefined ? defaultSessionStore : sessionStore;
  const effectiveWorkloadClient = workloadClient === undefined
    ? client === undefined ? defaultWorkloadClient : null
    : workloadClient;
  const effectiveRuntimeClient = runtimeClient === undefined
    ? client === undefined ? defaultRuntimeClient : null
    : runtimeClient;
  const restored = useMemo(() => effectiveSessionStore?.load() ?? null, [effectiveSessionStore]);
  const session = useInferenceSession({
    client: client ?? defaultClient,
    now,
    restored_state: restored?.session,
  });
  const [prompt, setPrompt] = useState(restored?.prompt ?? '');
  const [maxNewTokens, setMaxNewTokens] = useState(restored?.max_new_tokens ?? 8);
  const [deploymentRegistry, setDeploymentRegistry] = useState<DeploymentRegistryStatus | null>(null);
  const [deploymentSwitching, setDeploymentSwitching] = useState(false);
  const [deploymentError, setDeploymentError] = useState<string | null>(null);
  const [workloadComparison, setWorkloadComparison] = useState<M15PlanComparison | null>(null);
  const [workloadProfileId, setWorkloadProfileId] = useState(() => loadProductSettings().defaultWorkloadProfile);
  const [runtime, setRuntime] = useState<M16RuntimeStatus | null>(null);
  const [runtimeError, setRuntimeError] = useState<string | null>(null);
  const promptBytes = encoder.encode(prompt).byteLength;
  const promptReason = prompt.length === 0
    ? 'Prompt is required'
    : promptBytes > MAX_PROMPT_UTF8_BYTES
      ? `Prompt exceeds ${MAX_PROMPT_UTF8_BYTES} UTF-8 bytes`
      : null;
  const tokenReason = !Number.isSafeInteger(maxNewTokens) || maxNewTokens < 1 || maxNewTokens > MAX_NEW_TOKENS
    ? `Maximum new tokens must be between 1 and ${MAX_NEW_TOKENS}`
    : null;
  const formBlockReason = externalBlockReason ?? session.submit_block_reason ?? promptReason ?? tokenReason;
  const active = session.accepted_request !== null && (
    session.phase === 'submitting' ||
    session.phase === 'streaming' ||
    session.phase === 'interrupted' ||
    session.phase === 'cancelling'
  );
  const canResume = session.accepted_request !== null &&
    (session.phase === 'interrupted' || session.phase === 'cancelling');
  const qualification = session.qualification;
  const selectedDeployment = deploymentRegistry?.deployments.find(
    (item) => item.deployment_id === deploymentRegistry.selected_deployment_id,
  ) ?? null;
  const qualifiedDeployments = deploymentRegistry?.deployments.filter(
    (item) => item.health === 'qualified',
  ) ?? [];
  const activeModelName = qualification === null
    ? selectedDeployment?.model_id ?? 'Unknown'
    : modelDisplayName(qualification);
  const stageCount = Math.max(
    1,
    qualification?.binding.stage_load_proof_digests.length ?? 1,
  );
  const activity = activityCopy(session.phase, session.token_count, stageCount);
  const activeRuntimeRequest = runtime?.requests.find(
    (request) => request.request_id === session.accepted_request?.request_id,
  ) ?? null;
  const reloadQualification = session.reload_qualification;

  useLayoutEffect(() => {
    effectiveSessionStore?.save({
      prompt,
      max_new_tokens: maxNewTokens,
      session,
    });
  }, [effectiveSessionStore, maxNewTokens, prompt, session]);

  useEffect(() => {
    if (effectiveDeploymentClient === null) return;
    let controller: AbortController | null = null;
    const load = () => {
      controller?.abort();
      const request = new AbortController();
      controller = request;
      void effectiveDeploymentClient.status(request.signal)
        .then(setDeploymentRegistry)
        .catch(() => {
          if (!request.signal.aborted) setDeploymentRegistry(null);
        });
    };
    const reconcile = () => {
      load();
      void reloadQualification();
    };
    const reconcileWhenVisible = () => {
      if (document.visibilityState === 'visible') reconcile();
    };
    load();
    window.addEventListener(DEPLOYMENTS_CHANGED_EVENT, reconcile);
    window.addEventListener('focus', reconcile);
    window.addEventListener('hashchange', reconcile);
    document.addEventListener('visibilitychange', reconcileWhenVisible);
    const timer = window.setInterval(reconcile, DEPLOYMENT_RECONCILE_INTERVAL_MS);
    return () => {
      window.removeEventListener(DEPLOYMENTS_CHANGED_EVENT, reconcile);
      window.removeEventListener('focus', reconcile);
      window.removeEventListener('hashchange', reconcile);
      document.removeEventListener('visibilitychange', reconcileWhenVisible);
      window.clearInterval(timer);
      controller?.abort();
    };
  }, [effectiveDeploymentClient, reloadQualification]);

  useEffect(() => {
    if (effectiveWorkloadClient === null) return;
    void effectiveWorkloadClient.load()
      .then((comparison) => {
        setWorkloadComparison(comparison);
        if (!comparison.profiles.some((profile) => profile.profile_id === workloadProfileId)) {
          setWorkloadProfileId(comparison.profiles[0].profile_id);
        }
      })
      .catch(() => setWorkloadComparison(null));
  }, [effectiveWorkloadClient, workloadProfileId]);

  useEffect(() => {
    if (effectiveRuntimeClient === null) return;
    let mounted = true;
    let controller: AbortController | null = null;
    const load = () => {
      if (controller !== null) return;
      const requestController = new AbortController();
      controller = requestController;
      void effectiveRuntimeClient.load(requestController.signal)
        .then((status) => {
          if (!mounted) return;
          setRuntime(status);
          setRuntimeError(null);
        })
        .catch((reason) => {
          if (!mounted || requestController.signal.aborted) return;
          setRuntime(null);
          setRuntimeError(reason instanceof Error ? reason.message : 'm16_runtime_unavailable');
        })
        .finally(() => {
          if (controller === requestController) controller = null;
        });
    };
    load();
    const timer = window.setInterval(load, active ? 400 : 2_000);
    return () => { mounted = false; controller?.abort(); window.clearInterval(timer); };
  }, [active, effectiveRuntimeClient]);

  const selectDeployment = async (deploymentId: string): Promise<void> => {
    if (effectiveDeploymentClient === null || active || deploymentSwitching) return;
    setDeploymentSwitching(true);
    try {
      const next = await effectiveDeploymentClient.select(deploymentId);
      setDeploymentRegistry(next);
      setDeploymentError(null);
      await session.reload_qualification();
    } catch (reason) {
      setDeploymentError(reason instanceof Error ? reason.message.replaceAll('_', ' ') : 'Model switch rejected');
    } finally {
      setDeploymentSwitching(false);
    }
  };

  const submit = async (event?: FormEvent): Promise<void> => {
    event?.preventDefault();
    if (formBlockReason !== null) return;
    const profile = workloadComparison?.profiles.find((item) => item.profile_id === workloadProfileId);
    const comparison = workloadComparison?.comparisons.find((item) => item.profile_id === workloadProfileId);
    const candidate = comparison?.candidates.find((item) => item.candidate_id === comparison.selected_candidate_id);
    await session.start(
      prompt,
      maxNewTokens,
      profile === undefined || candidate === undefined
        ? undefined
        : {
            profile_id: profile.profile_id,
            qos_class: profile.scenarios[0].qos_class,
            planner_policy_id: candidate.policy_id,
            attribution_scope: 'client_visible_planner_intent',
          },
    );
  };

  const promptKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>): void => {
    if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) {
      event.preventDefault();
      void submit();
    }
  };

  return (
    <section className={styles.workspace} aria-labelledby="inference-workspace-title">
      <header className={styles.header}>
        <div>
          <p className={styles.eyebrow}>Authority-gated request gateway</p>
          <h1 id="inference-workspace-title">Inference</h1>
          <p className={styles.lede}>
            Submit only against the exact current qualification, then stream decoded output in this tab.
          </p>
        </div>
        <div className={styles.privacy} aria-label="Inference privacy policy">
          <strong>Tab-session history</strong>
          <span>Prompt and output stay only in this tab and survive navigation or refresh.</span>
        </div>
      </header>

      <div className={styles.grid}>
        <form className={styles.composer} onSubmit={(event) => void submit(event)}>
          <div className={styles.sectionHeading}>
            <div>
              <p className={styles.eyebrow}>Private request</p>
              <h2>Prompt and bounds</h2>
            </div>
            <span className={styles.counter} data-over-limit={promptBytes > MAX_PROMPT_UTF8_BYTES}>
              {promptBytes.toLocaleString()} / {MAX_PROMPT_UTF8_BYTES.toLocaleString()} bytes
            </span>
          </div>

          <label className={styles.field}>
            <span>Prompt</span>
            <textarea
              value={prompt}
              onChange={(event) => {
                setPrompt(event.currentTarget.value);
                session.clear_form_error();
              }}
              onKeyDown={promptKeyDown}
              rows={9}
              maxLength={MAX_PROMPT_UTF8_BYTES}
              aria-describedby="prompt-bound prompt-shortcut"
              autoComplete="off"
              spellCheck={false}
              disabled={active || externalBlockReason !== null}
            />
          </label>
          <div className={styles.hints}>
            <span id="prompt-bound">{MAX_PROMPT_UTF8_BYTES} UTF-8 bytes maximum</span>
            <span id="prompt-shortcut">Press Control+Enter or Command+Enter to submit</span>
          </div>

          <div className={styles.controls}>
            <label className={styles.field}>
              <span>Workload and QoS policy</span>
              <select
                value={workloadProfileId}
                disabled={active || workloadComparison === null}
                onChange={(event) => setWorkloadProfileId(event.currentTarget.value)}
                aria-label="Workload and QoS policy"
              >
                {workloadComparison === null
                  ? <option value="interactive_chat_v1">Workload policies unavailable</option>
                  : workloadComparison.profiles.map((profile) => {
                      const comparison = workloadComparison.comparisons.find((item) => item.profile_id === profile.profile_id)!;
                      const policy = comparison.selected_candidate_id.slice(profile.profile_id.length + 1);
                      return <option key={profile.profile_id} value={profile.profile_id}>{profile.profile_id} · {profile.scenarios[0].qos_class} · {policy}</option>;
                    })}
              </select>
              <small className={styles.fieldHelp}>Shows the planner policy requested by this client; runtime admission and queueing remain independently enforced.</small>
            </label>
            <label className={styles.field}>
              <span>Model</span>
              <select
                value={deploymentRegistry?.selected_deployment_id ?? (qualification === null ? 'unavailable' : qualification.binding.deployment_id)}
                disabled={
                  active ||
                  deploymentSwitching ||
                  deploymentRegistry === null ||
                  !deploymentRegistry.switching_allowed ||
                  qualifiedDeployments.length < 2
                }
                onChange={(event) => void selectDeployment(event.currentTarget.value)}
                aria-label="Model"
              >
                {qualification === null && deploymentRegistry === null
                  ? <option value="unavailable" disabled>Qualification unavailable</option>
                  : null}
                {deploymentRegistry !== null
                  ? deploymentRegistry.deployments.map((item) => (
                      <option key={item.deployment_id} value={item.deployment_id} disabled={item.health !== 'qualified'}>
                        {item.model_id} · {item.model_revision.slice(0, 8)} · {item.quantization} · {item.topology_size} stages
                      </option>
                    ))
                  : qualification !== null
                    ? <option value={qualification.binding.deployment_id}>{activeModelName} · {qualification.binding.deployment_id}</option>
                    : null}
              </select>
              <small className={styles.fieldHelp}>
                {deploymentRegistry === null
                  ? qualification === null
                    ? 'No current qualifier binding or selectable model registry is available.'
                    : `Using ${activeModelName}. This server has not published a selectable model registry.`
                  : qualifiedDeployments.length === 0
                    ? selectedDeployment === null
                      ? 'No model is currently qualified for new inference. Recheck swarm capacity or restore route evidence.'
                      : `${selectedDeployment.model_id} is the last selected deployment, but no model is currently qualified for new inference. Recheck swarm capacity or restore route evidence; unavailable models remain visible below with their exact reason.`
                    : qualifiedDeployments.length === 1
                      ? `Using ${qualifiedDeployments[0].model_id}. It is the only model currently qualified for this swarm; other local models appear below with their exact availability reason.`
                      : 'Choose any currently qualified model. Switching is atomic and disabled while a request is active.'}
              </small>
              {deploymentError === null ? null : <small className={styles.error} role="alert">Model not changed: {deploymentError}</small>}
            </label>
            <label className={styles.field}>
              <span>Maximum new tokens</span>
              <input
                type="number"
                min={1}
                max={MAX_NEW_TOKENS}
                step={1}
                value={maxNewTokens}
                onChange={(event) => setMaxNewTokens(event.currentTarget.valueAsNumber)}
                disabled={active || externalBlockReason !== null}
              />
            </label>
          </div>

          <div className={styles.actions}>
            <button
              className={styles.primary}
              type="submit"
              disabled={formBlockReason !== null}
              aria-describedby="submit-reason"
            >
              Start inference
            </button>
            <button
              type="button"
              onClick={() => void session.resume()}
              disabled={!canResume}
            >
              Resume stream
            </button>
            <button
              className={styles.danger}
              type="button"
              onClick={() => void session.cancel()}
              disabled={
                !active ||
                session.cancellation_requested
              }
            >
              Cancel request
            </button>
          </div>

          <div className={styles.reason} id="submit-reason" aria-live="polite">
            {formBlockReason ?? 'Qualification and request bounds accepted'}
          </div>
          {formBlockReason !== null ? <p>No model request was made.</p> : null}
          {session.form_error !== null ? (
            <p className={styles.error} role="alert">{session.form_error}</p>
          ) : null}
          {session.error_code !== null ? (
            <p className={styles.error}>Public error code: <code>{session.error_code}</code></p>
          ) : null}
        </form>

        <aside className={styles.qualification} aria-labelledby="qualification-title">
          <div className={styles.sectionHeading}>
            <div>
              <p className={styles.eyebrow}>Qualifier authority</p>
              <h2 id="qualification-title">Current binding</h2>
            </div>
            <button type="button" onClick={() => window.dispatchEvent(new Event(DEPLOYMENTS_CHANGED_EVENT))}>
              Refresh
            </button>
          </div>
          <p className={styles.executionClass} data-ready={qualification?.route_ready === true}>
            {executionLabel(
              qualification?.evidence_class ?? null,
              qualification?.route_ready === true,
            )}
          </p>
          {session.qualification_changed ? (
            <div className={styles.review}>
              <p>{session.submit_block_reason}</p>
              <button type="button" onClick={session.accept_current_qualification}>
                Accept current binding
              </button>
            </div>
          ) : null}
          <dl className={styles.binding}>
            <div>
              <dt>Qualification</dt>
              <dd>{qualification?.binding.qualification_id ?? 'Unavailable'}</dd>
            </div>
            <div>
              <dt>Model</dt>
              <dd className={styles.modelIdentity}>
                <strong>{activeModelName}</strong>
                {qualification !== null && activeModelName !== qualification.binding.model_id ? (
                  <small>Admission identity: {qualification.binding.model_id}</small>
                ) : null}
              </dd>
            </div>
            <div>
              <dt>Deployment</dt>
              <dd>{qualification?.binding.deployment_id ?? 'Unknown'}</dd>
            </div>
            <div>
              <dt>Epoch / topology</dt>
              <dd>
                {qualification === null
                  ? 'Unknown'
                  : `${qualification.binding.deployment_epoch} / ${qualification.binding.topology_version}`}
              </dd>
            </div>
            <div>
              <dt>Qualification digest</dt>
              <dd className={styles.digest}>{qualification?.binding.qualification_digest ?? 'Unknown'}</dd>
            </div>
          </dl>
        </aside>
      </div>

      {client === undefined ? <ModelCatalogControlSource /> : null}
      {client === undefined ? <ArtifactAcquisitionSource view="inference" /> : null}

      <section className={styles.outputPanel} aria-labelledby="output-title">
        <div className={styles.sectionHeading}>
          <div>
            <p className={styles.eyebrow}>In-memory decoded stream</p>
            <h2 id="output-title">Output</h2>
          </div>
          <div className={styles.terminal} role="status" aria-live="polite">
            <strong>{phaseLabel(session.phase)}</strong>
            <span>{session.token_count.toLocaleString()} tokens applied</span>
          </div>
        </div>
        {activity !== null ? (
          <div
            className={styles.routeActivity}
            role="status"
            aria-live="polite"
            aria-label="Distributed route activity"
          >
            <div className={styles.activityCopy}>
              <strong>{activity.title}</strong>
              <span>{activity.detail}</span>
            </div>
            <div className={styles.activityRoute} aria-hidden="true">
              <i className={styles.activityGateway} />
              <span className={styles.activityTrack}>
                <i className={styles.activitySignal} />
              </span>
              {Array.from({ length: Math.min(stageCount, 5) }, (_, index) => (
                <i
                  className={styles.activityStage}
                  style={{ animationDelay: `${index * 180}ms` }}
                  key={index}
                />
              ))}
            </div>
            <small>
              {activeRuntimeRequest === null
                ? runtimeError === null
                  ? 'Waiting for the runtime-owned admission projection.'
                  : `Runtime admission projection unavailable: ${runtimeError}`
                : `Server phase ${activeRuntimeRequest.phase} · ${activeRuntimeRequest.qos_class} QoS · ${activeRuntimeRequest.reservation_count} path reservations · topology v${activeRuntimeRequest.topology_version}`}
            </small>
          </div>
        ) : null}
        <pre
          className={styles.output}
          role="log"
          aria-label="Decoded output"
          aria-live="polite"
          aria-atomic="false"
          aria-relevant="additions"
          tabIndex={0}
        >
          {session.output || 'Decoded output will appear here and stay only in this tab session.'}
        </pre>
      </section>

      {session.history.length > 0 ? (
        <section className={styles.history} aria-labelledby="history-title">
          <div className={styles.sectionHeading}>
            <div>
              <p className={styles.eyebrow}>Privacy-safe tab metadata</p>
              <h2 id="history-title">Request history</h2>
            </div>
            <button
              type="button"
              onClick={() => {
                effectiveSessionStore?.clear();
                session.clear_session();
                setPrompt('');
                setMaxNewTokens(8);
              }}
              disabled={active}
            >
              Clear session history
            </button>
          </div>
          <div className={styles.tableWrap}>
            <table>
              <thead>
                <tr>
                  <th scope="col">Request</th>
                  <th scope="col">Prompt</th>
                  <th scope="col">Response</th>
                  <th scope="col">Model</th>
                  <th scope="col">Deployment</th>
                  <th scope="col">Workload / QoS / policy</th>
                  <th scope="col">Terminal state</th>
                  <th scope="col">Token count</th>
                  <th scope="col">Public error</th>
                </tr>
              </thead>
              <tbody>
                {session.history.map((entry) => (
                  <tr key={entry.request_id}>
                    <th scope="row">{entry.request_id}</th>
                    <td>
                      <details className={styles.historyText}>
                        <summary>{historyPreview(entry.prompt, 'Prompt not retained')}</summary>
                        <pre>{entry.prompt || 'Prompt not retained'}</pre>
                      </details>
                    </td>
                    <td>
                      <details className={styles.historyText}>
                        <summary>{historyPreview(entry.response, 'No decoded response')}</summary>
                        <pre>{entry.response || 'No decoded response'}</pre>
                      </details>
                    </td>
                    <td>
                      {qualification !== null && entry.model_id === qualification.binding.model_id
                        ? activeModelName
                        : entry.model_id}
                    </td>
                    <td>{entry.deployment_id}</td>
                    <td>{entry.workload_attribution === undefined ? 'Not attributed' : `${entry.workload_attribution.profile_id} · ${entry.workload_attribution.qos_class} · ${entry.workload_attribution.planner_policy_id}`}</td>
                    <td>{entry.terminal_state}</td>
                    <td>{entry.token_count}</td>
                    <td>{entry.error_code ?? 'None'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      ) : null}
    </section>
  );
}

export default InferenceWorkspace;
