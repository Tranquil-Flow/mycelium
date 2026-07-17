import { useState } from 'react';
import type { EvidenceRoute, EvidenceSnapshot } from '../model/types';

interface PlansViewProps {
  readonly snapshot: EvidenceSnapshot;
}

function metric(value: number, digits = 1): string {
  return new Intl.NumberFormat('en-US', {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  }).format(value);
}

function StrategyMetric({ value, unit }: { readonly value: number; readonly unit: string }) {
  return (
    <span className="table-metric">
      <strong>{metric(value)}</strong>
      <small>{unit}</small>
    </span>
  );
}

function findInitialRoute(routes: readonly EvidenceRoute[]): EvidenceRoute {
  return routes.find((route) => route.id === 'throughput_pruned_local') ?? routes[0];
}

export function PlansView({ snapshot }: PlansViewProps) {
  const initial = findInitialRoute(snapshot.routes);
  const [selectedId, setSelectedId] = useState(initial.id);
  const selected = snapshot.routes.find((route) => route.id === selectedId) ?? initial;

  return (
    <div className="view plans-view">
      <header className="view-heading">
        <div>
          <p className="eyebrow lime">Simulator plan space</p>
          <h2>Strategy comparison</h2>
          <p className="view-description">
            Relative estimates from one bundled workload. Values are modeled, not measured performance.
          </p>
        </div>
        <div className="model-boundary-card">
          <span className="synthetic-tag">synthetic</span>
          <div>
            <strong>{snapshot.routes.length} comparable {snapshot.routes.length === 1 ? 'plan' : 'plans'}</strong>
            <span>same fixture · same workload</span>
          </div>
        </div>
      </header>

      <section className="comparison-panel panel" aria-labelledby="comparison-table-title">
        <div className="panel-titlebar table-titlebar">
          <div>
            <span className="panel-kicker">Modeled estimates · not observations</span>
            <h3 id="comparison-table-title">Candidate ranking surface</h3>
          </div>
          <div className="table-legend">
            <span><i className="legend-dot lime-dot" /> higher throughput</span>
            <span><i className="legend-dot violet-dot" /> lower latency</span>
          </div>
        </div>
        <div className="table-scroll">
          <table className="strategy-table">
            <thead>
              <tr>
                <th scope="col">Strategy</th>
                <th scope="col">Active peers</th>
                <th scope="col">Combined tok/s</th>
                <th scope="col">Decode tok/s</th>
                <th scope="col">Prefill tok/s</th>
                <th scope="col">Single request tok/s</th>
                <th scope="col">Decode ms/token</th>
                <th scope="col"><span className="sr-only">Inspect</span></th>
              </tr>
            </thead>
            <tbody>
              {snapshot.routes.map((route) => (
                <tr key={route.id} className={selected.id === route.id ? 'is-selected' : undefined}>
                  <th scope="row">
                    <span className="strategy-name">{route.id}</span>
                    <span className="synthetic-inline">synthetic</span>
                  </th>
                  <td>{route.nodeOrder.length}</td>
                  <td><StrategyMetric value={route.metrics.combinedTokensPerSecond.value} unit="modeled" /></td>
                  <td><StrategyMetric value={route.metrics.decodeTokensPerSecond.value} unit="modeled" /></td>
                  <td><StrategyMetric value={route.metrics.prefillTokensPerSecond.value} unit="modeled" /></td>
                  <td><StrategyMetric value={route.metrics.singleRequestTokensPerSecond.value} unit="modeled" /></td>
                  <td><StrategyMetric value={route.metrics.decodeLatencyMsPerToken.value} unit="modeled" /></td>
                  <td>
                    <button
                      type="button"
                      className="inspect-strategy"
                      aria-label={`Inspect ${route.id}`}
                      aria-pressed={selected.id === route.id}
                      onClick={() => setSelectedId(route.id)}
                    >
                      View
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>

      <div className="plan-detail-grid">
        <section className="selected-plan panel" aria-labelledby="selected-plan-title">
          <div className="panel-titlebar compact">
            <div>
              <span className="panel-kicker">Selected strategy</span>
              <h3 id="selected-plan-title">Plan summary</h3>
            </div>
            <span className="synthetic-tag">synthetic</span>
          </div>
          <p className="selected-strategy-name">{selected.simulatorStrategy}</p>
          <div className="plan-route-line" aria-label="Selected strategy stage order">
            {selected.nodeOrder.map((nodeId, index) => (
              <span key={nodeId}>
                <i>{index + 1}</i>{nodeId}
              </span>
            ))}
          </div>
          <dl className="plan-summary-list">
            <div><dt>Ring identifier</dt><dd>{selected.ringId}</dd></div>
            <div><dt>Path class</dt><dd>{selected.pathClass} · priority {selected.pathPriority}</dd></div>
            <div><dt>Layer coverage</dt><dd>0–{snapshot.model.numLayers - 1} · complete</dd></div>
            <div><dt>Workload</dt><dd>{snapshot.workload.concurrentRequests} requests · {snapshot.workload.contextWindow} context</dd></div>
          </dl>
        </section>

        <section className="formula-panel panel" aria-labelledby="formula-title">
          <div className="panel-titlebar compact">
            <div>
              <span className="panel-kicker">Interpretation boundary</span>
              <h3 id="formula-title">Formula annotations</h3>
            </div>
            <span className="formula-symbol" aria-hidden="true">ƒ</span>
          </div>
          <div className="formula-row">
            <span className="formula-kind">Decode</span>
            <code>Σ stage compute + Σ handoff + decode closure</code>
            <p>Per-token modeled latency across the ordered route.</p>
          </div>
          <div className="formula-row">
            <span className="formula-kind">Prefill</span>
            <code>Σ stage prefill compute + Σ outgoing handoff</code>
            <p>Prompt pass estimate for the fixture workload envelope.</p>
          </div>
          <div className="formula-warning">
            <i aria-hidden="true">!</i>
            <p><strong>Do not treat as measured.</strong> Device compute, link cost, and throughput are synthetic simulator inputs and outputs.</p>
          </div>
        </section>
      </div>
    </div>
  );
}
