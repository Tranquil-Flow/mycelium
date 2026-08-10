import { useEffect, useState } from 'react';
import type { M15ComparisonClient, M15PlanComparison } from '../liveRoute/m15Comparison';
import type { DeploymentRegistryClient, DeploymentRegistryStatus } from '../inference/deploymentClient';
import { useProductSettings } from './SettingsContext';
import styles from '../membership/Membership.module.css';

export function SettingsWorkspace({ workloadClient = null, deploymentClient = null }: { readonly workloadClient?: M15ComparisonClient | null; readonly deploymentClient?: DeploymentRegistryClient | null }) {
  const { settings, update, reset } = useProductSettings();
  const [comparison, setComparison] = useState<M15PlanComparison | null>(null);
  const [registry, setRegistry] = useState<DeploymentRegistryStatus | null>(null);
  const [deploymentError, setDeploymentError] = useState<string | null>(null);
  useEffect(() => {
    if (workloadClient === null) return;
    let active = true;
    void workloadClient.load().then((value) => { if (active) setComparison(value); }).catch(() => { if (active) setComparison(null); });
    return () => { active = false; };
  }, [workloadClient]);
  useEffect(() => {
    if (deploymentClient === null) return;
    const controller = new AbortController();
    void deploymentClient.status(controller.signal).then((value) => {
      setRegistry(value);
      setDeploymentError(null);
      if (
        settings.preferredDeploymentId !== null
        && !value.deployments.some((deployment) => deployment.deployment_id === settings.preferredDeploymentId && deployment.health === 'qualified')
      ) update({ preferredDeploymentId: null });
    }).catch((reason) => {
      if (!controller.signal.aborted) setDeploymentError(reason instanceof Error ? reason.message : 'deployment_registry_unavailable');
    });
    return () => controller.abort();
  }, [deploymentClient, settings.preferredDeploymentId, update]);

  const selectPreferredDeployment = async (deploymentId: string) => {
    if (deploymentClient === null || registry === null || deploymentId.length === 0) {
      update({ preferredDeploymentId: null });
      return;
    }
    try {
      const selected = await deploymentClient.select(deploymentId);
      setRegistry(selected);
      update({ preferredDeploymentId: deploymentId });
      setDeploymentError(null);
    } catch (reason) {
      setDeploymentError(reason instanceof Error ? reason.message : 'deployment_preference_rejected');
    }
  };
  return <div className={styles.workspace}>
    <header><p className="eyebrow violet">Local preferences</p><h1>Settings</h1><p>Only presentation preferences are stored in this browser. Credentials, invite codes, endpoint addresses, prompts, and model output are never persisted here.</p></header>
    <section className={styles.panel} aria-labelledby="appearance-title"><h3 id="appearance-title">Appearance and access</h3>
      <label>Theme<select value={settings.theme} onChange={(event) => update({ theme: event.target.value as 'night' | 'system' })}><option value="night">Night</option><option value="system">System</option></select></label>
      <label>Density<select value={settings.density} onChange={(event) => update({ density: event.target.value as 'comfortable' | 'compact' })}><option value="comfortable">Comfortable</option><option value="compact">Compact</option></select></label>
      <label><input type="checkbox" checked={settings.reducedMotion} onChange={(event) => update({ reducedMotion: event.target.checked })} /> Reduce motion</label>
      <label><input type="checkbox" checked={settings.highContrast} onChange={(event) => update({ highContrast: event.target.checked })} /> High contrast</label>
    </section>
    <section className={styles.panel} aria-labelledby="privacy-title"><h3 id="privacy-title">Privacy</h3><label><input type="checkbox" checked={settings.concealNetworkIdentity} onChange={(event) => update({ concealNetworkIdentity: event.target.checked })} /> Conceal endpoint and network identity by default</label><p>Same-origin product APIs remain mandatory. No upstream bearer token is exposed to browser code.</p></section>
    <section className={styles.panel} aria-labelledby="workload-default-title"><h3 id="workload-default-title">Qualified workload default</h3>
      <label>Default workload and QoS profile<select aria-label="Default workload and QoS profile" value={settings.defaultWorkloadProfile} disabled={comparison === null} onChange={(event) => update({ defaultWorkloadProfile: event.target.value })}>
        {comparison === null ? <option value="interactive_chat_v1">Live M15 profiles unavailable</option> : comparison.profiles.map((profile) => <option key={profile.profile_id} value={profile.profile_id}>{profile.profile_id} · {profile.scenarios[0].qos_class}</option>)}
      </select></label>
      <p>This local preference applies only to future inference requests. It does not change an active request and does not imply that M16 admission, queueing, or batching exists.</p>
    </section>
    <section className={styles.panel} aria-labelledby="model-default-title"><h3 id="model-default-title">Qualified model preference</h3>
      <label>Preferred model for future requests<select aria-label="Preferred qualified model and deployment" value={settings.preferredDeploymentId ?? ''} disabled={registry === null || !registry.switching_allowed} onChange={(event) => void selectPreferredDeployment(event.currentTarget.value)}>
        <option value="">Follow active qualified deployment</option>
        {registry?.deployments.filter((deployment) => deployment.health === 'qualified').map((deployment) => <option key={deployment.deployment_id} value={deployment.deployment_id}>{deployment.model_id} · {deployment.model_revision.slice(0, 8)} · {deployment.quantization}</option>)}
      </select></label>
      <p>Only deployments currently accepted by the qualifier are offered. Changing this preference atomically changes future admissions and never rebinds an in-flight request.</p>
      {deploymentError === null ? null : <p role="alert">Model preference not changed: {deploymentError}</p>}
    </section>
    <button type="button" onClick={reset}>Reset local preferences</button>
  </div>;
}
