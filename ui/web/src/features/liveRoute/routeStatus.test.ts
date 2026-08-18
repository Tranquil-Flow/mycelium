import { describe, expect, it, vi } from 'vitest';
import {
  HttpLiveRouteStatusClient,
  decodeLiveRouteStatus,
} from './routeStatus';
import { liveRouteStatusFixture, m13PlacementFixture, m14TopologyFixture } from './routeStatusTestFixture';
import canonicalRuntimeStatus from '../../../../../contracts/compatibility-fixtures/live-route-status-v1.json';

describe('live route status contract', () => {
  it('decodes the bounded prompt-free physical projection', () => {
    const decoded = decodeLiveRouteStatus(structuredClone(liveRouteStatusFixture()));
    expect(decoded.model_id).toBe('Qwen/Qwen2.5-0.5B-Instruct');
    expect(decoded.stages.map((stage) => [stage.start_layer, stage.end_layer_exclusive])).toEqual([
      [0, 12],
      [12, 24],
    ]);
    expect(Object.isFrozen(decoded.recent_inferences)).toBe(true);
  });

  it('decodes the frozen runtime and KV compatibility fixture', () => {
    const decoded = decodeLiveRouteStatus(structuredClone(canonicalRuntimeStatus));
    expect(decoded.simulated).toBe(false);
    expect(decoded.decode_mode).toBe('stage_local_kv');
    expect(decoded.peers.every((peer) => peer.active_kv_state_count === 0)).toBe(true);
    expect(decoded.peers[0]).toMatchObject({
      architecture: 'qwen2',
      supported_decode_modes: ['complete_context_replay', 'stage_local_kv'],
      peak_kv_bytes: 4096,
      release_state: 'released',
      last_release_reason: 'normal_completion',
    });
  });

  it('decodes one bounded planner-v2 projection without making it route authority', () => {
    const fixture = liveRouteStatusFixture();
    const decoded = decodeLiveRouteStatus({ ...fixture, placement: m13PlacementFixture() });
    expect(decoded.placement?.placement_provenance).toBe('planner_v2');
    expect(decoded.placement?.nodes.map((node) => [node.start_layer, node.end_layer_exclusive])).toEqual([[0, 12], [12, 24]]);
    expect(decoded.placement?.route_ready).toBe(false);
  });

  it('decodes a configured deployment becoming unavailable', () => {
    const fixture = liveRouteStatusFixture();
    const decoded = decodeLiveRouteStatus({
      ...fixture,
      route_alive: false,
      counters: { ...fixture.counters, fatal: 'membership_member_lease_expired' },
      incidents: [{
        protocol: 'mycelium.live_route_incident.v1',
        incident_id: 'registry-incident-1',
        deployment_id: fixture.deployment_id,
        request_id: null,
        state: 'configured_deployment_unavailable',
        reason: 'membership_member_lease_expired',
        observed_at_unix_ms: 2_000,
      }],
    });

    expect(decoded.route_alive).toBe(false);
    expect(decoded.incidents[0].state).toBe('configured_deployment_unavailable');
  });

  it('decodes qualified service restoration after an unavailable incumbent', () => {
    const fixture = liveRouteStatusFixture();
    const decoded = decodeLiveRouteStatus({
      ...fixture,
      incidents: [{
        protocol: 'mycelium.live_route_incident.v1',
        incident_id: 'registry-incident-2',
        deployment_id: fixture.deployment_id,
        request_id: null,
        state: 'qualified_service_restored',
        reason: 'replaced_unavailable:incumbent-deployment',
        observed_at_unix_ms: 2_001,
      }],
    });

    expect(decoded.incidents[0].state).toBe('qualified_service_restored');
  });

  it('decodes a complete activation-plane directed matrix without making it route authority', () => {
    const decoded = decodeLiveRouteStatus({ ...liveRouteStatusFixture(), topology: m14TopologyFixture() });
    expect(decoded.topology?.edges).toHaveLength(6);
    expect(decoded.topology?.decision.opened_order).toEqual(['node-0', 'node-1', 'node-2']);
    expect(decoded.topology?.decision.loopback).toEqual({ src: 'node-2', dst: 'node-0' });
    expect(decoded.topology?.route_ready).toBe(false);
  });

  it('rejects an incomplete M14 directed matrix', () => {
    const source = structuredClone(m14TopologyFixture());
    const topology = { ...source, edges: source.edges.slice(0, -1) };
    expect(() => decodeLiveRouteStatus({ ...liveRouteStatusFixture(), topology })).toThrow(/complete directed matrix/i);
  });

  it('rejects prompt, token, and unknown evidence fields', () => {
    for (const field of ['prompt', 'token_ids', 'activation']) {
      const candidate = structuredClone(liveRouteStatusFixture()) as unknown as Record<string, unknown>;
      candidate[field] = 'private-value';
      expect(() => decodeLiveRouteStatus(candidate)).toThrow(/unknown|missing/i);
    }
  });

  it('uses only the fixed same-origin status path', async () => {
    const fetcher = vi.fn(async () => new Response(JSON.stringify(liveRouteStatusFixture()), {
      headers: { 'content-type': 'application/json' },
    }));
    const client = new HttpLiveRouteStatusClient(fetcher as typeof fetch);

    await expect(client.load()).resolves.toMatchObject({ route_alive: true, simulated: false });
    expect(fetcher).toHaveBeenCalledWith(
      '/__mycelium/live-status',
      expect.objectContaining({ credentials: 'same-origin', cache: 'no-store' }),
    );
  });
});
