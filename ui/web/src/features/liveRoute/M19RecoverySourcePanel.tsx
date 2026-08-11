import { useEffect, useMemo, useState } from 'react';
import { M19RecoveryPanel, type M19RecoveryView } from './M19RecoveryPanel';
import { HttpM19RecoveryClient, type M19Liveness, type M19RecoveryClient, type M19RecoveryPlan, type M19RecoveryRuntime } from './m19Recovery';

export function M19RecoverySourcePanel({ view, client, hideUnavailable = false }: { readonly view: M19RecoveryView; readonly client?: M19RecoveryClient; readonly hideUnavailable?: boolean }) {
  const fallback = useMemo(() => new HttpM19RecoveryClient(), []); const source = client ?? fallback;
  const [evidence, setEvidence] = useState<readonly [M19Liveness, M19RecoveryPlan, M19RecoveryRuntime] | null>(null); const [error, setError] = useState<string | null>(null);
  useEffect(() => { let mounted = true; let controller: AbortController | null = null; const load = () => { if (controller !== null) return; const current = new AbortController(); controller = current; void source.load(current.signal).then((value) => { if (mounted) { setEvidence(value); setError(null); } }).catch((reason) => { if (mounted && !current.signal.aborted) { setEvidence(null); setError(reason instanceof Error ? reason.message : 'm19_recovery_unavailable'); } }).finally(() => { if (controller === current) controller = null; }); }; load(); const timer = window.setInterval(load, 2_000); return () => { mounted = false; controller?.abort(); window.clearInterval(timer); }; }, [source]);
  if (evidence !== null) return <M19RecoveryPanel liveness={evidence[0]} plan={evidence[1]} runtime={evidence[2]} view={view} />;
  if (hideUnavailable) return null;
  return <section role={error === null ? 'status' : 'alert'}>{error === null ? 'Loading recovery evidence…' : `Recovery evidence unavailable: ${error}`}</section>;
}
