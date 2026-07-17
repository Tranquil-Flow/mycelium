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

const MANUAL_PROVISIONING_ROUTE_PROTOCOL = 'mycelium.manual_provisioning_route.v1';
const PROVISIONING_AUDIT_PROTOCOL = 'mycelium.provisioning_audit.v1';

function parseModel(value: unknown, path: string): ProvisioningModel {
  const model = record(value, path);
  const manifestDigest = string(
    model.manifest_digest,
    `${path}.manifest_digest`,
  );
  if (!/^sha256:[0-9a-f]{64}$/.test(manifestDigest)) {
    throw new EvidenceParseError(
      `${path}.manifest_digest`,
      'sha256:<64 lowercase hex>',
    );
  }
  const resolvedCommit = string(
    model.resolved_commit,
    `${path}.resolved_commit`,
  );
  if (!/^[0-9a-f]{40}$/.test(resolvedCommit)) {
    throw new EvidenceParseError(
      `${path}.resolved_commit`,
      'a 40-character lowercase hexadecimal commit',
    );
  }
  return {
    id: string(model.model_id, `${path}.model_id`),
    numLayers: positiveInteger(model.num_layers, `${path}.num_layers`),
    manifestDigest,
    resolvedCommit,
  };
}

function parseLayerRange(
  value: unknown,
  path: string,
  numLayers: number,
): Omit<ProvisioningAssignment, 'nodeId'> {
  const range = record(value, path);
  const startLayer = nonNegativeInteger(range.start_layer, `${path}.start_layer`);
  const endLayerExclusive = positiveInteger(
    range.end_layer_exclusive,
    `${path}.end_layer_exclusive`,
  );
  const layerCount = positiveInteger(range.layer_count, `${path}.layer_count`);

  if (endLayerExclusive <= startLayer) {
    throw new EvidenceParseError(path, 'a non-empty half-open layer range');
  }
  if (layerCount !== endLayerExclusive - startLayer) {
    throw new EvidenceParseError(`${path}.layer_count`, 'the size of the half-open layer range');
  }
  if (endLayerExclusive > numLayers) {
    throw new EvidenceParseError(
      `${path}.end_layer_exclusive`,
      `a layer boundary at or below model.num_layers (${numLayers})`,
    );
  }

  return { startLayer, endLayerExclusive, layerCount };
}

function parseAssignments(
  value: unknown,
  nodeOrder: readonly string[] | undefined,
  numLayers: number,
): ProvisioningAssignment[] {
  const route = array(value, 'manualProvisioningRoute.route');
  if (route.length === 0) {
    throw new EvidenceParseError('manualProvisioningRoute.route', 'at least one layer assignment');
  }
  if (nodeOrder !== undefined && route.length !== nodeOrder.length) {
    throw new EvidenceParseError(
      'manualProvisioningRoute.route',
      'one ordered layer assignment for every node in node_order',
    );
  }

  const assignments = route.map((value, index) => {
    const path = `manualProvisioningRoute.route[${index}]`;
    const assignment = record(value, path);
    const parsedRange = parseLayerRange(assignment.range, `${path}.range`, numLayers);

    return {
      nodeId: string(assignment.node_id, `${path}.node_id`),
      ...parsedRange,
    };
  });

  const assignmentNodeIds = assignments.map((assignment) => assignment.nodeId);
  unique(assignmentNodeIds, 'manualProvisioningRoute.route[].node_id');
  if (nodeOrder !== undefined) {
    sameStringArrays(
      assignmentNodeIds,
      nodeOrder,
      'manualProvisioningRoute.route[].node_id',
    );
  }

  let nextStart = 0;
  for (let index = 0; index < assignments.length; index += 1) {
    const assignment = assignments[index];
    if (assignment.startLayer !== nextStart) {
      throw new EvidenceParseError(
        `manualProvisioningRoute.route[${index}].range`,
        `a contiguous half-open range starting at layer ${nextStart}`,
      );
    }
    nextStart = assignment.endLayerExclusive;
  }
  if (nextStart !== numLayers) {
    throw new EvidenceParseError(
      'manualProvisioningRoute.route',
      `complete contiguous coverage of all ${numLayers} model layers`,
    );
  }

  return assignments;
}

function canonicalUuid(value: unknown, path: string): string {
  const parsed = string(value, path);
  if (!/^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/.test(parsed)) {
    throw new EvidenceParseError(path, 'a canonical lowercase UUID');
  }
  return parsed;
}

function assertMatchingAuditModel(routeModel: ProvisioningModel, auditModel: ProvisioningModel): void {
  const fields = [
    ['model_id', routeModel.id, auditModel.id],
    ['num_layers', routeModel.numLayers, auditModel.numLayers],
    ['manifest_digest', routeModel.manifestDigest, auditModel.manifestDigest],
    ['resolved_commit', routeModel.resolvedCommit, auditModel.resolvedCommit],
  ] as const;

  for (const [field, routeValue, auditValue] of fields) {
    if (auditValue !== routeValue) {
      throw new EvidenceParseError(`audit.model.${field}`, 'the matching manual route model value');
    }
  }
}

function assertAuditBindings(
  value: unknown,
  assignments: readonly ProvisioningAssignment[],
  numLayers: number,
): void {
  const bindings = array(value, 'audit.assignment_bindings');
  if (bindings.length !== assignments.length) {
    throw new EvidenceParseError(
      'audit.assignment_bindings',
      'one ordered binding for every manual route assignment',
    );
  }

  const assignmentIds = bindings.map((value, index) => {
    const path = `audit.assignment_bindings[${index}]`;
    const binding = record(value, path);
    const nodeId = string(binding.node_id, `${path}.node_id`);
    const assignmentId = canonicalUuid(binding.assignment_id, `${path}.assignment_id`);
    const range = parseLayerRange(binding.range, `${path}.range`, numLayers);
    const routeAssignment = assignments[index];

    if (nodeId !== routeAssignment.nodeId) {
      throw new EvidenceParseError(
        `${path}.node_id`,
        `the ordered route node ${JSON.stringify(routeAssignment.nodeId)}`,
      );
    }
    if (
      range.startLayer !== routeAssignment.startLayer ||
      range.endLayerExclusive !== routeAssignment.endLayerExclusive ||
      range.layerCount !== routeAssignment.layerCount
    ) {
      throw new EvidenceParseError(`${path}.range`, 'the matching manual route layer range');
    }

    return assignmentId;
  });
  unique(assignmentIds, 'audit.assignment_bindings[].assignment_id');
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
 * Joins a manual provisioning route artifact and its provisioning audit into an offline,
 * render-only evidence object. This does not claim a loaded or executable route.
 */
export function adaptProvisioningEvidence(
  manualProvisioningRouteInput: unknown,
  auditInput: unknown,
): ProvisioningEvidence {
  const manualProvisioningRoute = record(manualProvisioningRouteInput, 'manualProvisioningRoute');
  const audit = record(auditInput, 'audit');

  const manualProvisioningRouteProtocol = oneOf(
    manualProvisioningRoute.protocol,
    [MANUAL_PROVISIONING_ROUTE_PROTOCOL] as const,
    'manualProvisioningRoute.protocol',
  );
  const provisioningAuditProtocol = oneOf(
    audit.protocol,
    [PROVISIONING_AUDIT_PROTOCOL] as const,
    'audit.protocol',
  );
  if (!boolean(manualProvisioningRoute.ok, 'manualProvisioningRoute.ok')) {
    throw new EvidenceParseError('manualProvisioningRoute.ok', 'true for a usable manual provisioning route');
  }

  const model = parseModel(manualProvisioningRoute.model, 'manualProvisioningRoute.model');
  let declaredNodeOrder: string[] | undefined;
  if (manualProvisioningRoute.node_order !== undefined) {
    declaredNodeOrder = stringArray(
      manualProvisioningRoute.node_order,
      'manualProvisioningRoute.node_order',
    );
    if (declaredNodeOrder.length === 0) {
      throw new EvidenceParseError('manualProvisioningRoute.node_order', 'at least one node');
    }
    unique(declaredNodeOrder, 'manualProvisioningRoute.node_order');
  }
  const assignments = parseAssignments(
    manualProvisioningRoute.route,
    declaredNodeOrder,
    model.numLayers,
  );
  const nodeIds = declaredNodeOrder ?? assignments.map((assignment) => assignment.nodeId);

  const auditModel = parseModel(audit.model, 'audit.model');
  assertMatchingAuditModel(model, auditModel);
  canonicalUuid(audit.deployment_id, 'audit.deployment_id');
  nonNegativeInteger(audit.deployment_epoch, 'audit.deployment_epoch');
  assertAuditBindings(audit.assignment_bindings, assignments, model.numLayers);

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

  const manualProvisioningRouteClaimBoundary = string(
    manualProvisioningRoute.claim_boundary,
    'manualProvisioningRoute.claim_boundary',
  );
  const auditClaimBoundary = string(audit.claim_boundary, 'audit.claim_boundary');

  return deepFreeze({
    protocol: PROVISIONING_EVIDENCE_PROTOCOL,
    scope: 'artifact_provisioning',
    model,
    nodeIds,
    assignments,
    protocols: {
      manualProvisioningRoute: manualProvisioningRouteProtocol,
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
      manualProvisioningRoute: manualProvisioningRouteClaimBoundary,
      provisioningAudit: auditClaimBoundary,
    },
  });
}

export type {
  ProvisioningAssignment,
  ProvisioningEvidence,
  ProvisioningModel,
} from './types';
