import { useEffect, useState } from 'react';
import scenarioFixture from '../../tests/fixtures/source/hypothetical-six-node.json';
import simulationFixture from '../../tests/fixtures/source/planner-simulation.json';
import geographyFixture from '../../tests/fixtures/source/synthetic-geo.json';
import fixtureManifest from '../../tests/fixtures/source/ui-fixture-manifest.json';
import failoverFixture from '../../tests/fixtures/failover/failover-scenarios.json';
import manualProvisioningRouteFixture from '../../tests/fixtures/source/manual-provisioning-route-v1.json';
import provisioningAudit from '../../tests/fixtures/source/provisioning-audit.json';
import { AppShell, type ObservatoryView } from './components/AppShell';
import { adaptSimulator } from './model/adapter';
import { adaptFailoverScenarios } from './model/failover';
import { adaptProvisioningEvidence } from './model/provisioning';
import type { EvidenceSnapshot, FailoverIncident, ProvisioningEvidence } from './model/types';
import { EvidenceView } from './views/EvidenceView';
import { IncidentsView } from './views/IncidentsView';
import { NetworkView } from './views/NetworkView';
import { PlansView } from './views/PlansView';
import './styles.css';

interface LoadedBundle {
  readonly snapshot: EvidenceSnapshot;
  readonly incidents: readonly FailoverIncident[];
  readonly provisioning: ProvisioningEvidence;
}

type BundleResult =
  | { readonly state: 'ready'; readonly bundle: LoadedBundle }
  | { readonly state: 'error'; readonly message: string };

function loadOfflineBundle(): BundleResult {
  try {
    const snapshot = adaptSimulator(
      scenarioFixture,
      simulationFixture,
      geographyFixture,
      fixtureManifest,
    );
    const incidents = adaptFailoverScenarios(failoverFixture, {
      knownNodeIds: snapshot.nodes.map((node) => node.id),
      numLayers: snapshot.model.numLayers,
    });
    const provisioning = adaptProvisioningEvidence(manualProvisioningRouteFixture, provisioningAudit);

    return {
      state: 'ready',
      bundle: {
        snapshot,
        incidents,
        provisioning,
      },
    };
  } catch (reason: unknown) {
    return {
      state: 'error',
      message: reason instanceof Error ? reason.message : 'Unknown fixture parsing error',
    };
  }
}

const offlineBundle = loadOfflineBundle();

const OBSERVATORY_VIEWS: readonly ObservatoryView[] = [
  'network',
  'plans',
  'incidents',
  'evidence',
];

function viewFromHash(hash: string): ObservatoryView | null {
  const candidate = hash.replace(/^#/, '');
  return OBSERVATORY_VIEWS.includes(candidate as ObservatoryView)
    ? (candidate as ObservatoryView)
    : null;
}

function BundleError({ message }: { readonly message: string }) {
  return (
    <section className="bundle-error panel" role="alert">
      <span aria-hidden="true">!</span>
      <div>
        <p className="eyebrow caution">Offline fixture error</p>
        <h2>Evidence bundle unavailable</h2>
        <p>{message}</p>
        <small>No fallback values were inferred or presented.</small>
      </div>
    </section>
  );
}

export default function App() {
  const [activeView, setActiveView] = useState<ObservatoryView>(
    () => viewFromHash(window.location.hash) ?? 'network',
  );

  useEffect(() => {
    const synchronizeHash = () => {
      const nextView = viewFromHash(window.location.hash);
      if (nextView === null) {
        window.history.replaceState(null, '', '#network');
        setActiveView('network');
        return;
      }
      setActiveView(nextView);
    };

    synchronizeHash();
    window.addEventListener('hashchange', synchronizeHash);
    return () => window.removeEventListener('hashchange', synchronizeHash);
  }, []);

  const navigate = (view: ObservatoryView) => {
    setActiveView(view);
    const nextHash = `#${view}`;
    if (window.location.hash !== nextHash) {
      window.history.pushState(null, '', nextHash);
    }
  };

  let content;
  if (offlineBundle.state === 'error') {
    content = <BundleError message={offlineBundle.message} />;
  } else {
    const { snapshot, incidents, provisioning } = offlineBundle.bundle;
    switch (activeView) {
      case 'network':
        content = <NetworkView snapshot={snapshot} />;
        break;
      case 'plans':
        content = <PlansView snapshot={snapshot} />;
        break;
      case 'incidents':
        content = <IncidentsView incidents={incidents} />;
        break;
      case 'evidence':
        content = (
          <EvidenceView
            snapshot={snapshot}
            incidents={incidents}
            provisioning={provisioning}
          />
        );
        break;
    }
  }

  const scopeLabel =
    offlineBundle.state === 'ready'
      ? offlineBundle.bundle.snapshot.source.scenarioName
      : 'offline fixture';

  return (
    <AppShell activeView={activeView} onViewChange={navigate} scopeLabel={scopeLabel}>
      {content}
    </AppShell>
  );
}
