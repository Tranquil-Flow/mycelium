import { useEffect, useMemo, useState } from 'react';
import { HttpGovernanceReadinessClient, type GovernanceReadiness, type GovernanceReadinessClient } from './governanceReadiness';
import styles from '../liveRoute/LiveRouteWorkspace.module.css';

export function GovernanceReadinessSource({ client }: { readonly client?: GovernanceReadinessClient }) {
  const fallback = useMemo(() => new HttpGovernanceReadinessClient(), []);
  const source = client ?? fallback;
  const [evidence, setEvidence] = useState<GovernanceReadiness | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    const controller = new AbortController();
    void source.load(controller.signal).then((value) => { setEvidence(value); setError(null); }).catch((reason) => { if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : 'governance_readiness_unavailable'); });
    return () => controller.abort();
  }, [source]);
  if (evidence === null) return <section className={styles.panel} aria-labelledby="governance-readiness-title"><h2 id="governance-readiness-title">Build governance</h2><p role={error === null ? 'status' : 'alert'}>{error ?? 'Loading source and contract governance…'}</p></section>;
  return <section className={styles.panel} aria-labelledby="governance-readiness-title">
    <p className={styles.eyebrow}>Executable source boundary</p>
    <h2 id="governance-readiness-title">Build governance</h2>
    <p role="status"><strong>Not release-ready.</strong> Static governance {evidence.governance_gate_ok ? 'passes' : 'fails'}, but open runtime and physical gates remain.</p>
    <dl className={styles.measurements}>
      <div><dt>Source</dt><dd>{evidence.source_commit === null ? 'Unresolved' : evidence.source_commit.slice(0, 12)} · {evidence.source_worktree_clean ? 'clean' : 'modified'}</dd></div>
      <div><dt>Ledger</dt><dd>{evidence.ledger_protocol} · {evidence.ledger_digest.slice(7, 19)}</dd></div>
      <div><dt>Contracts</dt><dd>{evidence.contract_manifest_protocol} · {evidence.contract_manifest_digest.slice(7, 19)}</dd></div>
      <div><dt>Reviewed actions</dt><dd>{evidence.authorized_product_action_count}</dd></div>
      <div><dt>Capabilities</dt><dd>{evidence.capability_count}</dd></div>
      <div><dt>Tracked milestones</dt><dd>{evidence.milestone_count}</dd></div>
    </dl>
    <details><summary>{evidence.release_exclusions.length} current release exclusions</summary><ul>{evidence.release_exclusions.map((item) => <li key={item}>{item}</li>)}</ul></details>
  </section>;
}
