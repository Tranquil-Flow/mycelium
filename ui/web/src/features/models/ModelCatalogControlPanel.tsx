import type { DeploymentActivationStatus } from '../liveRoute/deploymentActivation';
import type { M17ModelOperation } from '../liveRoute/m17ModelOperation';
import { projectModelCatalogControls, type ModelCatalogRow } from './modelCatalogControl';
import styles from '../liveRoute/LiveRouteWorkspace.module.css';

function bytes(value: number): string {
  if (value === 0) return 'No complete weights';
  return `${new Intl.NumberFormat('en-US', { maximumFractionDigits: 1 }).format(value / 2 ** 30)} GiB`;
}

function Row({ row, busy, onActivate }: { readonly row: ModelCatalogRow; readonly busy: boolean; readonly onActivate: (candidateId: string) => void }) {
  const report = row.feasibility;
  const candidate = row.candidate;
  return <tr>
    <th scope="row">{row.entry.model_id}<small> · {row.entry.revision.slice(0, 8)}</small></th>
    <td>{row.entry.quantization}<small> · {row.entry.num_layers ?? 'unknown'} layers · {bytes(row.entry.weight_bytes)}</small></td>
    <td><strong>{row.status_label}</strong><small> · {row.detail}</small></td>
    <td>{report === null ? 'Not evaluated' : report.state === 'feasible'
      ? `${report.stages.length} stages · ${report.maximum_qualified_context_tokens.toLocaleString()} token context`
      : report.resource_bottleneck.kind.replaceAll('_', ' ')}
      {report === null ? null : <small> · {(report.cached_artifact_bytes / 2 ** 30).toFixed(1)} GiB cached · {(report.missing_artifact_bytes / 2 ** 30).toFixed(1)} GiB transfer</small>}
    </td>
    <td>{row.action !== null && candidate !== null ? <button type="button" disabled={busy} onClick={() => onActivate(candidate.candidate_id)}>
      {row.action === 'retry' ? 'Retry qualification' : 'Activate and qualify'}
    </button> : row.availability === 'qualified' ? 'Select above' : row.availability === 'active' ? 'Selected' : 'No action available'}</td>
  </tr>;
}

export function ModelCatalogControlPanel({
  operation,
  activation,
  nowUnixMs,
  error,
  onActivate,
  onRefresh,
}: {
  readonly operation: M17ModelOperation;
  readonly activation: DeploymentActivationStatus;
  readonly nowUnixMs: number;
  readonly error: string | null;
  readonly onActivate: (candidateId: string) => void;
  readonly onRefresh: () => void;
}) {
  const rows = projectModelCatalogControls(operation, activation, nowUnixMs);
  const prominent = rows.filter((row) => row.prominent);
  const other = rows.filter((row) => !row.prominent);
  const busy = activation.busy_candidate_id !== null;
  return <section className={styles.panel} aria-labelledby="model-catalog-control-title">
    <div className={styles.panelTitlebar}><div><p className={styles.eyebrow}>Swarm model control</p><h2 id="model-catalog-control-title">Model catalog</h2></div><button type="button" onClick={onRefresh}>Refresh deployment status</button></div>
    <p>
      This catalog is discovered from local model files. Models become selectable only after capacity planning,
      artifact verification, physical route loading, and distributed qualification all succeed. No action here downloads a model.
    </p>
    <p>Need more capacity? <a href="#nodes">Add or inspect swarm devices</a>. A new member contributes only after fresh capability evidence, planning, and route qualification.</p>
    <dl className={styles.measurements}>
      <div><dt>Local identities</dt><dd>{rows.length}</dd></div>
      <div><dt>Qualified choices</dt><dd>{rows.filter((row) => row.availability === 'qualified' || row.availability === 'active').length}</dd></div>
      <div><dt>Ready to activate</dt><dd>{rows.filter((row) => row.availability === 'ready_to_activate' || row.availability === 'activation_failed').length}</dd></div>
      <div><dt>Fits, needs preparation</dt><dd>{rows.filter((row) => row.availability === 'fits_swarm').length}</dd></div>
    </dl>
    <div className={styles.tableWrap}><table>
      <thead><tr><th>Model</th><th>Local artifact</th><th>Availability</th><th>Swarm fit</th><th>Action</th></tr></thead>
      <tbody>{prominent.map((row) => <Row key={row.identity} row={row} busy={busy} onActivate={onActivate} />)}</tbody>
    </table></div>
    {other.length === 0 ? null : <details><summary>Show {other.length} other local model {other.length === 1 ? 'identity' : 'identities'}</summary>
      <div className={styles.tableWrap}><table>
        <thead><tr><th>Model</th><th>Local artifact</th><th>Availability</th><th>Swarm fit</th><th>Action</th></tr></thead>
        <tbody>{other.map((row) => <Row key={row.identity} row={row} busy={busy} onActivate={onActivate} />)}</tbody>
      </table></div>
    </details>}
    {activation.invalid_candidate_count === 0 ? null : <p role="alert">{activation.invalid_candidate_count} unsafe or invalid prepared route {activation.invalid_candidate_count === 1 ? 'was' : 'were'} rejected.</p>}
    {error === null ? null : <p role="alert">Model control request failed: {error.replaceAll('_', ' ')}</p>}
  </section>;
}
