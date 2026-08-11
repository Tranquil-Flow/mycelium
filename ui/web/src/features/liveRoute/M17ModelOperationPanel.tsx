import type { M17ModelOperation } from './m17ModelOperation';
import styles from './LiveRouteWorkspace.module.css';

function bytes(value: number): string {
  return new Intl.NumberFormat('en-US', { maximumFractionDigits: 1 }).format(value / 2 ** 30) + ' GiB';
}

function milliseconds(value: number | null): string {
  if (value === null) return 'Not modeled';
  return value >= 1_000 ? `${(value / 1_000).toFixed(1)} s` : `${value.toFixed(1)} ms`;
}

function reason(value: string): string {
  const separator = value.indexOf(':');
  const code = separator === -1 ? value : value.slice(0, separator);
  const node = separator === -1 ? undefined : value.slice(separator + 1).trim();
  if (code === 'insufficient_disk') return `Not enough disk space${node ? ` on ${node}` : ''}`;
  if (code === 'insufficient_memory') return `Not enough safe memory${node ? ` on ${node}` : ''}`;
  if (code === 'missing_weight_artifact') return 'Model weights are incomplete in the local cache';
  if (code === 'swarm_capacity_feasible') return 'Fits the measured swarm but has not been provisioned and qualified';
  if (code === 'catalog_compatible') return 'Runtime-compatible, but not yet provisioned and qualified';
  if (code === 'architecture_adapter') return `Architecture adapter unavailable${node ? `: ${node}` : ''}`;
  if (code === 'runtime_adapter_unavailable') return `Runtime adapter unavailable${node ? `: ${node}` : ''}`;
  if (code === 'evidence_stale') return 'Capacity evidence is stale; re-evaluation is required';
  return value.replaceAll('_', ' ');
}

export type M17ModelOperationPanelView = 'plans' | 'readiness' | 'nodes' | 'incidents' | 'inference';

export function M17ModelOperationPanel({ operation, view }: { readonly operation: M17ModelOperation; readonly view: M17ModelOperationPanelView }) {
  const nowUnixMs = Date.now();
  const reports = new Map(operation.feasibility_reports.map((report) => [`${report.model_id}@${report.revision}`, report]));
  const lifecycle = new Map(operation.lifecycle.models.map((model) => [`${model.model_id}@${model.revision}`, model]));
  const evaluatedAlternatives = operation.entries.filter((entry) => {
    const model = lifecycle.get(`${entry.model_id}@${entry.revision}`);
    return reports.has(`${entry.model_id}@${entry.revision}`) && model?.selectable !== true;
  });
  return (
    <section className={styles.panel} aria-labelledby="m17-model-operation-title">
      <h2 id="m17-model-operation-title">Local models and swarm fit</h2>
      <p>
        Local catalog generation {operation.catalog_generation}. A model is selectable only after the swarm can fit it,
        its artifacts are provisioned, and its deployment passes qualification. Downloads require fresh operator approval.
      </p>
      {view === 'readiness' ? (
        <><dl className={styles.measurements}>
          <div><dt>Catalog entries</dt><dd>{operation.entries.length}</dd></div>
          <div><dt>Compatible</dt><dd>{operation.entries.filter((entry) => entry.state === 'compatible').length}</dd></div>
          <div><dt>Feasible</dt><dd>{operation.feasibility_reports.filter((report) => report.state === 'feasible').length}</dd></div>
          <div><dt>Selection authority</dt><dd>Qualified deployment registry</dd></div>
          <div><dt>Catalog route readiness</dt><dd>Never implied</dd></div>
          <div><dt>Download policy</dt><dd>Explicit approval required</dd></div>
          <div><dt>Active</dt><dd>{operation.lifecycle.models.filter((model) => model.state === 'active').length}</dd></div>
          <div><dt>Qualified standby</dt><dd>{operation.lifecycle.models.filter((model) => model.state === 'qualified').length}</dd></div>
          <div><dt>Fresh feasibility</dt><dd>{operation.feasibility_reports.filter((report) => report.evidence_valid_until_unix_ms >= nowUnixMs).length}</dd></div>
          <div><dt>Provisioning authorized now</dt><dd>{operation.feasibility_reports.filter((report) => report.provisioning_authorized && report.evidence_valid_until_unix_ms >= nowUnixMs).length}</dd></div>
        </dl><LifecycleTable operation={operation} /></>
      ) : view === 'nodes' ? (
        <div className={styles.tableWrap}><table>
          <thead><tr><th>Candidate / stage</th><th>Assigned range</th><th>Runtime</th><th>Memory envelope</th><th>Artifact / disk</th><th>Capacity envelope</th></tr></thead>
          <tbody>{operation.feasibility_reports.flatMap((report) => report.stages.map((stage) => <tr key={`${report.model_id}@${report.revision}:${stage.node_id}`}>
            <th scope="row">{report.model_id}<small> · {stage.node_id}</small></th>
            <td>[{stage.start_layer}, {stage.end_layer_exclusive})</td>
            <td>{stage.backend}<small> · {stage.dtype} · {stage.decode_mode}</small></td>
            <td>{bytes(stage.required_memory_bytes)} required<small> · {bytes(stage.available_memory_bytes)} available · {bytes(stage.headroom_bytes)} headroom · {bytes(stage.kv_bytes)} KV · {bytes(stage.activation_bytes)} activation · {bytes(stage.runtime_reserve_bytes)} reserve</small></td>
            <td>{bytes(stage.cached_artifact_bytes)} cached / {bytes(stage.missing_artifact_bytes)} missing<small> · {bytes(stage.required_disk_bytes)} staging need · {bytes(stage.disk_free_bytes)} free</small></td>
            <td>{stage.maximum_context_tokens.toLocaleString()} context<small> · concurrency {stage.maximum_concurrency} · transfer {milliseconds(stage.modeled_transfer_ms)} · service {milliseconds(stage.modeled_service_work_ms)}</small></td>
          </tr>))}</tbody>
        </table></div>
      ) : view === 'incidents' || view === 'inference' ? (
        <div className={styles.tableWrap}><table>
          <thead><tr><th>Other evaluated model</th><th>Availability</th><th>Capacity evidence</th><th>Reason</th></tr></thead>
          <tbody>{evaluatedAlternatives.map((entry) => {
            const state = lifecycle.get(`${entry.model_id}@${entry.revision}`);
            const report = reports.get(`${entry.model_id}@${entry.revision}`);
            return <tr key={`${entry.model_id}@${entry.revision}`}>
              <th scope="row">{entry.model_id}<small> · {entry.revision.slice(0, 8)}</small></th>
              <td>{report?.state === 'feasible' ? 'Fits, not deployed' : 'Unavailable'}</td>
              <td>{report === undefined ? 'Not evaluated' : report.evidence_valid_until_unix_ms >= nowUnixMs ? 'Current' : 'Stale — recheck required'}</td>
              <td>{reason(report?.reasons[0] ?? state?.reason ?? entry.reasons[0] ?? 'Not qualified')}</td>
            </tr>;
          })}</tbody>
        </table></div>
      ) : (
        <div className={styles.tableWrap}>
          <table>
            <thead><tr><th>Local model</th><th>Artifact</th><th>Catalog state</th><th>Feasibility envelope</th><th>Proposed contiguous allocation</th><th>Reason</th></tr></thead>
            <tbody>{operation.entries.map((entry) => {
              const report = reports.get(`${entry.model_id}@${entry.revision}`);
              const evidenceFresh = report !== undefined && report.evidence_valid_until_unix_ms >= nowUnixMs;
              const provisioningLabel = report?.provisioning_authorized === true
                ? evidenceFresh ? 'provisioning authorized' : 're-evaluation required'
                : 'transfer blocked';
              return <tr key={`${entry.model_id}@${entry.revision}`}>
                <th scope="row">{entry.model_id}<small> · {entry.revision.slice(0, 8)}</small></th>
                <td>{entry.quantization} · {entry.num_layers ?? 'unknown'} layers · {bytes(entry.weight_bytes)}</td>
                <td>{lifecycle.get(`${entry.model_id}@${entry.revision}`)?.state ?? entry.state}{entry.exact_tensor_accounting ? ' · exact bytes' : ''}</td>
                <td>{report ? <>{report.state}<small> · {provisioningLabel} · context {report.maximum_qualified_context_tokens.toLocaleString()} · concurrency {report.maximum_qualified_concurrency} · {bytes(report.cached_artifact_bytes)} cached / {bytes(report.missing_artifact_bytes)} missing · transfer {milliseconds(report.modeled_transfer_ms)} · execution {milliseconds(report.modeled_execution_ms)} · {report.resource_bottleneck.kind} bottleneck · evidence {evidenceFresh ? 'fresh' : 'stale'}</small></> : 'not evaluated'}</td>
                <td>{report?.stages.map((stage) => `${stage.node_id} [${stage.start_layer},${stage.end_layer_exclusive}) ${stage.backend}/${stage.quantization}`).join(' → ') ?? 'None'}</td>
                <td>{reason(report?.reasons[0] ?? entry.reasons[0] ?? 'None')}</td>
              </tr>;
            })}</tbody>
          </table>
        </div>
      )}
    </section>
  );
}

function LifecycleTable({ operation }: { readonly operation: M17ModelOperation }) {
  return <div className={styles.tableWrap}><table>
    <thead><tr><th>Model</th><th>State</th><th>Authority</th><th>Selectable</th><th>Reason</th></tr></thead>
    <tbody>{operation.lifecycle.models.map((model) => <tr key={`${model.model_id}@${model.revision}`}>
      <th scope="row">{model.model_id}<small> · {model.revision.slice(0, 8)}</small></th>
      <td>{model.state}</td><td>{model.authority.replaceAll('_', ' ')}</td><td>{model.selectable ? 'Yes' : 'No'}</td><td>{reason(model.reason)}</td>
    </tr>)}</tbody>
  </table></div>;
}
