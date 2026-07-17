import type {
  EvidenceProvenance,
  EvidenceSnapshot,
  FailoverIncident,
  ProvisioningEvidence,
} from '../model/types';

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
      name: 'route-plan-v2.json',
      detail: `separate provisioning scope · ${provisioning.protocols.routePlan}`,
      provenance: provisioning.provenance,
    },
    {
      kind: 'AUD',
      name: 'provisioning-audit.json',
      detail: new Date(provisioning.auditedAt).toISOString(),
      provenance: provisioning.provenance,
    },
  ];

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
            <span>Route plan protocol</span>
            <code>{provisioning.protocols.routePlan}</code>
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

          <article className="claim-boundary route-plan-boundary" aria-labelledby="plan-boundary-title">
            <div className="boundary-marker" aria-hidden="true">!</div>
            <div>
              <p className="panel-kicker">Verbatim route-plan boundary</p>
              <h4 id="plan-boundary-title">Allocation source limit</h4>
              <blockquote>{provisioning.sourceClaimBoundaries.routePlan}</blockquote>
            </div>
            <span className="source-protocol">{provisioning.protocols.routePlan}</span>
          </article>
        </div>
      </section>

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
