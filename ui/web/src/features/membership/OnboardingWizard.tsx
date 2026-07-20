import { useState, type FormEvent } from 'react';
import { HttpMembershipClient, type MembershipClient, type MembershipInvite } from './membershipClient';
import styles from './Membership.module.css';

export interface OnboardingWizardProps { readonly client?: MembershipClient; readonly readOnly?: boolean; }

export function OnboardingWizard({ client = new HttpMembershipClient(), readOnly = false }: OnboardingWizardProps) {
  const [mode, setMode] = useState<'choose' | 'invite' | 'join'>('choose');
  const [invite, setInvite] = useState<MembershipInvite | null>(null);
  const [code, setCode] = useState('');
  const [endpointId, setEndpointId] = useState('');
  const [status, setStatus] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function createInvite(): Promise<void> {
    setBusy(true); setStatus(null);
    try { setInvite(await client.createInvite(300)); setMode('invite'); }
    catch { setStatus('Invite creation failed with a bounded public error.'); }
    finally { setBusy(false); }
  }
  async function submitJoin(event: FormEvent): Promise<void> {
    event.preventDefault(); if (readOnly || code.trim() === '') return;
    setBusy(true); setStatus(null);
    try { const result = await client.join(code.trim(), endpointId.trim() || undefined); setStatus(result.accepted ? `${result.member_id} joined as ${result.state}. Qualification remains separate.` : 'Join was not accepted.'); }
    catch { setStatus('Join failed with a bounded public error.'); }
    finally { setBusy(false); }
  }

  return <div className={styles.workspace}>
    <header><p className="eyebrow cyan">Progressive enrollment</p><h2>Join a trusted swarm</h2><p>Invited → trusted → reachable → assigned → qualified. Every state requires separate evidence.</p></header>
    <section className={styles.panel} aria-label="Membership onboarding choices">
      <button type="button" disabled={busy || readOnly} onClick={() => void createInvite()}>Create single-use invite</button>
      <button type="button" disabled={readOnly} onClick={() => setMode('join')}>Join with invite code</button>
      {readOnly ? <p role="status">Enrollment is unavailable in offline evidence mode.</p> : null}
    </section>
    {mode === 'invite' && invite !== null ? <section className={styles.panel} aria-live="polite"><h3>Single-use invitation</h3><code className={styles.code}>{invite.invite_code}</code><p>Expires {invite.expires_at}. Invite is not route readiness and grants no browser inference capability.</p></section> : null}
    {mode === 'join' ? <form className={styles.panel} onSubmit={(event) => void submitJoin(event)}><h3>Enter invitation</h3><label>Invite code<input required maxLength={512} value={code} disabled={readOnly} onChange={(event) => setCode(event.target.value)} /></label><label>Endpoint identity (optional)<input maxLength={512} value={endpointId} disabled={readOnly} onChange={(event) => setEndpointId(event.target.value)} /></label><button type="submit" disabled={readOnly || busy || code.trim() === ''}>Join swarm</button></form> : null}
    {status === null ? null : <p role="status">{status}</p>}
  </div>;
}
