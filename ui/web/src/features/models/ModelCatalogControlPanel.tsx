import { useState } from 'react';
import type { DeploymentActivationStatus } from '../liveRoute/deploymentActivation';
import type { M17ModelOperation } from '../liveRoute/m17ModelOperation';
import type { ModelCapacityRefreshStatus } from './modelCapacityRefresh';
import type { ModelPreparationStatus, ModelRepresentationDecision } from './modelPreparation';
import { humanReason, projectModelCatalogControls, type ModelCatalogRow } from './modelCatalogControl';
import styles from '../liveRoute/LiveRouteWorkspace.module.css';

function bytes(value: number): string {
  if (value === 0) return 'No complete weights';
  return `${new Intl.NumberFormat('en-US', { maximumFractionDigits: 1 }).format(value / 2 ** 30)} GiB`;
}

function publicReason(value: string): string {
  return value.replace(/^m\d+_/, '').replaceAll('_', ' ');
}

function Row({ row, busy, actionsAvailable, onActivate, onUnload, onPrepare, onReacquire }: { readonly row: ModelCatalogRow; readonly busy: boolean; readonly actionsAvailable: boolean; readonly onActivate: (candidateId: string) => void; readonly onUnload: (candidateId: string) => void; readonly onPrepare: (decision: ModelRepresentationDecision) => void; readonly onReacquire: (candidateId: string, decision: ModelRepresentationDecision) => void }) {
  const report = row.feasibility;
  const candidate = row.candidate;
  const [reviewing, setReviewing] = useState(false);
  const [conversionAuthorized, setConversionAuthorized] = useState(false);
  const servingRepresentation = row.entry.serving_representations?.find((item) =>
    item.quantization === report?.serving_quantization
    && item.runtime_dtype === report?.serving_dtype
    && item.representation_digest === report?.representation_digest
  ) ?? null;
  const representationBound = report?.source_quantization !== undefined
    && report.serving_dtype !== undefined
    && report.serving_quantization !== undefined
    && report.representation_digest !== undefined
    && servingRepresentation !== null;
  const conversionRequired = representationBound && report.source_quantization !== report.serving_quantization;
  const discoveredMemberCount = row.entry.discovered_member_count ?? 0;
  const decision = representationBound ? {
    protocol: 'mycelium.model_representation_decision.v2' as const,
    model_id: row.entry.model_id,
    revision: row.entry.revision,
    source_quantization: report.source_quantization!,
    serving_dtype: report.serving_dtype!,
    serving_quantization: report.serving_quantization!,
    representation_digest: report.representation_digest!,
    conversion_authorized: conversionRequired ? conversionAuthorized : false,
    source_artifact_digest: row.entry.artifact_digest,
    quantizer: servingRepresentation!.quantizer,
    download_authorized: false as const,
  } : null;
  return <tr>
    <th scope="row">{row.entry.model_id}<small> · {row.entry.revision.slice(0, 8)}</small></th>
    <td>{row.entry.quantization}{report?.serving_quantization === undefined || report.serving_quantization === row.entry.quantization ? null : <> → {report.serving_quantization}</>}<small> · {row.entry.num_layers ?? 'unknown'} layers · {bytes(row.entry.weight_bytes)}</small></td>
    <td><strong>{row.status_label}</strong><small> · {row.detail}</small></td>
    <td>{report === null ? 'Not evaluated' : report.state === 'feasible'
      ? `${report.stages.length} stages · ${report.maximum_qualified_context_tokens.toLocaleString()} token context`
      : report.resource_bottleneck.kind.replaceAll('_', ' ')}
      {report === null ? null : <small> · {(report.cached_artifact_bytes / 2 ** 30).toFixed(1)} GiB cached · {(report.missing_artifact_bytes / 2 ** 30).toFixed(1)} GiB transfer</small>}
    </td>
    <td>{(row.entry.discovery_scope ?? ['coordinator']).includes('coordinator') ? 'Coordinator' : 'Member inventory'}
      {discoveredMemberCount === 0 ? null : <small> · seen on {discoveredMemberCount} current {discoveredMemberCount === 1 ? 'member' : 'members'}</small>}
      {row.entry.metadata_reconciled !== false ? null : <small> · metadata not reconciled</small>}
    </td>
    <td>{!actionsAvailable && row.action !== null ? 'Preparation and activation controls are unavailable in this single-route session.' : (row.action === 'activate' || row.action === 'retry') && candidate !== null ? <div>
      <button type="button" disabled={busy} onClick={() => onActivate(candidate.candidate_id)}>{row.action === 'retry' ? 'Retry qualification' : 'Activate and qualify'}</button>
      {!representationBound ? null : <><button type="button" disabled={busy} onClick={() => setReviewing((value) => !value)}>{reviewing ? 'Close cache verification' : 'Verify cached copies'}</button>
        {!reviewing ? null : <div role="group" aria-label={`Cache verification for ${row.entry.model_id}`}>
          <p><small>Rechecks the exact prepared representation on every assigned device. It will fail instead of transferring missing bytes, downloading, activating, or changing the selected model.</small></p>
          {conversionRequired ? <label><input type="checkbox" checked={conversionAuthorized} onChange={(event) => setConversionAuthorized(event.target.checked)} /> I confirm this exact derived representation remains owner-approved.</label> : null}
          <button type="button" disabled={busy || (conversionRequired && !conversionAuthorized)} onClick={() => decision === null ? undefined : onReacquire(candidate.candidate_id, decision)}>Recheck exact device caches</button>
        </div>}
      </>}
    </div> : row.action === 'prepare' || row.action === 'retry_prepare' ? !representationBound ? 'Representation binding unavailable' : <div>
      <button type="button" disabled={busy} onClick={() => setReviewing((value) => !value)}>{reviewing ? 'Close representation review' : 'Review representation'}</button>
      {!reviewing ? null : <div role="group" aria-label={`Representation authorization for ${row.entry.model_id}`}>
        <p><strong>Exact representation</strong><small> · revision {row.entry.revision.slice(0, 8)} · {report!.source_quantization} → {report!.serving_quantization} ({report!.serving_dtype}) · {servingRepresentation!.quantizer} · {report!.representation_digest!.slice(0, 15)}…</small></p>
        <p><small>Source {row.entry.artifact_digest.slice(0, 15)}… · download remains disabled</small></p>
        {conversionRequired ? <label><input type="checkbox" checked={conversionAuthorized} onChange={(event) => setConversionAuthorized(event.target.checked)} /> I authorize creating this exact derived representation.</label> : <p>No representation conversion is required.</p>}
        <button type="button" disabled={busy || (conversionRequired && !conversionAuthorized)} onClick={() => decision === null ? undefined : onPrepare(decision)}>{conversionRequired ? 'Authorize representation and prepare' : 'Confirm representation and prepare'}</button>
      </div>}
    </div> : row.availability === 'qualified' && candidate !== null ? <button type="button" disabled={busy} onClick={() => onUnload(candidate.candidate_id)}>Unload from memory</button> : row.availability === 'qualified' ? 'Select above' : row.availability === 'active' ? 'Selected' : row.availability === 'preparing' ? 'In progress' : 'No action available'}</td>
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
  onReacquire = () => undefined,
  actionsAvailable = true,
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
  readonly onPrepare?: (decision: ModelRepresentationDecision) => void;
  readonly onReacquire?: (candidateId: string, decision: ModelRepresentationDecision) => void;
  readonly actionsAvailable?: boolean;
}) {
  const [query, setQuery] = useState('');
  const [availabilityFilter, setAvailabilityFilter] = useState<'all' | 'selectable' | 'actionable' | 'blocked'>('all');
  const rows = projectModelCatalogControls(operation, activation, nowUnixMs, preparation ?? null);
  const normalizedQuery = query.trim().toLocaleLowerCase();
  const filtered = rows.filter((row) => {
    const matchesQuery = normalizedQuery === '' || `${row.entry.model_id} ${row.entry.revision} ${row.entry.quantization} ${row.status_label}`.toLocaleLowerCase().includes(normalizedQuery);
    const selectable = row.availability === 'active' || row.availability === 'qualified';
    const actionable = row.action !== null;
    const matchesAvailability = availabilityFilter === 'all'
      || (availabilityFilter === 'selectable' && selectable)
      || (availabilityFilter === 'actionable' && actionable)
      || (availabilityFilter === 'blocked' && !selectable && !actionable);
    return matchesQuery && matchesAvailability;
  }).sort((left, right) => Number(right.prominent) - Number(left.prominent) || left.entry.model_id.localeCompare(right.entry.model_id));
  const capacityBusy = capacityRefresh?.state === 'refreshing';
  const busy = activation.busy_candidate_id !== null || capacityBusy || preparation?.state === 'preparing';
  const discovery = operation.discovery ?? { scope: 'unavailable' as const, accepted_member_count: 0, rejected_member_count: 0, blockers: ['member_inventory_scope_unavailable'] };
  return <section id="model-catalog" className={styles.panel} aria-labelledby="model-catalog-control-title">
    <div className={styles.panelTitlebar}><div><p className={styles.eyebrow}>Swarm model control</p><h2 id="model-catalog-control-title">Available models</h2></div><div><button type="button" onClick={onRefresh}>Refresh deployment status</button>{capacityRefresh === null ? null : <button type="button" disabled={capacityBusy} onClick={onRecheckCapacity}>{capacityBusy ? 'Rechecking capacity…' : 'Recheck swarm capacity'}</button>}</div></div>
    <p>
      This catalogue is generated from immutable model identities currently visible to the coordinator. It has no fixed model or device list; enrolled peer inventories enter this same live contract after signed discovery and reconciliation.
      Models become selectable above only after capacity planning,
      artifact verification, physical route loading, and distributed qualification all succeed. No action here downloads a model.
    </p>
    <p>Need more capacity? <a href="#nodes">Add or inspect swarm devices</a>. A new member contributes only after fresh capability evidence, planning, and route qualification.</p>
    {capacityRefresh?.state === 'refreshing' ? <p role="status">Capturing signed device resources and rerunning the layer-allocation planner. This does not download or provision model files.</p> : null}
    {capacityRefresh?.state === 'succeeded' ? <p role="status">Capacity checked across the current planned route: {capacityRefresh.evaluated_model_count} compatible local model {capacityRefresh.evaluated_model_count === 1 ? 'identity' : 'identities'} evaluated.</p> : null}
    {capacityRefresh?.state === 'failed' ? <p role="alert">Capacity recheck failed: {publicReason(capacityRefresh.reason_code ?? 'capacity refresh failed')}.</p> : null}
    {preparation?.state === 'preparing' ? <p role="status">{preparation.operation === 'warm_reacquire' ? 'Rechecking the exact prepared model against verified device caches' : `Preparing ${preparation.model_id} across ${preparation.topology_size ?? 'the planned'} stages`}. Only assignment-owned local files are considered; no download or activation is authorized.</p> : null}
    {preparation?.state === 'succeeded' && preparation.operation === 'warm_reacquire' ? <p role="status">Verified {preparation.cache_receipt_count} current cache {preparation.cache_receipt_count === 1 ? 'receipt' : 'receipts'} covering {bytes(preparation.cached_verified_bytes)} with {preparation.transferred_verified_bytes === 0 ? 'zero bytes' : bytes(preparation.transferred_verified_bytes)} transferred and {preparation.origin_bytes === 0 ? 'zero bytes' : bytes(preparation.origin_bytes)} from origin.</p> : null}
    {preparation?.state === 'failed' ? <p role="alert">Model preparation failed: {humanReason(preparation.reason_code ?? 'model preparation failed')}. The active model was not changed.</p> : null}
    <p>Discovery scope: {discovery.scope === 'coordinator_and_members' ? `coordinator plus ${discovery.accepted_member_count} current signed ${discovery.accepted_member_count === 1 ? 'member' : 'members'}` : discovery.scope === 'coordinator_only' ? 'coordinator cache only' : 'not recorded by this catalogue generation'}.</p>
    {discovery.rejected_member_count === 0 ? null : <p>Member discovery rejected {discovery.rejected_member_count} {discovery.rejected_member_count === 1 ? 'inventory' : 'inventories'}.</p>}
    {discovery.blockers.length === 0 ? null : <p>Reconciliation blockers: {discovery.blockers.map(publicReason).join(', ')}.</p>}
    <dl className={styles.measurements}>
      <div><dt>Discovered identities</dt><dd>{rows.length}</dd></div>
      <div><dt>Qualified choices</dt><dd>{rows.filter((row) => row.availability === 'qualified' || row.availability === 'active').length}</dd></div>
      <div><dt>Ready to activate</dt><dd>{rows.filter((row) => row.availability === 'ready_to_activate' || row.availability === 'activation_failed').length}</dd></div>
      <div><dt>Fits, needs preparation</dt><dd>{rows.filter((row) => row.availability === 'fits_swarm').length}</dd></div>
    </dl>
    <div className={styles.panelTitlebar}>
      <label>Find a model<input aria-label="Find a model" type="search" value={query} onChange={(event) => setQuery(event.currentTarget.value)} placeholder="Name, revision, or format" /></label>
      <label>Show<select aria-label="Filter model availability" value={availabilityFilter} onChange={(event) => setAvailabilityFilter(event.currentTarget.value as typeof availabilityFilter)}>
        <option value="all">All discovered models</option>
        <option value="selectable">Qualified choices</option>
        <option value="actionable">Ready for an action</option>
        <option value="blocked">Blocked or unsupported</option>
      </select></label>
    </div>
    <div className={styles.tableWrap}><table>
      <caption>{filtered.length} of {rows.length} discovered model identities</caption>
      <thead><tr><th>Model</th><th>Known representation</th><th>Availability</th><th>Swarm fit</th><th>Discovery</th><th>Action</th></tr></thead>
      <tbody>{filtered.map((row) => <Row key={row.identity} row={row} busy={busy} actionsAvailable={actionsAvailable} onActivate={onActivate} onUnload={onUnload} onPrepare={onPrepare} onReacquire={onReacquire} />)}</tbody>
    </table></div>
    {filtered.length === 0 ? <p>No discovered model matches these filters.</p> : null}
    {activation.invalid_candidate_count === 0 ? null : <p role="alert">{activation.invalid_candidate_count} unsafe or invalid prepared route {activation.invalid_candidate_count === 1 ? 'was' : 'were'} rejected.</p>}
    {error === null ? null : <p role="alert">Some model actions are unavailable: {publicReason(error)}. Existing qualified inference remains usable.</p>}
  </section>;
}
