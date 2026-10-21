// SPDX-License-Identifier: AGPL-3.0-only
import type { ProductQualification } from '../../app/contracts';
import type { ProductEntity, ProductSnapshot } from '../productEvidence/contracts';
import type { ConsumerRouteStatusFacts } from './consumerObservatory';

/**
 * Consumer route facts, projected only from documents the product origin really
 * serves:
 *   * `/api/v1/qualification/current` (route identity, digests, readiness)
 *   * `/api/v1/product/snapshot` (route/stage/device entities and their
 *     `assigned_to` / `placed_on` relations)
 *   * `/api/v1/observatory/snapshot` (route status, sessions, incidents)
 *
 * Every field that the contracts do not carry stays `null` and is rendered as
 * unknown or unavailable. Nothing here infers a layer split, a node name, a
 * GeoIP region or a stage-to-host mapping from a count, a hash prefix or a
 * transport endpoint.
 */
export type ConsumerFactAvailability = 'available' | 'unknown';

export interface ConsumerDeviceFacts {
  readonly entity_id: string;
  readonly label: string | null;
  readonly peer_class: string | null;
  readonly transport: string | null;
  readonly runtime_backend: string | null;
  readonly lifecycle: string | null;
  readonly lease_freshness: string | null;
  readonly placement_id: string | null;
  readonly incarnation: string | null;
}

export interface ConsumerStageFacts {
  readonly stage_entity_id: string;
  readonly stage_index: number | null;
  readonly start_layer: number | null;
  readonly end_layer_exclusive: number | null;
  readonly component_roles: readonly string[] | null;
  readonly decode_mode: string | null;
  /** Device entity this stage is placed on, when a `placed_on` relation exists. */
  readonly device: ConsumerDeviceFacts | null;
}

export interface ConsumerRouteFacts {
  readonly model_id: string | null;
  readonly resolved_commit: string | null;
  readonly route_id: string | null;
  readonly route_generation: number | null;
  readonly topology_version: number | null;
  readonly deployment_id: string | null;
  readonly deployment_epoch: number | null;
  readonly qualification_id: string | null;
  readonly qualification_digest: string | null;
  readonly manifest_digest: string | null;
  readonly path_manifest_digest: string | null;
  readonly stage_load_proof_count: number | null;
  readonly evidence_class: string | null;
  readonly route_ready: boolean | null;
  readonly reason_codes: readonly string[];
  readonly decode_mode: string | null;
  readonly placement_provenance: string | null;
  readonly route_alive: boolean | null;
  readonly snapshot_generation: number | null;
  readonly snapshot_source_mode: string | null;
  readonly route_status_generation: number | null;
  readonly stages: readonly ConsumerStageFacts[];
  readonly devices: readonly ConsumerDeviceFacts[];
  readonly layer_split: ConsumerFactAvailability;
  readonly stage_placement: ConsumerFactAvailability;
}

export interface ConsumerRouteFactsInput {
  readonly qualification: ProductQualification | null;
  readonly snapshot: ProductSnapshot | null;
  readonly route_status: ConsumerRouteStatusFacts | null;
}

function attributeText(entity: ProductEntity, key: string): string | null {
  const value = entity.attributes[key];
  return typeof value === 'string' && value.length > 0 ? value : null;
}

function attributeInteger(entity: ProductEntity, key: string): number | null {
  const value = entity.attributes[key];
  return Number.isSafeInteger(value) ? (value as number) : null;
}

function attributeBoolean(entity: ProductEntity, key: string): boolean | null {
  const value = entity.attributes[key];
  return typeof value === 'boolean' ? value : null;
}

function attributeRoles(entity: ProductEntity): readonly string[] | null {
  const value = entity.attributes['component_roles'];
  if (!Array.isArray(value)) return null;
  const roles = value.filter((role): role is string => typeof role === 'string');
  return Object.freeze(roles);
}

function byEntityId(left: ProductEntity, right: ProductEntity): number {
  return left.entity_id < right.entity_id ? -1 : left.entity_id > right.entity_id ? 1 : 0;
}

function projectDevice(entity: ProductEntity): ConsumerDeviceFacts {
  return Object.freeze({
    entity_id: entity.entity_id,
    label: entity.label.length > 0 ? entity.label : null,
    peer_class: attributeText(entity, 'peer_class'),
    transport: attributeText(entity, 'transport'),
    runtime_backend: attributeText(entity, 'runtime_backend'),
    lifecycle: attributeText(entity, 'lifecycle'),
    lease_freshness: attributeText(entity, 'lease_freshness'),
    placement_id: attributeText(entity, 'placement_id'),
    incarnation: attributeText(entity, 'incarnation'),
  });
}

function projectStages(snapshot: ProductSnapshot | null): {
  readonly stages: readonly ConsumerStageFacts[];
  readonly devices: readonly ConsumerDeviceFacts[];
} {
  if (snapshot === null) return { stages: Object.freeze([]), devices: Object.freeze([]) };
  const entities = Array.isArray(snapshot.entities) ? snapshot.entities : [];
  const relations = Array.isArray(snapshot.relations) ? snapshot.relations : [];
  const devices = new Map<string, ProductEntity>();
  for (const entity of entities) {
    if (entity.kind === 'device') devices.set(entity.entity_id, entity);
  }
  const placement = new Map<string, string>();
  for (const relation of relations) {
    if (relation.kind !== 'placed_on') continue;
    if (!devices.has(relation.to_entity_id)) continue;
    if (!placement.has(relation.from_entity_id)) {
      placement.set(relation.from_entity_id, relation.to_entity_id);
    }
  }
  const stages = entities
    .filter((entity) => entity.kind === 'stage')
    .sort((left, right) => {
      const leftIndex = attributeInteger(left, 'stage_index');
      const rightIndex = attributeInteger(right, 'stage_index');
      if (leftIndex !== null && rightIndex !== null && leftIndex !== rightIndex) {
        return leftIndex - rightIndex;
      }
      return byEntityId(left, right);
    })
    .map((entity) => {
      const deviceId = placement.get(entity.entity_id) ?? null;
      const deviceEntity = deviceId === null ? undefined : devices.get(deviceId);
      return Object.freeze({
        stage_entity_id: entity.entity_id,
        stage_index: attributeInteger(entity, 'stage_index'),
        start_layer: attributeInteger(entity, 'start_layer'),
        end_layer_exclusive: attributeInteger(entity, 'end_layer_exclusive'),
        component_roles: attributeRoles(entity),
        decode_mode: attributeText(entity, 'decode_mode'),
        device: deviceEntity === undefined ? null : projectDevice(deviceEntity),
      });
    });
  return {
    stages: Object.freeze(stages),
    devices: Object.freeze([...devices.values()].sort(byEntityId).map(projectDevice)),
  };
}

export function projectConsumerRouteFacts(
  input: ConsumerRouteFactsInput,
): ConsumerRouteFacts {
  const { qualification, snapshot, route_status } = input;
  const route = snapshot === null || !Array.isArray(snapshot.entities)
    ? undefined
    : snapshot.entities.filter((entity) => entity.kind === 'route').sort(byEntityId)[0];
  const projection = projectStages(snapshot);
  const placementAvailable = projection.stages.some((stage) => stage.device !== null);
  const publication = snapshot === null
    ? undefined
    : (snapshot as Partial<ProductSnapshot>).publication;
  return Object.freeze({
    model_id: qualification?.binding.model_id ?? (route === undefined ? null : attributeText(route, 'model_id')),
    resolved_commit: qualification?.binding.resolved_commit ?? null,
    route_id: route?.binding.route_id ?? null,
    route_generation: route?.binding.route_generation ?? null,
    topology_version:
      qualification?.binding.topology_version ??
      route?.binding.topology_version ??
      route_status?.qualification?.topology_version ??
      null,
    deployment_id:
      qualification?.binding.deployment_id ?? route?.binding.deployment_id ?? null,
    deployment_epoch:
      qualification?.binding.deployment_epoch ?? route?.binding.deployment_epoch ?? null,
    qualification_id: qualification?.binding.qualification_id ?? null,
    qualification_digest: qualification?.binding.qualification_digest ?? null,
    manifest_digest: qualification?.binding.manifest_digest ?? null,
    path_manifest_digest: qualification?.binding.path_manifest_digest ?? null,
    stage_load_proof_count: qualification?.binding.stage_load_proof_digests.length ?? null,
    evidence_class: qualification?.evidence_class ?? route_status?.qualification?.evidence_class ?? null,
    route_ready: qualification?.route_ready ?? route_status?.route_ready ?? null,
    reason_codes: qualification === null ? Object.freeze([]) : Object.freeze([...qualification.reason_codes]),
    decode_mode: route === undefined ? null : attributeText(route, 'decode_mode'),
    placement_provenance: route === undefined ? null : attributeText(route, 'placement_provenance'),
    route_alive: route === undefined ? null : attributeBoolean(route, 'route_alive'),
    snapshot_generation:
      typeof publication?.generation === 'number' ? publication.generation : null,
    snapshot_source_mode:
      typeof publication?.source_mode === 'string' ? publication.source_mode : null,
    route_status_generation: route_status?.generation ?? null,
    stages: projection.stages,
    devices: projection.devices,
    layer_split: projection.stages.length > 0 ? 'available' : 'unknown',
    stage_placement: placementAvailable ? 'available' : 'unknown',
  });
}
