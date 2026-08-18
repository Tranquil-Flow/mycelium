export const LIVE_ROUTE_STATUS_PATH = '/__mycelium/live-status';
export const LIVE_ROUTE_STATUS_PROTOCOL = 'mycelium.live_route_status.v1' as const;

const SHA256 = /^sha256:[0-9a-f]{64}$/;
const IDENTIFIER = /^[A-Za-z0-9][A-Za-z0-9._:~\/-]{0,255}$/;
const MAX_ITEMS = 256;

export interface LiveRouteCounters {
  readonly frames_sent: number;
  readonly frames_received: number;
  readonly applied_operation_count: number;
  readonly fatal: string | null;
}

export interface LiveRouteStage {
  readonly stage_id: string;
  readonly placement_id: string;
  readonly node_id: string;
  readonly runtime_backend: 'mlx' | 'numpy' | 'pixel-stdlib';
  readonly start_layer: number;
  readonly end_layer_exclusive: number;
  readonly component_roles: readonly string[];
}

export interface LiveRoutePeer {
  readonly node_id: string;
  readonly placements: readonly LiveRouteStage[];
  readonly frames_sent: number;
  readonly frames_received: number;
  readonly applied_operation_count: number;
  readonly decode_mode: string | null;
  readonly architecture: string | null;
  readonly supported_decode_modes: readonly string[];
  readonly active_kv_state_count: number;
  readonly active_kv_bytes: number;
  readonly peak_kv_bytes: number;
  readonly prefill_operation_count: number;
  readonly prefill_input_token_count: number;
  readonly decode_operation_count: number;
  readonly decode_input_token_count: number;
  readonly activation_output_bytes: number;
  readonly current_position: number | null;
  readonly release_state: 'idle' | 'active' | 'released' | 'closed' | 'unknown';
  readonly last_release_reason: string | null;
  readonly retained_result_count: number;
  readonly release_counts: Readonly<Record<string, number>>;
}

export interface LiveRouteInferenceTiming {
  readonly context_tokens: number;
  readonly output_tokens: number;
  readonly prefill_ms: number | null;
  readonly ttft_ms: number | null;
  readonly tpot_ms: number | null;
  readonly total_ms: number;
  readonly peer_counter_deltas: readonly {
    readonly node_id: string;
    readonly frames_sent: number;
    readonly frames_received: number;
    readonly applied_operation_count: number;
  }[];
}

export interface LiveRouteIncident {
  readonly protocol: 'mycelium.live_route_incident.v1';
  readonly incident_id: string;
  readonly deployment_id: string;
  readonly request_id: string | null;
  readonly state: 'route_failed_closed' | 'configured_deployment_unavailable' | 'qualified_service_restored' | 'qualified_failover_selected' | 'qualified_deployment_selected' | 'qualified_candidate_promoted' | 'qualified_candidate_rolled_back';
  readonly reason: string;
  readonly observed_at_unix_ms: number;
}

export interface M13PlacementNode {
  readonly node_id: string;
  readonly backend: string;
  readonly decode_mode: string;
  readonly start_layer: number;
  readonly end_layer_exclusive: number;
  readonly fast_allocatable_bytes: number;
  readonly total_allocatable_bytes: number;
  readonly prefill_ms_per_layer_token: number;
  readonly decode_ms_per_layer_token: number;
  readonly profile_digest: string;
  readonly source_evidence_digest: string;
  readonly assignment_id: string;
  readonly assignment_digest: string;
  readonly assigned_object_count: number;
  readonly load_proof_digest: string | null;
  readonly ready: boolean;
}

export interface M13PlacementProjection {
  readonly protocol: 'mycelium.m13_placement_projection.v1';
  readonly snapshot_digest: string;
  readonly evidence_bundle_digest: string;
  readonly snapshot_generation: number;
  readonly authority_generation: number;
  readonly verification_key_digest: string;
  readonly valid_until_unix_ms: number;
  readonly placement_provenance: 'planner_v2';
  readonly decode_mode: string;
  readonly quantization: string;
  readonly nodes: readonly M13PlacementNode[];
  readonly links: readonly { readonly src: string; readonly dst: string; readonly rtt_ms: number; readonly jitter_ms: number; readonly bandwidth_Bps: number }[];
  readonly exclusions: readonly { readonly node_id: string; readonly reasons: readonly string[] }[];
  readonly ab_deltas: readonly {
    readonly kind: string;
    readonly changed_input: string;
    readonly baseline_snapshot_digest: string;
    readonly candidate_snapshot_digest: string;
    readonly allocation_before: readonly { readonly node_id: string; readonly start: number; readonly end: number }[];
    readonly allocation_after: readonly { readonly node_id: string; readonly start: number; readonly end: number }[];
  }[];
  readonly promotion: null | { readonly candidate_deployment_id: string; readonly incumbent_deployment_id: string; readonly decision: 'promote' | 'reject'; readonly reasons: readonly string[]; readonly sample_size: number };
  readonly route_ready: false;
}

export interface M14TopologyEdge {
  readonly src: string;
  readonly dst: string;
  readonly src_endpoint_digest: string;
  readonly dst_endpoint_digest: string;
  readonly path_class: 'direct' | 'relay';
  readonly relay_identity: string | null;
  readonly relay_region: string | null;
  readonly rtt_ms: number;
  readonly jitter_ms: number;
  readonly loss_ratio: number;
  readonly goodput_Bps: number;
  readonly sample_count: number;
  readonly connections_opened: number;
  readonly frames_sent: number;
  readonly connection_generation: number;
  readonly fresh_until_unix_ms: number;
  readonly observation_digest: string;
  readonly formula: 'one_way_rtt_plus_jitter_v1';
  readonly logical_role: 'physical_only' | 'forward' | 'decode_loopback';
}

export interface M14TopologyProjection {
  readonly protocol: 'mycelium.m14_topology_projection.v1';
  readonly measurement_source: 'iroh_activation_plane';
  readonly decision: {
    readonly mode: string;
    readonly globally_exact: boolean;
    readonly explored_candidates: number;
    readonly selected_cycle: readonly string[];
    readonly selected_cost_ms: number;
    readonly opened_order: readonly string[];
    readonly loopback: { readonly src: string; readonly dst: string };
    readonly canonical_node_id_order: readonly string[];
    readonly differs_from_canonical_order: boolean;
    readonly candidates: readonly {
      readonly order: readonly string[];
      readonly cost_ms: number;
      readonly selected: boolean;
      readonly rejection_reason: string | null;
    }[];
    readonly winning_rationale: string;
  };
  readonly allocation: readonly { readonly node_id: string; readonly start: number; readonly end: number }[];
  readonly edges: readonly M14TopologyEdge[];
  readonly exclusions: readonly string[];
  readonly promotion: M13PlacementProjection['promotion'];
  readonly route_ready: false;
}

export interface LiveRouteStatus {
  readonly protocol: typeof LIVE_ROUTE_STATUS_PROTOCOL;
  readonly route_alive: boolean;
  readonly simulated: boolean;
  readonly route_identity_digest: string | null;
  readonly deployment_id: string;
  readonly model_id: string;
  readonly topology_version: number;
  readonly decode_mode: string;
  readonly counters: LiveRouteCounters;
  readonly stages: readonly LiveRouteStage[];
  readonly peers: readonly LiveRoutePeer[];
  readonly recent_inferences: readonly LiveRouteInferenceTiming[];
  readonly incidents: readonly LiveRouteIncident[];
  readonly placement: M13PlacementProjection | null;
  readonly topology: M14TopologyProjection | null;
}

function record(value: unknown, keys: readonly string[], path: string): Record<string, unknown> {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) {
    throw new TypeError(`${path} must be an object`);
  }
  const actual = Object.keys(value);
  if (actual.length !== keys.length || actual.some((key) => !keys.includes(key))) {
    throw new TypeError(`${path} has unknown or missing fields`);
  }
  return value as Record<string, unknown>;
}

function array(value: unknown, path: string): unknown[] {
  if (!Array.isArray(value) || value.length > MAX_ITEMS) {
    throw new TypeError(`${path} must be a bounded array`);
  }
  return value;
}

function integer(value: unknown, path: string): number {
  if (!Number.isSafeInteger(value) || (value as number) < 0) {
    throw new TypeError(`${path} must be a non-negative safe integer`);
  }
  return value as number;
}

function finite(value: unknown, path: string): number {
  if (typeof value !== 'number' || !Number.isFinite(value) || value < 0) {
    throw new TypeError(`${path} must be a non-negative finite number`);
  }
  return value;
}

function optionalFinite(value: unknown, path: string): number | null {
  return value === null ? null : finite(value, path);
}

function identifier(value: unknown, path: string): string {
  if (typeof value !== 'string' || !IDENTIFIER.test(value)) {
    throw new TypeError(`${path} must be a public identifier`);
  }
  return value;
}

function boundedText(value: unknown, path: string): string {
  if (typeof value !== 'string' || value.length < 1 || value.length > 256) {
    throw new TypeError(`${path} must be bounded text`);
  }
  return value;
}

function counters(value: unknown, path: string): LiveRouteCounters {
  const item = record(
    value,
    ['frames_sent', 'frames_received', 'applied_operation_count', 'fatal'],
    path,
  );
  const fatal = item.fatal;
  if (fatal !== null && (typeof fatal !== 'string' || fatal.length > 128)) {
    throw new TypeError(`${path}.fatal must be a bounded code or null`);
  }
  return Object.freeze({
    frames_sent: integer(item.frames_sent, `${path}.frames_sent`),
    frames_received: integer(item.frames_received, `${path}.frames_received`),
    applied_operation_count: integer(
      item.applied_operation_count,
      `${path}.applied_operation_count`,
    ),
    fatal,
  });
}

function stage(value: unknown, path: string): LiveRouteStage {
  const item = record(
    value,
    [
      'stage_id',
      'placement_id',
      'node_id',
      'runtime_backend',
      'start_layer',
      'end_layer_exclusive',
      'component_roles',
    ],
    path,
  );
  const backend = item.runtime_backend;
  if (!['mlx', 'numpy', 'pixel-stdlib'].includes(String(backend))) {
    throw new TypeError(`${path}.runtime_backend is unsupported`);
  }
  const start = integer(item.start_layer, `${path}.start_layer`);
  const end = integer(item.end_layer_exclusive, `${path}.end_layer_exclusive`);
  if (end <= start) throw new TypeError(`${path} has an empty layer range`);
  return Object.freeze({
    stage_id: identifier(item.stage_id, `${path}.stage_id`),
    placement_id: identifier(item.placement_id, `${path}.placement_id`),
    node_id: identifier(item.node_id, `${path}.node_id`),
    runtime_backend: backend as LiveRouteStage['runtime_backend'],
    start_layer: start,
    end_layer_exclusive: end,
    component_roles: Object.freeze(
      array(item.component_roles, `${path}.component_roles`).map((role, index) =>
        identifier(role, `${path}.component_roles[${index}]`),
      ),
    ),
  });
}

function peer(value: unknown, path: string): LiveRoutePeer {
  const item = record(
    value,
    [
      'node_id',
      'placements',
      'frames_sent',
      'frames_received',
      'applied_operation_count',
      'decode_mode',
      'architecture',
      'supported_decode_modes',
      'active_kv_state_count',
      'active_kv_bytes',
      'peak_kv_bytes',
      'prefill_operation_count',
      'prefill_input_token_count',
      'decode_operation_count',
      'decode_input_token_count',
      'activation_output_bytes',
      'current_position',
      'release_state',
      'last_release_reason',
      'retained_result_count',
      'release_counts',
    ],
    path,
  );
  const mode = item.decode_mode;
  if (mode !== null && (typeof mode !== 'string' || mode.length > 64)) {
    throw new TypeError(`${path}.decode_mode is invalid`);
  }
  const architecture = item.architecture;
  if (architecture !== null && typeof architecture !== 'string') throw new TypeError(`${path}.architecture is invalid`);
  const currentPosition = item.current_position;
  if (currentPosition !== null) integer(currentPosition, `${path}.current_position`);
  if (!['idle', 'active', 'released', 'closed', 'unknown'].includes(String(item.release_state))) throw new TypeError(`${path}.release_state is invalid`);
  const lastReleaseReason = item.last_release_reason;
  if (lastReleaseReason !== null && typeof lastReleaseReason !== 'string') throw new TypeError(`${path}.last_release_reason is invalid`);
  if (typeof item.release_counts !== 'object' || item.release_counts === null || Array.isArray(item.release_counts)) {
    throw new TypeError(`${path}.release_counts must be an object`);
  }
  const releases = Object.fromEntries(
    Object.entries(item.release_counts).map(([reason, count]) => [
      identifier(reason, `${path}.release_counts reason`),
      integer(count, `${path}.release_counts.${reason}`),
    ]),
  );
  const base = counters(
    {
      frames_sent: item.frames_sent,
      frames_received: item.frames_received,
      applied_operation_count: item.applied_operation_count,
      fatal: null,
    },
    path,
  );
  return Object.freeze({
    frames_sent: base.frames_sent,
    frames_received: base.frames_received,
    applied_operation_count: base.applied_operation_count,
    node_id: identifier(item.node_id, `${path}.node_id`),
    placements: Object.freeze(
      array(item.placements, `${path}.placements`).map((candidate, index) =>
        stage(candidate, `${path}.placements[${index}]`),
      ),
    ),
    decode_mode: mode,
    architecture: architecture === null ? null : identifier(architecture, `${path}.architecture`),
    supported_decode_modes: Object.freeze(array(item.supported_decode_modes, `${path}.supported_decode_modes`).map((value, index) => identifier(value, `${path}.supported_decode_modes[${index}]`))),
    active_kv_state_count: integer(item.active_kv_state_count, `${path}.active_kv_state_count`),
    active_kv_bytes: integer(item.active_kv_bytes, `${path}.active_kv_bytes`),
    peak_kv_bytes: integer(item.peak_kv_bytes, `${path}.peak_kv_bytes`),
    prefill_operation_count: integer(item.prefill_operation_count, `${path}.prefill_operation_count`),
    prefill_input_token_count: integer(item.prefill_input_token_count, `${path}.prefill_input_token_count`),
    decode_operation_count: integer(item.decode_operation_count, `${path}.decode_operation_count`),
    decode_input_token_count: integer(item.decode_input_token_count, `${path}.decode_input_token_count`),
    activation_output_bytes: integer(item.activation_output_bytes, `${path}.activation_output_bytes`),
    current_position: currentPosition as number | null,
    release_state: item.release_state as LiveRoutePeer['release_state'],
    last_release_reason: lastReleaseReason === null ? null : identifier(lastReleaseReason, `${path}.last_release_reason`),
    retained_result_count: integer(item.retained_result_count, `${path}.retained_result_count`),
    release_counts: Object.freeze(releases),
  });
}

function timing(value: unknown, path: string): LiveRouteInferenceTiming {
  const item = record(
    value,
    ['context_tokens', 'output_tokens', 'prefill_ms', 'ttft_ms', 'tpot_ms', 'total_ms', 'peer_counter_deltas'],
    path,
  );
  return Object.freeze({
    context_tokens: integer(item.context_tokens, `${path}.context_tokens`),
    output_tokens: integer(item.output_tokens, `${path}.output_tokens`),
    prefill_ms: optionalFinite(item.prefill_ms, `${path}.prefill_ms`),
    ttft_ms: optionalFinite(item.ttft_ms, `${path}.ttft_ms`),
    tpot_ms: optionalFinite(item.tpot_ms, `${path}.tpot_ms`),
    total_ms: finite(item.total_ms, `${path}.total_ms`),
    peer_counter_deltas: Object.freeze(
      array(item.peer_counter_deltas, `${path}.peer_counter_deltas`).map((candidate, index) => {
        const delta = record(
          candidate,
          ['node_id', 'frames_sent', 'frames_received', 'applied_operation_count'],
          `${path}.peer_counter_deltas[${index}]`,
        );
        return Object.freeze({
          node_id: identifier(delta.node_id, `${path}.peer_counter_deltas[${index}].node_id`),
          frames_sent: integer(delta.frames_sent, `${path}.peer_counter_deltas[${index}].frames_sent`),
          frames_received: integer(delta.frames_received, `${path}.peer_counter_deltas[${index}].frames_received`),
          applied_operation_count: integer(delta.applied_operation_count, `${path}.peer_counter_deltas[${index}].applied_operation_count`),
        });
      }),
    ),
  });
}

function incident(value: unknown, path: string): LiveRouteIncident {
  const item = record(
    value,
    ['protocol', 'incident_id', 'deployment_id', 'request_id', 'state', 'reason', 'observed_at_unix_ms'],
    path,
  );
  if (item.protocol !== 'mycelium.live_route_incident.v1') {
    throw new TypeError(`${path}.protocol is unsupported`);
  }
  if (
    !['route_failed_closed', 'configured_deployment_unavailable', 'qualified_service_restored', 'qualified_failover_selected', 'qualified_deployment_selected', 'qualified_candidate_promoted', 'qualified_candidate_rolled_back'].includes(String(item.state))
  ) {
    throw new TypeError(`${path}.state is invalid`);
  }
  if (
    item.request_id !== null
    && (typeof item.request_id !== 'string' || item.request_id.length < 1 || item.request_id.length > 512)
  ) {
    throw new TypeError(`${path}.request_id is invalid`);
  }
  if (typeof item.reason !== 'string' || item.reason.length < 1 || item.reason.length > 128) {
    throw new TypeError(`${path}.reason is invalid`);
  }
  return Object.freeze({
    protocol: 'mycelium.live_route_incident.v1',
    incident_id: identifier(item.incident_id, `${path}.incident_id`),
    deployment_id: identifier(item.deployment_id, `${path}.deployment_id`),
    request_id: item.request_id,
    state: item.state as LiveRouteIncident['state'],
    reason: item.reason,
    observed_at_unix_ms: integer(item.observed_at_unix_ms, `${path}.observed_at_unix_ms`),
  });
}

function digest(value: unknown, path: string): string {
  if (typeof value !== 'string' || !SHA256.test(value)) throw new TypeError(`${path} must be a SHA-256 reference`);
  return value;
}

function placementNode(value: unknown, path: string): M13PlacementNode {
  const item = record(value, [
    'node_id', 'backend', 'decode_mode', 'start_layer', 'end_layer_exclusive',
    'fast_allocatable_bytes', 'total_allocatable_bytes', 'prefill_ms_per_layer_token',
    'decode_ms_per_layer_token', 'profile_digest', 'source_evidence_digest',
    'assignment_id', 'assignment_digest', 'assigned_object_count', 'load_proof_digest', 'ready',
  ], path);
  const loadProof = item.load_proof_digest;
  if (loadProof !== null) digest(loadProof, `${path}.load_proof_digest`);
  if (typeof item.ready !== 'boolean') throw new TypeError(`${path}.ready must be boolean`);
  return Object.freeze({
    node_id: identifier(item.node_id, `${path}.node_id`),
    backend: identifier(item.backend, `${path}.backend`),
    decode_mode: identifier(item.decode_mode, `${path}.decode_mode`),
    start_layer: integer(item.start_layer, `${path}.start_layer`),
    end_layer_exclusive: integer(item.end_layer_exclusive, `${path}.end_layer_exclusive`),
    fast_allocatable_bytes: integer(item.fast_allocatable_bytes, `${path}.fast_allocatable_bytes`),
    total_allocatable_bytes: integer(item.total_allocatable_bytes, `${path}.total_allocatable_bytes`),
    prefill_ms_per_layer_token: finite(item.prefill_ms_per_layer_token, `${path}.prefill_ms_per_layer_token`),
    decode_ms_per_layer_token: finite(item.decode_ms_per_layer_token, `${path}.decode_ms_per_layer_token`),
    profile_digest: digest(item.profile_digest, `${path}.profile_digest`),
    source_evidence_digest: digest(item.source_evidence_digest, `${path}.source_evidence_digest`),
    assignment_id: identifier(item.assignment_id, `${path}.assignment_id`),
    assignment_digest: digest(item.assignment_digest, `${path}.assignment_digest`),
    assigned_object_count: integer(item.assigned_object_count, `${path}.assigned_object_count`),
    load_proof_digest: loadProof as string | null,
    ready: item.ready,
  });
}

function placement(value: unknown, path: string): M13PlacementProjection | null {
  if (value === null) return null;
  const item = record(value, [
    'protocol', 'snapshot_digest', 'evidence_bundle_digest', 'snapshot_generation',
    'authority_generation', 'verification_key_digest', 'valid_until_unix_ms',
    'placement_provenance', 'decode_mode', 'quantization', 'nodes', 'links',
    'exclusions', 'ab_deltas', 'promotion', 'route_ready',
  ], path);
  if (item.protocol !== 'mycelium.m13_placement_projection.v1' || item.placement_provenance !== 'planner_v2' || item.route_ready !== false) {
    throw new TypeError(`${path} authority fields are invalid`);
  }
  const promotionValue = item.promotion;
  let decodedPromotion: M13PlacementProjection['promotion'] = null;
  if (promotionValue !== null) {
    const candidate = record(promotionValue, ['candidate_deployment_id', 'incumbent_deployment_id', 'decision', 'reasons', 'sample_size'], `${path}.promotion`);
    if (!['promote', 'reject'].includes(String(candidate.decision))) throw new TypeError(`${path}.promotion.decision is invalid`);
    decodedPromotion = Object.freeze({
      candidate_deployment_id: identifier(candidate.candidate_deployment_id, `${path}.promotion.candidate_deployment_id`),
      incumbent_deployment_id: identifier(candidate.incumbent_deployment_id, `${path}.promotion.incumbent_deployment_id`),
      decision: candidate.decision as 'promote' | 'reject',
      reasons: Object.freeze(array(candidate.reasons, `${path}.promotion.reasons`).map((reason, index) => identifier(reason, `${path}.promotion.reasons[${index}]`))),
      sample_size: integer(candidate.sample_size, `${path}.promotion.sample_size`),
    });
  }
  const links = array(item.links, `${path}.links`).map((value, index) => {
    const link = record(value, ['src', 'dst', 'rtt_ms', 'jitter_ms', 'bandwidth_Bps'], `${path}.links[${index}]`);
    return Object.freeze({ src: identifier(link.src, `${path}.links[${index}].src`), dst: identifier(link.dst, `${path}.links[${index}].dst`), rtt_ms: finite(link.rtt_ms, `${path}.links[${index}].rtt_ms`), jitter_ms: finite(link.jitter_ms, `${path}.links[${index}].jitter_ms`), bandwidth_Bps: finite(link.bandwidth_Bps, `${path}.links[${index}].bandwidth_Bps`) });
  });
  const exclusions = array(item.exclusions, `${path}.exclusions`).map((value, index) => {
    const exclusion = record(value, ['node_id', 'reasons'], `${path}.exclusions[${index}]`);
    return Object.freeze({ node_id: identifier(exclusion.node_id, `${path}.exclusions[${index}].node_id`), reasons: Object.freeze(array(exclusion.reasons, `${path}.exclusions[${index}].reasons`).map((reason, reasonIndex) => identifier(reason, `${path}.exclusions[${index}].reasons[${reasonIndex}]`))) });
  });
  const deltas = array(item.ab_deltas, `${path}.ab_deltas`).map((value, index) => {
    const deltaPath = `${path}.ab_deltas[${index}]`;
    const delta = record(value, ['kind', 'changed_input', 'baseline_snapshot_digest', 'candidate_snapshot_digest', 'allocation_before', 'allocation_after'], deltaPath);
    const allocation = (input: unknown, field: string) => Object.freeze(
      array(input, `${deltaPath}.${field}`).map((entry, allocationIndex) => {
        const entryPath = `${deltaPath}.${field}[${allocationIndex}]`;
        const item = record(entry, ['node_id', 'start', 'end'], entryPath);
        return Object.freeze({
          node_id: identifier(item.node_id, `${entryPath}.node_id`),
          start: integer(item.start, `${entryPath}.start`),
          end: integer(item.end, `${entryPath}.end`),
        });
      }),
    );
    return Object.freeze({
      kind: identifier(delta.kind, `${deltaPath}.kind`),
      changed_input: boundedText(delta.changed_input, `${deltaPath}.changed_input`),
      baseline_snapshot_digest: digest(delta.baseline_snapshot_digest, `${deltaPath}.baseline_snapshot_digest`),
      candidate_snapshot_digest: digest(delta.candidate_snapshot_digest, `${deltaPath}.candidate_snapshot_digest`),
      allocation_before: allocation(delta.allocation_before, 'allocation_before'),
      allocation_after: allocation(delta.allocation_after, 'allocation_after'),
    });
  });
  return Object.freeze({
    protocol: 'mycelium.m13_placement_projection.v1',
    snapshot_digest: digest(item.snapshot_digest, `${path}.snapshot_digest`),
    evidence_bundle_digest: digest(item.evidence_bundle_digest, `${path}.evidence_bundle_digest`),
    snapshot_generation: integer(item.snapshot_generation, `${path}.snapshot_generation`),
    authority_generation: integer(item.authority_generation, `${path}.authority_generation`),
    verification_key_digest: digest(item.verification_key_digest, `${path}.verification_key_digest`),
    valid_until_unix_ms: integer(item.valid_until_unix_ms, `${path}.valid_until_unix_ms`),
    placement_provenance: 'planner_v2',
    decode_mode: identifier(item.decode_mode, `${path}.decode_mode`),
    quantization: identifier(item.quantization, `${path}.quantization`),
    nodes: Object.freeze(array(item.nodes, `${path}.nodes`).map((node, index) => placementNode(node, `${path}.nodes[${index}]`))),
    links: Object.freeze(links), exclusions: Object.freeze(exclusions), ab_deltas: Object.freeze(deltas),
    promotion: decodedPromotion, route_ready: false,
  });
}

function topology(value: unknown, path: string): M14TopologyProjection | null {
  if (value === null) return null;
  const item = record(value, ['protocol', 'measurement_source', 'decision', 'allocation', 'edges', 'exclusions', 'promotion', 'route_ready'], path);
  if (item.protocol !== 'mycelium.m14_topology_projection.v1' || item.measurement_source !== 'iroh_activation_plane' || item.route_ready !== false) {
    throw new TypeError(`${path} authority fields are invalid`);
  }
  const decisionValue = record(item.decision, [
    'mode', 'globally_exact', 'explored_candidates', 'selected_cycle', 'selected_cost_ms',
    'opened_order', 'loopback', 'canonical_node_id_order', 'differs_from_canonical_order',
    'candidates', 'winning_rationale',
  ], `${path}.decision`);
  if (typeof decisionValue.globally_exact !== 'boolean' || typeof decisionValue.differs_from_canonical_order !== 'boolean') {
    throw new TypeError(`${path}.decision booleans are invalid`);
  }
  const nodeOrder = (input: unknown, orderPath: string) => Object.freeze(array(input, orderPath).map((node, index) => identifier(node, `${orderPath}[${index}]`)));
  const openedOrder = nodeOrder(decisionValue.opened_order, `${path}.decision.opened_order`);
  if (openedOrder.length < 3 || new Set(openedOrder).size !== openedOrder.length) throw new TypeError(`${path}.decision.opened_order is invalid`);
  const loopbackValue = record(decisionValue.loopback, ['src', 'dst'], `${path}.decision.loopback`);
  const loopback = Object.freeze({
    src: identifier(loopbackValue.src, `${path}.decision.loopback.src`),
    dst: identifier(loopbackValue.dst, `${path}.decision.loopback.dst`),
  });
  if (loopback.src !== openedOrder.at(-1) || loopback.dst !== openedOrder[0]) throw new TypeError(`${path}.decision.loopback does not close the opened route`);
  const candidates = Object.freeze(array(decisionValue.candidates, `${path}.decision.candidates`).map((candidate, index) => {
    const candidatePath = `${path}.decision.candidates[${index}]`;
    const candidateValue = record(candidate, ['order', 'cost_ms', 'selected', 'rejection_reason'], candidatePath);
    if (typeof candidateValue.selected !== 'boolean') throw new TypeError(`${candidatePath}.selected must be boolean`);
    if (candidateValue.rejection_reason !== null && typeof candidateValue.rejection_reason !== 'string') throw new TypeError(`${candidatePath}.rejection_reason is invalid`);
    return Object.freeze({
      order: nodeOrder(candidateValue.order, `${candidatePath}.order`),
      cost_ms: finite(candidateValue.cost_ms, `${candidatePath}.cost_ms`),
      selected: candidateValue.selected,
      rejection_reason: candidateValue.rejection_reason,
    });
  }));
  const allocation = Object.freeze(array(item.allocation, `${path}.allocation`).map((value, index) => {
    const allocationPath = `${path}.allocation[${index}]`;
    const entry = record(value, ['node_id', 'start', 'end'], allocationPath);
    const start = integer(entry.start, `${allocationPath}.start`);
    const end = integer(entry.end, `${allocationPath}.end`);
    if (end <= start) throw new TypeError(`${allocationPath} has an empty range`);
    return Object.freeze({ node_id: identifier(entry.node_id, `${allocationPath}.node_id`), start, end });
  }));
  const edges = Object.freeze(array(item.edges, `${path}.edges`).map((value, index) => {
    const edgePath = `${path}.edges[${index}]`;
    const edge = record(value, [
      'src', 'dst', 'src_endpoint_digest', 'dst_endpoint_digest', 'path_class',
      'relay_identity', 'relay_region', 'rtt_ms', 'jitter_ms', 'loss_ratio',
      'goodput_Bps', 'sample_count', 'connections_opened', 'frames_sent',
      'connection_generation', 'fresh_until_unix_ms', 'observation_digest', 'formula',
      'logical_role',
    ], edgePath);
    if (!['direct', 'relay'].includes(String(edge.path_class)) || !['physical_only', 'forward', 'decode_loopback'].includes(String(edge.logical_role)) || edge.formula !== 'one_way_rtt_plus_jitter_v1') {
      throw new TypeError(`${edgePath} classification is invalid`);
    }
    const nullableText = (candidate: unknown, field: string) => candidate === null ? null : boundedText(candidate, field);
    return Object.freeze({
      src: identifier(edge.src, `${edgePath}.src`), dst: identifier(edge.dst, `${edgePath}.dst`),
      src_endpoint_digest: digest(edge.src_endpoint_digest, `${edgePath}.src_endpoint_digest`),
      dst_endpoint_digest: digest(edge.dst_endpoint_digest, `${edgePath}.dst_endpoint_digest`),
      path_class: edge.path_class as M14TopologyEdge['path_class'],
      relay_identity: nullableText(edge.relay_identity, `${edgePath}.relay_identity`),
      relay_region: nullableText(edge.relay_region, `${edgePath}.relay_region`),
      rtt_ms: finite(edge.rtt_ms, `${edgePath}.rtt_ms`), jitter_ms: finite(edge.jitter_ms, `${edgePath}.jitter_ms`),
      loss_ratio: finite(edge.loss_ratio, `${edgePath}.loss_ratio`), goodput_Bps: finite(edge.goodput_Bps, `${edgePath}.goodput_Bps`),
      sample_count: integer(edge.sample_count, `${edgePath}.sample_count`), connections_opened: integer(edge.connections_opened, `${edgePath}.connections_opened`),
      frames_sent: integer(edge.frames_sent, `${edgePath}.frames_sent`), connection_generation: integer(edge.connection_generation, `${edgePath}.connection_generation`),
      fresh_until_unix_ms: integer(edge.fresh_until_unix_ms, `${edgePath}.fresh_until_unix_ms`), observation_digest: digest(edge.observation_digest, `${edgePath}.observation_digest`),
      formula: 'one_way_rtt_plus_jitter_v1' as const,
      logical_role: edge.logical_role as M14TopologyEdge['logical_role'],
    });
  }));
  const required = new Set(openedOrder.flatMap((src) => openedOrder.filter((dst) => dst !== src).map((dst) => `${src}\u0000${dst}`)));
  const actual = new Set(edges.map((edge) => `${edge.src}\u0000${edge.dst}`));
  if (required.size !== actual.size || [...required].some((edge) => !actual.has(edge))) throw new TypeError(`${path}.edges is not a complete directed matrix`);
  const promotionValue = item.promotion === null ? null : placement({ ...m13PromotionShell(), promotion: item.promotion }, `${path}.promotion_wrapper`)?.promotion ?? null;
  return Object.freeze({
    protocol: 'mycelium.m14_topology_projection.v1', measurement_source: 'iroh_activation_plane',
    decision: Object.freeze({
      mode: identifier(decisionValue.mode, `${path}.decision.mode`), globally_exact: decisionValue.globally_exact,
      explored_candidates: integer(decisionValue.explored_candidates, `${path}.decision.explored_candidates`),
      selected_cycle: nodeOrder(decisionValue.selected_cycle, `${path}.decision.selected_cycle`),
      selected_cost_ms: finite(decisionValue.selected_cost_ms, `${path}.decision.selected_cost_ms`), opened_order: openedOrder, loopback,
      canonical_node_id_order: nodeOrder(decisionValue.canonical_node_id_order, `${path}.decision.canonical_node_id_order`),
      differs_from_canonical_order: decisionValue.differs_from_canonical_order, candidates,
      winning_rationale: boundedText(decisionValue.winning_rationale, `${path}.decision.winning_rationale`),
    }),
    allocation, edges,
    exclusions: Object.freeze(array(item.exclusions, `${path}.exclusions`).map((reason, index) => boundedText(reason, `${path}.exclusions[${index}]`))),
    promotion: promotionValue, route_ready: false,
  });
}

function m13PromotionShell(): Record<string, unknown> {
  return {
    protocol: 'mycelium.m13_placement_projection.v1', snapshot_digest: `sha256:${'0'.repeat(64)}`,
    evidence_bundle_digest: `sha256:${'0'.repeat(64)}`, snapshot_generation: 0, authority_generation: 0,
    verification_key_digest: `sha256:${'0'.repeat(64)}`, valid_until_unix_ms: 0, placement_provenance: 'planner_v2',
    decode_mode: 'unknown', quantization: 'unknown', nodes: [], links: [], exclusions: [], ab_deltas: [], route_ready: false,
  };
}

export function decodeLiveRouteStatus(value: unknown): LiveRouteStatus {
  const compatible = typeof value === 'object' && value !== null && !Array.isArray(value)
    ? {
        ...(value as Record<string, unknown>),
        ...(!Object.prototype.hasOwnProperty.call(value, 'placement') ? { placement: null } : {}),
        ...(!Object.prototype.hasOwnProperty.call(value, 'topology') ? { topology: null } : {}),
      }
    : value;
  const item = record(
    compatible,
    [
      'protocol',
      'route_alive',
      'simulated',
      'route_identity_digest',
      'deployment_id',
      'model_id',
      'topology_version',
      'decode_mode',
      'counters',
      'stages',
      'peers',
      'recent_inferences',
      'incidents',
      'placement',
      'topology',
    ],
    'route_status',
  );
  if (item.protocol !== LIVE_ROUTE_STATUS_PROTOCOL) throw new TypeError('unsupported route status protocol');
  if (typeof item.route_alive !== 'boolean' || typeof item.simulated !== 'boolean') {
    throw new TypeError('route status booleans are invalid');
  }
  const digest = item.route_identity_digest;
  if (digest !== null && (typeof digest !== 'string' || !SHA256.test(digest))) {
    throw new TypeError('route identity digest is invalid');
  }
  return Object.freeze({
    protocol: LIVE_ROUTE_STATUS_PROTOCOL,
    route_alive: item.route_alive,
    simulated: item.simulated,
    route_identity_digest: digest,
    deployment_id: identifier(item.deployment_id, 'route_status.deployment_id'),
    model_id: identifier(item.model_id, 'route_status.model_id'),
    topology_version: integer(item.topology_version, 'route_status.topology_version'),
    decode_mode: identifier(item.decode_mode, 'route_status.decode_mode'),
    counters: counters(item.counters, 'route_status.counters'),
    stages: Object.freeze(array(item.stages, 'route_status.stages').map((candidate, index) => stage(candidate, `route_status.stages[${index}]`))),
    peers: Object.freeze(array(item.peers, 'route_status.peers').map((candidate, index) => peer(candidate, `route_status.peers[${index}]`))),
    recent_inferences: Object.freeze(array(item.recent_inferences, 'route_status.recent_inferences').map((candidate, index) => timing(candidate, `route_status.recent_inferences[${index}]`))),
    incidents: Object.freeze(array(item.incidents, 'route_status.incidents').map((candidate, index) => incident(candidate, `route_status.incidents[${index}]`))),
    placement: placement(item.placement, 'route_status.placement'),
    topology: topology(item.topology, 'route_status.topology'),
  });
}

export interface LiveRouteStatusClient {
  load(): Promise<LiveRouteStatus>;
}

export class HttpLiveRouteStatusClient implements LiveRouteStatusClient {
  readonly #fetcher: typeof fetch;

  constructor(fetcher: typeof fetch = globalThis.fetch.bind(globalThis)) {
    this.#fetcher = fetcher;
  }

  async load(): Promise<LiveRouteStatus> {
    const response = await this.#fetcher(LIVE_ROUTE_STATUS_PATH, {
      method: 'GET',
      credentials: 'same-origin',
      cache: 'no-store',
      redirect: 'error',
      headers: { Accept: 'application/json' },
    });
    if (!response.ok) throw new Error(`route_status_${response.status}`);
    return decodeLiveRouteStatus(await response.json());
  }
}
