import {
  PRODUCT_INFERENCE_PROTOCOL,
  type InferenceAcceptedResponse,
  type ProductBrowserWorker,
  type ProductEvidenceClass,
  type ProductNativeNode,
  type ProductQualification,
  type ProductSourceMode,
  type QualificationBinding,
} from '../../app/contracts';
import type {
  ObservatoryAdapterIncident,
  ObservatoryRequestSession,
} from '../../data/observatoryEventProjection';
import type { InferencePhase } from '../inference/types';

export type LifecycleState =
  | 'preparing'
  | 'loading'
  | 'unready'
  | 'qualified-ready'
  | 'generating'
  | 'cancelling'
  | 'peer-lost'
  | 'recovering'
  | 'stale'
  | 'revoked'
  | 'cleanup-complete';

export const LIFECYCLE_STATE_ORDER = Object.freeze([
  'preparing',
  'loading',
  'unready',
  'qualified-ready',
  'generating',
  'cancelling',
  'peer-lost',
  'recovering',
  'stale',
  'revoked',
  'cleanup-complete',
] as const satisfies readonly LifecycleState[]);

export const QUALIFIED_LIFECYCLE_READY_STATES = Object.freeze([
  'qualified-ready',
] as const satisfies readonly LifecycleState[]);

export function isReadyLifecycleState(state: LifecycleState): boolean {
  return (QUALIFIED_LIFECYCLE_READY_STATES as readonly LifecycleState[]).includes(state);
}

export interface LifecycleObservatoryInputs {
  readonly kind: ProductSourceMode;
  readonly source_cursor: number;
  readonly observed_at_unix_ms: number | null;
  readonly qualification: ProductQualification | null;
  readonly incidents: readonly ObservatoryAdapterIncident[];
  readonly sessions: readonly ObservatoryRequestSession[];
}

export interface LifecycleSwarmInputs {
  readonly kind: ProductSourceMode;
  readonly native_nodes: readonly ProductNativeNode[];
  readonly browser_workers: readonly ProductBrowserWorker[];
  readonly previous_browser_worker_ids: readonly string[];
  readonly previous_native_member_ids: readonly string[];
  readonly revoked_member_ids: readonly string[];
  readonly cleared_at_unix_ms: number | null;
  readonly cleanup: {
    readonly leave_confirmed: boolean;
    readonly session_cleared: boolean;
  };
}

export interface LifecycleInferenceInputs {
  readonly kind: ProductSourceMode;
  readonly qualification: ProductQualification | null;
  readonly qualification_loading: boolean;
  readonly phase: InferencePhase;
  readonly accepted_request: InferenceAcceptedResponse | null;
  readonly freshness_window_ms: number;
  readonly now_unix_ms: number;
}

export interface LifecycleInputs {
  readonly observatory: LifecycleObservatoryInputs;
  readonly swarm: LifecycleSwarmInputs;
  readonly inference: LifecycleInferenceInputs;
  readonly now_unix_ms?: number;
  readonly explicit_state?: LifecycleState;
}

export interface LifecycleProjection {
  readonly state: LifecycleState;
  readonly label: string;
  readonly accessibility_text: string;
  readonly route_ready: boolean;
  readonly inference_enabled: boolean;
  readonly qualifier_authority: boolean;
  readonly evidence_class: ProductEvidenceClass | null;
  readonly phase: InferencePhase | null;
  readonly block_reason: string | null;
  readonly lost_peer_ids: readonly string[];
  readonly previous_lost_peer_ids: readonly string[];
  readonly revoked_member_ids: readonly string[];
  readonly cleanup_complete: boolean;
  readonly accepted_request: InferenceAcceptedResponse | null;
  readonly qualified_binding: QualificationBinding | null;
  readonly claim_boundary: 'recorded_event_projection_only';
  readonly real_device: false;
  readonly physical_devices_present: 0;
}

interface StateCopy {
  readonly label: string;
  readonly description: string;
  readonly blocked: string | null;
}

export const LIFECYCLE_STATE_COPY: Readonly<Record<LifecycleState, StateCopy>> = Object.freeze({
  preparing: {
    label: 'Preparing qualifier evidence',
    description: 'Qualifier evidence has not yet been accepted.',
    blocked: 'Waiting for qualifier-owned evidence.',
  },
  loading: {
    label: 'Loading evidence snapshot',
    description: 'A read-only Observatory snapshot is still in flight.',
    blocked: 'Qualification snapshot is loading.',
  },
  unready: {
    label: 'Qualifier-owned not accepted',
    description: 'The qualifier is present, but route_ready is false or fail-closed.',
    blocked: 'Qualifier-owned route readiness is not accepted.',
  },
  'qualified-ready': {
    label: 'Qualified distributed execution',
    description: 'A live physical qualification from the product qualifier owns route_ready=true.',
    blocked: null,
  },
  generating: {
    label: 'Generating accepted request',
    description: 'An already accepted request is submitting or streaming against its captured binding.',
    blocked: 'An accepted request is active; new inference is disabled.',
  },
  cancelling: {
    label: 'Cancellation pending',
    description: 'Cancellation is pending for an accepted request.',
    blocked: 'Cancellation is pending; new inference is disabled.',
  },
  'peer-lost': {
    label: 'Peer lost',
    description: 'A previously required native node or browser worker is missing from the swarm.',
    blocked: 'A required peer is no longer present.',
  },
  recovering: {
    label: 'Recovering route binding',
    description: 'The qualifier has re-issued binding evidence after a peer loss.',
    blocked: 'Route binding is recovering after peer loss.',
  },
  stale: {
    label: 'Stale qualification',
    description: 'The latest qualification digest is older than the freshness window.',
    blocked: 'Qualification evidence is stale.',
  },
  revoked: {
    label: 'Revoked swarm member',
    description: 'A native node or browser worker has been revoked in the swarm.',
    blocked: 'A swarm member has been revoked.',
  },
  'cleanup-complete': {
    label: 'Cleanup complete',
    description: 'Every requested cleanup endpoint has completed.',
    blocked: 'Cleanup is complete; no route is active.',
  },
});

export const LIFECYCLE_STATE_LABELS: Readonly<Record<LifecycleState, string>> = Object.freeze(
  Object.fromEntries(
    LIFECYCLE_STATE_ORDER.map((state) => [state, LIFECYCLE_STATE_COPY[state].label]),
  ) as Record<LifecycleState, string>,
);

interface QualificationChoice {
  readonly qualification: ProductQualification | null;
  readonly conflicted: boolean;
}

interface LifecycleConditions {
  readonly qualification: ProductQualification | null;
  readonly qualification_conflicted: boolean;
  readonly qualifier_authority: boolean;
  readonly source_authoritative: boolean;
  readonly stale: boolean;
  readonly qualified_ready: boolean;
  readonly generating: boolean;
  readonly cancelling: boolean;
  readonly cleanup_complete: boolean;
  readonly lost_peer_ids: readonly string[];
  readonly previous_lost_peer_ids: readonly string[];
  readonly revoked_member_ids: readonly string[];
  readonly peer_disconnect_incident: boolean;
  readonly rebinding_incident: boolean;
}

const ACTIVE_GENERATION_PHASES = new Set<InferencePhase>(['submitting', 'streaming']);

function uniqueSorted(values: Iterable<string>): readonly string[] {
  return Object.freeze([...new Set([...values].filter((value) => value.length > 0))].sort());
}

function sameQualification(left: ProductQualification, right: ProductQualification): boolean {
  return (
    left.protocol === right.protocol &&
    left.issued_at_unix_ms === right.issued_at_unix_ms &&
    left.evidence_class === right.evidence_class &&
    left.route_ready === right.route_ready &&
    left.binding.qualification_id === right.binding.qualification_id &&
    left.binding.qualification_digest === right.binding.qualification_digest
  );
}

function selectQualification(inputs: LifecycleInputs): QualificationChoice {
  const inference = inputs.inference.qualification;
  const observatory = inputs.observatory.qualification;
  if (inference !== null && observatory !== null && !sameQualification(inference, observatory)) {
    return { qualification: null, conflicted: true };
  }
  return { qualification: inference ?? observatory, conflicted: false };
}

function isProductQualification(value: ProductQualification | null): value is ProductQualification {
  return value !== null && value.protocol === PRODUCT_INFERENCE_PROTOCOL;
}

function qualificationFresh(
  qualification: ProductQualification | null,
  nowUnixMs: number,
  freshnessWindowMs: number,
): boolean {
  if (qualification === null) return false;
  if (!Number.isSafeInteger(nowUnixMs) || nowUnixMs < qualification.issued_at_unix_ms) return false;
  if (!Number.isSafeInteger(freshnessWindowMs) || freshnessWindowMs < 0) return false;
  return nowUnixMs - qualification.issued_at_unix_ms <= freshnessWindowMs;
}

function hasAcceptedPhysicalEvidence(qualification: ProductQualification | null): boolean {
  return (
    qualification !== null &&
    qualification.evidence_class === 'physical_qualification' &&
    qualification.route_ready &&
    qualification.reason_codes.length === 0 &&
    qualification.binding.stage_load_proof_digests.length > 0
  );
}

function lostPeers(inputs: LifecycleInputs): readonly string[] {
  const currentBrowser = new Set(inputs.swarm.browser_workers.map((worker) => worker.peer_id));
  const currentNative = new Set(inputs.swarm.native_nodes.map((node) => node.member_id));
  return uniqueSorted([
    ...inputs.swarm.previous_browser_worker_ids.filter((peerId) => !currentBrowser.has(peerId)),
    ...inputs.swarm.previous_native_member_ids.filter((memberId) => !currentNative.has(memberId)),
  ]);
}

function revokedMembers(inputs: LifecycleInputs): readonly string[] {
  return uniqueSorted([
    ...inputs.swarm.revoked_member_ids,
    ...inputs.swarm.native_nodes
      .filter((node) => node.membership_state === 'revoked')
      .map((node) => node.member_id),
    ...inputs.swarm.browser_workers
      .filter((worker) => worker.state === 'revoked')
      .map((worker) => worker.peer_id),
  ]);
}

function hasIncident(inputs: LifecycleInputs, reason: string): boolean {
  return inputs.observatory.incidents.some((incident) => incident.reason === reason);
}

function buildConditions(inputs: LifecycleInputs): LifecycleConditions {
  const selected = selectQualification(inputs);
  const nowUnixMs = inputs.now_unix_ms ?? inputs.inference.now_unix_ms;
  const sourceAuthoritative =
    inputs.observatory.kind === 'live' && inputs.swarm.kind === 'live' && inputs.inference.kind === 'live';
  const qualifierAuthority =
    sourceAuthoritative && !selected.conflicted && isProductQualification(selected.qualification);
  const stale =
    qualifierAuthority &&
    selected.qualification !== null &&
    !qualificationFresh(selected.qualification, nowUnixMs, inputs.inference.freshness_window_ms);
  const qualifiedReady =
    qualifierAuthority &&
    !stale &&
    hasAcceptedPhysicalEvidence(selected.qualification);
  const acceptedRequest = inputs.inference.accepted_request;
  const generating =
    qualifiedReady &&
    acceptedRequest !== null &&
    acceptedRequest.accepted &&
    acceptedRequest.protocol === PRODUCT_INFERENCE_PROTOCOL &&
    ACTIVE_GENERATION_PHASES.has(inputs.inference.phase);
  const cancelling =
    acceptedRequest !== null &&
    acceptedRequest.accepted &&
    acceptedRequest.protocol === PRODUCT_INFERENCE_PROTOCOL &&
    inputs.inference.phase === 'cancelling';
  const cleanupComplete =
    inputs.swarm.cleared_at_unix_ms !== null &&
    inputs.swarm.cleanup.leave_confirmed &&
    inputs.swarm.cleanup.session_cleared;
  const lost = lostPeers(inputs);
  const revoked = revokedMembers(inputs);
  return Object.freeze({
    qualification: selected.qualification,
    qualification_conflicted: selected.conflicted,
    qualifier_authority: qualifierAuthority,
    source_authoritative: sourceAuthoritative,
    stale,
    qualified_ready: qualifiedReady,
    generating,
    cancelling,
    cleanup_complete: cleanupComplete,
    lost_peer_ids: lost,
    previous_lost_peer_ids: lost,
    revoked_member_ids: revoked,
    peer_disconnect_incident: hasIncident(inputs, 'peer_disconnect'),
    rebinding_incident: hasIncident(inputs, 'qualifier_rebinding'),
  });
}

function explicitStateAllowed(state: LifecycleState, inputs: LifecycleInputs, conditions: LifecycleConditions): boolean {
  switch (state) {
    case 'preparing':
      return conditions.qualification === null && inputs.inference.qualification_loading;
    case 'loading':
      return conditions.qualification === null && inputs.inference.qualification_loading;
    case 'unready':
      return conditions.qualification !== null && !conditions.qualified_ready;
    case 'qualified-ready':
      return conditions.qualified_ready;
    case 'generating':
      return conditions.generating;
    case 'cancelling':
      return conditions.cancelling;
    case 'peer-lost':
      return conditions.lost_peer_ids.length > 0 || conditions.peer_disconnect_incident;
    case 'recovering':
      return conditions.rebinding_incident && conditions.previous_lost_peer_ids.length > 0;
    case 'stale':
      return conditions.stale;
    case 'revoked':
      return conditions.revoked_member_ids.length > 0;
    case 'cleanup-complete':
      return conditions.cleanup_complete;
  }
}

function deriveState(inputs: LifecycleInputs, conditions: LifecycleConditions): LifecycleState {
  if (inputs.explicit_state !== undefined && explicitStateAllowed(inputs.explicit_state, inputs, conditions)) {
    return inputs.explicit_state;
  }
  if (conditions.cleanup_complete) return 'cleanup-complete';
  if (conditions.revoked_member_ids.length > 0) return 'revoked';
  if (conditions.cancelling) return 'cancelling';
  if (conditions.rebinding_incident && conditions.previous_lost_peer_ids.length > 0) return 'recovering';
  if (conditions.lost_peer_ids.length > 0 || conditions.peer_disconnect_incident) return 'peer-lost';
  if (conditions.stale) return 'stale';
  if (conditions.generating) return 'generating';
  if (conditions.qualified_ready) return 'qualified-ready';
  if (conditions.qualification !== null || conditions.qualification_conflicted) return 'unready';
  if (inputs.inference.qualification_loading) {
    return inputs.observatory.observed_at_unix_ms === null || inputs.observatory.source_cursor <= 0
      ? 'preparing'
      : 'loading';
  }
  return 'preparing';
}

function blockReason(state: LifecycleState, conditions: LifecycleConditions): string | null {
  if (state === 'qualified-ready') return null;
  if (conditions.qualification_conflicted) return 'Qualifier evidence conflicted between sources.';
  const base = LIFECYCLE_STATE_COPY[state].blocked;
  if (state === 'unready' && conditions.qualification?.reason_codes.length) {
    return `${base} ${conditions.qualification.reason_codes.join(', ')}`;
  }
  return base;
}

export function projectLifecycle(inputs: LifecycleInputs): LifecycleProjection {
  const conditions = buildConditions(inputs);
  const state = deriveState(inputs, conditions);
  const routeReady = state === 'qualified-ready' || (state === 'generating' && conditions.generating);
  const inferenceEnabled = state === 'qualified-ready';
  const copy = LIFECYCLE_STATE_COPY[state];
  const acceptedBinding =
    conditions.qualified_ready && conditions.qualification !== null ? conditions.qualification.binding : null;
  return Object.freeze({
    state,
    label: copy.label,
    accessibility_text: `${copy.label}: ${copy.description} route_ready=${routeReady ? 'true' : 'false'}; inference_enabled=${inferenceEnabled ? 'true' : 'false'}; claim_boundary=recorded_event_projection_only; real_device=false; physical_devices_present=0.`,
    route_ready: routeReady,
    inference_enabled: inferenceEnabled,
    qualifier_authority: conditions.qualifier_authority,
    evidence_class: conditions.qualification?.evidence_class ?? null,
    phase: inputs.inference.phase,
    block_reason: blockReason(state, conditions),
    lost_peer_ids: conditions.lost_peer_ids,
    previous_lost_peer_ids: conditions.previous_lost_peer_ids,
    revoked_member_ids: conditions.revoked_member_ids,
    cleanup_complete: conditions.cleanup_complete && state === 'cleanup-complete',
    accepted_request: inputs.inference.accepted_request,
    qualified_binding: state === 'generating' || state === 'qualified-ready' ? acceptedBinding : null,
    claim_boundary: 'recorded_event_projection_only' as const,
    real_device: false as const,
    physical_devices_present: 0 as const,
  });
}

export function projectLifecycleFromSources(inputs: LifecycleInputs): LifecycleProjection {
  return projectLifecycle(inputs);
}
