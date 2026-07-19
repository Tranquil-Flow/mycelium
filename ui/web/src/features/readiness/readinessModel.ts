import type { ProvisioningEvidence } from '../../model/types';

export type ReadinessState =
  | 'PROVEN'
  | 'NOT_PROVEN'
  | 'FAILED'
  | 'NOT_APPLICABLE'
  | 'CONFLICT';

export type ReadinessStageId =
  | 'discovered'
  | 'planned'
  | 'assigned'
  | 'artifacts_verified'
  | 'runtime_loaded'
  | 'stage_probed'
  | 'route_challenged'
  | 'route_ready';

export interface ReadinessStage {
  readonly id: ReadinessStageId;
  readonly label: string;
}

export interface ReadinessCell {
  readonly state: ReadinessState;
  readonly reason: string;
  readonly evidenceRef: string;
}

export interface NodeReadinessRow {
  readonly nodeId: string;
  readonly assignment: string;
  readonly cells: Readonly<Record<ReadinessStageId, ReadinessCell>>;
}

export interface ReadinessModel {
  readonly rows: readonly NodeReadinessRow[];
  readonly summary: {
    readonly routeReady: boolean;
    readonly readyForRuntimeLoad: boolean;
    readonly missingProofs: readonly string[];
  };
}

export const READINESS_STAGES: readonly ReadinessStage[] = Object.freeze([
  Object.freeze({ id: 'discovered', label: 'Discovered' }),
  Object.freeze({ id: 'planned', label: 'Planned' }),
  Object.freeze({ id: 'assigned', label: 'Assigned' }),
  Object.freeze({ id: 'artifacts_verified', label: 'Artifacts verified' }),
  Object.freeze({ id: 'runtime_loaded', label: 'Runtime loaded' }),
  Object.freeze({ id: 'stage_probed', label: 'Stage probed' }),
  Object.freeze({ id: 'route_challenged', label: 'Route challenged' }),
  Object.freeze({ id: 'route_ready', label: 'Route ready' }),
]);

function cell(state: ReadinessState, reason: string, evidenceRef: string): ReadinessCell {
  return Object.freeze({ state, reason, evidenceRef });
}

export function buildReadinessModel(provisioning: ProvisioningEvidence): ReadinessModel {
  const artifactState: ReadinessState =
    provisioning.errors.length > 0
      ? 'FAILED'
      : provisioning.allAssignmentsVerified
        ? 'PROVEN'
        : 'NOT_PROVEN';
  const artifactReason =
    artifactState === 'PROVEN'
      ? 'Provisioning audit reports all assignment artifacts verified.'
      : artifactState === 'FAILED'
        ? `Provisioning audit reports ${provisioning.errors.length} error(s).`
        : 'Artifact verification proof was not supplied.';

  const rows = provisioning.nodeIds.map((nodeId) => {
    const assignment = provisioning.assignments.find((candidate) => candidate.nodeId === nodeId);
    if (assignment === undefined) {
      return Object.freeze({
        nodeId,
        assignment: 'Unknown',
        cells: Object.freeze({
          discovered: cell('PROVEN', 'Node is named by the provisioning evidence.', 'provisioning.nodeIds'),
          planned: cell('CONFLICT', 'Node has no matching route assignment.', 'provisioning.assignments'),
          assigned: cell('CONFLICT', 'Node has no matching assignment.', 'provisioning.assignments'),
          artifacts_verified: cell('CONFLICT', 'Artifact proof cannot bind to an assignment.', 'provisioning.audit'),
          runtime_loaded: cell('NOT_PROVEN', 'No runtime-load proof is present.', 'missing:runtime_load'),
          stage_probed: cell('NOT_PROVEN', 'No stage probe is present.', 'missing:stage_probe'),
          route_challenged: cell('NOT_PROVEN', 'No route challenge is present.', 'missing:route_challenge'),
          route_ready: cell('NOT_PROVEN', 'No node-bound route-ready proof is present.', 'missing:route_ready'),
        }),
      });
    }

    const cells: Readonly<Record<ReadinessStageId, ReadinessCell>> = Object.freeze({
      discovered: cell(
        'PROVEN',
        'Node is explicitly named by the validated provisioning capture.',
        'provisioning.nodeIds',
      ),
      planned: cell(
        'PROVEN',
        'The manual provisioning route contains this node.',
        provisioning.protocols.manualProvisioningRoute,
      ),
      assigned: cell(
        'PROVEN',
        `Assignment binds the half-open range [${assignment.startLayer},${assignment.endLayerExclusive}).`,
        'provisioning.assignments',
      ),
      artifacts_verified: cell(
        artifactState,
        artifactReason,
        provisioning.protocols.provisioningAudit,
      ),
      runtime_loaded: cell(
        'NOT_PROVEN',
        provisioning.readyForRuntimeLoad
          ? 'Artifacts are ready for the next runtime-load step; no load proof is supplied.'
          : 'No runtime-load proof is supplied.',
        'missing:runtime_load',
      ),
      stage_probed: cell(
        'NOT_PROVEN',
        'No stage forward-pass probe bound to this assignment is supplied.',
        'missing:stage_probe',
      ),
      route_challenged: cell(
        'NOT_PROVEN',
        'No end-to-end route challenge bound to this node is supplied.',
        'missing:route_challenge',
      ),
      route_ready: cell(
        provisioning.routeReady ? 'PROVEN' : 'NOT_PROVEN',
        provisioning.routeReady
          ? 'The source audit explicitly reports route_ready=true.'
          : 'The source audit explicitly keeps route_ready=false; readiness is not proven.',
        provisioning.protocols.provisioningAudit,
      ),
    });
    return Object.freeze({
      nodeId,
      assignment: `[${assignment.startLayer},${assignment.endLayerExclusive})`,
      cells,
    });
  });

  const firstRow = rows[0];
  const missingProofs = READINESS_STAGES.filter((stage) => {
    if (firstRow === undefined) return true;
    return rows.some((row) => row.cells[stage.id].state !== 'PROVEN');
  }).map((stage) => stage.label);

  return Object.freeze({
    rows: Object.freeze(rows),
    summary: Object.freeze({
      routeReady: provisioning.routeReady,
      readyForRuntimeLoad: provisioning.readyForRuntimeLoad,
      missingProofs: Object.freeze(missingProofs),
    }),
  });
}

export function readinessStateLabel(state: ReadinessState): string {
  switch (state) {
    case 'PROVEN':
      return '✓ PROVEN';
    case 'NOT_PROVEN':
      return '— NOT PROVEN';
    case 'FAILED':
      return '× FAILED';
    case 'NOT_APPLICABLE':
      return '○ NOT APPLICABLE';
    case 'CONFLICT':
      return '◆ CONFLICT';
  }
}
