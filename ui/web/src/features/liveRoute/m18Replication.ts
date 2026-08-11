export const M18_PLAN_PROTOCOL = 'mycelium.replica_plan.v1' as const;
export const M18_RUNTIME_PROTOCOL = 'mycelium.replica_runtime.v1' as const;

export interface M18ReplicaPlacement {
  readonly placement_id: string;
  readonly replica_group_id: string;
  readonly node_id: string;
  readonly layer_range: { readonly start: number; readonly end: number };
  readonly component_roles: readonly string[];
  readonly primary: boolean;
  readonly service_capacity_rps: number;
}

export interface M18ReplicaTrack {
  readonly track_id: string;
  readonly planner_track_id: string;
  readonly placement_ids: readonly string[];
  readonly edge_digests: readonly string[];
  readonly traffic_fraction: number;
  readonly cost_ms: number;
}

export interface M18CandidateDecision {
  readonly placement_id: string;
  readonly node_id: string;
  readonly replica_group_id: string;
  readonly accepted: boolean;
  readonly reason: string;
  readonly baseline_admitted_rps: number;
  readonly proposed_admitted_rps: number;
  readonly robust_gain_rps: number;
  readonly minimum_required_gain_rps: number;
  readonly failure_domain: string;
  readonly failure_domain_warning: string | null;
}

export interface M18ReplicaPlan {
  readonly protocol: typeof M18_PLAN_PROTOCOL;
  readonly generated_at_unix_ms: number;
  readonly deployment: {
    readonly deployment_id: string;
    readonly deployment_epoch: number;
    readonly model_id: string;
    readonly model_revision: string;
    readonly representation_digest: string;
    readonly manifest_digest: string;
    readonly qualification_id: string;
    readonly qualification_digest: string;
    readonly decode_mode: string;
    readonly quantization: string;
  };
  readonly evidence: {
    readonly generation: number;
    readonly evidence_digest: string;
    readonly evaluated_at_unix_ms: number;
    readonly valid_until_unix_ms: number;
  };
  readonly planner_snapshot_digest: string;
  readonly workload_name: string;
  readonly parallelism: 'data_parallel_request_routing';
  readonly groups: readonly {
    readonly replica_group_id: string;
    readonly layer_range: { readonly start: number; readonly end: number };
    readonly component_roles: readonly string[];
    readonly primary_placement_id: string;
    readonly placement_ids: readonly string[];
  }[];
  readonly placements: readonly M18ReplicaPlacement[];
  readonly edges: readonly {
    readonly src_placement_id: string;
    readonly dst_placement_id: string;
    readonly kind: 'forward' | 'decode_closure';
    readonly capacity_rps: number | null;
    readonly cost_ms: number;
  }[];
  readonly tracks: readonly M18ReplicaTrack[];
  readonly flow: {
    readonly primary_capacity_rps: number;
    readonly replicated_capacity_rps: number;
    readonly predicted_gain_rps: number;
    readonly unmet_demand_rps: number;
  };
  readonly candidate_decisions: readonly M18CandidateDecision[];
  readonly zero_flow_removed_placement_ids: readonly string[];
  readonly failure_domain_warnings: readonly string[];
  readonly claim_boundary: string;
  readonly route_ready: false;
  readonly plan_digest: string;
}

export interface M18ReplicaRuntime {
  readonly protocol: typeof M18_RUNTIME_PROTOCOL;
  readonly generated_at_monotonic_s: number;
  readonly deployment: M18ReplicaPlan['deployment'];
  readonly replica_plan_digest: string;
  readonly parallelism: 'data_parallel_request_routing';
  readonly qualified_tracks: readonly {
    readonly track_id: string;
    readonly placement_ids: readonly string[];
    readonly traffic_fraction: number;
    readonly qualification_id: string;
    readonly qualification_digest: string;
    readonly admission_state: 'qualified' | 'removed';
    readonly active_request_count: number;
  }[];
  readonly requests: readonly {
    readonly request_id: string;
    readonly path_id: string;
    readonly track_id: string;
    readonly placement_ids: readonly string[];
    readonly qualification_id: string;
    readonly qualification_digest: string;
    readonly phase: string;
    readonly admitted_at_monotonic_s: number;
    readonly terminal_at_monotonic_s: number | null;
    readonly terminal_state: string | null;
    readonly placement_work: Readonly<Record<string, { readonly frames_sent: number; readonly frames_received: number; readonly work_items: number }>>;
    readonly kv_locality: 'request_track_pinned_no_migration';
  }[];
  readonly incidents: readonly {
    readonly incident_id: string;
    readonly kind: string;
    readonly track_id: string;
    readonly reason: string;
    readonly observed_at_monotonic_s: number;
    readonly recovery_claimed: false;
  }[];
  readonly throughput: {
    readonly evidence_digest: string;
    readonly mode: string;
    readonly baseline_request_count: number;
    readonly baseline_throughput_rps: number;
    readonly replicated_request_count: number;
    readonly replicated_throughput_rps: number;
    readonly gain_fraction: number;
    readonly minimum_required_fraction: number;
    readonly passed: boolean;
  } | null;
  readonly claim_boundary: string;
}

function object(value: unknown, fields: readonly string[], path: string): Record<string, unknown> {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) throw new TypeError(`${path} must be an object`);
  const record = value as Record<string, unknown>;
  if (Object.keys(record).sort().join('|') !== [...fields].sort().join('|')) throw new TypeError(`${path} has unknown or missing fields`);
  return record;
}

function text(value: unknown, path: string): string {
  if (typeof value !== 'string' || value.length === 0) throw new TypeError(`${path} must be text`);
  return value;
}

function number(value: unknown, path: string): number {
  if (typeof value !== 'number' || !Number.isFinite(value) || value < 0) throw new TypeError(`${path} must be finite and non-negative`);
  return value;
}

function integer(value: unknown, path: string): number {
  const decoded = number(value, path);
  if (!Number.isSafeInteger(decoded)) throw new TypeError(`${path} must be an integer`);
  return decoded;
}

function array(value: unknown, path: string, maximum = 512): unknown[] {
  if (!Array.isArray(value) || value.length > maximum) throw new TypeError(`${path} must be a bounded array`);
  return value;
}

function textArray(value: unknown, path: string): readonly string[] {
  return Object.freeze(array(value, path).map((item, index) => text(item, `${path}[${index}]`)));
}

const DEPLOYMENT_FIELDS = ['deployment_id', 'deployment_epoch', 'model_id', 'model_revision', 'representation_digest', 'manifest_digest', 'qualification_id', 'qualification_digest', 'decode_mode', 'quantization'] as const;
const RANGE_FIELDS = ['start', 'end'] as const;

function deployment(value: unknown, path: string): M18ReplicaPlan['deployment'] {
  const item = object(value, DEPLOYMENT_FIELDS, path);
  return Object.freeze({
    deployment_id: text(item.deployment_id, `${path}.deployment_id`), deployment_epoch: integer(item.deployment_epoch, `${path}.deployment_epoch`),
    model_id: text(item.model_id, `${path}.model_id`), model_revision: text(item.model_revision, `${path}.model_revision`),
    representation_digest: text(item.representation_digest, `${path}.representation_digest`), manifest_digest: text(item.manifest_digest, `${path}.manifest_digest`),
    qualification_id: text(item.qualification_id, `${path}.qualification_id`), qualification_digest: text(item.qualification_digest, `${path}.qualification_digest`),
    decode_mode: text(item.decode_mode, `${path}.decode_mode`), quantization: text(item.quantization, `${path}.quantization`),
  });
}

function range(value: unknown, path: string): { readonly start: number; readonly end: number } {
  const item = object(value, RANGE_FIELDS, path);
  const start = integer(item.start, `${path}.start`);
  const end = integer(item.end, `${path}.end`);
  if (end <= start) throw new TypeError(`${path} must be a non-empty half-open range`);
  return Object.freeze({ start, end });
}

const PLAN_FIELDS = ['protocol', 'generated_at_unix_ms', 'deployment', 'evidence', 'planner_snapshot_digest', 'workload_name', 'parallelism', 'groups', 'placements', 'edges', 'tracks', 'flow', 'candidate_decisions', 'zero_flow_removed_placement_ids', 'failure_domain_warnings', 'claim_boundary', 'route_ready', 'plan_digest'] as const;

export function decodeM18ReplicaPlan(value: unknown): M18ReplicaPlan {
  const source = object(value, PLAN_FIELDS, 'm18_plan');
  if (source.protocol !== M18_PLAN_PROTOCOL || source.parallelism !== 'data_parallel_request_routing' || source.route_ready !== false) throw new TypeError('m18_plan authority boundary is invalid');
  const evidence = object(source.evidence, ['generation', 'evidence_digest', 'evaluated_at_unix_ms', 'valid_until_unix_ms'], 'm18_plan.evidence');
  const groups = array(source.groups, 'm18_plan.groups', 256).map((value, index) => {
    const item = object(value, ['replica_group_id', 'layer_range', 'component_roles', 'primary_placement_id', 'placement_ids'], `groups[${index}]`);
    return Object.freeze({ replica_group_id: text(item.replica_group_id, 'replica_group_id'), layer_range: range(item.layer_range, 'layer_range'), component_roles: textArray(item.component_roles, 'component_roles'), primary_placement_id: text(item.primary_placement_id, 'primary_placement_id'), placement_ids: textArray(item.placement_ids, 'placement_ids') });
  });
  const placements = array(source.placements, 'm18_plan.placements').map((value, index): M18ReplicaPlacement => {
    const item = object(value, ['placement_id', 'replica_group_id', 'node_id', 'layer_range', 'component_roles', 'primary', 'service_capacity_rps'], `placements[${index}]`);
    if (typeof item.primary !== 'boolean') throw new TypeError('placement.primary must be boolean');
    return Object.freeze({ placement_id: text(item.placement_id, 'placement_id'), replica_group_id: text(item.replica_group_id, 'replica_group_id'), node_id: text(item.node_id, 'node_id'), layer_range: range(item.layer_range, 'layer_range'), component_roles: textArray(item.component_roles, 'component_roles'), primary: item.primary, service_capacity_rps: number(item.service_capacity_rps, 'service_capacity_rps') });
  });
  const edges = array(source.edges, 'm18_plan.edges', 2048).map((value, index) => {
    const item = object(value, ['src_placement_id', 'dst_placement_id', 'kind', 'capacity_rps', 'cost_ms'], `edges[${index}]`);
    if (item.kind !== 'forward' && item.kind !== 'decode_closure') throw new TypeError('edge kind is invalid');
    return Object.freeze({ src_placement_id: text(item.src_placement_id, 'src_placement_id'), dst_placement_id: text(item.dst_placement_id, 'dst_placement_id'), kind: item.kind, capacity_rps: item.capacity_rps === null ? null : number(item.capacity_rps, 'capacity_rps'), cost_ms: number(item.cost_ms, 'cost_ms') });
  });
  const tracks = array(source.tracks, 'm18_plan.tracks').map((value, index): M18ReplicaTrack => {
    const item = object(value, ['track_id', 'planner_track_id', 'placement_ids', 'edge_digests', 'traffic_fraction', 'cost_ms'], `tracks[${index}]`);
    return Object.freeze({ track_id: text(item.track_id, 'track_id'), planner_track_id: text(item.planner_track_id, 'planner_track_id'), placement_ids: textArray(item.placement_ids, 'placement_ids'), edge_digests: textArray(item.edge_digests, 'edge_digests'), traffic_fraction: number(item.traffic_fraction, 'traffic_fraction'), cost_ms: number(item.cost_ms, 'cost_ms') });
  });
  const flow = object(source.flow, ['primary_capacity_rps', 'replicated_capacity_rps', 'predicted_gain_rps', 'unmet_demand_rps'], 'm18_plan.flow');
  const decisions = array(source.candidate_decisions, 'm18_plan.candidate_decisions', 2048).map((value, index): M18CandidateDecision => {
    const item = object(value, ['iteration', 'placement_id', 'node_id', 'replica_group_id', 'accepted', 'reason', 'baseline_admitted_rps', 'proposed_admitted_rps', 'raw_gain_rps', 'robust_gain_rps', 'minimum_required_gain_rps', 'failure_domain', 'failure_domain_warning'], `candidate_decisions[${index}]`);
    integer(item.iteration, 'iteration'); number(item.raw_gain_rps, 'raw_gain_rps');
    if (typeof item.accepted !== 'boolean') throw new TypeError('candidate accepted must be boolean');
    return Object.freeze({ placement_id: text(item.placement_id, 'placement_id'), node_id: text(item.node_id, 'node_id'), replica_group_id: text(item.replica_group_id, 'replica_group_id'), accepted: item.accepted, reason: text(item.reason, 'reason'), baseline_admitted_rps: number(item.baseline_admitted_rps, 'baseline_admitted_rps'), proposed_admitted_rps: number(item.proposed_admitted_rps, 'proposed_admitted_rps'), robust_gain_rps: number(item.robust_gain_rps, 'robust_gain_rps'), minimum_required_gain_rps: number(item.minimum_required_gain_rps, 'minimum_required_gain_rps'), failure_domain: text(item.failure_domain, 'failure_domain'), failure_domain_warning: item.failure_domain_warning === null ? null : text(item.failure_domain_warning, 'failure_domain_warning') });
  });
  return Object.freeze({
    protocol: M18_PLAN_PROTOCOL, generated_at_unix_ms: integer(source.generated_at_unix_ms, 'generated_at_unix_ms'), deployment: deployment(source.deployment, 'deployment'),
    evidence: Object.freeze({ generation: integer(evidence.generation, 'evidence.generation'), evidence_digest: text(evidence.evidence_digest, 'evidence.evidence_digest'), evaluated_at_unix_ms: integer(evidence.evaluated_at_unix_ms, 'evidence.evaluated_at_unix_ms'), valid_until_unix_ms: integer(evidence.valid_until_unix_ms, 'evidence.valid_until_unix_ms') }),
    planner_snapshot_digest: text(source.planner_snapshot_digest, 'planner_snapshot_digest'), workload_name: text(source.workload_name, 'workload_name'), parallelism: 'data_parallel_request_routing',
    groups: Object.freeze(groups), placements: Object.freeze(placements), edges: Object.freeze(edges), tracks: Object.freeze(tracks),
    flow: Object.freeze({ primary_capacity_rps: number(flow.primary_capacity_rps, 'primary_capacity_rps'), replicated_capacity_rps: number(flow.replicated_capacity_rps, 'replicated_capacity_rps'), predicted_gain_rps: number(flow.predicted_gain_rps, 'predicted_gain_rps'), unmet_demand_rps: number(flow.unmet_demand_rps, 'unmet_demand_rps') }),
    candidate_decisions: Object.freeze(decisions), zero_flow_removed_placement_ids: textArray(source.zero_flow_removed_placement_ids, 'zero_flow_removed_placement_ids'), failure_domain_warnings: textArray(source.failure_domain_warnings, 'failure_domain_warnings'),
    claim_boundary: text(source.claim_boundary, 'claim_boundary'), route_ready: false, plan_digest: text(source.plan_digest, 'plan_digest'),
  });
}

const RUNTIME_FIELDS = ['protocol', 'generated_at_monotonic_s', 'deployment', 'replica_plan_digest', 'parallelism', 'qualified_tracks', 'requests', 'incidents', 'throughput', 'claim_boundary'] as const;

export function decodeM18ReplicaRuntime(value: unknown): M18ReplicaRuntime {
  const source = object(value, RUNTIME_FIELDS, 'm18_runtime');
  if (source.protocol !== M18_RUNTIME_PROTOCOL || source.parallelism !== 'data_parallel_request_routing') throw new TypeError('m18_runtime authority boundary is invalid');
  const qualifiedTracks = array(source.qualified_tracks, 'qualified_tracks').map((value, index) => {
    const item = object(value, ['track_id', 'placement_ids', 'traffic_fraction', 'qualification_id', 'qualification_digest', 'admission_state', 'active_request_count'], `qualified_tracks[${index}]`);
    if (item.admission_state !== 'qualified' && item.admission_state !== 'removed') throw new TypeError('admission_state is invalid');
    return Object.freeze({ track_id: text(item.track_id, 'track_id'), placement_ids: textArray(item.placement_ids, 'placement_ids'), traffic_fraction: number(item.traffic_fraction, 'traffic_fraction'), qualification_id: text(item.qualification_id, 'qualification_id'), qualification_digest: text(item.qualification_digest, 'qualification_digest'), admission_state: item.admission_state, active_request_count: integer(item.active_request_count, 'active_request_count') });
  });
  const requests = array(source.requests, 'requests', 1024).map((value, index) => {
    const item = object(value, ['request_id', 'path_id', 'track_id', 'placement_ids', 'qualification_id', 'qualification_digest', 'phase', 'admitted_at_monotonic_s', 'terminal_at_monotonic_s', 'terminal_state', 'placement_work', 'kv_locality'], `requests[${index}]`);
    if (item.kv_locality !== 'request_track_pinned_no_migration') throw new TypeError('kv locality is invalid');
    const rawWork = item.placement_work;
    if (typeof rawWork !== 'object' || rawWork === null || Array.isArray(rawWork)) throw new TypeError('placement_work must be an object');
    const placementWork: Record<string, { readonly frames_sent: number; readonly frames_received: number; readonly work_items: number }> = {};
    for (const [placementId, raw] of Object.entries(rawWork)) {
      const work = object(raw, ['frames_sent', 'frames_received', 'work_items'], `placement_work.${placementId}`);
      placementWork[placementId] = Object.freeze({ frames_sent: integer(work.frames_sent, 'frames_sent'), frames_received: integer(work.frames_received, 'frames_received'), work_items: integer(work.work_items, 'work_items') });
    }
    return Object.freeze({ request_id: text(item.request_id, 'request_id'), path_id: text(item.path_id, 'path_id'), track_id: text(item.track_id, 'track_id'), placement_ids: textArray(item.placement_ids, 'placement_ids'), qualification_id: text(item.qualification_id, 'qualification_id'), qualification_digest: text(item.qualification_digest, 'qualification_digest'), phase: text(item.phase, 'phase'), admitted_at_monotonic_s: number(item.admitted_at_monotonic_s, 'admitted_at_monotonic_s'), terminal_at_monotonic_s: item.terminal_at_monotonic_s === null ? null : number(item.terminal_at_monotonic_s, 'terminal_at_monotonic_s'), terminal_state: item.terminal_state === null ? null : text(item.terminal_state, 'terminal_state'), placement_work: Object.freeze(placementWork), kv_locality: 'request_track_pinned_no_migration' as const });
  });
  const incidents = array(source.incidents, 'incidents', 256).map((value, index) => {
    const item = object(value, ['incident_id', 'kind', 'track_id', 'reason', 'observed_at_monotonic_s', 'recovery_claimed'], `incidents[${index}]`);
    if (item.recovery_claimed !== false) throw new TypeError('M18 cannot claim recovery');
    return Object.freeze({ incident_id: text(item.incident_id, 'incident_id'), kind: text(item.kind, 'kind'), track_id: text(item.track_id, 'track_id'), reason: text(item.reason, 'reason'), observed_at_monotonic_s: number(item.observed_at_monotonic_s, 'observed_at_monotonic_s'), recovery_claimed: false as const });
  });
  let throughput: M18ReplicaRuntime['throughput'] = null;
  if (source.throughput !== null) {
    const item = object(source.throughput, ['evidence_digest', 'mode', 'baseline_request_count', 'baseline_throughput_rps', 'replicated_request_count', 'replicated_throughput_rps', 'gain_fraction', 'minimum_required_fraction', 'passed'], 'throughput');
    if (typeof item.passed !== 'boolean') throw new TypeError('throughput.passed must be boolean');
    throughput = Object.freeze({ evidence_digest: text(item.evidence_digest, 'evidence_digest'), mode: text(item.mode, 'mode'), baseline_request_count: integer(item.baseline_request_count, 'baseline_request_count'), baseline_throughput_rps: number(item.baseline_throughput_rps, 'baseline_throughput_rps'), replicated_request_count: integer(item.replicated_request_count, 'replicated_request_count'), replicated_throughput_rps: number(item.replicated_throughput_rps, 'replicated_throughput_rps'), gain_fraction: number(item.gain_fraction, 'gain_fraction'), minimum_required_fraction: number(item.minimum_required_fraction, 'minimum_required_fraction'), passed: item.passed });
  }
  return Object.freeze({ protocol: M18_RUNTIME_PROTOCOL, generated_at_monotonic_s: number(source.generated_at_monotonic_s, 'generated_at_monotonic_s'), deployment: deployment(source.deployment, 'deployment'), replica_plan_digest: text(source.replica_plan_digest, 'replica_plan_digest'), parallelism: 'data_parallel_request_routing', qualified_tracks: Object.freeze(qualifiedTracks), requests: Object.freeze(requests), incidents: Object.freeze(incidents), throughput, claim_boundary: text(source.claim_boundary, 'claim_boundary') });
}
