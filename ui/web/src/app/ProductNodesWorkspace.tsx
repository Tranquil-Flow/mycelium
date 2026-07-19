import { useMemo } from 'react';
import type { ProductSourceMode } from './contracts';
import { FixtureSwarmClient, SwarmMembershipAdapter } from './membershipSwarmAdapter';
import { AdminWorkspace } from '../features/admin/AdminWorkspace';
import { OnboardingWizard } from '../features/membership/OnboardingWizard';
import { NodesWorkspace } from '../features/nodes/NodesWorkspace';
import { HttpSwarmClient } from '../features/swarm/SwarmClient';
import { SwarmWorkspace } from '../features/swarm/SwarmWorkspace';
import type { EvidenceSnapshot, ProvisioningEvidence } from '../model/types';
import { makeProductSwarmFixture } from '../test/productFixtures';

const fixtureStatus = makeProductSwarmFixture();

export interface ProductNodesWorkspaceProps {
  readonly sourceMode: ProductSourceMode;
  readonly snapshot?: EvidenceSnapshot;
  readonly provisioning?: ProvisioningEvidence;
}

export function ProductNodesWorkspace({ sourceMode, snapshot, provisioning }: ProductNodesWorkspaceProps) {
  const fixture = sourceMode === 'fixture' || sourceMode === 'replay';
  const swarm = useMemo(() => fixture ? new FixtureSwarmClient(fixtureStatus) : new HttpSwarmClient(), [fixture]);
  const membership = useMemo(() => new SwarmMembershipAdapter(swarm), [swarm]);
  return (
    <div className="product-stack">
      {snapshot !== undefined && provisioning !== undefined ? <NodesWorkspace snapshot={snapshot} provisioning={provisioning} /> : null}
      <SwarmWorkspace client={swarm} initialStatus={fixture ? fixtureStatus : undefined} />
      <OnboardingWizard client={membership} />
      <AdminWorkspace client={membership} />
    </div>
  );
}
