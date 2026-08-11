import type { M20SpeculativePlan, M20SpeculativeRuntime } from './m20Speculation';
import styles from './LiveRouteWorkspace.module.css';

export type M20SpeculationView = 'plans' | 'inference' | 'readiness';
const metric = (value: number | null, suffix = '') => value === null ? 'not measured' : `${value.toFixed(2)}${suffix}`;

export function M20SpeculationPanel({ plan, runtime, view }: { readonly plan: M20SpeculativePlan; readonly runtime: M20SpeculativeRuntime; readonly view: M20SpeculationView }) {
  const enabled = plan.decision.state === 'qualified_enabled';
  return <section className={styles.panel} aria-label={`M20 ${view} speculative decoding evidence`}>
    <div className={styles.panelTitlebar}><div><p className={styles.eyebrow}>M20 · target-authoritative speculation</p><h2>{view === 'plans' ? 'Draft / target promotion gate' : view === 'readiness' ? 'Speculative qualification' : 'Optional draft overlay'}</h2></div><span className={styles.evidenceBadge}>{enabled ? 'qualified' : 'target-only'}</span></div>
    <dl className={styles.measurements}>
      <div><dt>Target</dt><dd>{plan.target.model_id}</dd></div>
      <div><dt>Draft candidate</dt><dd>{plan.draft.model_id}</dd></div>
      <div><dt>Proposal width</dt><dd>{plan.proposal_width}</dd></div>
      <div><dt>Target TPOT</dt><dd>{metric(plan.measurements.target_only_tpot_ms, ' ms')}</dd></div>
      <div><dt>Verification batch</dt><dd>{plan.compatibility.batched_target_verification ? metric(plan.measurements.verification_batch_ms, ' ms') : 'unavailable'}</dd></div>
      <div><dt>Observed acceptance</dt><dd>{metric(plan.measurements.observed_acceptance_fraction === null ? null : plan.measurements.observed_acceptance_fraction * 100, '%')}</dd></div>
      <div><dt>Observed gain</dt><dd>{metric(plan.measurements.observed_gain_fraction === null ? null : plan.measurements.observed_gain_fraction * 100, '%')}</dd></div>
      <div><dt>Decision</dt><dd>{plan.decision.reason.replaceAll('_', ' ')}</dd></div>
    </dl>
    {view === 'inference' ? runtime.requests.length === 0 ? <p><strong>Target-only active.</strong> No draft proposals are admitted while the promotion gate is disabled.</p> : <div className={styles.tableWrap}><table><thead><tr><th>Request</th><th>Proposed</th><th>Verified</th><th>Accepted</th><th>Rollback</th><th>Fallback</th><th>Terminal</th></tr></thead><tbody>{runtime.requests.map((request) => <tr key={request.request_id}><th scope="row">{request.request_id}</th><td>{request.proposed_count}</td><td>{request.target_verified_count}</td><td>{request.accepted_count}</td><td>{request.rollback_count}</td><td>{request.fallback_state.replaceAll('_', ' ')}</td><td>{request.terminal_state} · cleaned</td></tr>)}</tbody></table></div> : null}
    {view === 'plans' ? <p>Target-only is the safe baseline. Promotion requires compatible identities, target-owned parity and at least {(plan.decision.material_gain_threshold * 100).toFixed(0)}% measured end-to-end gain for {plan.workload_id}.</p> : null}
    {view === 'readiness' ? <p>{enabled ? 'Parity, cleanup and material gain are qualified.' : `Speculation is disabled: ${plan.decision.reason.replaceAll('_', ' ')}. Target-only qualification remains unchanged.`}</p> : null}
    <p><small>{plan.privacy}</small></p>
  </section>;
}
