import { useMemo } from 'react';
import type { ProductSourceMode } from './contracts';
import { FixtureSwarmClient, SwarmMembershipAdapter } from './membershipSwarmAdapter';
import { AdminWorkspace } from '../features/admin/AdminWorkspace';
import { OnboardingWizard } from '../features/membership/OnboardingWizard';
import { useProductSettings } from '../features/settings/SettingsContext';
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
  const { settings } = useProductSettings();
  const fixture = sourceMode === 'fixture' || sourceMode === 'replay';
  const swarm = useMemo(() => fixture ? new FixtureSwarmClient(fixtureStatus) : new HttpSwarmClient(), [fixture]);
  const membership = useMemo(() => new SwarmMembershipAdapter(swarm), [swarm]);
  return (
    <div className="product-stack">
      <header className="view-heading product-nodes-heading">
        <div><p className="eyebrow cyan">Device and membership control plane</p><h1>Nodes</h1></div>
      </header>
      {snapshot !== undefined && provisioning !== undefined ? <NodesWorkspace snapshot={snapshot} provisioning={provisioning} /> : null}
      <SwarmWorkspace
        key={`swarm-${sourceMode}`}
        client={swarm}
        initialStatus={fixture ? fixtureStatus : undefined}
        concealNetworkIdentity={settings.concealNetworkIdentity}
        readOnly={fixture}
      />
      <OnboardingWizard key={`onboarding-${sourceMode}`} client={membership} readOnly={fixture} />
      <AdminWorkspace key={`admin-${sourceMode}`} client={membership} readOnly={fixture} />
    </div>
  );
}
