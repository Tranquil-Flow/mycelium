// SPDX-License-Identifier: AGPL-3.0-only
import { useCallback, useEffect, useMemo, useState, type FormEvent } from 'react';
import { MAX_NEW_TOKENS, MAX_PROMPT_UTF8_BYTES } from '../../app/contracts';
import type { InferenceWorkspaceProps } from '../inference/InferenceWorkspace';
import { ProductInferenceClient } from '../inference/requestClient';
import { useInferenceSession } from '../inference/useInferenceSession';
import { useProductEvidence } from '../productEvidence/ProductEvidenceContext';
import { TimingInferenceClient, type ClientTimingMeasurement } from './clientTiming';
import {
  ConsumerRouteStatusClient,
  type ConsumerRouteStatusState,
} from './consumerObservatory';
import { ConsumerRoutePanel, type ConsumerRouteStatusPort } from './ConsumerRoutePanel';
import { useElapsedSeconds } from './useElapsedSeconds';
import styles from './ConsumerDemoPage.module.css';

// A presentation over InferenceWorkspace's controller, not a second request engine.
// Keep model administration out of the consumer surface; do not mount two sessions.
export type ConsumerDemoPageProps = Pick<InferenceWorkspaceProps, 'client' | 'now' | 'externalBlockReason'> & {
  /** Real `/api/v1/observatory/snapshot` reader. Injectable for tests only. */
  readonly routeStatus?: ConsumerRouteStatusPort;
  /** Serialize child requests after the root has established its cookie session. */
  readonly bootstrapReady?: boolean;
};
const phaseLabels = {
  idle: 'Ready for your question', submitting: 'Submitting prompt', streaming: 'Streaming response',
  interrupted: 'Stream interrupted', cancelling: 'Stopping generation',
  cancel_unconfirmed: 'Cancellation unconfirmed', completed: 'Response complete',
  cancelled: 'Request cancelled', failed: 'Request failed',
} as const;
const defaultRouteStatusClient = new ConsumerRouteStatusClient();

function measured(value: number | null, unit: string): string {
  return value === null ? 'Not measured' : `${value.toFixed(1)} ${unit}`;
}

export function ConsumerDemoPage({ client, now, externalBlockReason = null, routeStatus, bootstrapReady = true }: ConsumerDemoPageProps) {
  // Explicit supported basic stream until native V2 token-ID validators align.
  // Qualification, session ownership and CSRF checks remain unchanged.
  const defaultClient = useMemo(() => new ProductInferenceClient({
    submissionProtocol: 'mycelium.request_gateway.v1',
  }), []);
  const timed = useMemo(
    () => new TimingInferenceClient(client ?? defaultClient, now),
    [client, defaultClient, now],
  );
  const session = useInferenceSession({ client: timed, now, bootstrapReady });
  const evidence = useProductEvidence();
  const [prompt, setPrompt] = useState('');
  const [maxTokens, setMaxTokens] = useState(128);
  const [reconnecting, setReconnecting] = useState(false);
  const [reconnectCursor, setReconnectCursor] = useState(-1);
  const [lostAt, setLostAt] = useState<number | null>(null);
  const [routeAttempt, setRouteAttempt] = useState(0);
  const [routeState, setRouteState] = useState<ConsumerRouteStatusState>({ phase: 'loading' });
  const clock = useCallback(() => (now === undefined ? Date.now() : now()), [now]);
  const readRouteStatus = useMemo<ConsumerRouteStatusPort>(
    () => routeStatus ?? ((signal?: AbortSignal) => defaultRouteStatusClient.load(signal)),
    [routeStatus],
  );

  const bytes = new TextEncoder().encode(prompt).byteLength;
  const active = ['submitting', 'streaming', 'interrupted', 'cancelling'].includes(session.phase);
  const formReason = prompt.trim().length === 0 ? 'Write a prompt to begin.'
    : bytes > MAX_PROMPT_UTF8_BYTES ? `Prompt exceeds ${MAX_PROMPT_UTF8_BYTES} UTF-8 bytes.`
    : !Number.isSafeInteger(maxTokens) || maxTokens < 1 || maxTokens > MAX_NEW_TOKENS
      ? `Choose between 1 and ${MAX_NEW_TOKENS} output tokens.` : null;
  const blockReason = externalBlockReason ?? session.submit_block_reason ?? formReason;
  const retry = session.phase === 'failed' || (session.phase === 'idle' && session.error_code !== null);
  const timing: ClientTimingMeasurement = timed.measure();
  const elapsed = useElapsedSeconds(lostAt, clock);
  const phase = reconnecting && session.phase === 'streaming' && session.last_applied_sequence <= reconnectCursor ? 'Reconnecting stream'
    : session.phase === 'streaming' && session.token_count === 0 ? 'Waiting for first token'
    : phaseLabels[session.phase];
  const qualification = session.qualification;
  const publication = evidence.visible;
  const projection = publication?.snapshot.route_explorer;
  const sourceMode = publication?.source_mode;
  const partial = session.token_count > 0 && session.phase !== 'completed';
  const completion = session.phase === 'completed' ? 'Complete output'
    : session.phase === 'streaming' ? 'Streaming · not yet complete'
    : session.token_count > 0 ? 'Partial output · not complete'
    : 'No output yet';
  const routeReady = routeState.phase === 'ready' ? routeState.facts.route_ready : null;
  const evidenceDown = evidence.latest !== null && evidence.latest.status === 'disconnected';
  const routeDegradedReason = routeReady === false
    ? 'the origin reports route_ready=false in its route-status document'
    : evidenceDown ? 'the product evidence feed reports disconnected' : null;
  const feedLabel = evidence.loading ? 'Connecting evidence feed'
    : evidence.latest?.status === 'connecting' ? 'Reconnecting evidence feed'
    : evidence.latest?.status === 'disconnected' ? 'Evidence feed disconnected'
    : evidence.latest?.status === 'connected' ? 'Evidence feed connected' : 'Evidence feed unavailable';
  const sourceLabel = sourceMode === 'fixture' ? 'Test fixture · not live route evidence'
    : sourceMode === 'replay' ? 'Replay · not live route evidence'
    : sourceMode === 'degraded' ? 'Degraded source · current evidence unavailable'
    : sourceMode === 'live' ? `Live source · ${publication?.freshness ?? 'unknown freshness'}`
    : 'No route publication available';

  useEffect(() => {
    if (!bootstrapReady) return;
    let active = true;
    const controller = new AbortController();
    setRouteState({ phase: 'loading' });
    void readRouteStatus(controller.signal)
      .then((facts) => {
        if (active) setRouteState({ phase: 'ready', facts });
      })
      .catch((reason: unknown) => {
        if (!active) return;
        const code = typeof (reason as { code?: unknown })?.code === 'string'
          ? (reason as { code: string }).code : 'route_status_failed';
        const detail = reason instanceof Error ? reason.message : 'unknown failure';
        setRouteState({ phase: 'unavailable', code, detail });
      });
    return () => {
      active = false;
      controller.abort();
    };
  }, [readRouteStatus, routeAttempt, bootstrapReady]);

  useEffect(() => {
    if (session.phase === 'interrupted') {
      setLostAt((previous) => previous ?? clock());
      return;
    }
    if (session.phase === 'submitting' || session.phase === 'streaming' || session.phase === 'cancelling') {
      setLostAt(null);
    }
  }, [session.phase, clock]);

  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (blockReason !== null) return;
    setReconnecting(false);
    setLostAt(null);
    void session.start(prompt, maxTokens);
  };
  const reconnect = async () => {
    setReconnectCursor(session.last_applied_sequence);
    setReconnecting(true);
    try { await session.resume(); } finally { setReconnecting(false); }
  };

  return <div className={styles.page}>
    <a className={styles.skip} href="#consumer-prompt">Skip to prompt</a>
    <header className={styles.nav}>
      <a className={styles.brand} href="#consumer-top" aria-label="Mycelium home"><span aria-hidden="true">✳</span> mycelium<span className={styles.tag}>experimental · basic V1 stream</span></a>
      <nav aria-label="Consumer navigation"><a href="#consumer-try">Try it</a><a href="#consumer-how">How it works</a><a href="/operator">Operator ↗</a></nav>
    </header>
    <main id="consumer-top" className={styles.main}>
      <section className={styles.hero} aria-labelledby="consumer-title">
        <p className={styles.eyebrow}>A model. A route. Your question.</p>
        <h1 id="consumer-title">A little question. <br /><em>A shared intelligence.</em></h1>
        <p>Explore inference across a shared network. Ask something, watch the answer arrive, and inspect the route evidence alongside it.</p>
        <a className={styles.heroLink} href="#consumer-prompt">Make room for a question <span aria-hidden="true">↓</span></a>
      </section>
      <div className={styles.layout}>
        <section id="consumer-try" className={styles.conversation} aria-labelledby="consumer-try-title">
          <header className={styles.cardHeader}><div><p className={styles.eyebrow}>01 / Try it</p><h2 id="consumer-try-title">What are you curious about?</h2></div><span className={styles.model}>{qualification?.binding.model_id ?? 'Model unavailable'}</span></header>
          <form onSubmit={submit} className={styles.composer}>
            <label htmlFor="consumer-prompt">Your prompt</label>
            <textarea id="consumer-prompt" value={prompt} rows={5} placeholder="Ask a question, explore an idea, or give the model a small task…"
              disabled={active || externalBlockReason !== null} autoComplete="off" spellCheck={false}
              aria-describedby="consumer-prompt-help" onChange={event => { setPrompt(event.target.value); session.clear_form_error(); }}
              onKeyDown={event => { if (event.key === 'Enter' && (event.metaKey || event.ctrlKey)) { event.preventDefault(); event.currentTarget.form?.requestSubmit(); } }} />
            <div className={styles.composerFoot}>
              <span id="consumer-prompt-help">{bytes.toLocaleString()} / {MAX_PROMPT_UTF8_BYTES.toLocaleString()} bytes · ⌘ / Ctrl + Enter</span>
              <label className={styles.tokenField}>Output token limit<input type="number" min={1} max={MAX_NEW_TOKENS} value={maxTokens} disabled={active} onChange={event => setMaxTokens(event.target.valueAsNumber)} /></label>
            </div>
            <div className={styles.actions}>
              <button className={styles.primary} type="submit" disabled={blockReason !== null} aria-describedby="consumer-submit-reason">{retry ? 'Retry prompt' : 'Send prompt'} <span aria-hidden="true">↗</span></button>
              {session.phase === 'interrupted' || session.phase === 'cancelling' ? <button type="button" onClick={() => void reconnect()} disabled={reconnecting}>Reconnect stream</button> : null}
              {active && session.accepted_request !== null ? <button type="button" disabled={session.cancellation_requested} onClick={() => void session.cancel()}>Stop generation</button> : null}
              <button type="button" disabled={active} onClick={() => void session.reload_qualification()}>Check availability</button>
            </div>
            <p id="consumer-submit-reason" className={styles.hint}>{blockReason ?? 'The gateway rechecks qualification before every request.'}</p>
            {session.qualification_changed ? <div className={styles.notice}><p>The route binding changed. Review the model and binding below before sending again.</p><button type="button" onClick={session.accept_current_qualification}>Accept current binding</button></div> : null}
            {session.error_code || session.form_error ? <p className={styles.notice} role="alert">{session.error_code ? <>Request error: <code>{session.error_code}</code>. </> : null}{session.form_error}{retry ? ' Retry sends a new request; it does not recover the old one.' : null}{partial ? ` The answer above is ${session.token_count} tokens of a request that did not complete and is not a complete answer.` : null}</p> : null}
            {routeDegradedReason !== null ? <p className={styles.notice} role="status">Route condition: <code>route_unavailable</code> (client-side observation: {routeDegradedReason}). In-flight requests end with the origin's named error code. Retry control: use Send prompt again once the route reports ready; nothing is simulated.</p> : null}
            {session.phase === 'interrupted' && elapsed !== null ? <p className={styles.notice} role="status">Reconnecting (~{elapsed} s) · the stream ended at event {session.last_applied_sequence} without a terminal event. Reconnect stream resumes this request at that cursor; it does not restart a worker.</p> : null}
          </form>
          <section className={styles.response} aria-label="Model response">
            <div className={styles.responseHeading}><p role="status" aria-live="polite"><span className={styles.statusMark} aria-hidden="true" />{phase}</p><span>{session.token_count} tokens received</span></div>
            <pre className={styles.output} role="log" aria-label="Decoded output" aria-live="polite" aria-atomic="false" tabIndex={0}>{session.output || 'Your answer will appear here. Nothing is generated until you send a prompt.'}</pre>
            <p className={styles.completion} role="status" aria-label="Output completeness">{completion}</p>
            <dl className={styles.latency} aria-label="Client-side timing">
              <div><dt>TTFT</dt><dd aria-label="TTFT">{measured(timing.ttft_ms, 'ms')}</dd></div>
              <div><dt>TPOT</dt><dd aria-label="TPOT">{measured(timing.tpot_ms, 'ms')}</dd></div>
              <div><dt>Last token</dt><dd aria-label="Last token">{measured(timing.last_token_ms, 'ms')}</dd></div>
              <div><dt>Tokens counted</dt><dd aria-label="Tokens counted">{timing.tokens}</dd></div>
            </dl>
            <p className={styles.hint}>
              {timing.basis === 'client_measured'
                ? `Client-measured in this browser: TTFT is the first token's arrival minus this request's 202 acceptance; TPOT is the mean interval between the ${timing.tokens} distinct token events applied.`
                : timing.ttft_ms !== null
                  ? 'Client-measured in this browser: only one token was applied, so no mean interval exists.'
                  : `No latency is reported (${timing.unavailable_reason ?? 'not measured'}). The origin's snapshots carry no TTFT/TPOT measurement, so none is shown as measured.`}
              {timing.resumed_streams > 0 ? ` Includes ${timing.resumed_streams} stream reopen(s) of the same request.` : ''}
            </p>
            {session.phase === 'cancel_unconfirmed' ? <p className={styles.notice}>Cancellation was acknowledged, but the server has not confirmed a terminal state.</p> : null}
          </section>
          <footer className={styles.privacy}>This page keeps prompt and output in memory, not browser storage. Sending a prompt transmits it to the inference gateway. Avoid sensitive information.</footer>
        </section>
        <aside className={styles.route} aria-labelledby="consumer-route-title">
          <p className={styles.eyebrow}>Live route</p><h2 id="consumer-route-title">The path behind this answer</h2>
          <p className={styles.feed} role="status">{feedLabel}</p><p className={styles.hint}>{sourceLabel}{evidence.frozen ? ' · Frozen historical view' : ''}</p>
          <dl className={styles.facts}>
            <div><dt>Published peers</dt><dd aria-label="Published peers">{projection?.nodes.length ?? 'Unavailable'}</dd></div>
            <div><dt>Retained route segments</dt><dd>{projection?.segments.length ?? 'Unavailable'}</dd></div>
          </dl>
          <p className={styles.hint}>Peers include transport endpoints; they are not a count of Macs. Retained segments are not measured traffic.</p>
          <details className={styles.binding}><summary>Current request qualification</summary><p>{qualification === null ? 'Qualification unavailable' : qualification.evidence_class === 'synthetic_test_fixture' ? 'Test fixture — not physical qualification' : qualification.route_ready ? 'Gateway reports physical qualification; not a new two-Mac test.' : 'Gateway qualification is not ready.'}</p><p>Binding: {qualification?.binding.qualification_id ?? 'Unavailable'}</p><p>Deployment: {qualification?.binding.deployment_id ?? 'Unavailable'}</p></details>
          <a className={styles.heroLink} href="/operator#evidence">Evidence &amp; operator details ↗</a>
          <ConsumerRoutePanel
            qualification={qualification}
            snapshot={publication?.snapshot ?? null}
            status={routeState}
            onCheck={() => setRouteAttempt((value) => value + 1)}
          />
        </aside>
      </div>
      <section className={styles.how} id="consumer-how" aria-labelledby="consumer-how-title"><div><p className={styles.eyebrow}>A shared network, an inspectable path</p><h2 id="consumer-how-title">How it works</h2></div><ol><li><strong>Ask</strong><p>The gateway checks the current model qualification. No qualified route means no request.</p></li><li><strong>Watch</strong><p>Only decoded events from the request stream become your answer. Interruptions remain visible.</p></li><li><strong>Inspect</strong><p>Explore the published route. Receipts establish provenance, not proof that an answer is correct.</p></li></ol></section>
      <footer className={styles.footer}><span>mycelium / research in the open</span><span>No single-host fallback · No in-place recovery or soak claim</span></footer>
    </main>
  </div>;
}

/** Mount under the existing ProductEvidenceProvider at the consumer route. */
export const ConsumerDemoRoute = ConsumerDemoPage;
export default ConsumerDemoPage;
