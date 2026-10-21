// SPDX-License-Identifier: AGPL-3.0-only
import { describe, expect, it } from 'vitest';
import { PRODUCT_INFERENCE_PROTOCOL, type ProductQualification } from '../../app/contracts';
import type {
  ProductEntity,
  ProductRelation,
  ProductSnapshot,
} from '../productEvidence/contracts';
import { projectConsumerRouteFacts } from './consumerRouteFacts';

const hash = (character: string) => `sha256:${character.repeat(64)}`;

const qualification: ProductQualification = {
  protocol: PRODUCT_INFERENCE_PROTOCOL,
  issued_at_unix_ms: 1_000,
  evidence_class: 'physical_qualification',
  route_ready: true,
  reason_codes: [],
  binding: {
    qualification_id: 'sha256:qualification',
    qualification_digest: hash('1'),
    deployment_id: 'e1d1a2fa-f68d-5b38-a507-34b367c8d855',
    deployment_epoch: 1,
    topology_version: 4,
    model_id: 'Qwen/Qwen2.5-0.5B-Instruct',
    resolved_commit: '7ae557604adf67be50417f59c2c2f167def9a775',
    manifest_digest: hash('2'),
    path_manifest_digest: hash('3'),
    stage_load_proof_digests: [hash('4'), hash('5')],
  },
};

function entity(
  entity_id: string,
  kind: ProductEntity['kind'],
  attributes: Record<string, unknown>,
  binding: Partial<ProductEntity['binding']> = {},
): ProductEntity {
  return {
    entity_id,
    kind,
    label: `label-${entity_id}`,
    source_id: 'route-source',
    binding: {
      deployment_id: 'e1d1a2fa-f68d-5b38-a507-34b367c8d855',
      deployment_epoch: 1,
      route_id: 'route-abc123',
      route_generation: 4,
      topology_version: 4,
      ...binding,
    },
    freshness: { status: 'current', observed_at_unix_ms: 1_000, valid_until_unix_ms: 1_000 },
    attributes,
  };
}

function snapshot(
  entities: readonly ProductEntity[],
  relations: readonly ProductRelation[] = [],
): ProductSnapshot {
  return {
    protocol: 'mycelium.product_snapshot.v1',
    publication: {
      snapshot_id: 'snapshot-1',
      generation: 12,
      cursor: 34,
      published_at_unix_ms: 1_000,
      source_mode: 'live',
    },
    supported_entity_kinds: [],
    source_states: [],
    entities,
    relations,
    readiness: [],
    notices: [],
    internet_native: {},
    provenance: { projector: 'product-spine', projector_version: '1', source_mode: 'live' },
  } as unknown as ProductSnapshot;
}

const routeEntity = entity('route-abc123', 'route', {
  deployment_id: 'e1d1a2fa-f68d-5b38-a507-34b367c8d855',
  model_id: 'Qwen/Qwen2.5-0.5B-Instruct',
  topology_version: 4,
  decode_mode: 'complete_context_replay',
  placement_provenance: 'operator_selected',
  route_alive: true,
});

describe('consumer route facts', () => {
  it('projects published stage, layer and node placement without inventing a split', () => {
    const first = entity('stage-0', 'stage', {
      stage_index: 0,
      start_layer: 0,
      end_layer_exclusive: 12,
      component_roles: ['embedding', 'attention'],
      decode_mode: 'complete_context_replay',
    });
    const second = entity('stage-1', 'stage', {
      stage_index: 1,
      start_layer: 12,
      end_layer_exclusive: 24,
      component_roles: ['lm_head'],
      decode_mode: 'complete_context_replay',
    });
    const device = entity('device-1ce58d131f9fcaae560d', 'device', {
      peer_class: 'mac_mlx_iroh',
      transport: 'iroh',
      runtime_backend: 'mlx',
      lifecycle: 'ready',
      lease_freshness: 'fresh',
      placement_id: null,
      incarnation: 'a13-lite-ef1d632d',
    });
    const facts = projectConsumerRouteFacts({
      qualification,
      snapshot: snapshot(
        [routeEntity, second, first, device],
        [
          { relation_id: 'r-0', kind: 'assigned_to', from_entity_id: 'stage-0', to_entity_id: 'route-abc123', source_id: 'route-source' },
          { relation_id: 'r-1', kind: 'placed_on', from_entity_id: 'stage-0', to_entity_id: 'device-1ce58d131f9fcaae560d', source_id: 'route-source' },
        ],
      ),
      route_status: null,
    });

    expect(facts.layer_split).toBe('available');
    expect(facts.stage_placement).toBe('available');
    expect(facts.stages.map((stage) => stage.stage_entity_id)).toEqual(['stage-0', 'stage-1']);
    expect(facts.stages[0]).toMatchObject({
      stage_index: 0,
      start_layer: 0,
      end_layer_exclusive: 12,
      component_roles: ['embedding', 'attention'],
      decode_mode: 'complete_context_replay',
    });
    expect(facts.stages[0].device).toMatchObject({
      entity_id: 'device-1ce58d131f9fcaae560d',
      peer_class: 'mac_mlx_iroh',
      transport: 'iroh',
      runtime_backend: 'mlx',
      lifecycle: 'ready',
      placement_id: null,
    });
    expect(facts.stages[1].device).toBeNull();
    expect(facts.devices).toHaveLength(1);
    expect(facts.model_id).toBe('Qwen/Qwen2.5-0.5B-Instruct');
    expect(facts.route_id).toBe('route-abc123');
    expect(facts.route_generation).toBe(4);
    expect(facts.decode_mode).toBe('complete_context_replay');
    expect(facts.route_alive).toBe(true);
    expect(facts.snapshot_generation).toBe(12);
    expect(facts.stage_load_proof_count).toBe(2);
  });

  it('reports an absent layer split and placement as unknown instead of guessing', () => {
    const empty = projectConsumerRouteFacts({ qualification, snapshot: null, route_status: null });
    expect(empty.layer_split).toBe('unknown');
    expect(empty.stage_placement).toBe('unknown');
    expect(empty.stages).toEqual([]);
    expect(empty.devices).toEqual([]);
    expect(empty.route_id).toBeNull();
    expect(empty.route_generation).toBeNull();
    expect(empty.decode_mode).toBeNull();
    expect(empty.route_alive).toBeNull();
    expect(empty.snapshot_generation).toBeNull();
    expect(empty.model_id).toBe('Qwen/Qwen2.5-0.5B-Instruct');
    expect(empty.deployment_id).toBe('e1d1a2fa-f68d-5b38-a507-34b367c8d855');
    expect(empty.topology_version).toBe(4);

    const devicesOnly = projectConsumerRouteFacts({
      qualification,
      snapshot: snapshot([entity('device-x', 'device', {})]),
      route_status: null,
    });
    expect(devicesOnly.layer_split).toBe('unknown');
    expect(devicesOnly.stage_placement).toBe('unknown');
    expect(devicesOnly.devices[0].peer_class).toBeNull();
    expect(devicesOnly.devices[0].label).toBe('label-device-x');
  });

  it('keeps a stage whose placement relation is missing unresolved', () => {
    const facts = projectConsumerRouteFacts({
      qualification,
      snapshot: snapshot([
        entity('stage-0', 'stage', {
          stage_index: 0,
          start_layer: 0,
          end_layer_exclusive: 12,
          component_roles: [],
        }),
        entity('device-x', 'device', { peer_class: 'mac_mlx_iroh' }),
      ]),
      route_status: null,
    });
    expect(facts.layer_split).toBe('available');
    expect(facts.stage_placement).toBe('unknown');
    expect(facts.stages[0].device).toBeNull();
    expect(facts.stages[0].component_roles).toEqual([]);
  });

  it('takes readiness from the route-status document only when qualification is silent', () => {
    const facts = projectConsumerRouteFacts({
      qualification: null,
      snapshot: null,
      route_status: {
        generation: 7,
        source_cursor: 9,
        observed_at_unix_ms: 1_000,
        route_ready: false,
        buffered_sessions: 0,
        dropped_quarantine_count: 0,
        quarantine_capacity: 16,
        sessions: {
          total: 0,
          by_state: { accepted: 0, streaming: 0, completed: 0, cancelled: 0, failed: 0, quarantined: 0 },
          failed_request_ids: [],
          cancelled_request_ids: [],
          quarantined: [],
        },
        incidents: [],
        qualification: null,
      },
    });
    expect(facts.route_ready).toBe(false);
    expect(facts.route_status_generation).toBe(7);
    expect(facts.evidence_class).toBeNull();
    expect(facts.qualification_id).toBeNull();
    expect(facts.route_ready).toBe(false);
  });
});
