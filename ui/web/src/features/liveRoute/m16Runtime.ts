export const M16_RUNTIME_PATH = '/__mycelium/runtime/admission-status';
export const M16_RUNTIME_PROTOCOL = 'mycelium.m16_runtime_status.v1' as const;

export interface M16QueueStatus {
  readonly depth: number;
  readonly maximum_items: number;
  readonly queued_bytes: number;
  readonly maximum_bytes: number;
  readonly interactive_depth: number;
  readonly batch_depth: number;
  readonly active_request_id: string | null;
}

export interface M16PlacementStatus {
  readonly placement_id: string;
  readonly node_id: string;
  readonly memory_capacity_bytes: number;
  readonly reserved_memory_bytes: number;
  readonly free_memory_bytes: number;
  readonly kv_capacity_bytes: number;
  readonly reserved_kv_bytes: number;
  readonly free_kv_bytes: number;
  readonly workspace_capacity_bytes: number;
  readonly reserved_workspace_bytes: number;
  readonly free_workspace_bytes: number;
  readonly active_reservations: number;
  readonly maximum_reservations: number;
}

export interface M16RequestStatus {
  readonly request_id: string;
  readonly workload_profile_id: string;
  readonly qos_class: 'interactive' | 'batch';
  readonly phase: string;
  readonly path_id: string;
  readonly path_attempt: number;
  readonly path_manifest_digest: string;
  readonly topology_version: number;
  readonly path_state: 'locked';
  readonly candidate_placement_ids: readonly string[];
  readonly placement_ids: readonly string[];
  readonly reservation_count: number;
  readonly admitted_at_monotonic_s: number;
  readonly queued_at_monotonic_s: number;
  readonly dispatch_at_monotonic_s: number | null;
  readonly terminal_at_monotonic_s: number | null;
  readonly queue_wait_ms: number | null;
  readonly terminal_state: string | null;
}

export interface M16Incident {
  readonly incident_id: string;
  readonly kind: string;
  readonly request_id: string;
  readonly scope: string;
  readonly state: string;
  readonly observed_at_monotonic_s: number;
  readonly retry_after_seconds: number | null;
}

export interface M16RuntimeStatus {
  readonly protocol: typeof M16_RUNTIME_PROTOCOL;
  readonly generated_at_monotonic_s: number;
  readonly deployment_id: string;
  readonly deployment_epoch: number;
  readonly topology_version: number;
  readonly graph_digest: string;
  readonly queue: M16QueueStatus;
  readonly placements: readonly M16PlacementStatus[];
  readonly requests: readonly M16RequestStatus[];
  readonly incidents: readonly M16Incident[];
  readonly batch_state: {
    readonly mode: 'sequential_dispatch';
    readonly maximum_runtime_batch_size: number;
    readonly observed_batches: readonly unknown[];
    readonly continuous_batching: false;
    readonly pipeline_overlap: false;
  };
  readonly claim_boundary: string;
  readonly performance_budgets: readonly M16PerformanceBudget[];
}

export interface M16PerformanceBudget {
  readonly budget_id: string;
  readonly profile_id: string;
  readonly observed_request_count: number;
  readonly overall_state: 'met' | 'failed' | 'met_with_approved_exclusions';
  readonly dimensions: readonly { readonly dimension: string; readonly state: 'met' | 'failed' | 'approved_exclusion'; readonly observed: number; readonly bound: number; readonly unit: string; readonly reason: string }[];
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
  if (typeof value !== 'number' || !Number.isFinite(value) || value < 0) throw new TypeError(`${path} must be finite`);
  return value;
}

function integer(value: unknown, path: string): number {
  const decoded = number(value, path);
  if (!Number.isSafeInteger(decoded)) throw new TypeError(`${path} must be an integer`);
  return decoded;
}

function nullableNumber(value: unknown, path: string): number | null {
  return value === null ? null : number(value, path);
}

function array(value: unknown, path: string, maximum = 1_024): unknown[] {
  if (!Array.isArray(value) || value.length > maximum) throw new TypeError(`${path} must be a bounded array`);
  return value;
}

const QUEUE_FIELDS = ['depth', 'maximum_items', 'queued_bytes', 'maximum_bytes', 'interactive_depth', 'batch_depth', 'active_request_id'] as const;
const PLACEMENT_FIELDS = ['placement_id', 'node_id', 'memory_capacity_bytes', 'reserved_memory_bytes', 'free_memory_bytes', 'kv_capacity_bytes', 'reserved_kv_bytes', 'free_kv_bytes', 'workspace_capacity_bytes', 'reserved_workspace_bytes', 'free_workspace_bytes', 'active_reservations', 'maximum_reservations'] as const;
const REQUEST_FIELDS = ['request_id', 'workload_profile_id', 'qos_class', 'phase', 'path_id', 'path_attempt', 'path_manifest_digest', 'topology_version', 'path_state', 'candidate_placement_ids', 'placement_ids', 'reservation_count', 'admitted_at_monotonic_s', 'queued_at_monotonic_s', 'dispatch_at_monotonic_s', 'terminal_at_monotonic_s', 'queue_wait_ms', 'terminal_state'] as const;
const INCIDENT_FIELDS = ['incident_id', 'kind', 'request_id', 'scope', 'state', 'observed_at_monotonic_s', 'retry_after_seconds'] as const;
const TOP_FIELDS = ['protocol', 'generated_at_monotonic_s', 'deployment_id', 'deployment_epoch', 'topology_version', 'graph_digest', 'queue', 'placements', 'requests', 'incidents', 'batch_state', 'claim_boundary', 'performance_budgets'] as const;

export function decodeM16RuntimeStatus(value: unknown): M16RuntimeStatus {
  const source = object(value, TOP_FIELDS, 'm16_runtime');
  if (source.protocol !== M16_RUNTIME_PROTOCOL) throw new TypeError('m16_runtime protocol is invalid');
  const queue = object(source.queue, QUEUE_FIELDS, 'm16_runtime.queue');
  const decodedQueue: M16QueueStatus = {
    depth: integer(queue.depth, 'queue.depth'), maximum_items: integer(queue.maximum_items, 'queue.maximum_items'),
    queued_bytes: integer(queue.queued_bytes, 'queue.queued_bytes'), maximum_bytes: integer(queue.maximum_bytes, 'queue.maximum_bytes'),
    interactive_depth: integer(queue.interactive_depth, 'queue.interactive_depth'), batch_depth: integer(queue.batch_depth, 'queue.batch_depth'),
    active_request_id: queue.active_request_id === null ? null : text(queue.active_request_id, 'queue.active_request_id'),
  };
  const placements = array(source.placements, 'm16_runtime.placements').map((value, index): M16PlacementStatus => {
    const item = object(value, PLACEMENT_FIELDS, `placements[${index}]`);
    return {
      placement_id: text(item.placement_id, 'placement_id'), node_id: text(item.node_id, 'node_id'),
      memory_capacity_bytes: integer(item.memory_capacity_bytes, 'memory_capacity_bytes'), reserved_memory_bytes: integer(item.reserved_memory_bytes, 'reserved_memory_bytes'), free_memory_bytes: integer(item.free_memory_bytes, 'free_memory_bytes'),
      kv_capacity_bytes: integer(item.kv_capacity_bytes, 'kv_capacity_bytes'), reserved_kv_bytes: integer(item.reserved_kv_bytes, 'reserved_kv_bytes'), free_kv_bytes: integer(item.free_kv_bytes, 'free_kv_bytes'),
      workspace_capacity_bytes: integer(item.workspace_capacity_bytes, 'workspace_capacity_bytes'), reserved_workspace_bytes: integer(item.reserved_workspace_bytes, 'reserved_workspace_bytes'), free_workspace_bytes: integer(item.free_workspace_bytes, 'free_workspace_bytes'),
      active_reservations: integer(item.active_reservations, 'active_reservations'), maximum_reservations: integer(item.maximum_reservations, 'maximum_reservations'),
    };
  });
  const requests = array(source.requests, 'm16_runtime.requests').map((value, index): M16RequestStatus => {
    const item = object(value, REQUEST_FIELDS, `requests[${index}]`);
    if (item.qos_class !== 'interactive' && item.qos_class !== 'batch') throw new TypeError('request qos is invalid');
    if (item.path_state !== 'locked') throw new TypeError('request path state is invalid');
    return {
      request_id: text(item.request_id, 'request_id'), workload_profile_id: text(item.workload_profile_id, 'workload_profile_id'), qos_class: item.qos_class,
      phase: text(item.phase, 'phase'), path_id: text(item.path_id, 'path_id'), path_attempt: integer(item.path_attempt, 'path_attempt'), path_manifest_digest: text(item.path_manifest_digest, 'path_manifest_digest'), topology_version: integer(item.topology_version, 'topology_version'),
      path_state: 'locked', candidate_placement_ids: Object.freeze(array(item.candidate_placement_ids, 'candidate_placement_ids').map((entry) => text(entry, 'candidate_placement_id'))),
      placement_ids: Object.freeze(array(item.placement_ids, 'placement_ids').map((entry) => text(entry, 'placement_id'))), reservation_count: integer(item.reservation_count, 'reservation_count'),
      admitted_at_monotonic_s: number(item.admitted_at_monotonic_s, 'admitted_at'), queued_at_monotonic_s: number(item.queued_at_monotonic_s, 'queued_at'), dispatch_at_monotonic_s: nullableNumber(item.dispatch_at_monotonic_s, 'dispatch_at'), terminal_at_monotonic_s: nullableNumber(item.terminal_at_monotonic_s, 'terminal_at'), queue_wait_ms: nullableNumber(item.queue_wait_ms, 'queue_wait_ms'), terminal_state: item.terminal_state === null ? null : text(item.terminal_state, 'terminal_state'),
    };
  });
  const incidents = array(source.incidents, 'm16_runtime.incidents', 256).map((value, index): M16Incident => {
    const item = object(value, INCIDENT_FIELDS, `incidents[${index}]`);
    return { incident_id: text(item.incident_id, 'incident_id'), kind: text(item.kind, 'kind'), request_id: text(item.request_id, 'request_id'), scope: text(item.scope, 'scope'), state: text(item.state, 'state'), observed_at_monotonic_s: number(item.observed_at_monotonic_s, 'observed_at'), retry_after_seconds: nullableNumber(item.retry_after_seconds, 'retry_after') };
  });
  const batch = object(source.batch_state, ['mode', 'maximum_runtime_batch_size', 'observed_batches', 'continuous_batching', 'pipeline_overlap'], 'batch_state');
  if (batch.mode !== 'sequential_dispatch' || batch.continuous_batching !== false || batch.pipeline_overlap !== false || array(batch.observed_batches, 'observed_batches').length !== 0) throw new TypeError('batch claim boundary is invalid');
  const budgets = array(source.performance_budgets, 'performance_budgets', 16).map((value, index): M16PerformanceBudget => {
    const item = object(value, ['protocol', 'budget_id', 'profile_id', 'evidence_scope', 'observed_request_count', 'dimensions', 'overall_state'], `budgets[${index}]`);
    if (item.protocol !== 'mycelium.performance_budget.v3' || item.evidence_scope !== 'concurrent_physical_observed' || !['met', 'failed', 'met_with_approved_exclusions'].includes(String(item.overall_state))) throw new TypeError('M16 budget authority is invalid');
    const dimensions = array(item.dimensions, 'budget.dimensions', 32).map((value) => {
      const dimension = object(value, ['dimension', 'state', 'bound', 'observed', 'unit', 'evidence_digest', 'reason'], 'budget.dimension');
      if (!['met', 'failed', 'approved_exclusion'].includes(String(dimension.state))) throw new TypeError('M16 budget state is invalid');
      text(dimension.evidence_digest, 'evidence_digest');
      return { dimension: text(dimension.dimension, 'dimension'), state: dimension.state as 'met' | 'failed' | 'approved_exclusion', bound: number(dimension.bound, 'bound'), observed: number(dimension.observed, 'observed'), unit: text(dimension.unit, 'unit'), reason: text(dimension.reason, 'reason') };
    });
    return { budget_id: text(item.budget_id, 'budget_id'), profile_id: text(item.profile_id, 'profile_id'), observed_request_count: integer(item.observed_request_count, 'observed_request_count'), overall_state: item.overall_state as M16PerformanceBudget['overall_state'], dimensions: Object.freeze(dimensions) };
  });
  return Object.freeze({
    protocol: M16_RUNTIME_PROTOCOL,
    generated_at_monotonic_s: number(source.generated_at_monotonic_s, 'generated_at'), deployment_id: text(source.deployment_id, 'deployment_id'), deployment_epoch: integer(source.deployment_epoch, 'deployment_epoch'), topology_version: integer(source.topology_version, 'topology_version'), graph_digest: text(source.graph_digest, 'graph_digest'),
    queue: Object.freeze(decodedQueue), placements: Object.freeze(placements), requests: Object.freeze(requests), incidents: Object.freeze(incidents),
    batch_state: Object.freeze({ mode: 'sequential_dispatch', maximum_runtime_batch_size: integer(batch.maximum_runtime_batch_size, 'maximum_runtime_batch_size'), observed_batches: Object.freeze([]), continuous_batching: false, pipeline_overlap: false }),
    claim_boundary: text(source.claim_boundary, 'claim_boundary'),
    performance_budgets: Object.freeze(budgets),
  });
}

export interface M16RuntimeClient { load(signal?: AbortSignal): Promise<M16RuntimeStatus> }

export class HttpM16RuntimeClient implements M16RuntimeClient {
  constructor(private readonly fetcher: typeof fetch = globalThis.fetch.bind(globalThis)) {}
  async load(signal?: AbortSignal): Promise<M16RuntimeStatus> {
    const response = await this.fetcher(M16_RUNTIME_PATH, { method: 'GET', credentials: 'same-origin', cache: 'no-store', redirect: 'error', headers: { Accept: 'application/json' }, signal });
    if (!response.ok) throw new Error(`m16_runtime_${response.status}`);
    return decodeM16RuntimeStatus(await response.json());
  }
}
