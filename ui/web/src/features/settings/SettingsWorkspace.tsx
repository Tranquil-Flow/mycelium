import { useEffect, useState } from 'react';
import type { M15ComparisonClient, M15PlanComparison } from '../liveRoute/m15Comparison';
import { useProductSettings } from './SettingsContext';
import styles from '../membership/Membership.module.css';

export function SettingsWorkspace({ workloadClient = null }: { readonly workloadClient?: M15ComparisonClient | null }) {
  const { settings, update, reset } = useProductSettings();
  const [comparison, setComparison] = useState<M15PlanComparison | null>(null);
  useEffect(() => {
    if (workloadClient === null) return;
    let active = true;
    void workloadClient.load().then((value) => { if (active) setComparison(value); }).catch(() => { if (active) setComparison(null); });
    return () => { active = false; };
  }, [workloadClient]);
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
    <button type="button" onClick={reset}>Reset local preferences</button>
  </div>;
}
