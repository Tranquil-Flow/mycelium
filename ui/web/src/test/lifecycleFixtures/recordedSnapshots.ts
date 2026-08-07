/**
 * Recorded snapshots that the product lifecycle projection must classify
 * faithfully. These shapes mirror the literal product contracts in
 * `src/app/contracts.ts`, the Observatory adapter bundle, and the swarm
 * status. Nothing here is inferred — every value can be traced back to a
 * backend protocol document.
 */
import type {
  ProductBrowserWorker,
  ProductNativeNode,
  ProductQualification,
  ProductSourceMode,
  InferenceAcceptedResponse,
  QualificationBinding,
} from '../../app/contracts';
import type { ObservatoryAdapterBundle } from '../../data/observatoryEventProjection';
import type { LifecycleInputs, LifecycleState } from '../../features/lifecycle/lifecycleProjection';
import type { ObservatoryRequestState } from '../../data/observatoryEventProjection';
import type { InferencePhase as SessionInferencePhase } from '../../features/inference/types';

const FIXTURE_FRESHNESS_WINDOW_MS = 5_000;
const FIXTURE_NOW_UNIX_MS = 1_700_000_000_000;

const physicalBinding: QualificationBinding = Object.freeze({
  qualification_id: 'qualification-physical',
  qualification_digest: `sha256:${'a'.repeat(64)}`,
  deployment_id: 'deployment-alpha',
  deployment_epoch: 7,
  topology_version: 11,
  model_id: 'model-alpha',
  resolved_commit: 'commit-physical',
  manifest_digest: `sha256:${'b'.repeat(64)}`,
  path_manifest_digest: `sha256:${'c'.repeat(64)}`,
  stage_load_proof_digests: Object.freeze([`sha256:${'d'.repeat(64)}`, `sha256:${'e'.repeat(64)}`]),
});

const syntheticBinding: QualificationBinding = Object.freeze({
  qualification_id: 'qualification-synthetic',
  qualification_digest: `sha256:${'5'.repeat(64)}`,
  deployment_id: 'deployment-synthetic',
  deployment_epoch: 3,
  topology_version: 4,
  model_id: 'model-synthetic',
  resolved_commit: 'commit-synthetic',
  manifest_digest: `sha256:${'6'.repeat(64)}`,
  path_manifest_digest: `sha256:${'7'.repeat(64)}`,
  stage_load_proof_digests: Object.freeze([`sha256:${'8'.repeat(64)}`]),
});

const REQUEST_STATES: readonly ObservatoryRequestState[] = [
  'accepted',
  'streaming',
  'completed',
  'cancelled',
  'failed',
  'quarantined',
];

function requestSession(state: ObservatoryRequestState, sequence: number) {
  const eventCount = sequence + 1;
  return {
    request_id: `request-${state}`,
    state,
    last_sequence: sequence,
    event_count: eventCount,
    token_count: state === 'accepted' ? 0 : 1,
    terminal: ['completed', 'cancelled', 'failed'].includes(state),
    qualification_id: physicalBinding.qualification_id,
    started_at_unix_ms: FIXTURE_NOW_UNIX_MS - 1_000,
    updated_at_unix_ms: FIXTURE_NOW_UNIX_MS - 800,
    quarantine_reason: state === 'quarantined' ? 'peer_disconnect' : null,
  };
}

const acceptedRequest = (id: string): InferenceAcceptedResponse => ({
  protocol: 'mycelium.request_gateway.v1',
  request_id: id,
  accepted: true,
  event_path: `/api/v1/inference/events/${id}`,
  cancel_path: `/api/v1/inference/cancel/${id}`,
});

const physicalQualification = (
  overrides: Partial<ProductQualification> = {},
): ProductQualification => ({
  protocol: 'mycelium.request_gateway.v1',
  issued_at_unix_ms: FIXTURE_NOW_UNIX_MS - 1_000,
  evidence_class: 'physical_qualification',
  route_ready: true,
  reason_codes: [],
  binding: physicalBinding,
  ...overrides,
});

const syntheticQualification = (
  overrides: Partial<ProductQualification> = {},
): ProductQualification => ({
  protocol: 'mycelium.request_gateway.v1',
  issued_at_unix_ms: FIXTURE_NOW_UNIX_MS - 1_000,
  evidence_class: 'synthetic_test_fixture',
  route_ready: false,
  reason_codes: ['synthetic_test_fixture_not_accepted'],
  binding: syntheticBinding,
  ...overrides,
});

const nativeNode = (overrides: Partial<ProductNativeNode> = {}): ProductNativeNode => ({
  member_id: 'native-alpha',
  capability: 'native_inference_node',
  membership_state: 'qualified',
  connectivity: 'direct',
  endpoint_id: null,
  ...overrides,
});

const browserWorker = (
  overrides: Partial<ProductBrowserWorker> = {},
): ProductBrowserWorker => ({
  peer_id: 'browser-alpha',
  capability: 'synthetic_browser_probe',
  state: 'ready',
  expires_at_unix_ms: FIXTURE_NOW_UNIX_MS + 30_000,
  ...overrides,
});

function emptySwarm() {
  return {
    kind: 'live' as ProductSourceMode,
    native_nodes: [] as readonly ProductNativeNode[],
    browser_workers: [] as readonly ProductBrowserWorker[],
    previous_browser_worker_ids: [] as readonly string[],
    previous_native_member_ids: [] as readonly string[],
    revoked_member_ids: [] as readonly string[],
    cleared_at_unix_ms: null as number | null,
    cleanup: { leave_confirmed: false, session_cleared: false },
  };
}

function emptyInference(phase: SessionInferencePhase = 'idle') {
  return {
    kind: 'live' as ProductSourceMode,
    qualification: null as ProductQualification | null,
    qualification_loading: false,
    phase,
    accepted_request: null as InferenceAcceptedResponse | null,
    freshness_window_ms: FIXTURE_FRESHNESS_WINDOW_MS,
    now_unix_ms: FIXTURE_NOW_UNIX_MS,
  };
}

const baseInputs = (): LifecycleInputs => ({
  observatory: {
    kind: 'live',
    source_cursor: 7,
    observed_at_unix_ms: FIXTURE_NOW_UNIX_MS - 500,
    qualification: null,
    incidents: [],
    sessions: [],
  },
  swarm: emptySwarm(),
  inference: emptyInference('idle'),
  now_unix_ms: FIXTURE_NOW_UNIX_MS,
});

export const recordedFixtureCatalog = {
  requestStates: REQUEST_STATES,
  freshnessWindowMs: FIXTURE_FRESHNESS_WINDOW_MS,
  nowUnixMs: FIXTURE_NOW_UNIX_MS,
};

export function preparingFixture(options?: { readonly state?: LifecycleState }): LifecycleInputs {
  if (options?.state !== undefined && options.state !== 'preparing') {
    return fixtureForState(options.state);
  }
  return {
    ...baseInputs(),
    observatory: {
      kind: 'live',
      source_cursor: 0,
      observed_at_unix_ms: null,
      qualification: null,
      incidents: [],
      sessions: [],
    },
    swarm: emptySwarm(),
    inference: { ...emptyInference('idle'), qualification_loading: true },
    explicit_state: 'preparing',
  };
}

export function loadingFixture(): LifecycleInputs {
  return {
    ...baseInputs(),
    inference: { ...emptyInference('idle'), qualification_loading: true },
    explicit_state: 'loading',
  };
}

export function unreadyFixture(): LifecycleInputs {
  const qualification = syntheticQualification();
  return {
    ...baseInputs(),
    observatory: {
      kind: 'live',
      source_cursor: 7,
      observed_at_unix_ms: FIXTURE_NOW_UNIX_MS - 500,
      qualification,
      incidents: [],
      sessions: [],
    },
    inference: { ...emptyInference('idle'), qualification },
    explicit_state: 'unready',
  };
}

export function qualifiedReadyFixture(
  options: { readonly force_synthetic_route_ready?: boolean } = {},
): LifecycleInputs {
  const qualification = options.force_synthetic_route_ready
    ? { ...syntheticQualification(), route_ready: true, reason_codes: [] }
    : physicalQualification();
  return {
    ...baseInputs(),
    observatory: {
      kind: 'live',
      source_cursor: 9,
      observed_at_unix_ms: FIXTURE_NOW_UNIX_MS - 200,
      qualification,
      incidents: [],
      sessions: [],
    },
    swarm: {
      ...emptySwarm(),
      native_nodes: [nativeNode()],
      browser_workers: [browserWorker()],
      previous_browser_worker_ids: ['browser-alpha'],
      previous_native_member_ids: ['native-alpha'],
    },
    inference: { ...emptyInference('idle'), qualification },
    explicit_state: 'qualified-ready',
  };
}

export function generatingFixture(): LifecycleInputs {
  const qualification = physicalQualification();
  const sessions = [requestSession('streaming', 3), requestSession('accepted', 0)];
  return {
    ...baseInputs(),
    observatory: {
      kind: 'live',
      source_cursor: 11,
      observed_at_unix_ms: FIXTURE_NOW_UNIX_MS - 100,
      qualification,
      incidents: [],
      sessions,
    },
    swarm: {
      ...emptySwarm(),
      native_nodes: [nativeNode()],
      browser_workers: [browserWorker()],
      previous_browser_worker_ids: ['browser-alpha'],
      previous_native_member_ids: ['native-alpha'],
    },
    inference: {
      ...emptyInference('streaming'),
      qualification,
      accepted_request: acceptedRequest('request-streaming'),
    },
    explicit_state: 'generating',
  };
}

export function cancellingFixture(): LifecycleInputs {
  const qualification = physicalQualification();
  const sessions = [requestSession('streaming', 2)];
  return {
    ...baseInputs(),
    observatory: {
      kind: 'live',
      source_cursor: 12,
      observed_at_unix_ms: FIXTURE_NOW_UNIX_MS - 50,
      qualification,
      incidents: [],
      sessions,
    },
    inference: {
      ...emptyInference('cancelling'),
      qualification,
      accepted_request: acceptedRequest('request-cancelling'),
    },
    explicit_state: 'cancelling',
  };
}

export function peerLostFixture(): LifecycleInputs {
  const qualification = physicalQualification();
  const sessions = [requestSession('failed', 2)];
  return {
    ...baseInputs(),
    observatory: {
      kind: 'live',
      source_cursor: 13,
      observed_at_unix_ms: FIXTURE_NOW_UNIX_MS - 30,
      qualification,
      incidents: [
        {
          protocol: 'mycelium.request_event.v1',
          source_cursor: 13,
          reason: 'peer_disconnect',
        },
      ],
      sessions,
    },
    swarm: {
      ...emptySwarm(),
      native_nodes: [nativeNode({ member_id: 'native-alpha' })],
      previous_browser_worker_ids: ['browser-alpha'],
      previous_native_member_ids: ['native-alpha'],
    },
    inference: { ...emptyInference('failed'), qualification },
    explicit_state: 'peer-lost',
  };
}

export function peerLostNativeBrowserFixture(): LifecycleInputs {
  const qualification = physicalQualification();
  return {
    ...baseInputs(),
    swarm: {
      ...emptySwarm(),
      previous_browser_worker_ids: ['browser-1', 'browser-alpha'],
      previous_native_member_ids: ['native-alpha', 'native-beta'],
    },
    observatory: {
      kind: 'live',
      source_cursor: 14,
      observed_at_unix_ms: FIXTURE_NOW_UNIX_MS - 30,
      qualification,
      incidents: [
        {
          protocol: 'mycelium.request_event.v1',
          source_cursor: 14,
          reason: 'peer_disconnect',
        },
      ],
      sessions: [],
    },
    explicit_state: 'peer-lost',
  };
}

export function recoveringFixture(): LifecycleInputs {
  const qualification = physicalQualification({
    issued_at_unix_ms: FIXTURE_NOW_UNIX_MS - 100,
  });
  const previousLost = ['browser-alpha', 'native-alpha'];
  return {
    ...baseInputs(),
    observatory: {
      kind: 'live',
      source_cursor: 15,
      observed_at_unix_ms: FIXTURE_NOW_UNIX_MS - 30,
      qualification,
      incidents: [
        {
          protocol: 'mycelium.request_event.v1',
          source_cursor: 14,
          reason: 'peer_disconnect',
        },
        {
          protocol: 'mycelium.route_qualification.v1',
          source_cursor: 15,
          reason: 'qualifier_rebinding',
        },
      ],
      sessions: [],
    },
    swarm: {
      ...emptySwarm(),
      previous_browser_worker_ids: previousLost,
      previous_native_member_ids: previousLost,
    },
    inference: { ...emptyInference('idle'), qualification },
    explicit_state: 'recovering',
  };
}

export function staleFixture(): LifecycleInputs {
  const issuedAt = FIXTURE_NOW_UNIX_MS - (FIXTURE_FRESHNESS_WINDOW_MS * 4);
  const qualification = physicalQualification({ issued_at_unix_ms: issuedAt });
  return {
    ...baseInputs(),
    observatory: {
      kind: 'live',
      source_cursor: 16,
      observed_at_unix_ms: issuedAt,
      qualification,
      incidents: [],
      sessions: [],
    },
    inference: { ...emptyInference('idle'), qualification },
    explicit_state: 'stale',
  };
}

export function revokedFixture(): LifecycleInputs {
  return {
    ...baseInputs(),
    swarm: {
      ...emptySwarm(),
      native_nodes: [nativeNode({ member_id: 'native-alpha', membership_state: 'revoked' })],
      previous_browser_worker_ids: ['browser-alpha'],
      previous_native_member_ids: ['native-alpha'],
      revoked_member_ids: ['native-alpha'],
    },
    explicit_state: 'revoked',
  };
}

export function cleanupFixture(): LifecycleInputs {
  return {
    ...baseInputs(),
    swarm: {
      ...emptySwarm(),
      previous_browser_worker_ids: ['browser-alpha'],
      previous_native_member_ids: ['native-alpha'],
      revoked_member_ids: ['native-alpha'],
      cleared_at_unix_ms: FIXTURE_NOW_UNIX_MS,
      cleanup: { leave_confirmed: true, session_cleared: true },
    },
    inference: emptyInference('idle'),
    explicit_state: 'cleanup-complete',
  };
}

export function fixtureForState(state: LifecycleState): LifecycleInputs {
  switch (state) {
    case 'preparing':
      return preparingFixture();
    case 'loading':
      return loadingFixture();
    case 'unready':
      return unreadyFixture();
    case 'qualified-ready':
      return qualifiedReadyFixture();
    case 'generating':
      return generatingFixture();
    case 'cancelling':
      return cancellingFixture();
    case 'peer-lost':
      return peerLostFixture();
    case 'recovering':
      return recoveringFixture();
    case 'stale':
      return staleFixture();
    case 'revoked':
      return revokedFixture();
    case 'cleanup-complete':
      return cleanupFixture();
  }
}

export function recordedLifecycleFixtures() {
  return {
    preparing: preparingFixture(),
    loading: loadingFixture(),
    unready: unreadyFixture(),
    qualifiedReady: qualifiedReadyFixture(),
    generating: generatingFixture(),
    cancelling: cancellingFixture(),
    peerLost: peerLostFixture(),
    recovering: recoveringFixture(),
    stale: staleFixture(),
    revoked: revokedFixture(),
    cleanup: cleanupFixture(),
  } as const;
}

export type RecordedLifecycleFixtureName = keyof ReturnType<typeof recordedLifecycleFixtures>;

export { physicalBinding, syntheticBinding };
export type { ProductSourceMode };
export type ObservatoryFixtureBundle = ObservatoryAdapterBundle;
