import { useEffect, useMemo, useRef, useState } from 'react';
import {
  DEPLOYMENTS_CHANGED_EVENT,
  HttpDeploymentActivationClient,
  type DeploymentActivationClient,
  type DeploymentActivationStatus,
} from './deploymentActivation';
import { PreparedDeploymentsPanel, type PreparedDeploymentsView } from './PreparedDeploymentsPanel';

export function PreparedDeploymentsSourcePanel({
  view,
  client,
  hideUnavailable = false,
}: {
  readonly view: PreparedDeploymentsView;
  readonly client?: DeploymentActivationClient;
  readonly hideUnavailable?: boolean;
}) {
  const defaultClient = useMemo(() => new HttpDeploymentActivationClient(), []);
  const source = client ?? defaultClient;
  const [status, setStatus] = useState<DeploymentActivationStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [activatingCandidateId, setActivatingCandidateId] = useState<string | null>(null);
  const previousQualified = useRef<ReadonlySet<string>>(new Set());

  useEffect(() => {
    let mounted = true;
    let active: AbortController | null = null;
    const load = () => {
      if (active !== null) return;
      const controller = new AbortController();
      active = controller;
      void source.status(controller.signal).then((next) => {
        if (!mounted) return;
        const qualified = new Set(next.candidates.filter((item) => item.state === 'qualified' || item.state === 'active').map((item) => item.deployment_id));
        if ([...qualified].some((deploymentId) => !previousQualified.current.has(deploymentId))) {
          window.dispatchEvent(new Event(DEPLOYMENTS_CHANGED_EVENT));
        }
        previousQualified.current = qualified;
        setStatus(next);
        setError(null);
      }).catch((reason) => {
        if (!mounted || controller.signal.aborted) return;
        setError(reason instanceof Error ? reason.message : 'deployment_activation_unavailable');
      }).finally(() => {
        if (active === controller) active = null;
      });
    };
    load();
    const timer = window.setInterval(load, status?.busy_candidate_id === null ? 2_000 : 500);
    return () => { mounted = false; active?.abort(); window.clearInterval(timer); };
  }, [source, status?.busy_candidate_id]);

  const activate = async (candidateId: string): Promise<void> => {
    if (activatingCandidateId !== null) return;
    setActivatingCandidateId(candidateId);
    try {
      const next = await source.activate(candidateId);
      setStatus(next);
      setError(null);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'activation_failed');
    } finally {
      setActivatingCandidateId(null);
    }
  };

  if (status !== null) return <PreparedDeploymentsPanel status={status} view={view} activatingCandidateId={activatingCandidateId} error={error} onActivate={(candidateId) => void activate(candidateId)} />;
  if (hideUnavailable && error !== null) return null;
  return <section role={error === null ? 'status' : 'alert'}>{error === null ? 'Loading prepared deployments…' : `Prepared deployment activation is unavailable (${error}).`}</section>;
}
