import { useEffect, useMemo, useState } from 'react';
import { evidenceIsCurrentLive, HttpEvidenceProjectionClient, type EvidenceHistory, type EvidenceProjection, type EvidenceProjectionClient } from './evidenceProjection';

const capabilityLabels = {
  route_execution: 'Route execution',
  replicated_serving: 'Replicated serving',
  scoped_recovery: 'Scoped recovery',
  speculative_decoding: 'Speculative decoding',
  heterogeneous_participation: 'Heterogeneous participation',
  release_closure: 'Release closure',
  stage_local_kv: 'Stage-local KV',
} as const;

function sourceLabel(evidence: EvidenceProjection): string {
  if (evidence.source_kind === 'live_runtime') return evidenceIsCurrentLive(evidence, Date.now()) ? 'Live now' : evidence.freshness === 'degraded' ? 'Live · degraded' : 'Stale';
  if (evidence.source_kind === 'sealed_historical') return 'Recorded evidence';
  if (evidence.source_kind === 'replay') return 'Replay';
  if (evidence.source_kind === 'fixture') return 'Demo data';
  return evidence.freshness === 'stale' ? 'Stale plan' : 'Planner intent';
}
function time(unixMs: number): string { return new Date(unixMs).toLocaleString(); }

function runtimeSummary(evidence: EvidenceProjection | null): string | null {
  if (evidence === null || evidence.source_kind !== 'live_runtime') return null;
  const counters = evidence.payload.counters;
  const recent = evidence.payload.recent_inferences;
  if (typeof counters !== 'object' || counters === null || Array.isArray(counters) || !Array.isArray(recent)) return null;
  const sent = (counters as Record<string, unknown>).frames_sent;
  const received = (counters as Record<string, unknown>).frames_received;
  if (typeof sent !== 'number' || typeof received !== 'number') return null;
  return `${sent.toLocaleString()} frames sent · ${received.toLocaleString()} received · ${recent.length.toLocaleString()} terminal runs retained by server`;
}

export function EvidenceProvenanceSource({ client }: { readonly client?: EvidenceProjectionClient }) {
  const fallback = useMemo(() => new HttpEvidenceProjectionClient(), []);
  const source = client ?? fallback;
  const [runtime, setRuntime] = useState<EvidenceProjection | null>(null);
  const [history, setHistory] = useState<EvidenceHistory | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let mounted = true;
    let controller: AbortController | null = null;
    const load = () => {
      if (controller !== null) return;
      const current = new AbortController();
      controller = current;
      void source.loadRuntime(current.signal).then((value) => {
        if (mounted) { setRuntime(value); setError(null); }
      }).catch((reason) => {
        if (mounted && !current.signal.aborted) { setRuntime(null); setError(reason instanceof Error ? reason.message : 'runtime evidence unavailable'); }
      }).finally(() => { if (controller === current) controller = null; });
    };
    load();
    const timer = window.setInterval(load, 1_000);
    return () => { mounted = false; controller?.abort(); window.clearInterval(timer); };
  }, [source]);

  useEffect(() => {
    const controller = new AbortController();
    void source.loadHistory(controller.signal).then(setHistory).catch(() => setHistory(null));
    return () => controller.abort();
  }, [source]);

  return <section className="panel evidence-provenance" aria-label="Evidence provenance">
    <div>
      <p className="eyebrow">Evidence source</p>
      <h2>{runtime === null ? 'Live source unavailable' : sourceLabel(runtime)}</h2>
      <p>{runtime === null ? `No historical or demo fallback is being used${error === null ? '.' : `: ${error}`}` : `${capabilityLabels[runtime.capability]} · generation ${runtime.generation} · observed ${time(runtime.observed_at_unix_ms)}`}</p>
      {runtimeSummary(runtime) === null ? null : <p>{runtimeSummary(runtime)}</p>}
    </div>
    {history !== null && history.records.length > 0 ? <details>
      <summary>Recorded evidence ({history.records.length})</summary>
      <p>These records are historical. They do not describe current runtime readiness.</p>
      <ul>{history.records.map((record) => <li key={record.record_id}>
        <strong>{capabilityLabels[record.capability]}</strong>
        <span> · {sourceLabel(record)} · {time(record.observed_at_unix_ms)}</span>
        <small>Authority: {record.authority}</small>
        <details><summary>Protocol details</summary><code>{record.payload_protocol}</code></details>
      </li>)}</ul>
    </details> : null}
  </section>;
}
