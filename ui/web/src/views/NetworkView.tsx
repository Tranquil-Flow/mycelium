import { useEffect, useMemo, useState } from 'react';
import type { GraphLayout } from '../graph/graph';
import type { EvidenceRoute, EvidenceSnapshot } from '../model/types';
import { RouteCanvas } from '../components/RouteCanvas';

interface NetworkViewProps {
  readonly snapshot: EvidenceSnapshot;
}

const layouts: ReadonlyArray<{ id: GraphLayout; label: string; hint: string }> = [
  { id: 'pipeline', label: 'Pipeline', hint: 'Stage order' },
  { id: 'ring', label: 'Ring', hint: 'Decode loop' },
  { id: 'geo', label: 'Geo', hint: 'Synthetic location' },
];

function formatNumber(value: number, digits = 1): string {
  return new Intl.NumberFormat('en-US', {
    maximumFractionDigits: digits,
    minimumFractionDigits: digits,
  }).format(value);
}

function routeTitle(route: EvidenceRoute): string {
  return route.id.replaceAll('_', ' ');
}

export function NetworkView({ snapshot }: NetworkViewProps) {
  const preferredRoute = snapshot.routes.find((route) => route.id === 'global_best_shortest_subset');
  const [routeId, setRouteId] = useState(preferredRoute?.id ?? snapshot.routes[0].id);
  const [layout, setLayout] = useState<GraphLayout>('pipeline');
  const route = snapshot.routes.find((candidate) => candidate.id === routeId) ?? snapshot.routes[0];
  const [selectedNodeId, setSelectedNodeId] = useState(route.stages[0].nodeId);

  useEffect(() => {
    setSelectedNodeId(route.stages[0].nodeId);
  }, [route.id, route.stages]);

  const selectedStage = useMemo(
    () => route.stages.find((stage) => stage.nodeId === selectedNodeId) ?? route.stages[0],
    [route.stages, selectedNodeId],
  );
  const evidenceNode = snapshot.nodes.find((node) => node.id === selectedStage.nodeId);

  return (
    <div className="view network-view">
      <header className="view-heading">
        <div>
          <p className="eyebrow lime">Offline route projection</p>
          <h2>Network topology</h2>
          <p className="view-description">
            Read-only stage placement from bundled simulator evidence. Selection changes inspection only.
          </p>
        </div>
        <div className="header-facts" aria-label="Fixture summary">
          <div><span>Model</span><strong>{snapshot.model.id}</strong></div>
          <div><span>Layers</span><strong>{snapshot.model.numLayers}</strong></div>
          <div><span>Peers</span><strong>{snapshot.nodes.length}</strong></div>
        </div>
      </header>

      <section className="route-toolbar" aria-label="Route graph controls">
        <div className="strategy-control">
          <label htmlFor="route-strategy">Route strategy</label>
          <div className="select-wrap">
            <select
              id="route-strategy"
              value={route.id}
              onChange={(event) => setRouteId(event.target.value)}
            >
              {snapshot.routes.map((candidate) => (
                <option key={candidate.id} value={candidate.id}>{candidate.id}</option>
              ))}
            </select>
          </div>
          <span className="control-meta">{route.ringId} · {route.stages.length} stages</span>
        </div>

        <div className="layout-control">
          <span className="control-label">Layout projection</span>
          <div className="segmented-control">
            {layouts.map((item) => (
              <button
                type="button"
                key={item.id}
                aria-pressed={layout === item.id}
                onClick={() => setLayout(item.id)}
              >
                <span>{item.label}</span>
                <small>{item.hint}</small>
              </button>
            ))}
          </div>
        </div>

        <div className="route-provenance">
          <span className="synthetic-tag">synthetic</span>
          <div>
            <span>Evidence state</span>
            <strong>offline · modeled</strong>
          </div>
        </div>
      </section>

      <section className="metric-strip" aria-label="Selected route modeled metrics">
        <div>
          <span>Combined throughput <em>synthetic</em></span>
          <strong>{formatNumber(route.metrics.combinedTokensPerSecond.value)} <small>tok/s</small></strong>
        </div>
        <div>
          <span>Decode throughput <em>synthetic</em></span>
          <strong>{formatNumber(route.metrics.decodeTokensPerSecond.value)} <small>tok/s</small></strong>
        </div>
        <div>
          <span>Prefill latency <em>synthetic</em></span>
          <strong>{formatNumber(route.metrics.prefillLatencyMs.value)} <small>ms</small></strong>
        </div>
        <div>
          <span>Network workload <em>synthetic</em></span>
          <strong>{formatNumber(route.metrics.networkWorkloadCostMs.value)} <small>ms</small></strong>
        </div>
      </section>

      <div className="network-grid">
        <div className="graph-panel panel">
          <div className="panel-titlebar">
            <div>
              <span className="panel-kicker">Primary path · priority {route.pathPriority}</span>
              <h3>{routeTitle(route)}</h3>
            </div>
            <div className="read-only-state"><i aria-hidden="true" /> read only</div>
          </div>
          <RouteCanvas
            snapshot={snapshot}
            route={route}
            layout={layout}
            selectedNodeId={selectedStage.nodeId}
            onNodeSelect={setSelectedNodeId}
          />
        </div>

        <aside className="inspector panel" aria-label="Selected stage detail">
          <div className="panel-titlebar compact">
            <div>
              <span className="panel-kicker">Selection inspector</span>
              <h3>Stage {selectedStage.id.slice(-8)}</h3>
            </div>
            <span className="stage-number">S{String(route.stages.indexOf(selectedStage) + 1).padStart(2, '0')}</span>
          </div>

          <div className="inspector-identity">
            <span className="device-orbit" aria-hidden="true"><i /></span>
            <div>
              <strong>{selectedStage.nodeId}</strong>
              <span>
                {evidenceNode?.location.state === 'known'
                  ? `${evidenceNode.location.city}, ${evidenceNode.location.country}`
                  : 'Unknown location · not inferred'}
              </span>
            </div>
          </div>

          <dl className="inspector-list">
            <div>
              <dt>Layer range</dt>
              <dd>
                L{selectedStage.startLayer}–{selectedStage.endLayerExclusive - 1}
                <small>[{selectedStage.startLayer}, {selectedStage.endLayerExclusive}) normalized</small>
              </dd>
            </div>
            <div>
              <dt>Compute</dt>
              <dd>
                {formatNumber(evidenceNode?.resources.gpuTeraflops ?? 0)} GPU TFLOPS
                <small>{formatNumber(evidenceNode?.resources.cpuTeraflops ?? 0)} CPU TFLOPS</small>
              </dd>
            </div>
            <div>
              <dt>Stage memory</dt>
              <dd>
                {formatNumber(selectedStage.memory.vramUsedGb, 2)} GB VRAM
                <small>{formatNumber(selectedStage.memory.ramUsedGb, 2)} GB RAM · {formatNumber(selectedStage.memory.weightsGb, 2)} GB weights</small>
              </dd>
            </div>
            <div>
              <dt>Device</dt>
              <dd>
                {evidenceNode?.resources.unifiedMemory ? 'Unified memory' : 'Discrete accelerator'}
                <small>{formatNumber(evidenceNode?.resources.vramAvailableGb ?? 0)} GB VRAM available</small>
              </dd>
            </div>
            <div>
              <dt>Decode stage</dt>
              <dd>
                {formatNumber(selectedStage.metrics.decodeComputeMs.value, 2)} ms compute
                <small>{formatNumber(selectedStage.metrics.decodeOutgoingMs.value, 2)} ms outgoing</small>
              </dd>
            </div>
          </dl>

          <div className="provenance-block">
            <span>Provenance</span>
            <strong><i aria-hidden="true" /> synthetic simulator fixture</strong>
            <p>No runtime telemetry or peer location was observed.</p>
          </div>
        </aside>
      </div>
    </div>
  );
}
