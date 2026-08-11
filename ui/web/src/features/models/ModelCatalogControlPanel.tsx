import type { DeploymentActivationStatus } from '../liveRoute/deploymentActivation';
import type { M17ModelOperation } from '../liveRoute/m17ModelOperation';
import type { ModelCapacityRefreshStatus } from './modelCapacityRefresh';
import type { ModelPreparationStatus } from './modelPreparation';
import { projectModelCatalogControls, type ModelCatalogRow } from './modelCatalogControl';
import styles from '../liveRoute/LiveRouteWorkspace.module.css';

function bytes(value: number): string {
  if (value === 0) return 'No complete weights';
  return `${new Intl.NumberFormat('en-US', { maximumFractionDigits: 1 }).format(value / 2 ** 30)} GiB`;
}

function Row({ row, busy, onActivate, onUnload, onPrepare }: { readonly row: ModelCatalogRow; readonly busy: boolean; readonly onActivate: (candidateId: string) => void; readonly onUnload: (candidateId: string) => void; readonly onPrepare: (modelId: string, revision: string) => void }) {
  const report = row.feasibility;
  const candidate = row.candidate;
  return <tr>
    <th scope="row">{row.entry.model_id}<small> · {row.entry.revision.slice(0, 8)}</small></th>
    <td>{row.entry.quantization}{report?.serving_quantization === undefined || report.serving_quantization === row.entry.quantization ? null : <> → {report.serving_quantization}</>}<small> · {row.entry.num_layers ?? 'unknown'} layers · {bytes(row.entry.weight_bytes)}</small></td>
    <td><strong>{row.status_label}</strong><small> · {row.detail}</small></td>
    <td>{report === null ? 'Not evaluated' : report.state === 'feasible'
      ? `${report.stages.length} stages · ${report.maximum_qualified_context_tokens.toLocaleString()} token context`
      : report.resource_bottleneck.kind.replaceAll('_', ' ')}
      {report === null ? null : <small> · {(report.cached_artifact_bytes / 2 ** 30).toFixed(1)} GiB cached · {(report.missing_artifact_bytes / 2 ** 30).toFixed(1)} GiB transfer</small>}
    </td>
    <td>{(row.action === 'activate' || row.action === 'retry') && candidate !== null ? <button type="button" disabled={busy} onClick={() => onActivate(candidate.candidate_id)}>
      {row.action === 'retry' ? 'Retry qualification' : 'Activate and qualify'}
    </button> : row.action === 'prepare' || row.action === 'retry_prepare' ? <button type="button" disabled={busy} onClick={() => onPrepare(row.entry.model_id, row.entry.revision)}>{row.action === 'retry_prepare' ? 'Retry preparation' : 'Prepare on swarm'}</button> : row.availability === 'qualified' && candidate !== null ? <button type="button" disabled={busy} onClick={() => onUnload(candidate.candidate_id)}>Unload from memory</button> : row.availability === 'qualified' ? 'Select above' : row.availability === 'active' ? 'Selected' : row.availability === 'preparing' ? 'In progress' : 'No action available'}</td>
  </tr>;
}

export function ModelCatalogControlPanel({
  operation,
  activation,
  nowUnixMs,
  error,
  onActivate,
  onUnload = () => undefined,
  onRefresh,
  capacityRefresh,
  onRecheckCapacity,
  preparation,
  onPrepare = () => undefined,
}: {
  readonly operation: M17ModelOperation;
  readonly activation: DeploymentActivationStatus;
  readonly nowUnixMs: number;
  readonly error: string | null;
  readonly onActivate: (candidateId: string) => void;
  readonly onUnload?: (candidateId: string) => void;
  readonly onRefresh: () => void;
  readonly capacityRefresh: ModelCapacityRefreshStatus | null;
  readonly onRecheckCapacity: () => void;
  readonly preparation?: ModelPreparationStatus | null;
  readonly onPrepare?: (modelId: string, revision: string) => void;
}) {
  const rows = projectModelCatalogControls(operation, activation, nowUnixMs, preparation ?? null);
  const prominent = rows.filter((row) => row.prominent);
  const other = rows.filter((row) => !row.prominent);
  const capacityBusy = capacityRefresh?.state === 'refreshing';
  const busy = activation.busy_candidate_id !== null || capacityBusy || preparation?.state === 'preparing';
  return <section className={styles.panel} aria-labelledby="model-catalog-control-title">
    <div className={styles.panelTitlebar}><div><p className={styles.eyebrow}>Swarm model control</p><h2 id="model-catalog-control-title">Model catalog</h2></div><div><button type="button" onClick={onRefresh}>Refresh deployment status</button>{capacityRefresh === null ? null : <button type="button" disabled={capacityBusy} onClick={onRecheckCapacity}>{capacityBusy ? 'Rechecking capacity…' : 'Recheck swarm capacity'}</button>}</div></div>
    <p>
      This catalog is discovered from local model files. Models become selectable only after capacity planning,
      artifact verification, physical route loading, and distributed qualification all succeed. No action here downloads a model.
    </p>
    <p>Need more capacity? <a href="#nodes">Add or inspect swarm devices</a>. A new member contributes only after fresh capability evidence, planning, and route qualification.</p>
    {capacityRefresh?.state === 'refreshing' ? <p role="status">Capturing signed device resources and rerunning the layer-allocation planner. This does not download or provision model files.</p> : null}
    {capacityRefresh?.state === 'succeeded' ? <p role="status">Capacity checked across the current planned route: {capacityRefresh.evaluated_model_count} compatible local model {capacityRefresh.evaluated_model_count === 1 ? 'identity' : 'identities'} evaluated.</p> : null}
    {capacityRefresh?.state === 'failed' ? <p role="alert">Capacity recheck failed: {(capacityRefresh.reason_code ?? 'capacity refresh failed').replaceAll('_', ' ')}.</p> : null}
    {preparation?.state === 'preparing' ? <p role="status">Preparing {preparation.model_id} across {preparation.topology_size ?? 'the planned'} stages. Only assignment-owned local files are transferred; no download or activation is authorized.</p> : null}
    {preparation?.state === 'failed' ? <p role="alert">Model preparation failed: {(preparation.reason_code ?? 'model preparation failed').replaceAll('_', ' ')}. The active model was not changed.</p> : null}
    <dl className={styles.measurements}>
      <div><dt>Local identities</dt><dd>{rows.length}</dd></div>
      <div><dt>Qualified choices</dt><dd>{rows.filter((row) => row.availability === 'qualified' || row.availability === 'active').length}</dd></div>
      <div><dt>Ready to activate</dt><dd>{rows.filter((row) => row.availability === 'ready_to_activate' || row.availability === 'activation_failed').length}</dd></div>
      <div><dt>Fits, needs preparation</dt><dd>{rows.filter((row) => row.availability === 'fits_swarm').length}</dd></div>
    </dl>
    <div className={styles.tableWrap}><table>
      <thead><tr><th>Model</th><th>Local artifact</th><th>Availability</th><th>Swarm fit</th><th>Action</th></tr></thead>
      <tbody>{prominent.map((row) => <Row key={row.identity} row={row} busy={busy} onActivate={onActivate} onUnload={onUnload} onPrepare={onPrepare} />)}</tbody>
    </table></div>
    {other.length === 0 ? null : <details><summary>Show {other.length} other local model {other.length === 1 ? 'identity' : 'identities'}</summary>
      <div className={styles.tableWrap}><table>
        <thead><tr><th>Model</th><th>Local artifact</th><th>Availability</th><th>Swarm fit</th><th>Action</th></tr></thead>
        <tbody>{other.map((row) => <Row key={row.identity} row={row} busy={busy} onActivate={onActivate} onUnload={onUnload} onPrepare={onPrepare} />)}</tbody>
      </table></div>
    </details>}
    {activation.invalid_candidate_count === 0 ? null : <p role="alert">{activation.invalid_candidate_count} unsafe or invalid prepared route {activation.invalid_candidate_count === 1 ? 'was' : 'were'} rejected.</p>}
    {error === null ? null : <p role="alert">Model control request failed: {error.replaceAll('_', ' ')}</p>}
  </section>;
}
