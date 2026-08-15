import { useEffect, useMemo, useState } from 'react';
import { ArtifactAcquisitionPanel, type ArtifactAcquisitionView } from './ArtifactAcquisitionPanel';
import { HttpArtifactAcquisitionClient, type ArtifactAcquisitionClient, type ArtifactAcquisitionLedger } from './artifactAcquisition';

export function ArtifactAcquisitionSource({ view, client }: { readonly view: ArtifactAcquisitionView; readonly client?: ArtifactAcquisitionClient }) {
  const defaultClient = useMemo(() => new HttpArtifactAcquisitionClient(), []);
  const source = client ?? defaultClient;
  const [ledger, setLedger] = useState<ArtifactAcquisitionLedger | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let mounted = true;
    let active: AbortController | null = null;
    const load = () => {
      if (active !== null) return;
      const controller = new AbortController(); active = controller;
      void source.load(controller.signal).then((value) => { if (mounted) { setLedger(value); setError(null); } }).catch((reason) => { if (mounted && !controller.signal.aborted) setError(reason instanceof Error ? reason.message : 'artifact_acquisition_unavailable'); }).finally(() => { if (active === controller) active = null; });
    };
    load();
    const timer = window.setInterval(load, ledger?.current === null ? 2_000 : 500);
    return () => { mounted = false; active?.abort(); window.clearInterval(timer); };
  }, [ledger?.current, source]);
  if (ledger !== null) return <ArtifactAcquisitionPanel ledger={ledger} view={view} />;
  return <section role={error === null ? 'status' : 'alert'}>{error === null ? 'Loading swarm artifact acquisition status…' : `Artifact acquisition status is unavailable (${error}). Qualified inference remains independent.`}</section>;
}
