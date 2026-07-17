import { useMemo, useState } from 'react';
import { projectFailoverOverlay } from '../model/failover';
import type { FailoverIncident, FailoverMode, FailoverOverlayRoute } from '../model/types';

interface IncidentsViewProps {
  readonly incidents: readonly FailoverIncident[];
}

const modeCopy: Record<FailoverMode, { short: string; heading: string; description: string }> = {
  stable_drain: {
    short: 'Stable drain',
    heading: 'Stable drain',
    description: 'Planned departure',
  },
  active_failover: {
    short: 'Active',
    heading: 'Active failover',
    description: 'Checkpoint replacement',
  },
  circuit_break: {
    short: 'Circuit break',
    heading: 'Safety circuit engaged',
    description: 'Abort unsafe output',
  },
};

function routeState(route: FailoverOverlayRoute): string {
  if (route.role === 'replacement') return 'active replacement';
  return route.state === 'failed' ? 'failed prior route' : `${route.state} prior route`;
}

const triggerCopy: Record<FailoverMode, { label: string; className: string }> = {
  stable_drain: { label: 'Departing peer', className: 'draining-peer' },
  active_failover: { label: 'Suspect peer', className: 'failed-peer' },
  circuit_break: { label: 'Request-local trigger', className: 'unsafe-trigger-peer' },
};

export function IncidentsView({ incidents }: IncidentsViewProps) {
  const initial = incidents.find((incident) => incident.mode === 'active_failover') ?? incidents[0];
  const [selectedId, setSelectedId] = useState(initial.id);
  const selected = incidents.find((incident) => incident.id === selectedId) ?? initial;
  const overlay = useMemo(() => projectFailoverOverlay(selected), [selected]);
  const circuitIncident = incidents.find((incident) => incident.mode === 'circuit_break');
  const circuitOverlay = circuitIncident === undefined ? null : projectFailoverOverlay(circuitIncident);

  return (
    <div className="view incidents-view">
      <header className="view-heading incident-heading">
        <div>
          <p className="eyebrow caution">Synthetic incident laboratory</p>
          <h2>Failover replay</h2>
          <p className="view-description">
            {incidents.length} offline {incidents.length === 1 ? 'scenario exposes' : 'scenarios expose'}
            {' '}distinct drain, replacement, and abort semantics.
          </p>
        </div>
        <div className="fixture-warning">
          <i aria-hidden="true">!</i>
          <div><strong>No live incident occurred</strong><span>fixture timeline · render-only projection</span></div>
        </div>
      </header>

      <section className="incident-mode-strip" aria-label="Failover scenario modes">
        {incidents.map((incident, index) => {
          const copy = modeCopy[incident.mode];
          return (
            <button
              type="button"
              key={incident.id}
              className={`incident-mode ${incident.mode}`}
              aria-label={`Inspect ${copy.short} scenario`}
              aria-pressed={incident.id === selected.id}
              onClick={() => setSelectedId(incident.id)}
            >
              <span className="mode-index">0{index + 1}</span>
              <span className="mode-copy"><strong>{copy.short}</strong><small>{copy.description}</small></span>
              <span className={`mode-status ${incident.status}`}>{incident.status}</span>
            </button>
          );
        })}
      </section>

      <div className="incident-layout">
        <section className="incident-detail panel" aria-labelledby="incident-title">
          <div className="incident-detail-header">
            <div>
              <span className="panel-kicker">{selected.id} · epoch {selected.deploymentEpoch}</span>
              <h3 id="incident-title">{modeCopy[selected.mode].heading}</h3>
              <p>{selected.title}</p>
            </div>
            <div className="incident-state-stack">
              <span className={`mode-status ${selected.status}`}>{selected.status}</span>
              <span className="synthetic-tag">synthetic</span>
            </div>
          </div>

          <div className="route-generation-panel" aria-label="Route generation comparison">
            <div className="route-generation-head">
              <span>Route generations</span>
              <small>Static overlay · topology position retained</small>
            </div>
            <div className="generation-stack">
              {overlay.routes.map((route) => (
                <article key={`${route.role}-${route.generation}`} className={`generation-route ${route.role}`}>
                  <div className="generation-label">
                    <span>{route.role === 'old' ? 'Prior route' : 'Replacement route'}</span>
                    <strong>{route.label}</strong>
                    <small>{routeState(route)}</small>
                  </div>
                  <div className="generation-path">
                    {route.nodeIds.map((nodeId, index) => (
                      <span
                        key={nodeId}
                        className={
                          nodeId === overlay.triggerPeerId && route.role === 'old'
                            ? triggerCopy[selected.mode].className
                            : undefined
                        }
                      >
                        <i aria-hidden="true">{index + 1}</i>
                        {nodeId}
                      </span>
                    ))}
                  </div>
                </article>
              ))}
              {overlay.routes.length === 1 ? (
                <div className="no-replacement-route">
                  <span aria-hidden="true">×</span>
                  Replacement route intentionally absent
                </div>
              ) : null}
            </div>
          </div>

          <dl className="incident-facts">
            <div className={`trigger-fact ${triggerCopy[selected.mode].className}`}>
              <dt>{triggerCopy[selected.mode].label}</dt>
              <dd>{overlay.triggerPeerId}<small>{selected.trigger.kind} · {selected.trigger.scope}</small></dd>
            </div>
            <div>
              <dt>Checkpoint</dt>
              <dd>{overlay.checkpointLabel}<small>{selected.cutover.policy.replaceAll('_', ' ')}</small></dd>
            </div>
            <div>
              <dt>Backup readiness</dt>
              <dd>{selected.backupReadiness}<small>compatibility {selected.compatibility}</small></dd>
            </div>
            <div className="outcome-fact">
              <dt>Actual fixture outcome</dt>
              <dd>{overlay.outcome}</dd>
            </div>
          </dl>

          <div className="incident-boundary">
            <strong>Claim boundary</strong>
            <p>{selected.sourceClaimBoundary}</p>
          </div>
        </section>

        <aside className="timeline-panel panel" aria-labelledby="timeline-title">
          <div className="panel-titlebar compact">
            <div>
              <span className="panel-kicker">Request {selected.requestIds.join(', ')}</span>
              <h3 id="timeline-title">Transition timeline</h3>
            </div>
            <span className="elapsed-time">{selected.transitions.at(-1)?.atMs ?? 0} ms</span>
          </div>
          <ol className="incident-timeline">
            {selected.transitions.map((transition, index) => (
              <li key={`${transition.state}-${transition.atMs}`} className={index === selected.transitions.length - 1 ? 'final' : undefined}>
                <span className="timeline-node" aria-hidden="true" />
                <time>+{transition.atMs} ms</time>
                <strong>{transition.state.replaceAll('_', ' ')}</strong>
                <p>{transition.detail}</p>
              </li>
            ))}
          </ol>

          {selected.mode !== 'circuit_break' && circuitOverlay !== null ? (
            <div className="safety-stop-summary" aria-label="Circuit-break summary">
              <span className="stop-icon" aria-hidden="true">■</span>
              <div>
                <strong>Safety-stop fixture</strong>
                <p>{circuitOverlay.outcome}</p>
              </div>
            </div>
          ) : null}
        </aside>
      </div>
    </div>
  );
}
