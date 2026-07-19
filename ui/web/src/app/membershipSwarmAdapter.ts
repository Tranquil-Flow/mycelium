import type { ProductSwarmStatus } from './contracts';
import type { MembershipClient, MembershipInvite, MembershipJoinResult, MembershipRevokeResult, MembershipStatus } from '../features/membership/membershipClient';
import { HttpSwarmClient, type SwarmClient } from '../features/swarm/SwarmClient';

export class SwarmMembershipAdapter implements MembershipClient {
  constructor(private readonly swarm: SwarmClient = new HttpSwarmClient(), private readonly now: () => number = Date.now) {}

  async status(): Promise<MembershipStatus> {
    const status = await this.swarm.status();
    return {
      protocol: 'mycelium.product_membership.v1',
      generated_at: new Date(this.now()).toISOString(),
      members: status.native_nodes.map((node) => ({
        member_id: node.member_id,
        state: node.membership_state,
        connectivity: node.connectivity,
        endpoint_id: node.endpoint_id,
        evidence: 'supplied' as const,
      })),
      unknowns: [],
    };
  }

  async createInvite(expiresInSeconds: number): Promise<MembershipInvite> {
    const invite = await this.swarm.createInvite('native_inference_node', expiresInSeconds);
    return { invite_code: invite.invite_code, expires_at: new Date(invite.expires_at_unix_ms).toISOString(), single_use: true };
  }

  async join(inviteCode: string, endpointId?: string): Promise<MembershipJoinResult> {
    const result = await this.swarm.join(inviteCode, endpointId ?? 'native-node');
    return { accepted: result.joined, member_id: result.member_id, state: 'invited' };
  }

  async revoke(memberId: string): Promise<MembershipRevokeResult> {
    const result = await this.swarm.leave(memberId);
    return { revoked: result.left, member_id: result.member_id };
  }
}

export class FixtureSwarmClient implements SwarmClient {
  constructor(private readonly fixture: ProductSwarmStatus) {}
  async status(): Promise<ProductSwarmStatus> { return this.fixture; }
  async createInvite(): Promise<never> { throw new Error('fixture_source_read_only'); }
  async join(): Promise<never> { throw new Error('fixture_source_read_only'); }
  async leave(): Promise<never> { throw new Error('fixture_source_read_only'); }
}
