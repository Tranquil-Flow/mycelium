import { useState } from 'react';
import type { ProductRouteId } from '../../app/navigation';
import { PRODUCT_API_PATHS } from '../../app/contracts';
import { useProductEvidence } from './ProductEvidenceContext';
import type { ProductEntity, ProductSnapshot } from './contracts';

function attribute(entity: ProductEntity, key: string): string {
  const value = entity.attributes[key];
  if (value === null || value === undefined) return 'Unavailable';
  if (Array.isArray(value)) return value.join(', ') || 'None';
  if (typeof value === 'boolean') return value ? 'Yes' : 'No';
  return String(value);
}

function entities(snapshot: ProductSnapshot, kind: ProductEntity['kind']): readonly ProductEntity[] {
  return snapshot.entities.filter((entity) => entity.kind === kind);
}

function SourceProvenance({ snapshot }: { readonly snapshot: ProductSnapshot }) {
  return (
    <details className="panel">
      <summary>Source provenance</summary>
      <table>
        <thead><tr><th>Source</th><th>Authority</th><th>Status</th><th>Generation</th><th>Reason</th></tr></thead>
        <tbody>{snapshot.source_states.map((source) => (
          <tr key={source.source_id}>
            <th scope="row">{source.source_id}</th>
            <td>{source.authority}</td>
            <td>{source.status}</td>
            <td>{source.generation ?? 'Independent / unavailable'}</td>
            <td>{source.reason_code ?? 'None'}</td>
          </tr>
        ))}</tbody>
      </table>
    </details>
  );
}

function ChangeSummary() {
  const evidence = useProductEvidence();
  if (evidence.visible === null) return null;
  const visibleCursor = evidence.visible.cursor;
  const previous = [...evidence.history]
    .reverse()
    .find((state) => state.cursor < visibleCursor);
  if (previous === undefined) {
    return <p role="status">First retained product snapshot; no prior generation is available for comparison.</p>;
  }
  const priorIds = new Set(previous.snapshot.entities.map((entity) => entity.entity_id));
  const currentIds = new Set(evidence.visible.snapshot.entities.map((entity) => entity.entity_id));
  const added = [...currentIds].filter((id) => !priorIds.has(id)).length;
  const removed = [...priorIds].filter((id) => !currentIds.has(id)).length;
  return <p role="status">Change since cursor {previous.cursor}: {added} added · {removed} removed · value-only changes retain stable entity keys.</p>;
}

export function ProductEvidenceSummary({ compact = false }: { readonly compact?: boolean }) {
  const evidence = useProductEvidence();
  if (evidence.loading) return <section className="panel" role="status">Loading unified product evidence…</section>;
  if (evidence.error_code !== null) return <section className="panel" role="alert">Unified product evidence unavailable. Live claims are withheld.</section>;
  if (evidence.visible === null) return null;
  const { snapshot } = evidence.visible;
  const devices = entities(snapshot, 'device');
  const routes = entities(snapshot, 'route');
  return (
    <section className="panel" aria-label="Unified product evidence source">
      <p className="eyebrow">Unified product evidence · {snapshot.publication.source_mode}</p>
      <h2>{compact ? 'Evidence source' : 'Coherent product snapshot'}</h2>
      <p>
        Cursor {snapshot.publication.cursor} · generation {snapshot.publication.generation} · {devices.length} device{devices.length === 1 ? '' : 's'} · {routes.length} route{routes.length === 1 ? '' : 's'}
        {evidence.frozen ? ' · frozen view' : ''}
      </p>
      {evidence.visible.status !== 'connected' || evidence.visible.freshness !== 'current' ? (
        <p role="status">Live claim withheld: {evidence.visible.reason_code ?? evidence.visible.freshness}</p>
      ) : null}
    </section>
  );
}

function NetworkProjection({ snapshot }: { readonly snapshot: ProductSnapshot }) {
  const [showLogical, setShowLogical] = useState(true);
  const [showPhysical, setShowPhysical] = useState(true);
  const stages = entities(snapshot, 'stage');
  const links = entities(snapshot, 'directed_link');
  const placements = new Map(
    snapshot.relations
      .filter((relation) => relation.kind === 'placed_on')
      .map((relation) => [relation.from_entity_id, relation.to_entity_id]),
  );
  return <>
    <section className="panel" aria-label="Network projection controls">
      <h2>Topology layers</h2>
      <button type="button" aria-pressed={showLogical} onClick={() => setShowLogical((value) => !value)}>Logical execution</button>{' '}
      <button type="button" aria-pressed={showPhysical} onClick={() => setShowPhysical((value) => !value)}>Physical links</button>
    </section>
    {showLogical ? <section className="panel"><h2>Logical execution pipeline</h2>
      <table><thead><tr><th>Stage</th><th>Layers</th><th>Device</th><th>Decode state</th></tr></thead><tbody>
        {stages.map((stage) => <tr key={stage.entity_id}><th scope="row">{stage.label}</th><td>[{attribute(stage, 'start_layer')}, {attribute(stage, 'end_layer_exclusive')})</td><td>{placements.get(stage.entity_id) ?? 'Unknown-location tray'}</td><td>{attribute(stage, 'decode_mode')}</td></tr>)}
      </tbody></table>
    </section> : null}
    {showPhysical ? <section className="panel"><h2>Physical directed links</h2><p>{links.length === 0 ? 'No activation-plane links are declared.' : `${links.length} directed route link${links.length === 1 ? '' : 's'} declared; connectivity remains unknown until M14 measurement evidence.`}</p></section> : null}
  </>;
}

function NodesProjection({ snapshot }: { readonly snapshot: ProductSnapshot }) {
  const devices = entities(snapshot, 'device');
  const assignments = entities(snapshot, 'assignment');
  const loadProofs = entities(snapshot, 'load_proof');
  return <><section className="panel"><h2>Durable membership and capability</h2>
    <table><thead><tr><th>Device</th><th>Class</th><th>Member generation</th><th>Authority generation</th><th>Lifecycle</th><th>Runtime</th><th>Lease</th><th>Activation eligible</th><th>Placement</th></tr></thead><tbody>
      {devices.map((device) => <tr key={device.entity_id}><th scope="row">{device.entity_id}</th><td>{attribute(device, 'peer_class')}</td><td>{attribute(device, 'membership_generation')}</td><td>{attribute(device, 'authority_generation')}</td><td>{attribute(device, 'lifecycle')}</td><td>{attribute(device, 'runtime_backend')} · {attribute(device, 'transport')}</td><td>{attribute(device, 'lease_freshness')}</td><td>{attribute(device, 'activation_eligible')}</td><td>{attribute(device, 'placement_id')}</td></tr>)}
    </tbody></table>
  </section><section className="panel"><h2>Assignment and load evidence</h2>
    {assignments.length === 0 ? <p>No live assignment records are available from this source.</p> : <table><thead><tr><th>Assignment</th><th>Stage</th><th>Device</th><th>Load generation</th></tr></thead><tbody>{assignments.map((assignment) => <tr key={assignment.entity_id}><th scope="row">{assignment.entity_id}</th><td>{attribute(assignment, 'stage_id')}</td><td>{attribute(assignment, 'device_id')}</td><td>{attribute(assignment, 'load_generation')}</td></tr>)}</tbody></table>}
    <p>{loadProofs.length} qualified load proof record{loadProofs.length === 1 ? '' : 's'} projected.</p>
  </section></>;
}

function PlansProjection({ snapshot }: { readonly snapshot: ProductSnapshot }) {
  const routes = entities(snapshot, 'route');
  const assignments = entities(snapshot, 'assignment');
  return <section className="panel"><p className="eyebrow violet">Placement provenance</p><h2>Operator-selected deployment</h2>
    {routes.map((route) => <dl key={route.entity_id}><div><dt>Route</dt><dd>{route.entity_id}</dd></div><div><dt>Model</dt><dd>{attribute(route, 'model_id')}</dd></div><div><dt>Provenance</dt><dd>{attribute(route, 'placement_provenance')}</dd></div><div><dt>Decode mode</dt><dd>{attribute(route, 'decode_mode')}</dd></div></dl>)}
    <p role="status">Capability-aware planner output is unsupported until M13. No fixture planner result is presented as live.</p>
    <p>{assignments.length} validated operator assignment{assignments.length === 1 ? '' : 's'} bound to the current product snapshot.</p>
  </section>;
}

function ReadinessProjection({ snapshot }: { readonly snapshot: ProductSnapshot }) {
  return <section className="panel"><h2>Independent readiness matrix</h2>
    <table><thead><tr><th>Scope</th><th>Dimension</th><th>State</th><th>Reason</th><th>Authority source</th></tr></thead><tbody>
      {snapshot.readiness.map((item, index) => <tr key={`${item.scope_id}-${item.dimension}-${index}`}><th scope="row">{item.scope_id}</th><td>{item.dimension}</td><td>{item.state}</td><td>{item.reason_code ?? 'Accepted'}</td><td>{item.source_id}</td></tr>)}
    </tbody></table>
  </section>;
}

function IncidentsProjection({ snapshot }: { readonly snapshot: ProductSnapshot }) {
  const evidence = useProductEvidence();
  const incidents = entities(snapshot, 'incident');
  return <><section className="panel"><h2>Source degradation and conflicts</h2>
    {snapshot.notices.length === 0 ? <p>No product-source notices in this snapshot.</p> : <ul>{snapshot.notices.map((notice) => <li key={notice.notice_id}>{notice.severity}: {notice.code} · {notice.scope_id}</li>)}</ul>}
  </section><section className="panel"><h2>Route incidents</h2>{incidents.length === 0 ? <p>No route incidents in this snapshot.</p> : <ul>{incidents.map((incident) => <li key={incident.entity_id}>{attribute(incident, 'state')}: {attribute(incident, 'reason_code')}</li>)}</ul>}</section>
  <section className="panel"><h2>Product evidence timeline</h2><ol>{evidence.history.map((state) => <li key={`${state.cursor}-${state.status}`}>Cursor {state.cursor} · generation {state.generation} · {state.source_mode} · {state.status}/{state.freshness}</li>)}</ol></section></>;
}

export function ProductEvidenceSettings() {
  const evidence = useProductEvidence();
  return <section className="panel"><h2>Evidence source controls</h2>
    <p>Freeze keeps the current immutable snapshot visible while the source continues receiving newer evidence.</p>
    <button type="button" onClick={evidence.frozen ? evidence.resume : evidence.freeze} disabled={evidence.visible === null}>{evidence.frozen ? 'Resume current evidence' : 'Freeze visible evidence'}</button>{' '}
    <a href={PRODUCT_API_PATHS.product_export} download="mycelium-product-evidence.json">Export pseudonymized snapshot</a>
  </section>;
}

export function ProductEvidenceWorkspace({ view }: { readonly view: ProductRouteId }) {
  const evidence = useProductEvidence();
  if (evidence.loading) return <ProductEvidenceSummary />;
  if (evidence.error_code !== null || evidence.visible === null) return <ProductEvidenceSummary />;
  const snapshot = evidence.visible.snapshot;
  return <div>
    <ProductEvidenceSummary />
    <ChangeSummary />
    {view === 'network' ? <NetworkProjection snapshot={snapshot} /> : null}
    {view === 'nodes' ? <NodesProjection snapshot={snapshot} /> : null}
    {view === 'plans' ? <PlansProjection snapshot={snapshot} /> : null}
    {view === 'readiness' ? <ReadinessProjection snapshot={snapshot} /> : null}
    {view === 'incidents' ? <IncidentsProjection snapshot={snapshot} /> : null}
    <SourceProvenance snapshot={snapshot} />
  </div>;
}

export function productEvidenceRouteReady(state: ReturnType<typeof useProductEvidence>['visible']): boolean {
  if (
    state === null
    || state.status !== 'connected'
    || state.source_mode !== 'live'
    || state.freshness !== 'current'
  ) return false;
  return state.snapshot.readiness.some(
    (item) => item.dimension === 'qualification' && item.state === 'ready',
  );
}
