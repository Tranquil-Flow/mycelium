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
import { HttpModelCapacityRefreshClient, type ModelCapacityRefreshClient, type ModelCapacityRefreshStatus } from './modelCapacityRefresh';

export function ModelCatalogControlSource({
  operationClient,
  activationClient,
  capacityClient,
  now = Date.now,
}: {
  readonly operationClient?: M17ModelOperationClient;
  readonly activationClient?: DeploymentActivationClient;
  readonly capacityClient?: ModelCapacityRefreshClient;
  readonly now?: () => number;
}) {
  const defaultOperationClient = useMemo(() => new HttpM17ModelOperationClient(), []);
  const defaultActivationClient = useMemo(() => new HttpDeploymentActivationClient(), []);
  const defaultCapacityClient = useMemo(() => new HttpModelCapacityRefreshClient(), []);
  const operations = operationClient ?? defaultOperationClient;
  const activations = activationClient ?? defaultActivationClient;
  const capacity = capacityClient ?? defaultCapacityClient;
  const [operation, setOperation] = useState<M17ModelOperation | null>(null);
  const [activation, setActivation] = useState<DeploymentActivationStatus | null>(null);
  const [capacityRefresh, setCapacityRefresh] = useState<ModelCapacityRefreshStatus | null>(null);
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
      void Promise.all([operations.load(controller.signal), activations.status(controller.signal), capacity.status(controller.signal).catch(() => null)]).then(([nextOperation, nextActivation, nextCapacity]) => {
        if (!mounted) return;
        const qualified = new Set(nextActivation.candidates
          .filter((candidate) => candidate.state === 'qualified' || candidate.state === 'active')
          .map((candidate) => candidate.deployment_id));
        if ([...qualified].some((deploymentId) => !previousQualified.current.has(deploymentId))) {
          window.dispatchEvent(new Event(DEPLOYMENTS_CHANGED_EVENT));
        }
        previousQualified.current = qualified;
        setOperation(nextOperation); setActivation(nextActivation); setError(null);
        setCapacityRefresh(nextCapacity);
      }).catch((reason) => {
        if (!mounted || controller.signal.aborted) return;
        setError(reason instanceof Error ? reason.message : 'model_control_unavailable');
      }).finally(() => { if (active === controller) active = null; });
    };
    load();
    const busy = activation?.busy_candidate_id !== null || capacityRefresh?.state === 'refreshing';
    const timer = window.setInterval(load, busy ? 500 : 2_000);
    return () => { mounted = false; active?.abort(); window.clearInterval(timer); };
  }, [activation?.busy_candidate_id, activations, capacity, capacityRefresh?.state, operations, refreshGeneration]);

  const activate = async (candidateId: string): Promise<void> => {
    try {
      const next = await activations.activate(candidateId);
      setActivation(next); setError(null);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'activation_failed');
    }
  };

  const recheckCapacity = async (): Promise<void> => {
    try {
      setCapacityRefresh(await capacity.start()); setError(null);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'capacity_refresh_failed');
    }
  };

  if (operation !== null && activation !== null) return <ModelCatalogControlPanel operation={operation} activation={activation} capacityRefresh={capacityRefresh} nowUnixMs={now()} error={error} onActivate={(candidateId) => void activate(candidateId)} onRefresh={refresh} onRecheckCapacity={() => void recheckCapacity()} />;
  return <section role={error === null ? 'status' : 'alert'}>{error === null ? 'Loading model catalog and deployment status…' : `Model controls are unavailable (${error}). Existing qualified inference remains usable.`}</section>;
}
