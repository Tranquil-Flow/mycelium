import { useProductSettings } from './SettingsContext';
import styles from '../membership/Membership.module.css';

export function SettingsWorkspace() {
  const { settings, update, reset } = useProductSettings();
  return <div className={styles.workspace}>
    <header><p className="eyebrow violet">Local preferences</p><h2>Settings</h2><p>Only presentation preferences are stored in this browser. Credentials, invite codes, endpoint addresses, prompts, and model output are never persisted here.</p></header>
    <section className={styles.panel} aria-labelledby="appearance-title"><h3 id="appearance-title">Appearance and access</h3>
      <label>Theme<select value={settings.theme} onChange={(event) => update({ theme: event.target.value as 'night' | 'system' })}><option value="night">Night</option><option value="system">System</option></select></label>
      <label>Density<select value={settings.density} onChange={(event) => update({ density: event.target.value as 'comfortable' | 'compact' })}><option value="comfortable">Comfortable</option><option value="compact">Compact</option></select></label>
      <label><input type="checkbox" checked={settings.reducedMotion} onChange={(event) => update({ reducedMotion: event.target.checked })} /> Reduce motion</label>
      <label><input type="checkbox" checked={settings.highContrast} onChange={(event) => update({ highContrast: event.target.checked })} /> High contrast</label>
    </section>
    <section className={styles.panel} aria-labelledby="privacy-title"><h3 id="privacy-title">Privacy</h3><label><input type="checkbox" checked={settings.concealNetworkIdentity} onChange={(event) => update({ concealNetworkIdentity: event.target.checked })} /> Conceal endpoint and network identity by default</label><p>Same-origin product APIs remain mandatory. No upstream bearer token is exposed to browser code.</p></section>
    <button type="button" onClick={reset}>Reset local preferences</button>
  </div>;
}
