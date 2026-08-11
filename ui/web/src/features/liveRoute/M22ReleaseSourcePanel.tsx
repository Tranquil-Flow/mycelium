import { useEffect, useMemo, useState } from 'react';
import { M22ReleasePanel, type M22ReleaseView } from './M22ReleasePanel';
import { HttpM22ReleaseClient, type M22ReleaseClient, type M22ReleaseEvidence } from './m22Release';

export function M22ReleaseSourcePanel({ view, client, hideUnavailable = false }: { readonly view: M22ReleaseView; readonly client?: M22ReleaseClient; readonly hideUnavailable?: boolean }) {
  const fallback = useMemo(() => new HttpM22ReleaseClient(), []);
  const source = client ?? fallback;
  const [evidence, setEvidence] = useState<M22ReleaseEvidence | null>(null);
  const [unavailable, setUnavailable] = useState(false);
  useEffect(() => { const controller = new AbortController(); void source.load(controller.signal).then((value) => { setEvidence(value); setUnavailable(false); }).catch(() => { if (!controller.signal.aborted) setUnavailable(true); }); return () => controller.abort(); }, [source]);
  if (evidence !== null) return <M22ReleasePanel evidence={evidence} view={view} />;
  if (hideUnavailable) return null;
  return <section aria-label={`Release closure for ${view}`}><h2>Release closure unavailable</h2><p role={unavailable ? 'alert' : 'status'}>{unavailable ? 'Release evidence is not attached to this deployment.' : 'Loading release evidence…'}</p></section>;
}
