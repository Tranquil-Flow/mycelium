import { useEffect, useMemo, useState } from 'react';
import { M17ModelOperationPanel, type M17ModelOperationPanelView } from './M17ModelOperationPanel';
import {
  HttpM17ModelOperationClient,
  type M17ModelOperation,
  type M17ModelOperationClient,
} from './m17ModelOperation';

export function M17ModelOperationSourcePanel({
  view,
  client,
}: {
  readonly view: M17ModelOperationPanelView;
  readonly client?: M17ModelOperationClient;
}) {
  const defaultClient = useMemo(() => new HttpM17ModelOperationClient(), []);
  const source = client ?? defaultClient;
  const [operation, setOperation] = useState<M17ModelOperation | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let mounted = true;
    let active: AbortController | null = null;
    const load = () => {
      if (active !== null) return;
      const controller = new AbortController();
      active = controller;
      void source.load(controller.signal).then((value) => {
        if (!mounted) return;
        setOperation(value);
        setError(null);
      }).catch((reason) => {
        if (!mounted || controller.signal.aborted) return;
        setError(reason instanceof Error ? reason.message : 'm17_model_operation_unavailable');
      }).finally(() => {
        if (active === controller) active = null;
      });
    };
    load();
    const timer = window.setInterval(load, 2_000);
    return () => {
      mounted = false;
      active?.abort();
      window.clearInterval(timer);
    };
  }, [source]);

  if (operation !== null) return <M17ModelOperationPanel operation={operation} view={view} />;
  return <section role={error === null ? 'status' : 'alert'}>
    {error === null ? 'Loading local model availability…' : `Local model availability is not attached to this server (${error}). The active qualified model remains usable.`}
  </section>;
}
