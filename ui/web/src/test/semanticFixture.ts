export const semanticBinding = () => ({
  deployment: { id: 'deployment-alpha', epoch: 7 },
  model: {
    id: 'mycelium-model',
    revision: 'commit-abc123',
    manifest_digest: `sha256:${'a'.repeat(64)}`,
    num_layers: 4,
  },
  route: {
    id: 'route-primary',
    generation: 11,
    digest: `sha256:${'b'.repeat(64)}`,
    assignments: [
      {
        id: 'assignment-0',
        peer_id: 'peer-alpha',
        start_layer: 0,
        end_layer_exclusive: 2,
      },
      {
        id: 'assignment-1',
        peer_id: 'peer-beta',
        start_layer: 2,
        end_layer_exclusive: 4,
      },
    ],
  },
});

export const semanticFreshness = (
  observed_at = '2026-07-18T11:59:00Z',
  valid_until = '2026-07-18T12:05:00Z',
) => ({ observed_at, valid_until });

const provenance = (kind: string, producer: string) => ({ kind, producer });

export function validSemanticSnapshot() {
  const binding = semanticBinding();
  return {
    protocol: 'mycelium.observatory.snapshot.v1',
    snapshot_id: 'snapshot-0001',
    freshness: semanticFreshness(),
    binding,
    claims: [
      {
        id: 'claim-deployment',
        scope: { kind: 'deployment', id: 'deployment-alpha' },
        statement: 'deployment_bound',
        value: 'confirmed',
        freshness: semanticFreshness(),
        provenance: provenance('gateway_projection', 'mycelium_gateway'),
      },
      {
        id: 'claim-model',
        scope: { kind: 'model', id: 'mycelium-model' },
        statement: 'model_bound',
        value: 'confirmed',
        freshness: semanticFreshness(),
        provenance: provenance('provisioning_audit', 'mycelium_provisioning'),
      },
      ...binding.route.assignments.map((assignment, index) => ({
        id: `claim-assignment-${index}`,
        scope: { kind: 'assignment', id: assignment.id },
        statement: 'assignment_ready',
        value: 'confirmed',
        freshness: semanticFreshness(),
        provenance: provenance('provisioning_audit', 'mycelium_provisioning'),
      })),
      {
        id: 'claim-challenge',
        scope: { kind: 'route', id: 'route-primary' },
        statement: 'route_challenge_succeeded',
        value: 'confirmed',
        freshness: semanticFreshness(),
        provenance: provenance('route_challenge', 'mycelium_router'),
      },
      {
        id: 'claim-request',
        scope: { kind: 'request', id: 'request-observation-1' },
        statement: 'request_lifecycle_observed',
        value: 'confirmed',
        freshness: semanticFreshness(),
        provenance: provenance('router_runtime', 'mycelium_router'),
      },
    ],
    conflicts: [] as Array<{
      claim_ids: string[];
      scope: { kind: string; id: string };
      reason: string;
    }>,
    route_challenge: {
      id: 'challenge-0001',
      status: 'succeeded',
      freshness: semanticFreshness(),
      binding: structuredClone(binding),
      provenance: provenance('route_challenge', 'mycelium_router'),
    },
    request_lifecycle: {
      request_id: 'request-observation-1',
      state: 'completed',
      path_attempt: 1,
      freshness: semanticFreshness(),
      binding: structuredClone(binding),
      provenance: provenance('router_runtime', 'mycelium_router'),
    },
    provenance: provenance('gateway_projection', 'mycelium_gateway'),
  };
}

export function validSemanticEvent(generation = 1) {
  return {
    protocol: 'mycelium.observatory.event.v1',
    generation,
    snapshot: validSemanticSnapshot(),
  };
}
