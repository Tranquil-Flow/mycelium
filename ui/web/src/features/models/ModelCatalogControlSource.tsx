import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  DEPLOYMENTS_CHANGED_EVENT,
  HttpDeploymentActivationClient,
  type DeploymentActivationClient,
  type DeploymentActivationStatus,
} from '../liveRoute/deploymentActivation';
import {
  HttpM17ModelOperationClient,
  type M17ModelOperation,
  type M17ModelOperationClient,
} from '../liveRoute/m17ModelOperation';
import { ModelCatalogControlPanel } from './ModelCatalogControlPanel';

export function ModelCatalogControlSource({
  operationClient,
  activationClient,
  now = Date.now,
}: {
  readonly operationClient?: M17ModelOperationClient;
  readonly activationClient?: DeploymentActivationClient;
  readonly now?: () => number;
}) {
  const defaultOperationClient = useMemo(() => new HttpM17ModelOperationClient(), []);
  const defaultActivationClient = useMemo(() => new HttpDeploymentActivationClient(), []);
  const operations = operationClient ?? defaultOperationClient;
  const activations = activationClient ?? defaultActivationClient;
  const [operation, setOperation] = useState<M17ModelOperation | null>(null);
  const [activation, setActivation] = useState<DeploymentActivationStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [refreshGeneration, setRefreshGeneration] = useState(0);
  const previousQualified = useRef<ReadonlySet<string>>(new Set());

  const refresh = useCallback(() => setRefreshGeneration((value) => value + 1), []);
  useEffect(() => {
    let mounted = true;
    let active: AbortController | null = null;
    const load = () => {
      if (active !== null) return;
      const controller = new AbortController();
      active = controller;
      void Promise.all([operations.load(controller.signal), activations.status(controller.signal)]).then(([nextOperation, nextActivation]) => {
        if (!mounted) return;
        const qualified = new Set(nextActivation.candidates
          .filter((candidate) => candidate.state === 'qualified' || candidate.state === 'active')
          .map((candidate) => candidate.deployment_id));
        if ([...qualified].some((deploymentId) => !previousQualified.current.has(deploymentId))) {
          window.dispatchEvent(new Event(DEPLOYMENTS_CHANGED_EVENT));
        }
        previousQualified.current = qualified;
        setOperation(nextOperation); setActivation(nextActivation); setError(null);
      }).catch((reason) => {
        if (!mounted || controller.signal.aborted) return;
        setError(reason instanceof Error ? reason.message : 'model_control_unavailable');
      }).finally(() => { if (active === controller) active = null; });
    };
    load();
    const timer = window.setInterval(load, activation?.busy_candidate_id === null ? 2_000 : 500);
    return () => { mounted = false; active?.abort(); window.clearInterval(timer); };
  }, [activation?.busy_candidate_id, activations, operations, refreshGeneration]);

  const activate = async (candidateId: string): Promise<void> => {
    try {
      const next = await activations.activate(candidateId);
      setActivation(next); setError(null);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'activation_failed');
    }
  };

  if (operation !== null && activation !== null) return <ModelCatalogControlPanel operation={operation} activation={activation} nowUnixMs={now()} error={error} onActivate={(candidateId) => void activate(candidateId)} onRefresh={refresh} />;
  return <section role={error === null ? 'status' : 'alert'}>{error === null ? 'Loading model catalog and deployment status…' : `Model controls are unavailable (${error}). Existing qualified inference remains usable.`}</section>;
}
