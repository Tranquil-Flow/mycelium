import type {
  EvidenceSnapshot,
  FailoverIncident,
  ProvisioningEvidence,
} from '../../model/types';

export interface EvidenceMetadataValue {
  readonly state: 'known' | 'unknown';
  readonly value: string | null;
  readonly reason: string | null;
}

export interface EvidenceSourceRecord {
  readonly id: string;
  readonly name: string;
  readonly protocol: string;
  readonly locator: string;
  readonly rawDigest: EvidenceMetadataValue;
  readonly producedAt: EvidenceMetadataValue;
  readonly acquiredAt: EvidenceMetadataValue;
  readonly freshnessBasis: 'source_time_only' | 'acquired_only' | 'none';
  readonly adapter: string;
  readonly validation: {
    readonly state: 'VALIDATED';
    readonly errors: readonly string[];
  };
  readonly claimBoundary: string;
  readonly missingArtifacts: readonly string[];
}

export type TimelineFrameKind =
  | 'simulator_capture'
  | 'provisioning_audit'
  | 'incident_detected'
  | 'incident_transition';

export interface EvidenceTimelineFrame {
  readonly id: string;
  readonly kind: TimelineFrameKind;
  readonly atMs: number;
  readonly label: string;
  readonly detail: string;
  readonly evidenceRef: string;
}

export interface ComparableEvidenceFrame {
  readonly id: string;
  readonly values: Readonly<Record<string, string>>;
}

export type EvidenceFrameDiff =
  | {
      readonly state: 'not_comparable';
      readonly reason: string;
      readonly changes: readonly [];
    }
  | {
      readonly state: 'compared';
      readonly baselineId: string;
      readonly candidateId: string;
      readonly changes: readonly {
        readonly field: string;
        readonly before: string | null;
        readonly after: string | null;
      }[];
    };

const unknown = (reason: string): EvidenceMetadataValue =>
  Object.freeze({ state: 'unknown', value: null, reason });
const known = (value: string): EvidenceMetadataValue =>
  Object.freeze({ state: 'known', value, reason: null });

export function evidenceSources(
  snapshot: EvidenceSnapshot,
  provisioning: ProvisioningEvidence,
  incidents: readonly FailoverIncident[],
): readonly EvidenceSourceRecord[] {
  const rawDigestMissing = unknown('Raw digest not supplied by the browser projection.');
  const acquiredMissing = unknown('Acquisition time not supplied by this offline projection.');
  const sourceRecords: EvidenceSourceRecord[] = snapshot.source.fixtureFiles.map((fileName, index) => {
    const protocol =
      fileName === 'planner-simulation.json'
        ? snapshot.source.reportProtocol
        : fileName === 'synthetic-geo.json'
          ? snapshot.source.geographyProtocol
          : 'NOT_SUPPLIED';
    return Object.freeze({
      id: `simulator-source-${index + 1}`,
      name: fileName,
      protocol,
      locator: `bundled://redacted/simulator-source-${index + 1}`,
      rawDigest: rawDigestMissing,
      producedAt: known(snapshot.source.generatedAt),
      acquiredAt: acquiredMissing,
      freshnessBasis: 'source_time_only' as const,
      adapter: `simulator/${snapshot.source.reportProtocol}`,
      validation: Object.freeze({ state: 'VALIDATED' as const, errors: Object.freeze([]) }),
      claimBoundary: snapshot.sourceClaimBoundary,
      missingArtifacts: Object.freeze([
        ...(protocol === 'NOT_SUPPLIED' ? ['source protocol'] : []),
        'raw source digest',
        'acquisition timestamp',
      ]),
    });
  });

  sourceRecords.push(
    Object.freeze({
      id: 'simulator-manifest',
      name: 'ui-fixture-manifest.json',
      protocol: snapshot.source.manifestProtocol,
      locator: 'bundled://redacted/simulator-manifest',
      rawDigest: rawDigestMissing,
      producedAt: known(snapshot.source.generatedAt),
      acquiredAt: acquiredMissing,
      freshnessBasis: 'source_time_only',
      adapter: `manifest/${snapshot.source.manifestProtocol}`,
      validation: Object.freeze({ state: 'VALIDATED', errors: Object.freeze([]) }),
      claimBoundary: snapshot.sourceClaimBoundary,
      missingArtifacts: Object.freeze(['raw source digest', 'acquisition timestamp']),
    }),
    Object.freeze({
      id: 'provisioning-route',
      name: 'manual-provisioning-route-v1.json',
      protocol: provisioning.protocols.manualProvisioningRoute,
      locator: 'bundled://redacted/provisioning-route',
      rawDigest: rawDigestMissing,
      producedAt: unknown('Manual route source does not report a production timestamp.'),
      acquiredAt: acquiredMissing,
      freshnessBasis: 'none',
      adapter: `provisioning/${provisioning.protocol}`,
      validation: Object.freeze({ state: 'VALIDATED', errors: Object.freeze([]) }),
      claimBoundary: provisioning.sourceClaimBoundaries.manualProvisioningRoute,
      missingArtifacts: Object.freeze(['raw source digest', 'produced timestamp', 'acquisition timestamp']),
    }),
    Object.freeze({
      id: 'provisioning-audit',
      name: 'provisioning-audit.json',
      protocol: provisioning.protocols.provisioningAudit,
      locator: 'bundled://redacted/provisioning-audit',
      rawDigest: rawDigestMissing,
      producedAt: known(provisioning.auditedAt),
      acquiredAt: acquiredMissing,
      freshnessBasis: 'source_time_only',
      adapter: `provisioning/${provisioning.protocol}`,
      validation: Object.freeze({
        state: 'VALIDATED',
        errors: Object.freeze([...provisioning.errors]),
      }),
      claimBoundary: provisioning.sourceClaimBoundaries.provisioningAudit,
      missingArtifacts: Object.freeze([
        'runtime-load proof',
        'stage probe',
        'route challenge',
        'raw source digest',
        'acquisition timestamp',
      ]),
    }),
    Object.freeze({
      id: 'failover-fixture',
      name: 'failover-scenarios.json',
      protocol: 'mycelium.ui_failover_fixture.v1',
      locator: 'bundled://redacted/failover-fixture',
      rawDigest: rawDigestMissing,
      producedAt:
        incidents.length > 0
          ? known(incidents.map((incident) => incident.trigger.detectedAt).sort()[0])
          : unknown('No incident timestamp supplied.'),
      acquiredAt: acquiredMissing,
      freshnessBasis: 'source_time_only',
      adapter: 'failover/mycelium.ui_failover_fixture.v1',
      validation: Object.freeze({ state: 'VALIDATED', errors: Object.freeze([]) }),
      claimBoundary: incidents[0]?.sourceClaimBoundary ?? 'No incident claim boundary supplied.',
      missingArtifacts: Object.freeze(['raw source digest', 'acquisition timestamp', 'live event contract']),
    }),
  );

  return Object.freeze(sourceRecords);
}

function validTime(value: string, context: string): number {
  const parsed = Date.parse(value);
  if (!Number.isFinite(parsed)) throw new TypeError(`${context} must contain a supplied ISO timestamp`);
  return parsed;
}

export function buildEvidenceTimeline(
  snapshot: EvidenceSnapshot,
  provisioning: ProvisioningEvidence,
  incidents: readonly FailoverIncident[],
): readonly EvidenceTimelineFrame[] {
  const frames: EvidenceTimelineFrame[] = [
    Object.freeze({
      id: 'simulator-capture',
      kind: 'simulator_capture' as const,
      atMs: validTime(snapshot.source.generatedAt, 'snapshot.source.generatedAt'),
      label: 'Simulator capture produced',
      detail: `${snapshot.routes.length} modeled strategies; synthetic offline evidence.`,
      evidenceRef: snapshot.source.reportProtocol,
    }),
    Object.freeze({
      id: 'provisioning-audit',
      kind: 'provisioning_audit' as const,
      atMs: validTime(provisioning.auditedAt, 'provisioning.auditedAt'),
      label: 'Provisioning audit produced',
      detail: provisioning.readyForRuntimeLoad
        ? 'Artifacts reported ready for runtime load; runtime load remains not proven.'
        : 'Artifact readiness not proven.',
      evidenceRef: provisioning.protocols.provisioningAudit,
    }),
  ];

  for (const incident of incidents) {
    const detectedAt = validTime(incident.trigger.detectedAt, `${incident.id}.trigger.detectedAt`);
    frames.push(
      Object.freeze({
        id: `${incident.id}:detected`,
        kind: 'incident_detected',
        atMs: detectedAt,
        label: `${incident.mode.replaceAll('_', ' ')} detected`,
        detail: `${incident.trigger.kind} · detector scope ${incident.trigger.scope}`,
        evidenceRef: incident.id,
      }),
    );
    for (const transition of incident.transitions) {
      frames.push(
        Object.freeze({
          id: `${incident.id}:${transition.state}:${transition.atMs}`,
          kind: 'incident_transition',
          atMs: detectedAt + transition.atMs,
          label: transition.state.replaceAll('_', ' '),
          detail: transition.detail,
          evidenceRef: incident.id,
        }),
      );
    }
  }

  frames.sort((left, right) => left.atMs - right.atMs || left.id.localeCompare(right.id));
  return Object.freeze(frames);
}

export function diffEvidenceFrames(
  baseline: ComparableEvidenceFrame | null,
  candidate: ComparableEvidenceFrame,
): EvidenceFrameDiff {
  if (baseline === null) {
    return Object.freeze({
      state: 'not_comparable',
      reason: 'No prior comparable capture was supplied.',
      changes: Object.freeze([]) as readonly [],
    });
  }
  const fields = [...new Set([...Object.keys(baseline.values), ...Object.keys(candidate.values)])].sort();
  const changes = fields
    .filter((field) => baseline.values[field] !== candidate.values[field])
    .map((field) =>
      Object.freeze({
        field,
        before: baseline.values[field] ?? null,
        after: candidate.values[field] ?? null,
      }),
    );
  return Object.freeze({
    state: 'compared' as const,
    baselineId: baseline.id,
    candidateId: candidate.id,
    changes: Object.freeze(changes),
  });
}
