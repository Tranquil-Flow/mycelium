import { useEffect, useMemo, useState } from 'react';
import { M21HeterogeneousPanel, type M21HeterogeneousView } from './M21HeterogeneousPanel';
import { HttpM21HeterogeneousClient, type M21HeterogeneousClient, type M21HeterogeneousEvidence } from './m21Heterogeneous';

export function M21HeterogeneousSourcePanel({ view, client, hideUnavailable = false }: { readonly view: M21HeterogeneousView; readonly client?: M21HeterogeneousClient; readonly hideUnavailable?: boolean }) {
  const fallback = useMemo(() => new HttpM21HeterogeneousClient(), []); const source = client ?? fallback;
  const [evidence, setEvidence] = useState<M21HeterogeneousEvidence | null>(null); const [error, setError] = useState<string | null>(null);
  useEffect(() => { let mounted = true; let controller: AbortController | null = null; const load = () => { if (controller !== null) return; const current = new AbortController(); controller = current; void source.load(current.signal).then((value) => { if (mounted) { setEvidence(value); setError(null); } }).catch((reason) => { if (mounted && !current.signal.aborted) { setEvidence(null); setError(reason instanceof Error ? reason.message : 'm21_heterogeneous_unavailable'); } }).finally(() => { if (controller === current) controller = null; }); }; load(); const timer = window.setInterval(load, 3_000); return () => { mounted = false; controller?.abort(); window.clearInterval(timer); }; }, [source]);
  if (evidence !== null) return <M21HeterogeneousPanel evidence={evidence} view={view} />;
  if (hideUnavailable) return null;
  return <section role={error === null ? 'status' : 'alert'}>{error === null ? 'Loading heterogeneous-swarm evidence…' : `Heterogeneous-swarm evidence unavailable: ${error}`}</section>;
}
