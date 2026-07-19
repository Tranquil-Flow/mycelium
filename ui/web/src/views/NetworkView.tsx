import { useEffect, useMemo, useState } from 'react';
import type { GraphLayout } from '../graph/graph';
import type { EvidenceRoute, EvidenceSnapshot } from '../model/types';
import { RouteCanvas } from '../components/RouteCanvas';
import { NodeDetail } from './NodeDetail';
import { StageDetail } from './StageDetail';

interface NetworkViewProps {
  readonly snapshot: EvidenceSnapshot;
}

const layouts: ReadonlyArray<{ id: GraphLayout; label: string; hint: string }> = [
  { id: 'pipeline', label: 'Pipeline', hint: 'Stage order' },
  { id: 'ring', label: 'Ring', hint: 'Decode loop' },
  { id: 'scc', label: 'SCC', hint: 'Condensed cycles' },
  { id: 'geo', label: 'Elastic geo', hint: 'Distance compressed' },
  { id: 'map', label: 'True map', hint: 'WGS84 projection' },
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
  const [selectedStageId, setSelectedStageId] = useState(route.stages[0].id);

  useEffect(() => {
    setSelectedStageId(route.stages[0].id);
  }, [route.id, route.stages]);

  const selectedStage = useMemo(
    () => route.stages.find((stage) => stage.id === selectedStageId) ?? route.stages[0],
    [route.stages, selectedStageId],
  );
  const evidenceNode = snapshot.nodes.find((node) => node.id === selectedStage.nodeId);
  const selectedGraphNodeId = `stage:${route.id}:${selectedStage.id}`;
  const selectGraphNode = (graphNodeId: string): void => {
    const prefix = `stage:${route.id}:`;
    if (graphNodeId.startsWith(prefix)) setSelectedStageId(graphNodeId.slice(prefix.length));
  };

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
            selectedNodeId={selectedGraphNodeId}
            onNodeSelect={selectGraphNode}
          />
        </div>

        <aside className="inspector panel" aria-label="Selected stage detail">
          <StageDetail route={route} stage={selectedStage} node={evidenceNode ?? null} />
          <NodeDetail node={evidenceNode ?? null} links={snapshot.links} />
        </aside>
      </div>
    </div>
  );
}
