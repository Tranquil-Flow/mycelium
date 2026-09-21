import { useMemo, useState, type FormEvent } from 'react';
import { MAX_NEW_TOKENS, MAX_PROMPT_UTF8_BYTES } from '../../app/contracts';
import type { InferenceWorkspaceProps } from '../inference/InferenceWorkspace';
import { ProductInferenceClient } from '../inference/requestClient';
import { useInferenceSession } from '../inference/useInferenceSession';
import { useProductEvidence } from '../productEvidence/ProductEvidenceContext';
import { RouteExplorerWorkspace } from '../routeExplorer/RouteExplorer';
import styles from './ConsumerDemoPage.module.css';

// A presentation over InferenceWorkspace's controller, not a second request engine.
// Keep model administration out of the consumer surface; do not mount two sessions.
export type ConsumerDemoPageProps = Pick<InferenceWorkspaceProps, 'client' | 'now' | 'externalBlockReason'>;
const phaseLabels = {
  idle: 'Ready for your question', submitting: 'Submitting prompt', streaming: 'Streaming response',
  interrupted: 'Stream interrupted', cancelling: 'Stopping generation',
  cancel_unconfirmed: 'Cancellation unconfirmed', completed: 'Response complete',
  cancelled: 'Request cancelled', failed: 'Request failed',
} as const;

export function ConsumerDemoPage({ client, now, externalBlockReason = null }: ConsumerDemoPageProps) {
  const defaultClient = useMemo(() => new ProductInferenceClient(), []);
  const session = useInferenceSession({ client: client ?? defaultClient, now });
  const evidence = useProductEvidence();
  const [prompt, setPrompt] = useState('');
  const [maxTokens, setMaxTokens] = useState(128);
  const [reconnecting, setReconnecting] = useState(false);
  const [reconnectCursor, setReconnectCursor] = useState(-1);
  const bytes = new TextEncoder().encode(prompt).byteLength;
  const active = ['submitting', 'streaming', 'interrupted', 'cancelling'].includes(session.phase);
  const formReason = prompt.trim().length === 0 ? 'Write a prompt to begin.'
    : bytes > MAX_PROMPT_UTF8_BYTES ? `Prompt exceeds ${MAX_PROMPT_UTF8_BYTES} UTF-8 bytes.`
    : !Number.isSafeInteger(maxTokens) || maxTokens < 1 || maxTokens > MAX_NEW_TOKENS
      ? `Choose between 1 and ${MAX_NEW_TOKENS} output tokens.` : null;
  const blockReason = externalBlockReason ?? session.submit_block_reason ?? formReason;
  const retry = session.phase === 'failed' || (session.phase === 'idle' && session.error_code !== null);
  const phase = reconnecting && session.phase === 'streaming' && session.last_applied_sequence <= reconnectCursor ? 'Reconnecting stream'
    : session.phase === 'streaming' && session.token_count === 0 ? 'Waiting for first token'
    : phaseLabels[session.phase];
  const qualification = session.qualification;
  const publication = evidence.visible;
  const projection = publication?.snapshot.route_explorer;
  const sourceMode = publication?.source_mode;
  const feedLabel = evidence.loading ? 'Connecting evidence feed'
    : evidence.latest?.status === 'connecting' ? 'Reconnecting evidence feed'
    : evidence.latest?.status === 'disconnected' ? 'Evidence feed disconnected'
    : evidence.latest?.status === 'connected' ? 'Evidence feed connected' : 'Evidence feed unavailable';
  const sourceLabel = sourceMode === 'fixture' ? 'Test fixture · not live route evidence'
    : sourceMode === 'replay' ? 'Replay · not live route evidence'
    : sourceMode === 'degraded' ? 'Degraded source · current evidence unavailable'
    : sourceMode === 'live' ? `Live source · ${publication?.freshness ?? 'unknown freshness'}`
    : 'No route publication available';

  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (blockReason !== null) return;
    setReconnecting(false);
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
      <a className={styles.brand} href="#consumer-top" aria-label="Mycelium home"><span aria-hidden="true">✳</span> mycelium<span className={styles.tag}>research preview</span></a>
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
            {session.error_code || session.form_error ? <p className={styles.notice} role="alert">{session.error_code ? <>Request error: <code>{session.error_code}</code>. </> : null}{session.form_error}{retry ? ' Retry sends a new request; it does not recover the old one.' : null}</p> : null}
          </form>
          <section className={styles.response} aria-label="Model response">
            <div className={styles.responseHeading}><p role="status" aria-live="polite"><span className={styles.statusMark} aria-hidden="true" />{phase}</p><span>{session.token_count} tokens received</span></div>
            <pre className={styles.output} role="log" aria-label="Decoded output" aria-live="polite" aria-atomic="false" tabIndex={0}>{session.output || 'Your answer will appear here. Nothing is generated until you send a prompt.'}</pre>
            {session.phase === 'interrupted' ? <p className={styles.notice}>The stream ended without a terminal event. Reconnect resumes this request; it does not restart a worker.</p> : null}
            {session.phase === 'cancel_unconfirmed' ? <p className={styles.notice}>Cancellation was acknowledged, but the server has not confirmed a terminal state.</p> : null}
          </section>
          <footer className={styles.privacy}>This page keeps prompt and output in memory, not browser storage. Sending a prompt transmits it to the inference gateway. Avoid sensitive information.</footer>
        </section>
        <aside className={styles.route} aria-labelledby="consumer-route-title">
          <p className={styles.eyebrow}>02 / Behind the answer</p><h2 id="consumer-route-title">Route, not guesswork.</h2>
          <p className={styles.feed} role="status">{feedLabel}</p><p className={styles.hint}>{sourceLabel}{evidence.frozen ? ' · Frozen historical view' : ''}</p>
          <dl className={styles.facts}>
            <div><dt>Published peers</dt><dd aria-label="Published peers">{projection?.nodes.length ?? 'Unavailable'}</dd></div>
            <div><dt>Retained route segments</dt><dd>{projection?.segments.length ?? 'Unavailable'}</dd></div>
            <div><dt>TTFT</dt><dd aria-label="TTFT">Unavailable</dd></div>
            <div><dt>TPOT</dt><dd aria-label="TPOT">Unavailable</dd></div>
          </dl>
          <p className={styles.hint}>Peers include transport endpoints; they are not a count of Macs. Retained segments are not measured traffic.</p>
          <p className={styles.hint}>Mac names and layer placement are unavailable in the route projection. TTFT/TPOT are not supplied by the request-event or route-explorer contracts.</p>
          <div className={styles.recovery}><h3>Recovery, honestly</h3><p>Cold-restart status and ETA are unavailable in the current contracts. An evidence-feed reconnect is not proof of worker recovery.</p></div>
          <details className={styles.binding}><summary>Current request qualification</summary><p>{qualification === null ? 'Qualification unavailable' : qualification.evidence_class === 'synthetic_test_fixture' ? 'Test fixture — not physical qualification' : qualification.route_ready ? 'Gateway reports physical qualification; not a new two-Mac test.' : 'Gateway qualification is not ready.'}</p><p>Binding: {qualification?.binding.qualification_id ?? 'Unavailable'}</p><p>Deployment: {qualification?.binding.deployment_id ?? 'Unavailable'}</p></details>
          <a className={styles.heroLink} href="/operator#evidence">Evidence & operator details ↗</a>
        </aside>
      </div>
      <details className={styles.explorer}><summary>Inspect the published route <span>Read-only · provenance and freshness included</span></summary><RouteExplorerWorkspace /></details>
      <section className={styles.how} id="consumer-how" aria-labelledby="consumer-how-title"><div><p className={styles.eyebrow}>A shared network, an inspectable path</p><h2 id="consumer-how-title">How it works</h2></div><ol><li><strong>Ask</strong><p>The gateway checks the current model qualification. No qualified route means no request.</p></li><li><strong>Watch</strong><p>Only decoded events from the request stream become your answer. Interruptions remain visible.</p></li><li><strong>Inspect</strong><p>Explore the published route. Receipts establish provenance, not proof that an answer is correct.</p></li></ol></section>
      <footer className={styles.footer}><span>mycelium / research in the open</span><span>No single-host fallback · No in-place recovery or soak claim</span></footer>
    </main>
  </div>;
}

/** Mount under the existing ProductEvidenceProvider at the consumer route. */
export const ConsumerDemoRoute = ConsumerDemoPage;
export default ConsumerDemoPage;
