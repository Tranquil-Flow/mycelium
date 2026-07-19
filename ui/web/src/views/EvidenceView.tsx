import { useState } from 'react';
import type {
  EvidenceProvenance,
  EvidenceSnapshot,
  FailoverIncident,
  ProvisioningEvidence,
} from '../model/types';
import { buildReadinessModel, READINESS_STAGES, readinessStateLabel } from '../features/readiness/readinessModel';
import { buildEvidenceTimeline, evidenceSources } from '../features/readiness/evidenceHistory';
import { createPseudonymizedExport } from '../features/readiness/pseudonymizedExport';

interface EvidenceViewProps {
  readonly snapshot: EvidenceSnapshot;
  readonly incidents: readonly FailoverIncident[];
  readonly provisioning: ProvisioningEvidence;
}

interface SourceLedgerEntry {
  readonly kind: string;
  readonly name: string;
  readonly detail: string;
  readonly provenance: EvidenceProvenance;
}

function yesNo(value: boolean): string {
  return value ? 'YES' : 'NO';
}

function simulatorSource(
  fileName: string,
  snapshot: EvidenceSnapshot,
): SourceLedgerEntry {
  if (fileName === 'hypothetical-six-node.json') {
    return {
      kind: 'JSON',
      name: fileName,
      detail: 'scenario · device and link inputs',
      provenance: 'synthetic',
    };
  }
  if (fileName === 'planner-simulation.json') {
    return {
      kind: 'JSON',
      name: fileName,
      detail: snapshot.source.reportProtocol,
      provenance: 'synthetic',
    };
  }
  if (fileName === 'synthetic-geo.json') {
    return {
      kind: 'GEO',
      name: fileName,
      detail: snapshot.source.geographyProtocol,
      provenance: 'synthetic',
    };
  }
  return {
    kind: 'JSON',
    name: fileName,
    detail: 'validated simulator fixture input',
    provenance: 'synthetic',
  };
}

export function EvidenceView({ snapshot, incidents, provisioning }: EvidenceViewProps) {
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [timelineCursor, setTimelineCursor] = useState(0);
  const [exportJson, setExportJson] = useState<string | null>(null);
  const routeReadiness = provisioning.routeReady ? 'YES' : 'NOT PROVEN';
  const routeSubgate = provisioning.routeReady ? 'NOT SEPARATELY REPORTED' : 'NOT PROVEN';
  const sourceLedger: readonly SourceLedgerEntry[] = [
    ...snapshot.source.fixtureFiles.map((fileName) => simulatorSource(fileName, snapshot)),
    {
      kind: 'MAN',
      name: 'ui-fixture-manifest.json',
      detail: snapshot.source.manifestProtocol,
      provenance: 'synthetic',
    },
    {
      kind: 'EVT',
      name: 'failover-scenarios.json',
      detail: `${incidents.length} offline incident ${incidents.length === 1 ? 'mode' : 'modes'}`,
      provenance: 'synthetic',
    },
    {
      kind: 'PLAN',
      name: 'manual-provisioning-route-v1.json',
      detail: `separate provisioning scope · ${provisioning.protocols.manualProvisioningRoute}`,
      provenance: provisioning.provenance,
    },
    {
      kind: 'AUD',
      name: 'provisioning-audit.json',
      detail: new Date(provisioning.auditedAt).toISOString(),
      provenance: provisioning.provenance,
    },
  ];
  const readiness = buildReadinessModel(provisioning);
  const sources = evidenceSources(snapshot, provisioning, incidents);
  const timeline = buildEvidenceTimeline(snapshot, provisioning, incidents);
  const timelineIndex = Math.max(0, Math.min(timelineCursor, timeline.length - 1));
  const timelineFrame = timeline[timelineIndex];

  return (
    <div className="view evidence-view">
      <header className="view-heading">
        <div>
          <p className="eyebrow cyan">Independent evidence readout</p>
          <h2>Proof Matrix</h2>
          <p className="view-description">
            Artifact facts, simulator claims, and executable-route readiness remain separate.
          </p>
        </div>
        <div className="evidence-seal" aria-label="Offline evidence bundle verified for display">
          <span aria-hidden="true">✓</span>
          <div><strong>Bundle parsed</strong><small>{snapshot.protocol}</small></div>
        </div>
      </header>

      <section className="separate-scope-card panel" aria-labelledby="provisioning-scope-title">
        <div className="separate-scope-banner">
          <span className="separate-scope-mark" aria-hidden="true">◇</span>
          <div className="separate-scope-copy">
            <p className="panel-kicker">Separate claim scope</p>
            <h3 id="provisioning-scope-title">Independent provisioning capture</h3>
            <p>
              This separate scope is not part of the active simulation and must not be read as
              simulator route evidence.
            </p>
          </div>
          <span className="scope-badge">{provisioning.scope.replaceAll('_', ' ')}</span>
        </div>

        <dl className="separate-scope-facts">
          <div><dt>Model ID</dt><dd>{provisioning.model.id}</dd></div>
          <div><dt>Peer IDs</dt><dd>{provisioning.nodeIds.join(', ')}</dd></div>
          <div><dt>Layer count</dt><dd>{provisioning.model.numLayers}</dd></div>
          <div><dt>Assignments</dt><dd>{provisioning.assignments.length}</dd></div>
        </dl>

        <div className="scope-protocols" aria-label="Provisioning source protocols">
          <div>
            <span>Manual provisioning route protocol</span>
            <code>{provisioning.protocols.manualProvisioningRoute}</code>
          </div>
          <div>
            <span>Provisioning audit protocol</span>
            <code>{provisioning.protocols.provisioningAudit}</code>
          </div>
          <div>
            <span>UI evidence protocol</span>
            <code>{provisioning.protocol}</code>
          </div>
        </div>

        <div className="proof-grid provisioning-proof-grid" aria-label="Provisioning and route readiness comparison">
          <article className={`proof-card ${provisioning.readyForRuntimeLoad ? 'proof-positive' : 'proof-negative'}`}>
            <div className="proof-card-head">
              <span className="proof-icon" aria-hidden="true">↧</span>
              <span className={`proof-result ${provisioning.readyForRuntimeLoad ? 'yes' : 'no'}`}>
                {yesNo(provisioning.readyForRuntimeLoad)}
              </span>
            </div>
            <p className="panel-kicker">Artifact gate</p>
            <h4>Ready for runtime load</h4>
            <p>
              Provisioning reports verified artifacts for the next runtime step. Passing this gate
              does not establish executable-route readiness.
            </p>
            <dl>
              <div><dt>Assignments verified</dt><dd>{yesNo(provisioning.allAssignmentsVerified)}</dd></div>
              <div><dt>Verified peers</dt><dd>{provisioning.nodeIds.length}</dd></div>
              <div><dt>Audit errors</dt><dd>{provisioning.errors.length}</dd></div>
            </dl>
          </article>

          <article className={`proof-card ${provisioning.routeReady ? 'proof-positive' : 'proof-negative'}`}>
            <div className="proof-card-head">
              <span className="proof-icon" aria-hidden="true">⤨</span>
              <span className={`proof-result ${provisioning.routeReady ? 'yes' : 'unproven'}`}>
                {routeReadiness}
              </span>
            </div>
            <p className="panel-kicker">Execution gate</p>
            <h4>Route ready</h4>
            <p>
              {provisioning.routeReady
                ? 'The audit reports executable-route readiness without separately reporting each underlying runtime subgate.'
                : 'Runtime layer load, stage probe, and end-to-end route challenge remain required by the audit.'}
            </p>
            <dl>
              <div><dt>Runtime load proven</dt><dd>{routeSubgate}</dd></div>
              <div><dt>Stage probe proven</dt><dd>{routeSubgate}</dd></div>
              <div><dt>Route challenge proven</dt><dd>{routeSubgate}</dd></div>
            </dl>
          </article>
        </div>

        <div className="scope-boundary-grid">
          <article className="claim-boundary" aria-labelledby="audit-boundary-title">
            <div className="boundary-marker" aria-hidden="true">!</div>
            <div>
              <p className="panel-kicker">Verbatim provisioning audit boundary</p>
              <h4 id="audit-boundary-title">What the audit does not prove</h4>
              <blockquote>{provisioning.sourceClaimBoundaries.provisioningAudit}</blockquote>
            </div>
            <span className="source-protocol">{provisioning.protocols.provisioningAudit}</span>
          </article>

          <article className="claim-boundary manual-provisioning-route-boundary" aria-labelledby="plan-boundary-title">
            <div className="boundary-marker" aria-hidden="true">!</div>
            <div>
              <p className="panel-kicker">Verbatim manual provisioning route boundary</p>
              <h4 id="plan-boundary-title">Allocation source limit</h4>
              <blockquote>{provisioning.sourceClaimBoundaries.manualProvisioningRoute}</blockquote>
            </div>
            <span className="source-protocol">{provisioning.protocols.manualProvisioningRoute}</span>
          </article>
        </div>
      </section>

      <section className="panel" aria-labelledby="readiness-matrix-title">
        <h3 id="readiness-matrix-title">Node-by-stage readiness matrix</h3>
        <p>Ready for runtime load is not runtime loaded; each proof gate remains independent.</p>
        <div className="table-scroll"><table className="strategy-table" aria-label="Node-by-stage readiness matrix"><thead><tr><th scope="col">Node / assignment</th>{READINESS_STAGES.map((stage) => <th scope="col" key={stage.id}>{stage.label}</th>)}</tr></thead><tbody>{readiness.rows.map((row) => <tr key={row.nodeId}><th scope="row">{row.nodeId}<small>{row.assignment}</small></th>{READINESS_STAGES.map((stage) => <td key={stage.id} title={row.cells[stage.id].reason}>{readinessStateLabel(row.cells[stage.id].state)}</td>)}</tr>)}</tbody></table></div>
      </section>

      <button type="button" aria-expanded={drawerOpen} aria-controls="source-evidence-drawer" onClick={() => setDrawerOpen((open) => !open)}>{drawerOpen ? 'Close' : 'Open'} source &amp; evidence drawer</button>
      {drawerOpen ? <section id="source-evidence-drawer" className="panel" role="region" aria-label="Source & evidence drawer">
        <h3>Protocol records</h3><p><strong>Claim boundary:</strong> display-only validated projections; missing metadata remains unknown.</p>
        <ul className="source-list">{sources.map((source) => <li key={source.id}><div><strong>{source.name}</strong><code>{source.protocol}</code><small>{source.rawDigest.state === 'unknown' ? 'Raw digest not supplied' : source.rawDigest.value}</small><small>{source.validation.state.toLowerCase()} · {source.claimBoundary}</small></div></li>)}</ul>
      </section> : null}

      <section className="panel" role="region" aria-label="Evidence timeline replay">
        <h3>Evidence timeline replay</h3><p>Supplied evidence event; no prior comparable capture was supplied.</p>
        <input type="range" aria-label="Evidence replay position" min={0} max={Math.max(0, timeline.length - 1)} value={timelineIndex} onChange={(event) => setTimelineCursor(Number(event.target.value))} />
        <div className="actions"><button type="button" aria-label="Previous evidence event" onClick={() => setTimelineCursor((cursor) => Math.max(0, cursor - 1))}>Previous</button><button type="button" aria-label="Next evidence event" onClick={() => setTimelineCursor((cursor) => Math.min(timeline.length - 1, cursor + 1))}>Next</button></div>
        <p role="status">Event {timelineIndex + 1} of {timeline.length}: {timelineFrame?.label ?? 'No supplied evidence event'}</p><p>{timelineFrame?.detail}</p>
      </section>

      <section className="panel"><h3>Pseudonymized evidence export</h3><button type="button" onClick={() => setExportJson(JSON.stringify(createPseudonymizedExport(snapshot, provisioning, incidents), null, 2))}>Create pseudonymized export</button>{exportJson === null ? null : <div role="region" aria-label="Pseudonymized export preview"><pre>{exportJson}</pre><a download="mycelium-evidence-pseudonymized.json" href={`data:application/json;charset=utf-8,${encodeURIComponent(exportJson)}`}>Download pseudonymized JSON</a></div>}</section>

      <div className="evidence-lower-grid">
        <section className="source-ledger panel" aria-labelledby="source-ledger-title">
          <div className="panel-titlebar compact">
            <div>
              <span className="panel-kicker">Bundled inputs · mixed scopes labeled</span>
              <h3 id="source-ledger-title">Source &amp; provenance ledger</h3>
            </div>
            <span className="ledger-count">{sourceLedger.length} sources</span>
          </div>
          <ul className="source-list">
            {sourceLedger.map((source) => (
              <li key={source.name}>
                <span className="file-kind">{source.kind}</span>
                <div><strong>{source.name}</strong><small>{source.detail}</small></div>
                <span className={`provenance ${source.provenance}`}>{source.provenance}</span>
              </li>
            ))}
          </ul>
        </section>

        <section className="evidence-metadata panel" aria-labelledby="metadata-title">
          <div className="panel-titlebar compact">
            <div>
              <span className="panel-kicker">Active simulator snapshot contract</span>
              <h3 id="metadata-title">Simulator evidence metadata</h3>
            </div>
          </div>
          <dl className="metadata-list">
            <div><dt>State</dt><dd><span className="offline-value">offline</span></dd></div>
            <div><dt>Scenario</dt><dd>{snapshot.source.scenarioName}</dd></div>
            <div><dt>Generated</dt><dd>{snapshot.source.generatedAt}</dd></div>
            <div><dt>Model</dt><dd>{snapshot.model.id}</dd></div>
            <div><dt>Routes parsed</dt><dd>{snapshot.routes.length}</dd></div>
            <div><dt>Locations</dt><dd>{snapshot.nodes.filter((node) => node.location.state === 'known').length} synthetic · {snapshot.nodes.filter((node) => node.location.state === 'unknown').length} unknown</dd></div>
          </dl>
          <div className="metadata-boundary">
            <strong>Verbatim simulator manifest boundary</strong>
            <p>{snapshot.sourceClaimBoundary}</p>
          </div>
        </section>
      </div>
    </div>
  );
}
