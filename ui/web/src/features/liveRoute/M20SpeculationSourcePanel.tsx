import { useEffect, useMemo, useState } from 'react';
import { M20SpeculationPanel, type M20SpeculationView } from './M20SpeculationPanel';
import { HttpM20SpeculationClient, type M20SpeculationClient, type M20SpeculativePlan, type M20SpeculativeRuntime } from './m20Speculation';

export function M20SpeculationSourcePanel({ view, client, hideUnavailable = false }: { readonly view: M20SpeculationView; readonly client?: M20SpeculationClient; readonly hideUnavailable?: boolean }) {
  const fallback = useMemo(() => new HttpM20SpeculationClient(), []); const source = client ?? fallback;
  const [evidence, setEvidence] = useState<readonly [M20SpeculativePlan, M20SpeculativeRuntime] | null>(null); const [error, setError] = useState<string | null>(null);
  useEffect(() => { const controller = new AbortController(); void source.load(controller.signal).then((value) => { setEvidence(value); setError(null); }).catch((reason) => { if (!controller.signal.aborted) { setEvidence(null); setError(reason instanceof Error ? reason.message : 'm20_speculation_unavailable'); } }); return () => controller.abort(); }, [source]);
  if (evidence !== null) return <M20SpeculationPanel plan={evidence[0]} runtime={evidence[1]} view={view} />;
  if (hideUnavailable) return null;
  return <section role={error === null ? 'status' : 'alert'}>{error === null ? 'Loading speculative-decoding evidence…' : `Speculative-decoding evidence unavailable: ${error}`}</section>;
}
