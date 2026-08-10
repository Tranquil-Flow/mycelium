import { useCallback, useEffect, useState } from 'react';
import { HttpMembershipClient, type MembershipClient, type MembershipStatus } from '../membership/membershipClient';
import styles from '../membership/Membership.module.css';

export interface AdminWorkspaceProps {
  readonly client?: MembershipClient;
  readonly readOnly?: boolean;
  readonly readOnlyReason?: string;
}

export function AdminWorkspace({
  client = new HttpMembershipClient(),
  readOnly = false,
  readOnlyReason = 'Revocation is unavailable in offline evidence mode.',
}: AdminWorkspaceProps) {
  const [snapshot, setSnapshot] = useState<MembershipStatus | null>(null);
  const [confirming, setConfirming] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const load = useCallback(async () => { try { setSnapshot(await client.status()); } catch { setMessage('Membership status unavailable. Unknown state preserved.'); } }, [client]);
  useEffect(() => { void load(); }, [load]);
  async function revoke(memberId: string): Promise<void> { try { const result = await client.revoke(memberId); setMessage(result.revoked ? `${memberId} revoked.` : `${memberId} was not revoked.`); setConfirming(null); await load(); } catch { setMessage('Revocation failed with a bounded public error.'); } }
  return <div className={styles.workspace}>
    <header><p className="eyebrow caution">Local membership administration</p><h2>Membership administration</h2><p>Revocation affects trust membership only. It does not synthesize route, assignment, or qualification evidence.</p></header>
    <section className={styles.panel}><button type="button" onClick={() => void load()}>Refresh membership</button>{readOnly ? <p role="status">{readOnlyReason}</p> : null}{snapshot === null ? <p>Loading or unknown</p> : <div className="table-scroll"><table aria-label="Membership inventory"><thead><tr><th scope="col">Member</th><th scope="col">State</th><th scope="col">Connectivity</th><th scope="col">Evidence</th><th scope="col">Action</th></tr></thead><tbody>{snapshot.members.map((member) => <tr key={member.member_id}><th scope="row">{member.member_id}</th><td>{member.state}</td><td>{member.connectivity}</td><td>{member.evidence}</td><td>{confirming === member.member_id ? <><span>Revoke trust for {member.member_id}?</span><button type="button" disabled={readOnly} onClick={() => void revoke(member.member_id)}>Confirm revoke {member.member_id}</button><button type="button" onClick={() => setConfirming(null)}>Cancel</button></> : <button type="button" disabled={readOnly || member.state === 'revoked'} onClick={() => setConfirming(member.member_id)}>Revoke {member.member_id}</button>}</td></tr>)}</tbody></table></div>}</section>
    {snapshot?.unknowns.map((unknown) => <p key={unknown}>Unknown: {unknown}</p>)}
    {message === null ? null : <p role="status">{message}</p>}
  </div>;
}
