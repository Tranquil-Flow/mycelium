import {
  type FailoverCutover,
  type FailoverIncident,
  type FailoverMode,
  type FailoverOverlay,
  type FailoverOverlayRoute,
  type FailoverRoute,
  type FailoverTransition,
  type FailoverTransitionState,
  type FailoverTrigger,
  type FailoverValidationContext,
} from './types';
import {
  EvidenceParseError,
  array,
  dateTimeString,
  deepFreeze,
  nonNegativeInteger,
  nullableInteger,
  offlineClaimBoundary,
  oneOf,
  positiveInteger,
  record,
  string,
  stringArray,
  unique,
} from './runtime';

const FAILOVER_FIXTURE_PROTOCOL = 'mycelium.ui_failover_fixture.v1';
const FAILOVER_MODES = ['stable_drain', 'active_failover', 'circuit_break'] as const;
const FAILOVER_STATUSES = ['resumed', 'aborted'] as const;
const ROUTE_STATES = ['draining', 'active', 'failed', 'aborted'] as const;
const TRANSITION_STATES = [
  'DETECTED',
  'QUARANTINED_LOCAL',
  'ROUTE_AT_RISK',
  'REPLAN_STARTED',
  'CANDIDATE_SELECTED',
  'REPLACEMENT_LOADING',
  'CUTOVER_STARTED',
  'RESUMED',
  'ABORTED',
] as const satisfies readonly FailoverTransitionState[];

interface ParsedValidationContext {
  readonly knownNodeIds: ReadonlySet<string>;
  readonly numLayers: number;
}

function parseValidationContext(value: unknown): ParsedValidationContext {
  const context = record(value, 'validationContext');
  const knownNodeIds = stringArray(context.knownNodeIds, 'validationContext.knownNodeIds');
  if (knownNodeIds.length === 0) {
    throw new EvidenceParseError('validationContext.knownNodeIds', 'at least one known node');
  }
  unique(knownNodeIds, 'validationContext.knownNodeIds');
  return {
    knownNodeIds: new Set(knownNodeIds),
    numLayers: positiveInteger(context.numLayers, 'validationContext.numLayers'),
  };
}

function parseRoute(
  value: unknown,
  path: string,
  knownNodeIds: ReadonlySet<string>,
): FailoverRoute {
  const route = record(value, path);
  const nodeIds = stringArray(route.nodes, `${path}.nodes`);
  if (nodeIds.length === 0) {
    throw new EvidenceParseError(`${path}.nodes`, 'at least one peer id');
  }
  unique(nodeIds, `${path}.nodes`);
  for (let index = 0; index < nodeIds.length; index += 1) {
    if (!knownNodeIds.has(nodeIds[index])) {
      throw new EvidenceParseError(
        `${path}.nodes[${index}]`,
        'a known node from validationContext.knownNodeIds',
      );
    }
  }

  return {
    id: string(route.route_id, `${path}.route_id`),
    generation: nonNegativeInteger(route.generation, `${path}.generation`),
    nodeIds,
    state: oneOf(route.state, ROUTE_STATES, `${path}.state`),
  };
}

function parseTrigger(value: unknown, path: string): FailoverTrigger {
  const trigger = record(value, path);
  return {
    kind: string(trigger.kind, `${path}.kind`),
    peerId: string(trigger.peer_id, `${path}.peer_id`),
    scope: string(trigger.scope, `${path}.scope`),
    detectedAt: dateTimeString(trigger.detected_at, `${path}.detected_at`),
  };
}

function parseCutover(value: unknown, path: string, numLayers: number): FailoverCutover {
  const cutover = record(value, path);
  const lastGoodLayer = nullableInteger(cutover.last_good_layer, `${path}.last_good_layer`);
  if (lastGoodLayer !== null && lastGoodLayer >= numLayers) {
    throw new EvidenceParseError(
      `${path}.last_good_layer`,
      `a layer index below validationContext.numLayers (${numLayers})`,
    );
  }

  return {
    policy: string(cutover.policy, `${path}.policy`),
    lastGoodLayer,
    lastCommittedToken: nullableInteger(
      cutover.last_committed_token,
      `${path}.last_committed_token`,
    ),
    checkpointKind: string(cutover.checkpoint_kind, `${path}.checkpoint_kind`),
  };
}

function parseTransitions(value: unknown, path: string): FailoverTransition[] {
  const transitions = array(value, path).map((item, index) => {
    const transitionPath = `${path}[${index}]`;
    const transition = record(item, transitionPath);
    return {
      state: oneOf(transition.state, TRANSITION_STATES, `${transitionPath}.state`),
      atMs: nonNegativeInteger(transition.at_ms, `${transitionPath}.at_ms`),
      detail: string(transition.detail, `${transitionPath}.detail`),
    };
  });
  if (transitions.length === 0) {
    throw new EvidenceParseError(path, 'at least one state transition');
  }
  if (transitions[0].state !== 'DETECTED') {
    throw new EvidenceParseError(`${path}[0].state`, 'DETECTED as the initial transition');
  }
  for (let index = 1; index < transitions.length; index += 1) {
    if (transitions[index].atMs < transitions[index - 1].atMs) {
      throw new EvidenceParseError(`${path}[${index}].at_ms`, 'monotonic transition time');
    }
  }
  return transitions;
}

function assertIncidentSemantics(
  mode: FailoverMode,
  status: 'resumed' | 'aborted',
  oldRoute: FailoverRoute,
  newRoute: FailoverRoute | null,
  trigger: FailoverTrigger,
  cutover: FailoverCutover,
  transitions: readonly FailoverTransition[],
  path: string,
): void {
  if (!oldRoute.nodeIds.includes(trigger.peerId)) {
    throw new EvidenceParseError(
      `${path}.trigger.peer_id`,
      'a trigger peer present in old_route.nodes',
    );
  }
  if (newRoute !== null && newRoute.nodeIds.includes(trigger.peerId)) {
    throw new EvidenceParseError(
      `${path}.new_route.nodes`,
      'a replacement route that excludes the trigger peer',
    );
  }

  if (mode === 'circuit_break') {
    if (newRoute !== null) {
      throw new EvidenceParseError(`${path}.new_route`, 'null for a circuit break');
    }
    if (oldRoute.state !== 'aborted') {
      throw new EvidenceParseError(`${path}.old_route.state`, '"aborted" for a circuit break');
    }
    if (status !== 'aborted') {
      throw new EvidenceParseError(`${path}.status`, '"aborted" for a circuit break');
    }
  } else {
    if (newRoute === null) {
      throw new EvidenceParseError(`${path}.new_route`, `a replacement route for ${mode}`);
    }
    if (newRoute.id === oldRoute.id) {
      throw new EvidenceParseError(`${path}.new_route.route_id`, 'a distinct replacement route id');
    }
    if (newRoute.generation <= oldRoute.generation) {
      throw new EvidenceParseError(
        `${path}.new_route.generation`,
        'a generation newer than old_route.generation',
      );
    }
    if (newRoute.state !== 'active') {
      throw new EvidenceParseError(`${path}.new_route.state`, '"active" for a replacement route');
    }
    if (status !== 'resumed') {
      throw new EvidenceParseError(`${path}.status`, `"resumed" for ${mode}`);
    }

    if (mode === 'stable_drain' && oldRoute.state !== 'draining') {
      throw new EvidenceParseError(`${path}.old_route.state`, '"draining" for a stable drain');
    }
    if (mode === 'active_failover' && oldRoute.state !== 'failed') {
      throw new EvidenceParseError(`${path}.old_route.state`, '"failed" for active failover');
    }
  }

  if (
    mode === 'active_failover' &&
    (cutover.lastGoodLayer === null || cutover.lastCommittedToken === null)
  ) {
    throw new EvidenceParseError(
      `${path}.cutover`,
      'both a layer and token checkpoint for active failover',
    );
  }

  const expectedTerminal = status === 'resumed' ? 'RESUMED' : 'ABORTED';
  if (transitions.at(-1)?.state !== expectedTerminal) {
    throw new EvidenceParseError(
      `${path}.transitions`,
      `terminal transition ${expectedTerminal} for status ${JSON.stringify(status)}`,
    );
  }
}

function parseIncident(
  value: unknown,
  index: number,
  sourceClaimBoundary: string,
  validation: ParsedValidationContext,
): FailoverIncident {
  const path = `failover.scenarios[${index}]`;
  const scenario = record(value, path);
  const mode = oneOf(scenario.mode, FAILOVER_MODES, `${path}.mode`);
  const status = oneOf(scenario.status, FAILOVER_STATUSES, `${path}.status`);
  const oldRoute = parseRoute(scenario.old_route, `${path}.old_route`, validation.knownNodeIds);
  const newRoute =
    scenario.new_route === null
      ? null
      : parseRoute(scenario.new_route, `${path}.new_route`, validation.knownNodeIds);
  const trigger = parseTrigger(scenario.trigger, `${path}.trigger`);
  const cutover = parseCutover(scenario.cutover, `${path}.cutover`, validation.numLayers);
  const transitions = parseTransitions(scenario.transitions, `${path}.transitions`);
  assertIncidentSemantics(
    mode,
    status,
    oldRoute,
    newRoute,
    trigger,
    cutover,
    transitions,
    path,
  );

  const requestIds = stringArray(scenario.request_ids, `${path}.request_ids`);
  if (requestIds.length === 0) {
    throw new EvidenceParseError(`${path}.request_ids`, 'at least one request id');
  }
  unique(requestIds, `${path}.request_ids`);

  return {
    id: string(scenario.incident_id, `${path}.incident_id`),
    title: string(scenario.title, `${path}.title`),
    mode,
    status,
    requestIds,
    deploymentId: string(scenario.deployment_id, `${path}.deployment_id`),
    deploymentEpoch: nonNegativeInteger(scenario.deployment_epoch, `${path}.deployment_epoch`),
    oldRoute,
    newRoute,
    trigger,
    cutover,
    backupReadiness: string(scenario.backup_readiness, `${path}.backup_readiness`),
    compatibility: string(scenario.compatibility, `${path}.compatibility`),
    transitions,
    evidenceState: 'offline',
    provenance: 'synthetic',
    claimBoundary: offlineClaimBoundary(sourceClaimBoundary),
    sourceClaimBoundary,
  };
}

/**
 * Parses synthetic incident fixtures into a UI-owned, offline-only model.
 * The validation context prevents fixture routes and checkpoints from claiming
 * peers or model layers outside the evidence bundle.
 */
export function adaptFailoverScenarios(
  input: unknown,
  context: FailoverValidationContext,
): FailoverIncident[] {
  const validation = parseValidationContext(context);
  const fixture = record(input, 'failover');
  oneOf(
    fixture.protocol,
    [FAILOVER_FIXTURE_PROTOCOL] as const,
    'failover.protocol',
  );
  oneOf(fixture.provenance, ['synthetic'] as const, 'failover.provenance');
  const sourceClaimBoundary = string(fixture.claim_boundary, 'failover.claim_boundary');
  const incidents = array(fixture.scenarios, 'failover.scenarios').map((scenario, index) =>
    parseIncident(scenario, index, sourceClaimBoundary, validation),
  );
  if (incidents.length === 0) {
    throw new EvidenceParseError('failover.scenarios', 'at least one incident');
  }
  unique(
    incidents.map((incident) => incident.id),
    'failover.scenarios[].incident_id',
  );
  return deepFreeze(incidents);
}

function overlayRoute(route: FailoverRoute, role: 'old' | 'replacement'): FailoverOverlayRoute {
  return {
    ...route,
    role,
    label: `${role === 'old' ? 'Old' : 'New'} g${route.generation}`,
  };
}

function checkpointLabel(cutover: FailoverCutover): string {
  const coordinates: string[] = [];
  if (cutover.lastGoodLayer !== null) {
    coordinates.push(`layer ${cutover.lastGoodLayer}`);
  }
  if (cutover.lastCommittedToken !== null) {
    coordinates.push(`token ${cutover.lastCommittedToken}`);
  }
  if (coordinates.length === 0) {
    return 'No checkpoint claimed';
  }
  const kind = cutover.checkpointKind.replaceAll('_', ' ');
  return `${kind} checkpoint · ${coordinates.join(' · ')}`;
}

function outcome(incident: FailoverIncident): string {
  switch (incident.mode) {
    case 'stable_drain':
      return `Drain resumed · old g${incident.oldRoute.generation} remains visible · offline evidence only`;
    case 'active_failover':
      return `Request resumed · old g${incident.oldRoute.generation} replaced by new g${incident.newRoute?.generation} · offline evidence only`;
    case 'circuit_break':
      return 'Output rejected · 503 · no reroute claimed';
  }
}

/** Produces a deeply immutable render-only overlay; it never selects or executes a route. */
export function projectFailoverOverlay(incident: FailoverIncident): FailoverOverlay {
  const routes: FailoverOverlayRoute[] = [overlayRoute(incident.oldRoute, 'old')];
  if (incident.newRoute !== null) {
    routes.push(overlayRoute(incident.newRoute, 'replacement'));
  }

  return deepFreeze({
    incidentId: incident.id,
    title: incident.title,
    mode: incident.mode,
    routes,
    triggerPeerId: incident.trigger.peerId,
    triggerKind: incident.trigger.kind,
    triggerScope: incident.trigger.scope,
    // Compatibility alias for the existing view. The canonical name does not
    // incorrectly label a planned-drain trigger as a failed peer.
    failedPeerId: incident.trigger.peerId,
    checkpointLabel: checkpointLabel(incident.cutover),
    outcome: outcome(incident),
    evidenceState: 'offline',
    provenance: 'synthetic',
    claimBoundary: incident.claimBoundary,
  });
}

export type {
  FailoverIncident,
  FailoverMode,
  FailoverOverlay,
  FailoverOverlayRoute,
  FailoverRoute,
  FailoverValidationContext,
} from './types';
