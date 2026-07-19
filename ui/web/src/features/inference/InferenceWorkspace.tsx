import { useMemo, useState, type FormEvent, type KeyboardEvent } from 'react';
import {
  MAX_NEW_TOKENS,
  MAX_PROMPT_UTF8_BYTES,
} from '../../app/contracts';
import { ProductInferenceClient, type InferenceClient } from './requestClient';
import { isTerminalInferencePhase } from './types';
import { useInferenceSession } from './useInferenceSession';
import styles from './InferenceWorkspace.module.css';

const encoder = new TextEncoder();

export interface InferenceWorkspaceProps {
  readonly client?: InferenceClient;
  readonly now?: () => number;
  readonly externalBlockReason?: string | null;
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

export function InferenceWorkspace({ client, now, externalBlockReason = null }: InferenceWorkspaceProps) {
  const defaultClient = useMemo(() => new ProductInferenceClient(), []);
  const session = useInferenceSession({ client: client ?? defaultClient, now });
  const [prompt, setPrompt] = useState('');
  const [maxNewTokens, setMaxNewTokens] = useState(256);
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
  const active = session.accepted_request !== null && !isTerminalInferencePhase(session.phase);
  const canResume = session.accepted_request !== null &&
    (session.phase === 'interrupted' || session.phase === 'cancelling');
  const qualification = session.qualification;

  const submit = async (event?: FormEvent): Promise<void> => {
    event?.preventDefault();
    if (formBlockReason !== null) return;
    await session.start(prompt, maxNewTokens);
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
          <strong>Session memory only</strong>
          <span>Prompt and output are not persisted by default.</span>
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
              <span>Qualified model and deployment</span>
              <select
                value={qualification === null ? 'unavailable' : 'current'}
                disabled
                aria-label="Qualified model and deployment"
              >
                <option value="unavailable">Qualification unavailable</option>
                {qualification !== null ? (
                  <option value="current">
                    {qualification.binding.model_id} · {qualification.binding.deployment_id}
                  </option>
                ) : null}
              </select>
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
              disabled={!active || session.cancellation_requested}
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
            <button type="button" onClick={() => void session.reload_qualification()}>
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
              <dd>{qualification?.binding.model_id ?? 'Unknown'}</dd>
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
        <pre
          className={styles.output}
          role="log"
          aria-label="Decoded output"
          aria-live="polite"
          aria-atomic="false"
          aria-relevant="additions"
          tabIndex={0}
        >
          {session.output || 'Decoded output will appear here. Nothing is stored after this page session.'}
        </pre>
      </section>

      {session.history.length > 0 ? (
        <section className={styles.history} aria-labelledby="history-title">
          <div className={styles.sectionHeading}>
            <div>
              <p className={styles.eyebrow}>Privacy-safe tab metadata</p>
              <h2 id="history-title">Request history</h2>
            </div>
          </div>
          <div className={styles.tableWrap}>
            <table>
              <thead>
                <tr>
                  <th scope="col">Request</th>
                  <th scope="col">Model</th>
                  <th scope="col">Deployment</th>
                  <th scope="col">Terminal state</th>
                  <th scope="col">Token count</th>
                  <th scope="col">Public error</th>
                </tr>
              </thead>
              <tbody>
                {session.history.map((entry) => (
                  <tr key={entry.request_id}>
                    <th scope="row">{entry.request_id}</th>
                    <td>{entry.model_id}</td>
                    <td>{entry.deployment_id}</td>
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
