import type { ArtifactAcquisitionLedger, ArtifactAcquisitionStatus } from './artifactAcquisition';
import styles from '../liveRoute/LiveRouteWorkspace.module.css';

export type ArtifactAcquisitionView = 'inference' | 'nodes' | 'plans' | 'readiness' | 'incidents' | 'settings';

function bytes(value: number): string {
  if (value === 0) return '0 B';
  const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB'];
  const power = Math.min(Math.floor(Math.log(value) / Math.log(1024)), units.length - 1);
  return `${(value / 1024 ** power).toFixed(power === 0 ? 0 : 1)} ${units[power]}`;
}

function rate(value: number): string { return value === 0 ? 'Idle' : `${bytes(value)}/s`; }
function state(value: string): string { return value.replaceAll('_', ' '); }
function short(value: string): string { return `${value.slice(0, 15)}…`; }
function terminalRecords(ledger: ArtifactAcquisitionLedger): readonly ArtifactAcquisitionStatus[] { return ledger.history.slice(-12).reverse(); }

function CurrentSummary({ acquisition }: { readonly acquisition: ArtifactAcquisitionStatus }) {
  const verified = acquisition.cached_verified_bytes + acquisition.transferred_verified_bytes;
  const progress = acquisition.total_bytes === 0 ? 0 : Math.round(verified / acquisition.total_bytes * 100);
  return <>
    <p role="status"><strong>{acquisition.model_id}</strong> · {state(acquisition.state)} · {progress}% verified</p>
    <dl className={styles.measurements}>
      <div><dt>Assignment</dt><dd>{acquisition.placement_id} · layers [{acquisition.layer_start}, {acquisition.layer_end_exclusive})</dd></div>
      <div><dt>Representation</dt><dd>{acquisition.representation}</dd></div>
      <div><dt>Verified</dt><dd>{bytes(verified)} / {bytes(acquisition.total_bytes)}</dd></div>
      <div><dt>Sources</dt><dd>{acquisition.active_source_count} active / {acquisition.eligible_source_count} eligible</dd></div>
      <div><dt>Transfer</dt><dd>{rate(acquisition.aggregate_bytes_per_second)}{acquisition.eta_seconds === null ? '' : ` · ${acquisition.eta_seconds.toFixed(1)} s ETA`}</dd></div>
      <div><dt>Warm reuse</dt><dd>{bytes(acquisition.duplicate_bytes_prevented)} duplicate transfer prevented</dd></div>
    </dl>
  </>;
}

export function ArtifactAcquisitionPanel({ ledger, view }: { readonly ledger: ArtifactAcquisitionLedger; readonly view: ArtifactAcquisitionView }) {
  const current = ledger.current;
  const recent = terminalRecords(ledger);
  const latest = current ?? recent[0] ?? null;
  return <section className={styles.panel} aria-labelledby={`artifact-acquisition-${view}-title`}>
    <p className={styles.eyebrow}>Provisioner-owned artifact lifecycle</p>
    <h2 id={`artifact-acquisition-${view}-title`}>Swarm artifact acquisition</h2>
    <p>Only assignment-owned chunks from explicitly authorized sources are accepted. Verified acquisition prepares bytes for loading; it never makes a model selectable without independent physical qualification.</p>
    {current === null ? <p role="status">No artifact acquisition is running.{recent.length === 0 ? ' No acquisition has been recorded on this Provisioner yet.' : ` ${recent.length} recent terminal acquisition ${recent.length === 1 ? 'is' : 'are'} retained below.`}</p> : <CurrentSummary acquisition={current} />}
    {view === 'nodes' && latest !== null ? <div className={styles.tableWrap}><table>
      <caption>Privacy-reduced artifact sources for {latest.placement_id}</caption>
      <thead><tr><th>Source</th><th>State</th><th>Verified contribution</th></tr></thead>
      <tbody>{latest.sources.map((source) => <tr key={source.source_ref}><th scope="row">{source.source_ref}</th><td>{state(source.state)}</td><td>{bytes(source.verified_bytes)}</td></tr>)}</tbody>
    </table></div> : null}
    {view === 'plans' && latest !== null ? <dl className={styles.measurements}>
      <div><dt>Allocation</dt><dd>{latest.stage_id} · [{latest.layer_start}, {latest.layer_end_exclusive})</dd></div>
      <div><dt>Source strategy</dt><dd>{latest.eligible_source_count > 1 ? `Up to ${latest.eligible_source_count} authorized swarm sources` : latest.eligible_source_count === 1 ? 'Single authorized swarm source' : latest.origin_bytes > 0 ? 'Approved origin fallback' : 'No source recorded'}</dd></div>
      <div><dt>Origin fallback used</dt><dd>{bytes(latest.origin_bytes)}</dd></div>
      <div><dt>Representation binding</dt><dd><code>{short(latest.representation_digest)}</code></dd></div>
      <div><dt>Assignment binding</dt><dd><code>{short(latest.assignment_digest)}</code></dd></div>
      <div><dt>Feasibility generation</dt><dd>{latest.evidence_generation} · <code>{short(latest.feasibility_digest)}</code></dd></div>
    </dl> : null}
    {view === 'readiness' && latest !== null ? <dl className={styles.measurements}>
      <div><dt>Manifest bound</dt><dd>{latest.manifest_digest ? 'Verified contract' : 'Blocked'}</dd></div>
      <div><dt>Authorization</dt><dd>{latest.assignment_digest && latest.representation_digest ? 'Exact assignment and representation bound' : 'Blocked'}</dd></div>
      <div><dt>Chunks verified</dt><dd>{latest.verified_chunk_count} / {latest.chunk_count}</dd></div>
      <div><dt>Pack promotion</dt><dd>{latest.promotion_digest === null ? 'Not promoted' : `Promoted · ${short(latest.promotion_digest)}`}</dd></div>
      <div><dt>Runtime load</dt><dd>Separate load proof required</dd></div>
      <div><dt>Route qualification</dt><dd>Separate physical qualification required</dd></div>
    </dl> : null}
    {view === 'settings' ? <dl className={styles.measurements}>
      <div><dt>Protocol</dt><dd>Signed assignment-local chunks</dd></div>
      <div><dt>Source identities</dt><dd>Privacy-reduced per acquisition</dd></div>
      <div><dt>Downloads</dt><dd>Never authorized by this panel</dd></div>
      <div><dt>Retained terminal records</dt><dd>{ledger.history.length}</dd></div>
    </dl> : null}
    {(view === 'incidents' || view === 'inference') && recent.length > 0 ? <div className={styles.tableWrap}><table>
      <caption>Recent terminal artifact acquisitions</caption>
      <thead><tr><th>Model / placement</th><th>Result</th><th>Transfer evidence</th><th>Reason</th></tr></thead>
      <tbody>{recent.map((item) => <tr key={item.acquisition_id}><th scope="row">{item.model_id}<small> · {item.placement_id} · [{item.layer_start}, {item.layer_end_exclusive})</small></th><td>{state(item.state)}<small> · {item.verified_chunk_count}/{item.chunk_count} chunks · {bytes(item.cached_verified_bytes)} cached</small></td><td>{item.sources.length} sources · {bytes(item.transferred_verified_bytes)} swarm · {bytes(item.origin_bytes)} origin<small> · {item.source_rotation_count} rotations · {bytes(item.quarantined_bytes)} quarantined</small></td><td>{item.reason_code === null ? item.state === 'ready' ? 'Verified and atomically promoted; load and qualification remain separate' : 'Cancelled without promotion' : `${state(item.reason_code)}${item.retryable ? ' · retryable' : ''}`}</td></tr>)}</tbody>
    </table></div> : null}
  </section>;
}
