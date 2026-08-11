import { useEffect, useMemo, useState } from 'react';
import type { ProductSourceMode } from './contracts';
import { FixtureSwarmClient } from './membershipSwarmAdapter';
import { useProductSettings } from '../features/settings/SettingsContext';
import { NodesWorkspace } from '../features/nodes/NodesWorkspace';
import { HttpSwarmClient } from '../features/swarm/SwarmClient';
import { SwarmWorkspace } from '../features/swarm/SwarmWorkspace';
import type { EvidenceSnapshot, ProvisioningEvidence } from '../model/types';
import { makeProductSwarmFixture } from '../test/productFixtures';
import styles from './ProductNodesWorkspace.module.css';
import { HttpLiveRouteStatusClient, type M13PlacementProjection, type M14TopologyProjection } from '../features/liveRoute/routeStatus';
import { M13PlacementPanel } from '../features/liveRoute/M13PlacementPanel';
import { M14TopologyPanel } from '../features/liveRoute/M14TopologyPanel';
import { M16RuntimePanel } from '../features/liveRoute/M16RuntimePanel';
import { HttpM16RuntimeClient, type M16RuntimeStatus } from '../features/liveRoute/m16Runtime';

const fixtureStatus = makeProductSwarmFixture();

export interface ProductNodesWorkspaceProps {
  readonly sourceMode: ProductSourceMode;
  readonly snapshot?: EvidenceSnapshot;
  readonly provisioning?: ProvisioningEvidence;
}

export function ProductNodesWorkspace({ sourceMode, snapshot, provisioning }: ProductNodesWorkspaceProps) {
  const { settings } = useProductSettings();
  const fixture = sourceMode === 'fixture' || sourceMode === 'replay';
  const readOnlyReason = fixture
    ? 'Enrollment and membership changes are unavailable in offline evidence mode.'
    : 'Native enrollment is operator-only: issue one owner-only signed bundle per device from the durable seed.';
  const swarm = useMemo(() => fixture ? new FixtureSwarmClient(fixtureStatus) : new HttpSwarmClient(), [fixture]);
  const [placement, setPlacement] = useState<M13PlacementProjection | null>(null);
  const [topology, setTopology] = useState<M14TopologyProjection | null>(null);
  const [runtime, setRuntime] = useState<M16RuntimeStatus | null>(null);
  useEffect(() => {
    if (fixture) return;
    let active = true;
    void new HttpLiveRouteStatusClient().load().then((status) => {
      if (active) {
        setPlacement(status.placement);
        setTopology(status.topology);
      }
    }).catch(() => {
      if (active) {
        setPlacement(null);
        setTopology(null);
      }
    });
    void new HttpM16RuntimeClient().load().then((status) => {
      if (active) setRuntime(status);
    }).catch(() => {
      if (active) setRuntime(null);
    });
    return () => { active = false; };
  }, [fixture]);
  return (
    <div className="product-stack">
      <header className="view-heading product-nodes-heading">
        <div><p className="eyebrow cyan">Device and membership control plane</p><h1>Nodes</h1></div>
      </header>
      {!fixture ? (
        <section className={`panel ${styles.readiness}`} aria-labelledby="multi-device-onboarding-title">
          <div className="panel-titlebar">
            <div>
              <p className="panel-kicker">Trusted multi-device onboarding</p>
              <h2 id="multi-device-onboarding-title">Ready for multiple invited users and devices</h2>
            </div>
            <span className="badge evidence">Operator controlled</span>
          </div>
          <p>
            The durable seed can issue up to 64 unique, short-lived, single-use native-device bundles per batch.
            Each device keeps an independent signing key, membership generation, and renewable lease.
          </p>
          <p>
            Joining never changes the active route. Capability, placement, assigned artifacts, runtime load, and the
            rebuilt physical topology must all requalify before a new member can serve inference.
          </p>
          <p>
            Current boundary: known trusted invitees on the private operator network. Public or mutually untrusted
            enrollment and Tailscale-independent operation remain planned work.
          </p>
        </section>
      ) : null}
      {topology === null ? null : <M14TopologyPanel topology={topology} view="nodes" />}
      {placement === null ? null : <M13PlacementPanel placement={placement} view="nodes" />}
      {runtime === null ? null : <M16RuntimePanel runtime={runtime} view="nodes" />}
      {snapshot !== undefined && provisioning !== undefined ? <NodesWorkspace snapshot={snapshot} provisioning={provisioning} /> : null}
      <SwarmWorkspace
        key={`swarm-${sourceMode}`}
        client={swarm}
        initialStatus={fixture ? fixtureStatus : undefined}
        concealNetworkIdentity={settings.concealNetworkIdentity}
        readOnly={fixture}
        readOnlyReason={readOnlyReason}
        targetDeviceEnrollment={!fixture}
        supportsBrowserProbes={fixture}
      />
    </div>
  );
}
