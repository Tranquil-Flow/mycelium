export const M17_MODEL_OPERATION_PATH = '/__mycelium/models/operation';

const SHA256 = /^sha256:[0-9a-f]{64}$/;
const COMMIT = /^[0-9a-f]{40}$/;
const MAX_ENTRIES = 256;

export interface M17CatalogEntry {
  readonly model_id: string;
  readonly revision: string;
  readonly state: 'incomplete' | 'discovered' | 'compatible';
  readonly architecture: string;
  readonly adapter_id: string | null;
  readonly checkpoint_format: string;
  readonly quantization: string;
  readonly num_layers: number | null;
  readonly weight_bytes: number;
  readonly exact_tensor_accounting: boolean;
  readonly required_file_count: number;
  readonly present_file_count: number;
  readonly reasons: readonly string[];
  readonly artifact_digest: string;
  readonly serving_representations?: readonly M17ServingRepresentation[];
  readonly discovery_scope?: readonly ('coordinator' | 'member_inventory')[];
  readonly discovered_member_count?: number;
  readonly metadata_reconciled?: boolean;
  readonly discovery_blockers?: readonly string[];
}

export interface M17ServingRepresentation {
  readonly quantization: string;
  readonly runtime_dtype: string;
  readonly quantizer: string;
  readonly representation_digest: string;
  readonly resident_weight_bytes: number;
  readonly load_peak_weight_bytes: number;
  readonly preparation_required: boolean;
}

export interface M17CatalogDiscovery {
  readonly scope: 'coordinator_only' | 'coordinator_and_members' | 'unavailable';
  readonly accepted_member_count: number;
  readonly rejected_member_count: number;
  readonly blockers: readonly string[];
}

export interface M17FeasibilityStage {
  readonly node_id: string;
  readonly start_layer: number;
  readonly end_layer_exclusive: number;
  readonly required_memory_bytes: number;
  readonly available_memory_bytes: number;
  readonly headroom_bytes: number;
  readonly activation_bytes: number;
  readonly kv_bytes: number;
  readonly workspace_bytes: number;
  readonly runtime_reserve_bytes: number;
  readonly rss_bytes: number;
  readonly swap_used_bytes: number;
  readonly disk_free_bytes: number;
  readonly required_disk_bytes: number;
  readonly cached_artifact_bytes: number;
  readonly missing_artifact_bytes: number;
  readonly backend: string;
  readonly dtype: string;
  readonly quantization: string;
  readonly decode_mode: string;
  readonly maximum_context_tokens: number;
  readonly maximum_concurrency: number;
  readonly modeled_transfer_ms: number;
  readonly modeled_service_work_ms: number;
  readonly thermal_state: string | null;
  readonly power_state: string | null;
}

export interface M17DirectedEdge {
  readonly src: string;
  readonly dst: string;
  readonly observation_digest: string;
  readonly valid_until_unix_ms: number;
  readonly goodput_Bps: number;
  readonly rtt_ms: number;
  readonly jitter_ms: number;
  readonly loss_ratio: number;
}

export interface M17ResourceBottleneck {
  readonly kind: string;
  readonly node_id: string | null;
  readonly headroom_bytes: number | null;
  readonly reason: string | null;
}

export interface M17RepresentationAuthority {
  readonly kind: 'locally_derived_candidate' | 'approved_existing_immutable_representation';
  readonly owner_decision_digest: string | null;
  readonly prior_feasibility_digest: string | null;
  readonly source_artifact_digest: string | null;
  readonly quantizer: string | null;
}

export interface M17FeasibilityReport {
  readonly model_id: string;
  readonly revision: string;
  readonly state: 'feasible' | 'infeasible';
  readonly planner: 'capability_aware_contiguous_exact_weight_dp';
  readonly stages: readonly M17FeasibilityStage[];
  readonly reasons: readonly string[];
  readonly evidence_generation: number;
  readonly evidence_valid_until_unix_ms: number;
  readonly evaluated_at_unix_ms: number;
  readonly provisioning_authorized: boolean;
  readonly maximum_qualified_context_tokens: number;
  readonly maximum_qualified_concurrency: number;
  readonly cached_artifact_bytes: number;
  readonly missing_artifact_bytes: number;
  readonly modeled_transfer_ms: number;
  readonly modeled_execution_ms: number | null;
  readonly resource_bottleneck: M17ResourceBottleneck;
  readonly required_directed_edges: readonly M17DirectedEdge[];
  readonly feasibility_digest: string;
  readonly source_quantization?: string;
  readonly serving_quantization?: string;
  readonly serving_dtype?: string;
  readonly representation_digest?: string;
  readonly representation_authority: M17RepresentationAuthority;
}

export type M17LifecycleState =
  | 'incomplete'
  | 'discovered'
  | 'compatible'
  | 'feasible'
  | 'provisioning'
  | 'provisioned'
  | 'loaded'
  | 'qualified'
  | 'active'
  | 'unavailable'
  | 'retired';

export interface M17LifecycleModel {
  readonly model_id: string;
  readonly revision: string;
  readonly artifact_digest: string;
  readonly state: M17LifecycleState;
  readonly authority: string;
  readonly reason: string;
  readonly evidence_ref: string | null;
  readonly deployment_ids: readonly string[];
  readonly active_deployment_id: string | null;
  readonly selectable: boolean;
}

export interface M17Lifecycle {
  readonly protocol: 'mycelium.model_lifecycle.v1';
  readonly catalog_digest: string;
  readonly models: readonly M17LifecycleModel[];
  readonly lifecycle_digest: string;
  readonly route_ready: false;
}

export interface M17ModelOperation {
  readonly protocol: 'mycelium.model_operation.v1';
  readonly catalog_generation: number;
  readonly catalog_digest: string;
  readonly entries: readonly M17CatalogEntry[];
  readonly discovery?: M17CatalogDiscovery;
  readonly feasibility_reports: readonly M17FeasibilityReport[];
  readonly selection_authority: 'qualified_deployment_registry';
  readonly download_policy: 'operator_approval_required';
  readonly lifecycle: M17Lifecycle;
  readonly route_ready: false;
  readonly operation_digest: string;
}

function object(value: unknown, path: string): Record<string, unknown> {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) {
    throw new TypeError(`${path} must be an object`);
  }
  return value as Record<string, unknown>;
}

function integer(value: unknown, path: string): number {
  if (!Number.isSafeInteger(value) || (value as number) < 0) {
    throw new TypeError(`${path} must be a non-negative integer`);
  }
  return value as number;
}

function finite(value: unknown, path: string): number {
  if (typeof value !== 'number' || !Number.isFinite(value) || value < 0) {
    throw new TypeError(`${path} must be finite and non-negative`);
  }
  return value;
}

function nullableText(value: unknown, path: string): string | null {
  return value === null ? null : text(value, path);
}

function text(value: unknown, path: string): string {
  if (typeof value !== 'string' || value.length < 1 || value.length > 512) {
    throw new TypeError(`${path} must be bounded text`);
  }
  return value;
}

function strings(value: unknown, path: string): readonly string[] {
  if (!Array.isArray(value) || value.length > 32) throw new TypeError(`${path} must be bounded`);
  return Object.freeze(value.map((item, index) => text(item, `${path}[${index}]`)));
}

function entry(value: unknown, path: string): M17CatalogEntry {
  const item = object(value, path);
  const state = item.state;
  if (state !== 'incomplete' && state !== 'discovered' && state !== 'compatible') {
    throw new TypeError(`${path}.state is invalid`);
  }
  const revision = text(item.revision, `${path}.revision`);
  if (!COMMIT.test(revision)) throw new TypeError(`${path}.revision is invalid`);
  const digest = text(item.artifact_digest, `${path}.artifact_digest`);
  if (!SHA256.test(digest)) throw new TypeError(`${path}.artifact_digest is invalid`);
  const layerCount = item.num_layers === null ? null : integer(item.num_layers, `${path}.num_layers`);
  if (typeof item.exact_tensor_accounting !== 'boolean') throw new TypeError(`${path}.exact_tensor_accounting is invalid`);
  const discoveryScope = item.discovery_scope === undefined ? ['coordinator'] : strings(item.discovery_scope, `${path}.discovery_scope`);
  if (discoveryScope.some((scope) => scope !== 'coordinator' && scope !== 'member_inventory') || new Set(discoveryScope).size !== discoveryScope.length) {
    throw new TypeError(`${path}.discovery_scope is invalid`);
  }
  const metadataReconciled = item.metadata_reconciled === undefined ? true : item.metadata_reconciled;
  if (typeof metadataReconciled !== 'boolean') throw new TypeError(`${path}.metadata_reconciled is invalid`);
  const representations = item.serving_representations === undefined ? [] : item.serving_representations;
  if (!Array.isArray(representations) || representations.length > 16) throw new TypeError(`${path}.serving_representations is invalid`);
  const servingRepresentations = representations.map((value, index) => {
    const representation = object(value, `${path}.serving_representations[${index}]`);
    const representationDigest = text(representation.representation_digest, `${path}.serving_representations[${index}].representation_digest`);
    if (!SHA256.test(representationDigest) || typeof representation.preparation_required !== 'boolean') throw new TypeError(`${path}.serving_representations[${index}] is invalid`);
    return Object.freeze({
      quantization: text(representation.quantization, `${path}.serving_representations[${index}].quantization`),
      runtime_dtype: text(representation.runtime_dtype, `${path}.serving_representations[${index}].runtime_dtype`),
      quantizer: text(representation.quantizer, `${path}.serving_representations[${index}].quantizer`),
      representation_digest: representationDigest,
      resident_weight_bytes: integer(representation.resident_weight_bytes, `${path}.serving_representations[${index}].resident_weight_bytes`),
      load_peak_weight_bytes: integer(representation.load_peak_weight_bytes, `${path}.serving_representations[${index}].load_peak_weight_bytes`),
      preparation_required: representation.preparation_required,
    });
  });
  return Object.freeze({
    model_id: text(item.model_id, `${path}.model_id`),
    revision,
    state,
    architecture: text(item.architecture, `${path}.architecture`),
    adapter_id: item.adapter_id === null ? null : text(item.adapter_id, `${path}.adapter_id`),
    checkpoint_format: text(item.checkpoint_format, `${path}.checkpoint_format`),
    quantization: text(item.quantization, `${path}.quantization`),
    num_layers: layerCount,
    weight_bytes: integer(item.weight_bytes, `${path}.weight_bytes`),
    exact_tensor_accounting: item.exact_tensor_accounting,
    required_file_count: integer(item.required_file_count, `${path}.required_file_count`),
    present_file_count: integer(item.present_file_count, `${path}.present_file_count`),
    reasons: strings(item.reasons, `${path}.reasons`),
    artifact_digest: digest,
    serving_representations: Object.freeze(servingRepresentations),
    discovery_scope: Object.freeze(discoveryScope as ('coordinator' | 'member_inventory')[]),
    discovered_member_count: item.discovered_member_count === undefined ? 0 : integer(item.discovered_member_count, `${path}.discovered_member_count`),
    metadata_reconciled: metadataReconciled,
    discovery_blockers: item.discovery_blockers === undefined ? Object.freeze([]) : strings(item.discovery_blockers, `${path}.discovery_blockers`),
  });
}

function feasibility(value: unknown, path: string): M17FeasibilityReport {
  const item = object(value, path);
  if (item.state !== 'feasible' && item.state !== 'infeasible') throw new TypeError(`${path}.state is invalid`);
  if (item.planner !== 'capability_aware_contiguous_exact_weight_dp') throw new TypeError(`${path}.planner is invalid`);
  if (!Array.isArray(item.stages) || item.stages.length > MAX_ENTRIES) throw new TypeError(`${path}.stages is invalid`);
  const stages = item.stages.map((value, index) => {
    const stage = object(value, `${path}.stages[${index}]`);
    return Object.freeze({
      node_id: text(stage.node_id, `${path}.stages[${index}].node_id`),
      start_layer: integer(stage.start_layer, `${path}.stages[${index}].start_layer`),
      end_layer_exclusive: integer(stage.end_layer_exclusive, `${path}.stages[${index}].end_layer_exclusive`),
      required_memory_bytes: integer(stage.required_memory_bytes, `${path}.stages[${index}].required_memory_bytes`),
      available_memory_bytes: integer(stage.available_memory_bytes, `${path}.stages[${index}].available_memory_bytes`),
      headroom_bytes: integer(stage.headroom_bytes, `${path}.stages[${index}].headroom_bytes`),
      activation_bytes: integer(stage.activation_bytes, `${path}.stages[${index}].activation_bytes`),
      kv_bytes: integer(stage.kv_bytes, `${path}.stages[${index}].kv_bytes`),
      workspace_bytes: integer(stage.workspace_bytes, `${path}.stages[${index}].workspace_bytes`),
      runtime_reserve_bytes: integer(stage.runtime_reserve_bytes, `${path}.stages[${index}].runtime_reserve_bytes`),
      rss_bytes: integer(stage.rss_bytes, `${path}.stages[${index}].rss_bytes`),
      swap_used_bytes: integer(stage.swap_used_bytes, `${path}.stages[${index}].swap_used_bytes`),
      disk_free_bytes: integer(stage.disk_free_bytes, `${path}.stages[${index}].disk_free_bytes`),
      required_disk_bytes: integer(stage.required_disk_bytes, `${path}.stages[${index}].required_disk_bytes`),
      cached_artifact_bytes: integer(stage.cached_artifact_bytes, `${path}.stages[${index}].cached_artifact_bytes`),
      missing_artifact_bytes: integer(stage.missing_artifact_bytes, `${path}.stages[${index}].missing_artifact_bytes`),
      backend: text(stage.backend, `${path}.stages[${index}].backend`),
      dtype: text(stage.dtype, `${path}.stages[${index}].dtype`),
      quantization: text(stage.quantization, `${path}.stages[${index}].quantization`),
      decode_mode: text(stage.decode_mode, `${path}.stages[${index}].decode_mode`),
      maximum_context_tokens: integer(stage.maximum_context_tokens, `${path}.stages[${index}].maximum_context_tokens`),
      maximum_concurrency: integer(stage.maximum_concurrency, `${path}.stages[${index}].maximum_concurrency`),
      modeled_transfer_ms: finite(stage.modeled_transfer_ms, `${path}.stages[${index}].modeled_transfer_ms`),
      modeled_service_work_ms: finite(stage.modeled_service_work_ms, `${path}.stages[${index}].modeled_service_work_ms`),
      thermal_state: nullableText(stage.thermal_state, `${path}.stages[${index}].thermal_state`),
      power_state: nullableText(stage.power_state, `${path}.stages[${index}].power_state`),
    });
  });
  const revision = text(item.revision, `${path}.revision`);
  const digest = text(item.feasibility_digest, `${path}.feasibility_digest`);
  const representationDigest = item.representation_digest === undefined ? undefined : text(item.representation_digest, `${path}.representation_digest`);
  if (representationDigest !== undefined && !SHA256.test(representationDigest)) throw new TypeError(`${path}.representation_digest is invalid`);
  if (!COMMIT.test(revision) || !SHA256.test(digest)) throw new TypeError(`${path} identity is invalid`);
  if (typeof item.provisioning_authorized !== 'boolean') throw new TypeError(`${path}.provisioning_authorized is invalid`);
  if (item.provisioning_authorized !== (item.state === 'feasible')) throw new TypeError(`${path}.provisioning authority is invalid`);
  const bottleneckValue = object(item.resource_bottleneck, `${path}.resource_bottleneck`);
  const resourceBottleneck = Object.freeze({
    kind: text(bottleneckValue.kind, `${path}.resource_bottleneck.kind`),
    node_id: nullableText(bottleneckValue.node_id ?? null, `${path}.resource_bottleneck.node_id`),
    headroom_bytes: bottleneckValue.headroom_bytes === undefined || bottleneckValue.headroom_bytes === null
      ? null : integer(bottleneckValue.headroom_bytes, `${path}.resource_bottleneck.headroom_bytes`),
    reason: nullableText(bottleneckValue.reason ?? null, `${path}.resource_bottleneck.reason`),
  });
  const authorityValue = object(item.representation_authority, `${path}.representation_authority`);
  if (authorityValue.kind !== 'locally_derived_candidate' && authorityValue.kind !== 'approved_existing_immutable_representation') {
    throw new TypeError(`${path}.representation_authority.kind is invalid`);
  }
  const authorityDigest = (field: 'owner_decision_digest' | 'prior_feasibility_digest' | 'source_artifact_digest'): string | null => {
    const value = authorityValue[field];
    if (value === undefined || value === null) return null;
    const digest = text(value, `${path}.representation_authority.${field}`);
    if (!SHA256.test(digest)) throw new TypeError(`${path}.representation_authority.${field} is invalid`);
    return digest;
  };
  const representationAuthority = Object.freeze({
    kind: authorityValue.kind,
    owner_decision_digest: authorityDigest('owner_decision_digest'),
    prior_feasibility_digest: authorityDigest('prior_feasibility_digest'),
    source_artifact_digest: authorityDigest('source_artifact_digest'),
    quantizer: authorityValue.quantizer === undefined || authorityValue.quantizer === null
      ? null : text(authorityValue.quantizer, `${path}.representation_authority.quantizer`),
  });
  if (representationAuthority.kind === 'approved_existing_immutable_representation'
    && (representationAuthority.owner_decision_digest === null || representationAuthority.prior_feasibility_digest === null)) {
    throw new TypeError(`${path}.representation_authority binding is incomplete`);
  }
  if (!Array.isArray(item.required_directed_edges) || item.required_directed_edges.length > MAX_ENTRIES) {
    throw new TypeError(`${path}.required_directed_edges is invalid`);
  }
  const requiredEdges = item.required_directed_edges.map((value, index) => {
    const edge = object(value, `${path}.required_directed_edges[${index}]`);
    const observationDigest = text(edge.observation_digest, `${path}.required_directed_edges[${index}].observation_digest`);
    if (!SHA256.test(observationDigest)) throw new TypeError(`${path}.required_directed_edges[${index}] digest is invalid`);
    return Object.freeze({
      src: text(edge.src, `${path}.required_directed_edges[${index}].src`),
      dst: text(edge.dst, `${path}.required_directed_edges[${index}].dst`),
      observation_digest: observationDigest,
      valid_until_unix_ms: integer(edge.valid_until_unix_ms, `${path}.required_directed_edges[${index}].valid_until_unix_ms`),
      goodput_Bps: finite(edge.goodput_Bps, `${path}.required_directed_edges[${index}].goodput_Bps`),
      rtt_ms: finite(edge.rtt_ms, `${path}.required_directed_edges[${index}].rtt_ms`),
      jitter_ms: finite(edge.jitter_ms, `${path}.required_directed_edges[${index}].jitter_ms`),
      loss_ratio: finite(edge.loss_ratio, `${path}.required_directed_edges[${index}].loss_ratio`),
    });
  });
  return Object.freeze({
    model_id: text(item.model_id, `${path}.model_id`),
    revision,
    state: item.state,
    planner: item.planner,
    stages: Object.freeze(stages),
    reasons: strings(item.reasons, `${path}.reasons`),
    evidence_generation: integer(item.evidence_generation, `${path}.evidence_generation`),
    evidence_valid_until_unix_ms: integer(item.evidence_valid_until_unix_ms, `${path}.evidence_valid_until_unix_ms`),
    evaluated_at_unix_ms: integer(item.evaluated_at_unix_ms, `${path}.evaluated_at_unix_ms`),
    provisioning_authorized: item.provisioning_authorized,
    maximum_qualified_context_tokens: integer(item.maximum_qualified_context_tokens, `${path}.maximum_qualified_context_tokens`),
    maximum_qualified_concurrency: integer(item.maximum_qualified_concurrency, `${path}.maximum_qualified_concurrency`),
    cached_artifact_bytes: integer(item.cached_artifact_bytes, `${path}.cached_artifact_bytes`),
    missing_artifact_bytes: integer(item.missing_artifact_bytes, `${path}.missing_artifact_bytes`),
    modeled_transfer_ms: finite(item.modeled_transfer_ms, `${path}.modeled_transfer_ms`),
    modeled_execution_ms: item.modeled_execution_ms === null ? null : finite(item.modeled_execution_ms, `${path}.modeled_execution_ms`),
    resource_bottleneck: resourceBottleneck,
    required_directed_edges: Object.freeze(requiredEdges),
    feasibility_digest: digest,
    source_quantization: item.source_quantization === undefined ? undefined : text(item.source_quantization, `${path}.source_quantization`),
    serving_quantization: item.serving_quantization === undefined ? undefined : text(item.serving_quantization, `${path}.serving_quantization`),
    serving_dtype: item.serving_dtype === undefined ? undefined : text(item.serving_dtype, `${path}.serving_dtype`),
    representation_digest: representationDigest,
    representation_authority: representationAuthority,
  });
}

const LIFECYCLE_STATES = new Set<M17LifecycleState>([
  'incomplete', 'discovered', 'compatible', 'feasible', 'provisioning',
  'provisioned', 'loaded', 'qualified', 'active', 'unavailable', 'retired',
]);

function lifecycle(value: unknown): M17Lifecycle {
  const item = object(value, 'model_operation.lifecycle');
  if (
    item.protocol !== 'mycelium.model_lifecycle.v1'
    || item.route_ready !== false
    || !Array.isArray(item.models)
    || item.models.length > MAX_ENTRIES
  ) throw new TypeError('model operation lifecycle is invalid');
  const catalogDigest = text(item.catalog_digest, 'model_operation.lifecycle.catalog_digest');
  const lifecycleDigest = text(item.lifecycle_digest, 'model_operation.lifecycle.lifecycle_digest');
  if (!SHA256.test(catalogDigest) || !SHA256.test(lifecycleDigest)) {
    throw new TypeError('model operation lifecycle digest is invalid');
  }
  const models = item.models.map((value, index): M17LifecycleModel => {
    const model = object(value, `model_operation.lifecycle.models[${index}]`);
    const state = model.state;
    if (typeof state !== 'string' || !LIFECYCLE_STATES.has(state as M17LifecycleState)) {
      throw new TypeError(`model_operation.lifecycle.models[${index}].state is invalid`);
    }
    const revision = text(model.revision, `model_operation.lifecycle.models[${index}].revision`);
    const artifactDigest = text(model.artifact_digest, `model_operation.lifecycle.models[${index}].artifact_digest`);
    if (!COMMIT.test(revision) || !SHA256.test(artifactDigest)) {
      throw new TypeError(`model_operation.lifecycle.models[${index}] identity is invalid`);
    }
    if (!Array.isArray(model.deployment_ids) || model.deployment_ids.length > MAX_ENTRIES) {
      throw new TypeError(`model_operation.lifecycle.models[${index}].deployment_ids is invalid`);
    }
    if (typeof model.selectable !== 'boolean') {
      throw new TypeError(`model_operation.lifecycle.models[${index}].selectable is invalid`);
    }
    if (model.selectable !== (state === 'qualified' || state === 'active')) {
      throw new TypeError(`model_operation.lifecycle.models[${index}] selection authority is invalid`);
    }
    return Object.freeze({
      model_id: text(model.model_id, `model_operation.lifecycle.models[${index}].model_id`),
      revision,
      artifact_digest: artifactDigest,
      state: state as M17LifecycleState,
      authority: text(model.authority, `model_operation.lifecycle.models[${index}].authority`),
      reason: text(model.reason, `model_operation.lifecycle.models[${index}].reason`),
      evidence_ref: model.evidence_ref === null ? null : text(model.evidence_ref, `model_operation.lifecycle.models[${index}].evidence_ref`),
      deployment_ids: Object.freeze(model.deployment_ids.map((value, deploymentIndex) => text(value, `model_operation.lifecycle.models[${index}].deployment_ids[${deploymentIndex}]`))),
      active_deployment_id: model.active_deployment_id === null ? null : text(model.active_deployment_id, `model_operation.lifecycle.models[${index}].active_deployment_id`),
      selectable: model.selectable,
    });
  });
  return Object.freeze({
    protocol: 'mycelium.model_lifecycle.v1',
    catalog_digest: catalogDigest,
    models: Object.freeze(models),
    lifecycle_digest: lifecycleDigest,
    route_ready: false,
  });
}

export function decodeM17ModelOperation(value: unknown): M17ModelOperation {
  const item = object(value, 'model_operation');
  if (
    item.protocol !== 'mycelium.model_operation.v1' ||
    item.selection_authority !== 'qualified_deployment_registry' ||
    item.download_policy !== 'operator_approval_required' ||
    item.route_ready !== false
  ) throw new TypeError('model operation authority is invalid');
  const catalog = object(item.catalog, 'model_operation.catalog');
  if (catalog.protocol !== 'mycelium.model_catalog.v1' || !Array.isArray(catalog.entries) || catalog.entries.length > MAX_ENTRIES) {
    throw new TypeError('model operation catalog is invalid');
  }
  if (!Array.isArray(item.feasibility_reports) || item.feasibility_reports.length > MAX_ENTRIES) {
    throw new TypeError('model operation feasibility reports are invalid');
  }
  const catalogDigest = text(item.catalog_digest, 'model_operation.catalog_digest');
  const operationDigest = text(item.operation_digest, 'model_operation.operation_digest');
  if (!SHA256.test(catalogDigest) || !SHA256.test(operationDigest) || catalog.catalog_digest !== catalogDigest) {
    throw new TypeError('model operation digest binding is invalid');
  }
  const decodedLifecycle = lifecycle(item.lifecycle);
  if (decodedLifecycle.catalog_digest !== catalogDigest) {
    throw new TypeError('model operation lifecycle catalog binding is invalid');
  }
  let discovery: M17CatalogDiscovery;
  if (catalog.discovery === undefined) {
    discovery = Object.freeze({
      scope: 'unavailable',
      accepted_member_count: 0,
      rejected_member_count: 0,
      blockers: Object.freeze(['member_inventory_scope_unavailable']),
    });
  } else {
    const rawDiscovery = object(catalog.discovery, 'model_operation.catalog.discovery');
    if (rawDiscovery.scope !== 'coordinator_only' && rawDiscovery.scope !== 'coordinator_and_members') {
      throw new TypeError('model operation catalog discovery scope is invalid');
    }
    discovery = Object.freeze({
      scope: rawDiscovery.scope,
      accepted_member_count: integer(rawDiscovery.accepted_member_count, 'model_operation.catalog.discovery.accepted_member_count'),
      rejected_member_count: integer(rawDiscovery.rejected_member_count, 'model_operation.catalog.discovery.rejected_member_count'),
      blockers: strings(rawDiscovery.blockers, 'model_operation.catalog.discovery.blockers'),
    });
  }
  return Object.freeze({
    protocol: 'mycelium.model_operation.v1',
    catalog_generation: integer(item.catalog_generation, 'model_operation.catalog_generation'),
    catalog_digest: catalogDigest,
    entries: Object.freeze(catalog.entries.map((value, index) => entry(value, `model_operation.catalog.entries[${index}]`))),
    discovery,
    feasibility_reports: Object.freeze(item.feasibility_reports.map((value, index) => feasibility(value, `model_operation.feasibility_reports[${index}]`))),
    selection_authority: 'qualified_deployment_registry',
    download_policy: 'operator_approval_required',
    lifecycle: decodedLifecycle,
    route_ready: false,
    operation_digest: operationDigest,
  });
}

export interface M17ModelOperationClient {
  load(signal?: AbortSignal): Promise<M17ModelOperation>;
}

export class HttpM17ModelOperationClient implements M17ModelOperationClient {
  async load(signal?: AbortSignal): Promise<M17ModelOperation> {
    const response = await fetch(M17_MODEL_OPERATION_PATH, {
      method: 'GET', signal, cache: 'no-store', credentials: 'same-origin', redirect: 'error',
      headers: { accept: 'application/json' },
    });
    if (!response.ok) throw new Error(`model_operation_${response.status}`);
    return decodeM17ModelOperation(await response.json());
  }
}
