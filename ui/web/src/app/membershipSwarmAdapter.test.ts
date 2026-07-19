import { describe, expect, it, vi } from 'vitest';
import { FixtureSwarmClient, SwarmMembershipAdapter } from './membershipSwarmAdapter';
import { makeProductSwarmFixture } from '../test/productFixtures';
import type { SwarmClient } from '../features/swarm/SwarmClient';

const status = makeProductSwarmFixture();

describe('SwarmMembershipAdapter', () => {
  it('projects only supplied native membership evidence', async () => {
    const adapter = new SwarmMembershipAdapter(new FixtureSwarmClient(status), () => Date.UTC(2026, 6, 19));
    const projected = await adapter.status();
    expect(projected.members).toEqual([{ member_id: 'fixture-native-node-1', state: 'reachable', connectivity: 'local', endpoint_id: 'fixture-endpoint-1', evidence: 'supplied' }]);
    expect(projected.unknowns).toEqual([]);
  });

  it('maps invite, join, and confirmed leave without inventing readiness', async () => {
    const swarm: SwarmClient = {
      status: vi.fn(async () => status),
      createInvite: vi.fn(async () => ({ protocol: status.protocol, invite_id: 'invite-1', invite_code: 'A'.repeat(24), capability: 'native_inference_node' as const, expires_at_unix_ms: 1_800_000_000_000 })),
      join: vi.fn(async () => ({ protocol: status.protocol, joined: true as const, member_id: 'member-2', capability: 'native_inference_node' as const })),
      leave: vi.fn(async (memberId: string) => ({ protocol: status.protocol, member_id: memberId, left: true })),
    };
    const adapter = new SwarmMembershipAdapter(swarm);
    expect((await adapter.createInvite(300)).single_use).toBe(true);
    expect((await adapter.join('A'.repeat(24))).state).toBe('invited');
    expect(await adapter.revoke('member-2')).toEqual({ revoked: true, member_id: 'member-2' });
  });
});
