import { useEffect, useMemo, useState } from 'react';
import { M18ReplicationPanel, type M18ReplicationView } from './M18ReplicationPanel';
import { HttpM18ReplicationClient, type M18ReplicaPlan, type M18ReplicaRuntime, type M18ReplicationClient } from './m18Replication';

export function M18ReplicationSourcePanel({ view, client, hideUnavailable = false }: { readonly view: M18ReplicationView; readonly client?: M18ReplicationClient; readonly hideUnavailable?: boolean }) {
  const defaultClient = useMemo(() => new HttpM18ReplicationClient(), []);
  const source = client ?? defaultClient;
  const [plan, setPlan] = useState<M18ReplicaPlan | null>(null);
  const [runtime, setRuntime] = useState<M18ReplicaRuntime | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let mounted = true;
    let controller: AbortController | null = null;
    const load = () => {
      if (controller !== null) return;
      const current = new AbortController();
      controller = current;
      void Promise.allSettled([source.loadPlan(current.signal), source.loadRuntime(current.signal)]).then(([planned, running]) => {
        if (!mounted) return;
        if (planned.status === 'fulfilled') { setPlan(planned.value); setError(null); } else { setPlan(null); setError(planned.reason instanceof Error ? planned.reason.message : 'm18_replica_plan_unavailable'); }
        setRuntime(running.status === 'fulfilled' ? running.value : null);
      }).finally(() => { if (controller === current) controller = null; });
    };
    load();
    const timer = window.setInterval(load, 2_000);
    return () => { mounted = false; controller?.abort(); window.clearInterval(timer); };
  }, [source]);
  if (plan !== null) return <M18ReplicationPanel plan={plan} runtime={runtime} view={view} />;
  if (hideUnavailable) return null;
  return <section role={error === null ? 'status' : 'alert'}>{error === null ? 'Loading replica evidence…' : `Replica evidence unavailable: ${error}`}</section>;
}
