import type { M23KvEvidence } from './m23Kv';
import styles from './LiveRouteWorkspace.module.css';

export type M23KvView = 'inference' | 'nodes' | 'plans' | 'readiness' | 'incidents';

function bytes(value: number): string {
  return `${(value / (1024 * 1024)).toFixed(2)} MiB`;
}

export function M23KvPanel({ evidence, view }: { readonly evidence: M23KvEvidence; readonly view: M23KvView }) {
  const improvement = `${(evidence.measurements.tpot_improvement_ratio * 100).toFixed(1)}%`;
  return <section className={styles.panel} aria-label={`M23 heterogeneous KV gate for ${view}`}>
    <p className={styles.eyebrow}>M23 · heterogeneous stage-local KV</p>
    <h2>{evidence.promotion_state === 'qualified' ? 'Stage-local KV qualified' : 'Stage-local KV withheld'}</h2>
    {view === 'inference' && <dl className={styles.measurements}><div><dt>Measured TPOT</dt><dd>{evidence.measurements.kv_tpot_ms.toFixed(1)} ms</dd></div><div><dt>Replay baseline</dt><dd>{evidence.measurements.replay_tpot_ms.toFixed(1)} ms</dd></div><div><dt>Improvement</dt><dd>{improvement}</dd></div></dl>}
    {view === 'nodes' && <dl className={styles.measurements}><div><dt>One-token decode</dt><dd>{evidence.gates.one_token_decode_every_stage ? 'Verified on every stage' : 'Not verified'}</dd></div><div><dt>Physical counters</dt><dd>{evidence.gates.all_stages_advanced_physical_counters ? 'Advanced on every stage' : 'Not verified'}</dd></div><div><dt>KV cleanup</dt><dd>{evidence.gates.kv_active_then_terminally_released ? 'Active then released to zero' : 'Not verified'}</dd></div></dl>}
    {view === 'plans' && <dl className={styles.measurements}><div><dt>A/B route binding</dt><dd>{evidence.gates.same_route_model_stages_hosts && evidence.gates.same_prompt_and_budget ? 'Same route, model, hosts, prompt, and budget' : 'Not comparable'}</dd></div><div><dt>Exact output parity</dt><dd>{evidence.gates.exact_output_parity ? 'Verified' : 'Failed'}</dd></div><div><dt>Activation output</dt><dd>{bytes(evidence.measurements.replay_activation_output_bytes)} → {bytes(evidence.measurements.kv_activation_output_bytes)}</dd></div><div><dt>TPOT reduction</dt><dd>{improvement}</dd></div></dl>}
    {view === 'readiness' && <dl className={styles.measurements}>{Object.entries(evidence.gates).map(([gate, passed]) => <div key={gate}><dt>{gate.replaceAll('_', ' ')}</dt><dd>{passed ? 'Passed' : 'Withheld'}</dd></div>)}</dl>}
    {view === 'incidents' && <p>{evidence.gates.no_fatal_or_cleanup_failure ? 'No fatal or KV cleanup failure occurred in the sealed physical A/B.' : 'The sealed A/B contains a fatal or cleanup failure.'}</p>}
    <small>{evidence.claim_boundary}</small>
  </section>;
}
