import {
  PROVISIONING_EVIDENCE_PROTOCOL,
  type ProvisioningAssignment,
  type ProvisioningEvidence,
  type ProvisioningModel,
} from './types';
import {
  EvidenceParseError,
  array,
  boolean,
  dateTimeString,
  deepFreeze,
  nonNegativeInteger,
  offlineClaimBoundary,
  oneOf,
  positiveInteger,
  record,
  sameStringArrays,
  string,
  stringArray,
  unique,
} from './runtime';

const ROUTE_PLAN_PROTOCOL = 'mycelium.route_plan.v2';
const PROVISIONING_AUDIT_PROTOCOL = 'mycelium.provisioning_audit.v1';

function parseModel(value: unknown): ProvisioningModel {
  const model = record(value, 'routePlan.model');
  return {
    id: string(model.model_id, 'routePlan.model.model_id'),
    numLayers: positiveInteger(model.num_layers, 'routePlan.model.num_layers'),
    manifestDigest: string(model.manifest_digest, 'routePlan.model.manifest_digest'),
    resolvedCommit: string(model.resolved_commit, 'routePlan.model.resolved_commit'),
  };
}

function parseAssignments(
  value: unknown,
  nodeOrder: readonly string[],
  numLayers: number,
): ProvisioningAssignment[] {
  const route = array(value, 'routePlan.route');
  if (route.length === 0) {
    throw new EvidenceParseError('routePlan.route', 'at least one layer assignment');
  }
  if (route.length !== nodeOrder.length) {
    throw new EvidenceParseError(
      'routePlan.route',
      'one ordered layer assignment for every node in node_order',
    );
  }

  const assignments = route.map((value, index) => {
    const path = `routePlan.route[${index}]`;
    const assignment = record(value, path);
    const range = record(assignment.range, `${path}.range`);
    const startLayer = nonNegativeInteger(range.start_layer, `${path}.range.start_layer`);
    const endLayerExclusive = positiveInteger(
      range.end_layer_exclusive,
      `${path}.range.end_layer_exclusive`,
    );
    const layerCount = positiveInteger(range.layer_count, `${path}.range.layer_count`);

    if (endLayerExclusive <= startLayer) {
      throw new EvidenceParseError(`${path}.range`, 'a non-empty half-open layer range');
    }
    if (layerCount !== endLayerExclusive - startLayer) {
      throw new EvidenceParseError(
        `${path}.range.layer_count`,
        'the size of the half-open layer range',
      );
    }
    if (endLayerExclusive > numLayers) {
      throw new EvidenceParseError(
        `${path}.range.end_layer_exclusive`,
        `a layer boundary at or below model.num_layers (${numLayers})`,
      );
    }

    return {
      nodeId: string(assignment.node_id, `${path}.node_id`),
      startLayer,
      endLayerExclusive,
      layerCount,
    };
  });

  sameStringArrays(
    assignments.map((assignment) => assignment.nodeId),
    nodeOrder,
    'routePlan.route[].node_id',
  );

  let nextStart = 0;
  for (let index = 0; index < assignments.length; index += 1) {
    const assignment = assignments[index];
    if (assignment.startLayer !== nextStart) {
      throw new EvidenceParseError(
        `routePlan.route[${index}].range`,
        `a contiguous half-open range starting at layer ${nextStart}`,
      );
    }
    nextStart = assignment.endLayerExclusive;
  }
  if (nextStart !== numLayers) {
    throw new EvidenceParseError(
      'routePlan.route',
      `complete contiguous coverage of all ${numLayers} model layers`,
    );
  }

  return assignments;
}

function assertAuditCoherence(
  allAssignmentsVerified: boolean,
  readyForRuntimeLoad: boolean,
  routeReady: boolean,
  errors: readonly string[],
): void {
  if (errors.length > 0 && (allAssignmentsVerified || readyForRuntimeLoad || routeReady)) {
    throw new EvidenceParseError(
      'audit.errors',
      'no errors when verification or readiness is claimed',
    );
  }
  if (readyForRuntimeLoad && !allAssignmentsVerified) {
    throw new EvidenceParseError(
      'audit.ready_for_runtime_load',
      'false unless all_assignments_verified is true',
    );
  }
  if (routeReady && !readyForRuntimeLoad) {
    throw new EvidenceParseError(
      'audit.route_ready',
      'false unless ready_for_runtime_load is true',
    );
  }
}

/**
 * Joins a route-plan artifact and its provisioning audit into an offline,
 * render-only evidence object. This does not claim a loaded or executable route.
 */
export function adaptProvisioningEvidence(
  routePlanInput: unknown,
  auditInput: unknown,
): ProvisioningEvidence {
  const routePlan = record(routePlanInput, 'routePlan');
  const audit = record(auditInput, 'audit');

  const routePlanProtocol = oneOf(
    routePlan.protocol,
    [ROUTE_PLAN_PROTOCOL] as const,
    'routePlan.protocol',
  );
  const provisioningAuditProtocol = oneOf(
    audit.protocol,
    [PROVISIONING_AUDIT_PROTOCOL] as const,
    'audit.protocol',
  );
  if (!boolean(routePlan.ok, 'routePlan.ok')) {
    throw new EvidenceParseError('routePlan.ok', 'true for a usable route plan');
  }

  const model = parseModel(routePlan.model);
  const nodeIds = stringArray(routePlan.node_order, 'routePlan.node_order');
  if (nodeIds.length === 0) {
    throw new EvidenceParseError('routePlan.node_order', 'at least one node');
  }
  unique(nodeIds, 'routePlan.node_order');
  const assignments = parseAssignments(routePlan.route, nodeIds, model.numLayers);

  const verifiedNodes = stringArray(audit.verified_nodes, 'audit.verified_nodes');
  unique(verifiedNodes, 'audit.verified_nodes');
  sameStringArrays(verifiedNodes, nodeIds, 'audit.verified_nodes');

  const allAssignmentsVerified = boolean(
    audit.all_assignments_verified,
    'audit.all_assignments_verified',
  );
  const readyForRuntimeLoad = boolean(
    audit.ready_for_runtime_load,
    'audit.ready_for_runtime_load',
  );
  const routeReady = boolean(audit.route_ready, 'audit.route_ready');
  const errors = array(audit.errors, 'audit.errors').map((error, index) =>
    string(error, `audit.errors[${index}]`),
  );
  assertAuditCoherence(allAssignmentsVerified, readyForRuntimeLoad, routeReady, errors);

  const routePlanClaimBoundary = string(
    routePlan.claim_boundary,
    'routePlan.claim_boundary',
  );
  const auditClaimBoundary = string(audit.claim_boundary, 'audit.claim_boundary');

  return deepFreeze({
    protocol: PROVISIONING_EVIDENCE_PROTOCOL,
    scope: 'artifact_provisioning',
    model,
    nodeIds,
    assignments,
    protocols: {
      routePlan: routePlanProtocol,
      provisioningAudit: provisioningAuditProtocol,
    },
    auditedAt: dateTimeString(audit.timestamp, 'audit.timestamp'),
    allAssignmentsVerified,
    readyForRuntimeLoad,
    routeReady,
    errors,
    evidenceState: 'offline',
    provenance: 'declared',
    claimBoundary: offlineClaimBoundary(auditClaimBoundary),
    sourceClaimBoundaries: {
      routePlan: routePlanClaimBoundary,
      provisioningAudit: auditClaimBoundary,
    },
  });
}

export type {
  ProvisioningAssignment,
  ProvisioningEvidence,
  ProvisioningModel,
} from './types';
