import { useEffect, useMemo, useState } from 'react';
import { M23KvPanel, type M23KvView } from './M23KvPanel';
import { HttpM23KvClient, type M23KvClient, type M23KvEvidence } from './m23Kv';

export function M23KvSourcePanel({ view, client, hideUnavailable = false }: { readonly view: M23KvView; readonly client?: M23KvClient; readonly hideUnavailable?: boolean }) {
  const fallback = useMemo(() => new HttpM23KvClient(), []);
  const source = client ?? fallback;
  const [evidence, setEvidence] = useState<M23KvEvidence | null>(null);
  const [unavailable, setUnavailable] = useState(false);
  useEffect(() => { const controller = new AbortController(); void source.load(controller.signal).then((value) => { setEvidence(value); setUnavailable(false); }).catch(() => { if (!controller.signal.aborted) setUnavailable(true); }); return () => controller.abort(); }, [source]);
  if (evidence !== null) return <M23KvPanel evidence={evidence} view={view} />;
  if (hideUnavailable) return null;
  return <section aria-label={`M23 heterogeneous KV gate for ${view}`}><h2>Stage-local KV evidence unavailable</h2><p role={unavailable ? 'alert' : 'status'}>{unavailable ? 'M23 evidence is not attached to this deployment.' : 'Loading M23 evidence…'}</p></section>;
}
