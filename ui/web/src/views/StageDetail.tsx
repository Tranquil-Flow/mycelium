import type { EvidenceNode, EvidenceRoute, EvidenceRouteStage } from '../model/types';

export interface StageDetailProps {
  readonly route: EvidenceRoute;
  readonly stage: EvidenceRouteStage;
  readonly node: EvidenceNode | null;
}

function format(value: number, digits = 2): string {
  return new Intl.NumberFormat('en-US', { maximumFractionDigits: digits }).format(value);
}

export function StageDetail({ route, stage, node }: StageDetailProps) {
  return (
    <section aria-labelledby="stage-detail-title">
      <div className="panel-titlebar compact">
        <div>
          <span className="panel-kicker">Exact stage instance</span>
          <h3 id="stage-detail-title">Stage {stage.id}</h3>
        </div>
        <span className="stage-number">S{String(route.stages.indexOf(stage) + 1).padStart(2, '0')}</span>
      </div>
      <div className="inspector-identity">
        <span className="device-orbit" aria-hidden="true"><i /></span>
        <div><strong>{stage.nodeId}</strong><span>Route {route.id}</span></div>
      </div>
      <dl className="inspector-list">
        <div><dt>Half-open layers</dt><dd>[{stage.startLayer}, {stage.endLayerExclusive})<small>L{stage.startLayer}–{stage.endLayerExclusive - 1} · {stage.layerCount} layers</small></dd></div>
        <div><dt>Path role</dt><dd>{stage.pathClass}<small>Priority {stage.pathPriority}</small></dd></div>
        <div><dt>Stage memory</dt><dd>{format(stage.memory.vramUsedGb)} GB VRAM<small>{format(stage.memory.ramUsedGb)} GB RAM · {format(stage.memory.weightsGb)} GB weights</small></dd></div>
        <div><dt>Decode</dt><dd>{format(stage.metrics.decodeComputeMs.value)} ms compute<small>{format(stage.metrics.decodeOutgoingMs.value)} ms outgoing</small></dd></div>
        <div><dt>Prefill</dt><dd>{format(stage.metrics.prefillComputeMs.value)} ms compute<small>{format(stage.metrics.prefillOutgoingMs.value)} ms outgoing</small></dd></div>
        <div><dt>Device memory mode</dt><dd>{node?.resources.unifiedMemory ? 'Unified memory' : 'Discrete or unknown'}<small>{node === null ? 'Device evidence unavailable' : `${format(node.resources.vramAvailableGb)} GB VRAM available`}</small></dd></div>
      </dl>
      <div className="provenance-block">
        <span>Claim boundary</span>
        <strong><i aria-hidden="true" /> {stage.provenance} stage projection</strong>
        <p>Stage placement is inspected evidence; this panel does not execute inference or alter Router state.</p>
      </div>
    </section>
  );
}

export default StageDetail;
